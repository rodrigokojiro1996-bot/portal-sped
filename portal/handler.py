"""Rotas HTTP do portal: lê a requisição, consulta o banco, chama as regras e devolve o HTML das views."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import time
from datetime import date
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import resumo_sped
from portal import auditoria, config
from portal import db as storage
from portal.auth import STAFF_ROLES, create_user, token_hash, verify_password
from portal.calendario import competence_timeline
from portal.db import connect, ensure_secret
from portal.publicacao import competence_summary, publish_sped
from portal.sped import DOCUMENT_TYPES, PublishError, format_cnpj, normalize_cnpj
from portal.views import painel
from portal.views.acesso import login_page
from portal.views.dialogos import publish_dialog, published_dialog
from portal.views.layout import message_page, page_heading, render_page
from portal.views.monitoramento import monitoring_csv, monitoring_main

STATIC_FILES = {
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8", 3600),
    "/static/portal.js": ("portal.js", "text/javascript; charset=utf-8", 3600),
    "/static/farmacias-associadas-logo.png": ("farmacias-associadas-logo.png", "image/png", 86400),
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "PortalFiscal"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    # ------------------------------------------------------------------ respostas

    def send_bytes(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if getattr(self, "session_cookie", None):
            self.send_header("Set-Cookie", f"portal_session={self.session_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={config.SESSION_SECONDS}" + ("; Secure" if config.COOKIE_SECURE else ""))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_page(self, status: int, title: str, content: str, user: sqlite3.Row | None = None) -> None:
        self.send_bytes(status, render_page(title, content, user))

    def send_message(self, status: int, title: str, text: str, user: sqlite3.Row | None = None) -> None:
        self.send_page(status, title, message_page(text), user)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    # ------------------------------------------------------------------ sessão e formulários

    def cookies(self) -> SimpleCookie:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie

    def login_nonce(self) -> str:
        cookie = self.cookies().get("login_nonce")
        return cookie.value if cookie else ""

    def login_token(self) -> tuple[str, str]:
        """Código de segurança do formulário de login.

        Reaproveita o código que o navegador já tem: assim uma aba extra, o pré-carregamento
        da página pelo navegador ou um recarregamento não invalidam o formulário já aberto.
        """
        nonce = self.login_nonce()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", nonce):
            nonce = secrets.token_urlsafe(24)
        return nonce, hmac.new(ensure_secret(), nonce.encode(), hashlib.sha256).hexdigest()

    def send_login_page(self, status: int = HTTPStatus.OK, message: str = "", credential: str = "") -> None:
        nonce, csrf = self.login_token()
        cookie = f"login_nonce={nonce}; Path=/; HttpOnly; SameSite=Strict; Max-Age={config.LOGIN_FORM_SECONDS}" + ("; Secure" if config.COOKIE_SECURE else "")
        self.send_bytes(status, render_page("Acesso", login_page(csrf, message, credential)), headers={"Set-Cookie": cookie})

    def get_user(self) -> sqlite3.Row | None:
        cookie = self.cookies().get("portal_session")
        if cookie is None:
            return None
        with connect() as db:
            row = db.execute("SELECT users.*, sessions.csrf_token FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>? AND users.active=1", (token_hash(cookie.value), int(time.time()))).fetchone()
        self.session_cookie = cookie.value
        return row

    def form(self) -> dict[str, str | bytes]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > config.MAX_UPLOAD_BYTES + 65536:
            raise ValueError("O envio excede o limite permitido de 40 MB.")
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            message = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + content_type.encode("ascii", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
            values: dict[str, str | bytes] = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                payload = part.get_payload(decode=True) or b""
                if part.get_filename():
                    values[name] = payload
                    values["file_name"] = part.get_filename()
                else:
                    values[name] = payload.decode("utf-8", "replace")
            return values
        parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def check_csrf(self, data: dict[str, str | bytes], user: sqlite3.Row | None) -> bool:
        submitted = data.get("csrf", "")
        if not isinstance(submitted, str):
            return False
        if user:
            return hmac.compare_digest(submitted, user["csrf_token"])
        nonce, supplied_mac = self.login_nonce(), submitted
        expected = hmac.new(ensure_secret(), nonce.encode(), hashlib.sha256).hexdigest() if nonce else ""
        return bool(nonce) and hmac.compare_digest(supplied_mac, expected)

    def audit(self, user: sqlite3.Row | dict | None, action: str, store: sqlite3.Row | dict | None = None, document_type: str | None = None,
              competence: str | None = None, file_name: str | None = None, detail: str | None = None) -> None:
        auditoria.record(user, action, self.client_address[0], self.headers.get("User-Agent", ""), store, document_type, competence, file_name, detail)

    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in STATIC_FILES:
            name, content_type, max_age = STATIC_FILES[path]
            self.send_bytes(HTTPStatus.OK, (config.ROOT / "static" / name).read_bytes(), content_type, {"Cache-Control": f"public, max-age={max_age}"})
            return
        if path == "/saude":
            self.health()
            return
        if path in ("/", "/login"):
            if self.get_user():
                self.redirect("/dashboard")
            else:
                self.send_login_page()
            return
        user = self.get_user()
        if not user:
            self.redirect("/login")
            return
        if path == "/dashboard":
            self.dashboard_workspace(user)
        elif path.startswith("/download/"):
            self.download(user, path.removeprefix("/download/"))
        elif config.MONITORING_ENABLED and path in ("/monitoramento", "/monitoramento.csv"):
            if user["role"] != "admin":
                self.audit(user, "acao_bloqueada", detail="abrir o monitoramento do suporte")
                self.send_message(HTTPStatus.FORBIDDEN, "Acesso negado", "Área restrita à administração.", user)
            elif path.endswith(".csv"):
                self.monitoring_export()
            else:
                self.monitoring_page(user)
        else:
            self.send_message(HTTPStatus.NOT_FOUND, "Página não encontrada", "Página não encontrada", user)

    def health(self) -> None:
        """Rota de saúde, sem login: o banco responde e está na versão de migração que este código espera.

        Usada pelo iniciar-portal.bat e pelo servico-windows.ps1 para saber se o portal está no ar de verdade.
        Em caso de falha devolve 503 com uma mensagem genérica; o detalhe vai só para o log do servidor.
        """
        body: dict[str, object] = {"ok": False, "versao": config.PORTAL_VERSION}
        try:
            with connect() as db:
                db.execute("SELECT 1").fetchone()
            version, expected = storage.schema_version(), storage.latest_version()
            body.update(ok=version == expected, migracao=version)
            if version != expected:
                body["erro"] = f"Banco na migração {version}; este código espera a {expected}."
        except Exception as error:  # noqa: BLE001 - qualquer falha do banco deixa o portal "não saudável"
            print(f"/saude: {type(error).__name__}: {error}", file=sys.stderr)
            body["erro"] = "Banco de dados indisponível."
        status = HTTPStatus.OK if body["ok"] else HTTPStatus.SERVICE_UNAVAILABLE
        self.send_bytes(status, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def dashboard_workspace(self, user: sqlite3.Row, dialog: str = "", status: int = HTTPStatus.OK) -> None:
        """Tela principal: barra lateral (loja e tipo de SPED) e área de trabalho da loja ou da rede."""
        query = self.query()
        document_type = query.get("type", ["fiscal"])[0]
        if document_type not in DOCUMENT_TYPES:
            document_type = "fiscal"
        requested_store = query.get("store_id", [""])[0]
        admin = user["role"] == "admin"
        staff = user["role"] in STAFF_ROLES

        with connect() as db:
            if staff:
                stores = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 ORDER BY name").fetchall()
                selected_store = next((store for store in stores if str(store["id"]) == requested_store), None)
            else:
                cnpj_root = normalize_cnpj(user["cnpj"] or "")[:8]
                stores = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 AND substr(cnpj,1,8)=? ORDER BY CASE WHEN substr(cnpj,9,4)='0001' THEN 0 ELSE 1 END, name", (cnpj_root,)).fetchall()
                selected_store = next((store for store in stores if str(store["id"]) == requested_store), None) or next((store for store in stores if store["id"] == user["id"]), user)
            store_rows: list[sqlite3.Row] = []
            matrix_with_file = None
            if selected_store:
                documents = db.execute("SELECT * FROM documents WHERE store_id=? AND document_type=? ORDER BY competence DESC, uploaded_at DESC", (selected_store["id"], document_type)).fetchall()
                if not documents and document_type == "contribuicoes":
                    # O SPED Contribuições é entregue pela matriz e inclui as filiais.
                    matrix_with_file = db.execute("SELECT DISTINCT users.id, users.name FROM users JOIN documents ON documents.store_id=users.id WHERE users.role='store' AND users.active=1 AND substr(users.cnpj,1,8)=? AND users.id<>? AND documents.document_type='contribuicoes' ORDER BY substr(users.cnpj,9,4) LIMIT 1", (normalize_cnpj(selected_store["cnpj"] or "")[:8], selected_store["id"])).fetchone()
            else:
                documents = db.execute("SELECT documents.*, users.name AS store_name FROM documents JOIN users ON users.id=documents.store_id WHERE documents.document_type=? ORDER BY documents.uploaded_at DESC LIMIT 20", (document_type,)).fetchall()
                store_rows = db.execute("SELECT users.id, users.cnpj, users.name, COUNT(documents.id) AS total FROM users LEFT JOIN documents ON documents.store_id=users.id AND documents.document_type=? WHERE users.role='store' AND users.active=1 GROUP BY users.id ORDER BY users.name", (document_type,)).fetchall()

        label, subject, _ = DOCUMENT_TYPES[document_type]
        competences = sorted({doc["competence"] for doc in documents}, reverse=True) if selected_store else []
        months = competence_timeline(competences) if selected_store and not matrix_with_file else []
        requested_competence = query.get("competence", [""])[0]
        competence = requested_competence if requested_competence in months else (competences[0] if competences else (months[0] if months else ""))
        units = 1
        if selected_store:
            root = normalize_cnpj(selected_store["cnpj"] or "")[:8]
            units = max(1, sum(1 for store in stores if normalize_cnpj(store["cnpj"] or "")[:8] == root))
            self.audit(user, "abriu_loja", selected_store, document_type, competence)
        else:
            self.audit(user, "abriu_central", document_type=document_type)

        main = ""
        message = {"store-created": "Acesso da loja criado com sucesso.", "invalid": "Não foi possível concluir. Confira os dados informados."}.get(query.get("status", [""])[0], "")
        if message:
            main += f"<div class='notice' role='status'>{message}</div>"
        if user["role"] == "support":
            main += painel.support_notice()
        if selected_store:
            main += page_heading(f"{label} · {subject}", selected_store["name"], f"CNPJ {format_cnpj(selected_store['cnpj'])}")
        elif admin:
            main += page_heading(f"{label} · {subject}", "Central fiscal", "Publique arquivos, cadastre lojas e acompanhe a rede.")
        else:
            main += page_heading(f"{label} · {subject}", "Central fiscal", "Escolha uma loja para consultar ou baixe os arquivos mais recentes.")
        if admin:
            main += painel.admin_tools(user, selected_store)
        if selected_store and matrix_with_file:
            main += painel.matrix_notice(document_type, matrix_with_file)
        elif selected_store:
            main += painel.period_nav(selected_store, document_type, months, competences, competence, units)
            if competence in competences:
                main += painel.files_card(documents, competence)
                main += self.summary_section(documents, competence)
            else:
                main += painel.forecast_card(document_type, competence, units)
        else:
            main += painel.stores_table(store_rows, document_type) + painel.recent_uploads(documents)

        if admin and not dialog:
            dialog = self.published_dialog(query.get("published", [""])[0])
        content = dialog + "<div class='workspace'>" + painel.sidebar(user, stores, selected_store, document_type) + "<main class='workspace-main'>" + main + "</main></div>"
        self.send_page(status, selected_store["name"] if selected_store else "Central fiscal", content, user)

    def summary_section(self, documents: list[sqlite3.Row], competence: str) -> str:
        competences = sorted({doc["competence"] for doc in documents})
        summaries = {comp: competence_summary(documents, comp) for comp in sorted(set(competences[-12:]) | {competence})}
        history = [(comp, resumo_sped.headline(summary)) for comp, summary in summaries.items() if summary and summary.get("operacoes")]
        return painel.summary_section(competence, summaries.get(competence), history)

    def published_dialog(self, document_id: str) -> str:
        if not document_id:
            return ""
        with connect() as db:
            document = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.id=?", (document_id,)).fetchone()
        return published_dialog(document) if document else ""

    def monitoring_filters(self) -> tuple[str, str, str]:
        query = self.query()
        period = query.get("periodo", ["7d"])[0]
        category = query.get("acao", [""])[0]
        return query.get("usuario", [""])[0], period if period in auditoria.AUDIT_PERIODS else "7d", category if category in auditoria.AUDIT_FILTERS else ""

    def monitoring_page(self, user: sqlite3.Row) -> None:
        """Tela da administração com tudo o que o suporte fez: resumo, filtros e histórico agrupado por dia."""
        user_filter, period, category = self.monitoring_filters()
        with connect() as db:
            support_users = db.execute("SELECT id, name, login_name FROM users WHERE role='support' ORDER BY name").fetchall()
            stores = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 ORDER BY name").fetchall()
        everything = auditoria.audit_entries(user_filter, period, "", limit=None)
        entries = auditoria.audit_entries(user_filter, period, category, limit=501)
        main = monitoring_main(support_users, everything, entries[:500], len(entries) > 500, user_filter, period, category)
        content = "<div class='workspace'>" + painel.sidebar(user, stores, None, "fiscal", section="monitoramento") + "<main class='workspace-main'>" + main + "</main></div>"
        self.send_page(HTTPStatus.OK, "Monitoramento do suporte", content, user)

    def monitoring_export(self) -> None:
        user_filter, period, category = self.monitoring_filters()
        body = monitoring_csv(auditoria.audit_entries(user_filter, period, category, limit=None))
        self.send_bytes(HTTPStatus.OK, body, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="monitoramento-suporte-{date.today():%Y-%m-%d}.csv"'})

    def download(self, user: sqlite3.Row, document_id: str) -> None:
        with connect() as db:
            if user["role"] in STAFF_ROLES:
                document = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.id=?", (document_id,)).fetchone()
            else:
                cnpj_root = normalize_cnpj(user["cnpj"] or "")[:8]
                document = db.execute("SELECT documents.* FROM documents JOIN users AS document_store ON document_store.id=documents.store_id WHERE documents.id=? AND document_store.role='store' AND substr(document_store.cnpj,1,8)=?", (document_id, cnpj_root)).fetchone()
        path = storage.DOCUMENTS / (document["id"] + ".txt") if document else None
        if not path or not path.is_file():
            self.send_message(HTTPStatus.NOT_FOUND, "Arquivo indisponível", "Arquivo não encontrado", user)
            return
        body = path.read_bytes()
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(document["original_name"]).name)[:150] or "sped.txt"
        if user["role"] in auditoria.AUDITED_ROLES:
            # Registra antes de enviar: o download nunca deixa de constar no histórico.
            store = {"id": document["store_id"], "name": document["store_name"], "cnpj": document["store_cnpj"]}
            self.audit(user, "baixou_arquivo", store, document["document_type"], document["competence"], file_name=document["original_name"])
        self.send_bytes(HTTPStatus.OK, body, "text/plain; charset=utf-8", {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.form()
        except (ValueError, OSError):
            self.send_message(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Envio inválido", "Arquivo acima do limite de 40 MB.")
            return
        user = self.get_user()
        if path == "/login" and not self.check_csrf(data, None):
            # Nunca volta ao login em silêncio: mostra o formulário de novo, com o CNPJ já preenchido.
            credential = data.get("cnpj", "")
            self.send_login_page(HTTPStatus.OK, "A página de acesso expirou. Digite a senha e clique em Entrar novamente.", credential if isinstance(credential, str) else "")
            return
        if path != "/login" and not self.check_csrf(data, user):
            self.send_message(HTTPStatus.FORBIDDEN, "Sessão expirada", "Sessão expirada. Atualize a página e tente novamente.", user)
            return
        if path == "/login":
            self.login(data)
        elif path == "/logout" and user:
            self.audit(user, "logout")
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(self.session_cookie),))
            self.session_cookie = None
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "portal_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers()
        elif user and user["role"] == "admin" and path == "/stores":
            self.add_store(data)
        elif user and user["role"] == "admin" and path == "/upload":
            self.upload(data)
        else:
            attempted = {"/upload": "publicar um arquivo SPED", "/stores": "cadastrar uma loja"}.get(path, f"uma ação não permitida ({path})")
            self.audit(user, "acao_bloqueada", detail=attempted)
            self.send_message(HTTPStatus.FORBIDDEN, "Acesso negado", "Ação não autorizada.", user)

    def login(self, data: dict[str, str | bytes]) -> None:
        ip = self.client_address[0]
        now = int(time.time())
        with connect() as db:
            attempt = db.execute("SELECT attempts, window_start FROM login_attempts WHERE ip=?", (ip,)).fetchone()
            if attempt and attempt["window_start"] > now - 900 and attempt["attempts"] >= 10:
                self.send_login_page(HTTPStatus.TOO_MANY_REQUESTS, "Muitas tentativas seguidas. Aguarde 15 minutos e tente de novo.")
                return
            credential, password = data.get("cnpj", ""), data.get("password", "")
            credential = credential.strip() if isinstance(credential, str) else ""
            cnpj = normalize_cnpj(credential)
            password = password if isinstance(password, str) else ""
            account = db.execute("SELECT * FROM users WHERE active=1 AND ((role='store' AND cnpj=?) OR (role IN ('admin', 'support') AND login_name=?))", (cnpj, credential.lower())).fetchone()
            failed = not account or not verify_password(password, account["password_hash"])
            if failed:
                attempts = attempt["attempts"] + 1 if attempt and attempt["window_start"] > now - 900 else 1
                db.execute("INSERT INTO login_attempts VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET attempts=excluded.attempts, window_start=excluded.window_start", (ip, attempts, attempt["window_start"] if attempt and attempt["window_start"] > now - 900 else now))
        if failed:
            self.audit(account, "login_falhou")
            self.send_login_page(HTTPStatus.UNAUTHORIZED, "CNPJ ou senha incorretos. Confira e tente novamente.", credential)
            return
        with connect() as db:
            db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
            raw_token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            db.execute("DELETE FROM sessions WHERE user_id=?", (account["id"],))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (token_hash(raw_token), account["id"], csrf, now + config.SESSION_SECONDS))
        self.audit(account, "login")
        self.session_cookie = raw_token
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/dashboard")
        self.send_header("Set-Cookie", f"portal_session={raw_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={config.SESSION_SECONDS}" + ("; Secure" if config.COOKIE_SECURE else ""))
        self.send_header("Set-Cookie", "login_nonce=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self.end_headers()

    def add_store(self, data: dict[str, str | bytes]) -> None:
        name, cnpj, password = data.get("name", ""), data.get("cnpj", ""), data.get("password", "")
        if not all(isinstance(value, str) for value in (name, cnpj, password)) or not name.strip() or len(name) > 120:
            self.redirect("/dashboard?status=invalid")
            return
        try:
            create_user(cnpj, name, "store", password)
        except (ValueError, sqlite3.IntegrityError):
            self.redirect("/dashboard?status=invalid")
            return
        self.redirect("/dashboard?status=store-created")

    def upload(self, data: dict[str, str | bytes]) -> None:
        """Publica o SPED na loja do CNPJ do arquivo, na aba do tipo e no mês lidos do registro 0000."""
        file_content, file_name = data.get("file", b""), data.get("file_name", "")
        try:
            published = publish_sped(file_content if isinstance(file_content, bytes) else b"", file_name if isinstance(file_name, str) else "")
        except PublishError as error:
            self.path = "/dashboard"
            self.dashboard_workspace(self.get_user(), publish_dialog(False, str(error)), HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        self.redirect(f"/dashboard?store_id={published['store_id']}&type={published['document_type']}&competence={published['competence']}&published={published['id']}")
