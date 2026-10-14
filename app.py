from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PORTAL_DATA_DIR", ROOT / "data"))
DATABASE = DATA_DIR / "portal.sqlite3"
DOCUMENTS = DATA_DIR / "documentos"
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
APP_SECRET: bytes | None = None


def configure_storage(data_dir: Path | None = None) -> None:
    global DATA_DIR, DATABASE, DOCUMENTS, APP_SECRET
    if data_dir is not None:
        DATA_DIR = data_dir
        DATABASE = DATA_DIR / "portal.sqlite3"
        DOCUMENTS = DATA_DIR / "documentos"
        APP_SECRET = None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(DOCUMENTS, 0o700)
    except OSError:
        pass
    secret_path = DATA_DIR / ".secret"
    if not secret_path.exists():
        secret_path.write_bytes(secrets.token_bytes(48))
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
    APP_SECRET = secret_path.read_bytes()
    initialize_database()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                cnpj TEXT UNIQUE,
                login_name TEXT UNIQUE,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'store')),
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES users(id),
                document_type TEXT NOT NULL CHECK(document_type IN ('fiscal', 'contribuicoes')),
                competence TEXT NOT NULL,
                original_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                UNIQUE(store_id, document_type, sha256)
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                window_start INTEGER NOT NULL
            );
            """
        )
        document_columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
        unique_indexes = []
        for index in db.execute("PRAGMA index_list(documents)"):
            if index["unique"]:
                unique_indexes.append(tuple(row["name"] for row in db.execute(f"PRAGMA index_info('{index['name']}')")))
        must_migrate = "document_type" not in document_columns or ("store_id", "document_type", "competence") in unique_indexes
        if must_migrate:
            db.execute("ALTER TABLE documents RENAME TO documents_legacy")
            db.execute("""CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES users(id),
                document_type TEXT NOT NULL CHECK(document_type IN ('fiscal', 'contribuicoes')),
                competence TEXT NOT NULL,
                original_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                UNIQUE(store_id, document_type, sha256)
            )""")
            if "document_type" in document_columns:
                db.execute("""INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at)
                    SELECT id, store_id, document_type, competence, original_name, size, sha256, uploaded_at FROM documents_legacy""")
            else:
                db.execute("""INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at)
                    SELECT id, store_id, 'fiscal', competence, original_name, size, sha256, uploaded_at FROM documents_legacy""")
            db.execute("DROP TABLE documents_legacy")


def normalize_cnpj(value: str) -> str:
    return re.sub(r"\D", "", value)


def valid_cnpj(value: str) -> bool:
    digits = normalize_cnpj(value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    for length, weights in ((12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
                            (13, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))):
        remainder = sum(int(digit) * weight for digit, weight in zip(digits[:length], weights)) % 11
        check_digit = 0 if remainder < 2 else 11 - remainder
        if int(digits[length]) != check_digit:
            return False
    return True


def parse_sped_header(content: bytes, document_type: str = "fiscal") -> dict[str, str]:
    if document_type not in ("fiscal", "contribuicoes"):
        raise ValueError("Tipo de SPED não reconhecido.")
    date_start_index, date_end_index, name_index, cnpj_index = (
        (6, 7, 8, 9) if document_type == "contribuicoes" else (4, 5, 6, 7)
    )
    text = content.decode("utf-8-sig", errors="replace")
    for line in text.splitlines()[:500]:
        fields = line.strip().split("|")
        if len(fields) > cnpj_index and fields[1] == "0000":
            cnpj = normalize_cnpj(fields[cnpj_index])
            if not valid_cnpj(cnpj):
                raise ValueError("O registro 0000 contém um CNPJ inválido.")
            try:
                start = datetime.strptime(fields[date_start_index], "%d%m%Y").date()
                end = datetime.strptime(fields[date_end_index], "%d%m%Y").date()
            except ValueError as error:
                raise ValueError("As datas do registro 0000 não são válidas.") from error
            if start > end or start.year != end.year or start.month != end.month:
                raise ValueError("O período informado no registro 0000 não corresponde a um único mês.")
            if start.day != 1 or end.day != (date(start.year + (start.month == 12), start.month % 12 + 1, 1) - timedelta(days=1)).day:
                raise ValueError("O SPED não cobre o mês-calendário completo.")
            return {"cnpj": cnpj, "competence": start.strftime("%Y-%m"), "company": fields[name_index].strip()}
    raise ValueError("Não foi encontrado um registro 0000 válido no início do arquivo.")


def password_digest(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_user(cnpj: str | None, name: str, role: str, password: str, login_name: str | None = None) -> None:
    normalized = normalize_cnpj(cnpj or "") if role == "store" else None
    if role == "store" and not valid_cnpj(normalized or ""):
        raise ValueError("Informe um CNPJ válido.")
    username = (login_name or "").strip().lower() if role == "admin" else None
    if role == "admin" and (not username or not re.fullmatch(r"[a-z0-9._-]{3,40}", username)):
        raise ValueError("O usuário administrativo deve ter 3 a 40 caracteres: letras, números, ponto, hífen ou sublinhado.")
    if len(password) < 12:
        raise ValueError("A senha deve ter pelo menos 12 caracteres.")
    with connect() as db:
        db.execute(
            "INSERT INTO users (cnpj, login_name, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (normalized, username, name.strip(), role, password_digest(password), datetime.now(timezone.utc).isoformat()),
        )


def ensure_secret() -> bytes:
    global APP_SECRET
    if APP_SECRET is None:
        configure_storage()
    assert APP_SECRET is not None
    return APP_SECRET


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_page(title: str, content: str, user: sqlite3.Row | None = None) -> bytes:
    nav = ""
    if user:
        nav = f"<div class='user-chip'><span class='user-mark'>{h(user['name'][:1].upper())}</span><span>{h(user['name'])}<small>{'Administração' if user['role'] == 'admin' else 'Acesso da loja'}</small></span><form method='post' action='/logout'><input type='hidden' name='csrf' value='{h(user['csrf_token'])}'><button class='icon-button' title='Sair' aria-label='Sair'>↗</button></form></div>"
    brand = "<a class='brand' href='/dashboard'><img class='brand-logo' src='/static/farmacias-associadas-logo.png' alt='Farmácias Associadas'><span class='brand-context'>PORTAL FISCAL</span></a>"
    return (f"<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='theme-color' content='#087e70'><title>{h(title)} · Portal Fiscal</title><link rel='stylesheet' href='/static/styles.css'></head><body><header class='topbar'>{brand}{nav}</header>{content}<footer class='site-footer'><span>PORTAL FISCAL <i></i> FARMÁCIAS ASSOCIADAS</span><span>Documentos acessíveis somente à loja autorizada.</span></footer></body></html>").encode("utf-8")


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "PortalFiscal"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_bytes(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        if getattr(self, "session_cookie", None):
            cookie = f"portal_session={self.session_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}"
            if os.environ.get("COOKIE_SECURE") == "1":
                cookie += "; Secure"
            self.send_header("Set-Cookie", cookie)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_page(self, status: int, title: str, content: str, user: sqlite3.Row | None = None) -> None:
        self.send_bytes(status, render_page(title, content, user))

    def cookies(self) -> SimpleCookie:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie

    def login_nonce(self) -> str:
        cookie = self.cookies().get("login_nonce")
        return cookie.value if cookie else ""

    def issue_login_nonce(self) -> tuple[str, str]:
        nonce = secrets.token_urlsafe(24)
        mac = hmac.new(ensure_secret(), nonce.encode(), hashlib.sha256).hexdigest()
        return nonce, mac

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
        if length < 0 or length > MAX_UPLOAD_BYTES + 65536:
            raise ValueError("O envio excede o limite permitido de 40 MB.")
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            from email.parser import BytesParser
            from email.policy import default
            message = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode("ascii", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/static/styles.css":
            css = (ROOT / "static" / "styles.css").read_bytes()
            self.send_bytes(HTTPStatus.OK, css, "text/css; charset=utf-8", {"Cache-Control": "public, max-age=3600"})
            return
        if path == "/static/farmacias-associadas-logo.png":
            logo = (ROOT / "static" / "farmacias-associadas-logo.png").read_bytes()
            self.send_bytes(HTTPStatus.OK, logo, "image/png", {"Cache-Control": "public, max-age=86400"})
            return
        if path == "/" or path == "/login":
            user = self.get_user()
            if user:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/dashboard")
                self.end_headers()
                return
            nonce, csrf = self.issue_login_nonce()
            content = "<main class='login-layout'><section class='login-brand'><div class='eyebrow'>ÁREA EXCLUSIVA</div><h1>Fiscal em dia.<br><em>Sem complicação.</em></h1><p>Seus arquivos fiscais, organizados por competência e protegidos em um só lugar.</p><div class='login-foot'>ACESSO SEGURO <i></i> FARMÁCIAS ASSOCIADAS</div></section><section class='login-panel'><div class='login-heading'><span class='section-kicker'>BEM-VINDA, LOJA</span><h2>Acesse seu portal</h2><p>Entre com o CNPJ cadastrado e sua senha.</p></div><form method='post' action='/login' class='stack-form'><input type='hidden' name='csrf' value='" + h(csrf) + "'><label>CNPJ ou usuário<input name='cnpj' autocomplete='username' placeholder='CNPJ da loja ou usuário admin' maxlength='40' required></label><label>Senha<input name='password' type='password' autocomplete='current-password' placeholder='Sua senha de acesso' required></label><button class='button button-primary button-wide'>Entrar no portal <span>→</span></button></form><div class='login-note'><span class='lock-icon'>▣</span> Conexão protegida. Seus arquivos são privados.</div></section></main>"
            self.send_bytes(HTTPStatus.OK, render_page("Acesso", content), headers={"Set-Cookie": f"login_nonce={nonce}; Path=/; HttpOnly; SameSite=Strict; Max-Age=600" + ("; Secure" if os.environ.get("COOKIE_SECURE") == "1" else "")})
            return
        user = self.get_user()
        if not user:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path == "/dashboard":
            self.dashboard_workspace(user)
        elif path.startswith("/download/"):
            self.download(user, path.removeprefix("/download/"))
        else:
            self.send_page(HTTPStatus.NOT_FOUND, "Página não encontrada", "<main class='content'><h1>Página não encontrada</h1></main>", user)

    def dashboard(self, user: sqlite3.Row) -> None:
        with connect() as db:
            if user["role"] == "admin":
                stores = db.execute("SELECT users.id, users.cnpj, users.name, COUNT(documents.id) AS total FROM users LEFT JOIN documents ON documents.store_id=users.id WHERE users.role='store' GROUP BY users.id ORDER BY users.name").fetchall()
                documents = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj FROM documents JOIN users ON users.id=documents.store_id ORDER BY competence DESC, store_name LIMIT 50").fetchall()
            else:
                stores = []
                documents = db.execute("SELECT * FROM documents WHERE store_id=? ORDER BY competence DESC", (user["id"],)).fetchall()
        content = "<main class='content'><div class='page-heading'><div><span class='section-kicker'>VISÃO GERAL</span><h1>" + ("Central fiscal" if user["role"] == "admin" else "Seus documentos") + "</h1><p>" + ("Gerencie lojas e disponibilize arquivos por competência." if user["role"] == "admin" else "Arquivos fiscais da sua loja, organizados por período.") + "</p></div><span class='date-stamp'>" + h(datetime.now().strftime("%d.%m.%Y")) + "</span></div>"
        if user["role"] == "admin":
            content += "<section class='admin-grid'><div class='panel upload-panel'><div class='panel-title'><span class='panel-icon upload-icon'>↑</span><div><span class='section-kicker'>NOVO ARQUIVO</span><h2>Disponibilizar SPED</h2></div></div><p class='panel-copy'>O CNPJ e a competência são conferidos diretamente no registro 0000.</p><form method='post' enctype='multipart/form-data' action='/upload' class='stack-form'><input type='hidden' name='csrf' value='" + h(user["csrf_token"]) + "'><label>Loja destinatária<select name='store_id' required><option value=''>Selecione a loja</option>" + "".join(f"<option value='{store['id']}'>{h(store['name'])} · {h(store['cnpj'])}</option>" for store in stores) + "</select></label><label>Competência<input name='competence' type='month' required></label><label>Arquivo SPED EFD<input class='file-input' name='file' type='file' accept='.txt,text/plain' required><small class='field-help'>Arquivo .txt · até 40 MB · somente texto</small></label><button class='button button-primary'>Validar e publicar <span>→</span></button></form></div><div class='panel store-panel'><div class='panel-title'><span class='panel-icon store-icon'>＋</span><div><span class='section-kicker'>ACESSOS</span><h2>Cadastrar loja</h2></div></div><p class='panel-copy'>Crie o acesso privado da farmácia à plataforma.</p><form method='post' action='/stores' class='stack-form'><input type='hidden' name='csrf' value='" + h(user["csrf_token"]) + "'><label>Nome da loja<input name='name' maxlength='120' placeholder='Ex.: Farmácia Central' required></label><label>CNPJ<input name='cnpj' inputmode='numeric' maxlength='18' placeholder='00.000.000/0000-00' required></label><label>Senha inicial<input name='password' type='password' minlength='12' autocomplete='new-password' placeholder='Mínimo de 12 caracteres' required></label><button class='button button-outline'>Criar acesso <span>→</span></button></form></div></section><section class='section-block'><div class='section-heading'><div><span class='section-kicker'>REDE</span><h2>Lojas cadastradas</h2></div><span class='count-badge'>" + str(len(stores)) + " lojas</span></div><div class='table-wrap'><table><thead><tr><th>LOJA</th><th>CNPJ</th><th>ARQUIVOS</th><th>ACESSO</th></tr></thead><tbody>" + ("".join(f"<tr><td class='store-name'>{h(store['name'])}</td><td class='mono'>{h(store['cnpj'])}</td><td>{store['total']} <span class='muted'>documentos</span></td><td><span class='status-active'><i></i> Ativo</span></td></tr>" for store in stores) or "<tr><td colspan='4' class='empty-cell'>Nenhuma loja cadastrada. Crie o primeiro acesso acima.</td></tr>") + "</tbody></table></div></section>"
        content += "<section class='section-block documents-block'><div class='section-heading'><div><span class='section-kicker'>ARQUIVO FISCAL</span><h2>Últimos documentos</h2></div><span class='count-badge'>" + str(len(documents)) + " itens</span></div>" + self.documents_table(documents, user["role"] == "admin") + "</section>"
        message = self.query_message()
        if message:
            content = content.replace("<main class='content'>", f"<main class='content'><div class='notice'>{h(message)}</div>", 1)
        content += "</main>"
        self.send_page(HTTPStatus.OK, "Central fiscal", content, user)

    def documents_table(self, documents: list[sqlite3.Row], admin: bool) -> str:
        if not documents:
            return "<div class='empty-state'><span class='empty-mark'>—</span><strong>Nenhum arquivo por aqui ainda</strong><span>Os documentos publicados aparecerão nesta lista.</span></div>"
        rows = "".join(f"<tr><td class='store-name'>{h(doc['store_name'])}</td>" if admin else "<tr>" for doc in documents)
        rows = "".join((f"<tr><td class='store-name'>{h(doc['store_name'])}</td>" if admin else "<tr>") + f"<td><span class='month-tag'>{h(doc['competence'][5:7] + '/' + doc['competence'][:4])}</span></td><td class='file-name'>{h(doc['original_name'])}</td><td class='muted'>{h(datetime.fromisoformat(doc['uploaded_at']).strftime('%d/%m/%Y'))}</td><td><a class='download-link' href='/download/{h(doc['id'])}' title='Baixar arquivo' aria-label='Baixar {h(doc['original_name'])}'>↓</a></td></tr>" for doc in documents)
        columns = "<th>LOJA</th>" if admin else ""
        return f"<div class='table-wrap'><table><thead><tr>{columns}<th>COMPETÊNCIA</th><th>ARQUIVO</th><th>PUBLICADO</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>"

    def dashboard_workspace(self, user: sqlite3.Row) -> None:
        query = parse_qs(urlparse(self.path).query)
        document_type = query.get("type", ["fiscal"])[0]
        if document_type not in ("fiscal", "contribuicoes"):
            document_type = "fiscal"
        type_label = "SPED Fiscal" if document_type == "fiscal" else "SPED Contribuições"
        requested_store = query.get("store_id", [""])[0]

        with connect() as db:
            if user["role"] == "admin":
                stores = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 ORDER BY name").fetchall()
                selected_store = next((store for store in stores if str(store["id"]) == requested_store), None)
                if selected_store:
                    documents = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.store_id=? AND documents.document_type=? ORDER BY competence DESC", (selected_store["id"], document_type)).fetchall()
                else:
                    selected_store = None
                    documents = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.document_type=? ORDER BY competence DESC, store_name LIMIT 100", (document_type,)).fetchall()
                store_rows = db.execute("SELECT users.id, users.cnpj, users.name, COUNT(documents.id) AS total FROM users LEFT JOIN documents ON documents.store_id=users.id WHERE users.role='store' AND users.active=1 GROUP BY users.id ORDER BY users.name").fetchall()
            else:
                cnpj_root = normalize_cnpj(user["cnpj"] or "")[:8]
                stores = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 AND substr(cnpj,1,8)=? ORDER BY CASE WHEN substr(cnpj,9,4)='0001' THEN 0 ELSE 1 END, name", (cnpj_root,)).fetchall()
                selected_store = next((store for store in stores if str(store["id"]) == requested_store), None)
                if selected_store is None:
                    selected_store = next((store for store in stores if store["id"] == user["id"]), user)
                documents = db.execute("SELECT * FROM documents WHERE store_id=? AND document_type=? ORDER BY competence DESC, original_name", (selected_store["id"], document_type)).fetchall()
                store_rows = []

        store_identity = "<strong>Todas as lojas</strong><small>Visão administrativa</small>"
        if selected_store:
            store_identity = f"<strong>{h(selected_store['name'])}</strong><small class='store-cnpj'>CNPJ {h(selected_store['cnpj'])}</small>"
        store_picker = ""
        if user["role"] == "admin" or len(stores) > 1:
            selector_label = "Selecionar loja" if user["role"] == "admin" else "Trocar filial"
            all_option = "<option value=''>Todas as lojas</option>" if user["role"] == "admin" else ""
            store_picker = "<form class='store-picker' method='get' action='/dashboard'><input type='hidden' name='type' value='" + h(document_type) + "'><label for='store_id'>" + selector_label + "</label><select id='store_id' name='store_id'>" + all_option + "".join(f"<option value='{store['id']}'{' selected' if selected_store and store['id'] == selected_store['id'] else ''}>{h(store['name'])} · {h(store['cnpj'])}</option>" for store in stores) + "</select><button class='store-trigger' type='submit'>" + ("Loja" if user["role"] == "admin" else "Abrir filial") + " <span>→</span></button></form>"
        tabs = "".join(
            f"<a class='sped-tab{' active' if document_type == key else ''}' href='/dashboard?{('store_id=' + str(selected_store['id']) + '&') if selected_store and (user['role'] == 'admin' or user['role'] == 'store') else ''}type={key}'><span class='tab-mark'>{mark}</span>{label}</a>"
            for key, label, mark in (("fiscal", "SPED Fiscal", "F"), ("contribuicoes", "SPED Contribuições", "C"))
        )
        sidebar = "<aside class='workspace-sidebar'><div class='side-overline'>PORTAL DA REDE</div><div class='side-store'><div class='side-store-heading'><span class='side-store-mark'>⌂</span><span>LOJA</span></div><div class='side-store-identity'>" + store_identity + "</div>" + store_picker + "</div><nav class='sped-navigation' aria-label='Tipo de arquivo'><span class='side-section-label'>DOCUMENTOS</span>" + tabs + "</nav><div class='side-bottom'><span class='secure-dot'></span> Acesso protegido</div></aside>"

        content = "<div class='workspace'>" + sidebar + "<main class='workspace-main'><div class='page-heading'><div><span class='section-kicker'>ARQUIVO FISCAL</span><h1>" + h(selected_store["name"] if selected_store else "Central fiscal") + "</h1><p>" + ("Consulte documentos da loja selecionada." if selected_store else "Selecione uma loja ou consulte a rede inteira.") + "</p></div><span class='date-stamp'>" + h(datetime.now().strftime("%d.%m.%Y")) + "</span></div>"
        if user["role"] == "admin":
            content += "<section class='admin-grid'><div class='panel upload-panel'><div class='panel-title'><span class='panel-icon upload-icon'>↑</span><div><span class='section-kicker'>NOVO ARQUIVO · " + h(type_label.upper()) + "</span><h2>Disponibilizar documento</h2></div></div><p class='panel-copy'>CNPJ, período e tipo do SPED serão conferidos antes da publicação.</p><form method='post' enctype='multipart/form-data' action='/upload' class='stack-form'><input type='hidden' name='csrf' value='" + h(user["csrf_token"]) + "'><input type='hidden' name='document_type' value='" + h(document_type) + "'><label>Loja destinatária<select name='store_id' required><option value=''>Selecione a loja</option>" + "".join(f"<option value='{store['id']}'>{h(store['name'])} · {h(store['cnpj'])}</option>" for store in stores) + "</select></label><label>Competência<input name='competence' type='month' required></label><label>Arquivo .txt<input class='file-input' name='file' type='file' accept='.txt,text/plain' required><small class='field-help'>Até 40 MB · CNPJ e competência devem corresponder à loja.</small></label><button class='button button-primary'>Validar e publicar <span>→</span></button></form></div><div class='panel store-panel'><div class='panel-title'><span class='panel-icon store-icon'>＋</span><div><span class='section-kicker'>ACESSOS</span><h2>Cadastrar loja</h2></div></div><p class='panel-copy'>Crie o acesso privado da farmácia à plataforma.</p><form method='post' action='/stores' class='stack-form'><input type='hidden' name='csrf' value='" + h(user["csrf_token"]) + "'><label>Nome da loja<input name='name' maxlength='120' placeholder='Ex.: Farmácia Central' required></label><label>CNPJ<input name='cnpj' inputmode='numeric' maxlength='18' placeholder='00.000.000/0000-00' required></label><label>Senha inicial<input name='password' type='password' minlength='12' autocomplete='new-password' placeholder='Mínimo de 12 caracteres' required></label><button class='button button-outline'>Criar acesso <span>→</span></button></form></div></section>"
            content += "<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>REDE</span><h2>Lojas cadastradas</h2></div><span class='count-badge'>" + str(len(store_rows)) + " lojas</span></div><div class='table-wrap'><table><thead><tr><th>LOJA</th><th>CNPJ</th><th>ABRIR</th></tr></thead><tbody>" + ("".join(f"<tr><td class='store-name'>{h(store['name'])}</td><td class='mono'>{h(store['cnpj'])}</td><td><a class='store-open-link' href='/dashboard?store_id={store['id']}&type={document_type}'>Ver documentos →</a></td></tr>" for store in store_rows) or "<tr><td colspan='3' class='empty-cell'>Nenhuma loja cadastrada ainda.</td></tr>") + "</tbody></table></div></section>"
        content += "<section class='section-block documents-block'><div class='section-heading'><div><span class='section-kicker'>" + h(type_label.upper()) + "</span><h2>Arquivos disponíveis</h2></div><span class='count-badge'>" + str(len(documents)) + " arquivos</span></div>" + self.documents_table_workspace(documents, user["role"] == "admin" and selected_store is None) + "</section>"
        message = self.query_message()
        if message:
            content = content.replace("<main class='workspace-main'>", f"<main class='workspace-main'><div class='notice'>{h(message)}</div>", 1)
        content += "</main></div>"
        self.send_page(HTTPStatus.OK, type_label, content, user)

    def documents_table_workspace(self, documents: list[sqlite3.Row], show_store: bool) -> str:
        if not documents:
            return "<div class='empty-state'><span class='empty-mark'>—</span><strong>Nenhum arquivo deste tipo nesta loja</strong><span>Quando um arquivo for publicado, ele aparecerá aqui por competência.</span></div>"
        rows = "".join((f"<tr><td class='store-name'>{h(doc['store_name'])}</td>" if show_store else "<tr>") + f"<td><span class='month-tag'>{h(doc['competence'][5:7] + '/' + doc['competence'][:4])}</span></td><td class='file-name'>{h(doc['original_name'])}</td><td class='muted'>{h(datetime.fromisoformat(doc['uploaded_at']).strftime('%d/%m/%Y'))}</td><td><a class='download-link' href='/download/{h(doc['id'])}' title='Baixar arquivo' aria-label='Baixar {h(doc['original_name'])}'>↓</a></td></tr>" for doc in documents)
        columns = "<th>LOJA</th>" if show_store else ""
        return f"<div class='table-wrap'><table><thead><tr>{columns}<th>COMPETÊNCIA</th><th>ARQUIVO</th><th>PUBLICADO</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>"

    def query_message(self) -> str:
        code = parse_qs(urlparse(self.path).query).get("status", [""])[0]
        return {"uploaded": "Arquivo validado e publicado para a loja.", "store-created": "Acesso da loja criado com sucesso.", "exists": "Este mesmo arquivo já está cadastrado para a loja e o tipo de SPED.", "invalid": "Não foi possível concluir. Confira os dados e a validação do arquivo."}.get(code, "")

    def download(self, user: sqlite3.Row, document_id: str) -> None:
        with connect() as db:
            if user["role"] == "admin":
                document = db.execute("SELECT documents.*, users.name AS store_name FROM documents JOIN users ON users.id=documents.store_id WHERE documents.id=?", (document_id,)).fetchone()
            else:
                cnpj_root = normalize_cnpj(user["cnpj"] or "")[:8]
                document = db.execute("SELECT documents.* FROM documents JOIN users AS document_store ON document_store.id=documents.store_id WHERE documents.id=? AND document_store.role='store' AND substr(document_store.cnpj,1,8)=?", (document_id, cnpj_root)).fetchone()
        if not document:
            self.send_page(HTTPStatus.NOT_FOUND, "Arquivo indisponível", "<main class='content'><h1>Arquivo não encontrado</h1></main>", user)
            return
        path = DOCUMENTS / (document["id"] + ".txt")
        if not path.is_file():
            self.send_page(HTTPStatus.NOT_FOUND, "Arquivo indisponível", "<main class='content'><h1>Arquivo não encontrado</h1></main>", user)
            return
        body = path.read_bytes()
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(document["original_name"]).name)[:150] or "sped.txt"
        self.send_bytes(HTTPStatus.OK, body, "text/plain; charset=utf-8", {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.form()
        except (ValueError, OSError):
            self.send_page(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Envio inválido", "<main class='content'><h1>Arquivo acima do limite de 40 MB.</h1></main>")
            return
        user = self.get_user()
        if not self.check_csrf(data, user):
            if path == "/login":
                self.redirect("/login")
                return
            self.send_page(HTTPStatus.FORBIDDEN, "Sessão expirada", "<main class='content'><h1>Sessão expirada. Atualize a página e tente novamente.</h1></main>", user)
            return
        if path == "/login":
            self.login(data)
        elif path == "/logout" and user:
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
            self.send_page(HTTPStatus.FORBIDDEN, "Acesso negado", "<main class='content'><h1>Ação não autorizada.</h1></main>", user)

    def login(self, data: dict[str, str | bytes]) -> None:
        ip = self.client_address[0]
        now = int(time.time())
        with connect() as db:
            attempt = db.execute("SELECT attempts, window_start FROM login_attempts WHERE ip=?", (ip,)).fetchone()
            if attempt and attempt["window_start"] > now - 900 and attempt["attempts"] >= 10:
                self.send_page(HTTPStatus.TOO_MANY_REQUESTS, "Aguarde", "<main class='content'><h1>Muitas tentativas. Aguarde 15 minutos.</h1></main>")
                return
            credential, password = data.get("cnpj", ""), data.get("password", "")
            credential = credential.strip() if isinstance(credential, str) else ""
            cnpj = normalize_cnpj(credential)
            password = password if isinstance(password, str) else ""
            account = db.execute("SELECT * FROM users WHERE active=1 AND ((role='store' AND cnpj=?) OR (role='admin' AND login_name=?))", (cnpj, credential.lower())).fetchone()
            if not account or not verify_password(password, account["password_hash"]):
                attempts = attempt["attempts"] + 1 if attempt and attempt["window_start"] > now - 900 else 1
                db.execute("INSERT INTO login_attempts VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET attempts=excluded.attempts, window_start=excluded.window_start", (ip, attempts, attempt["window_start"] if attempt and attempt["window_start"] > now - 900 else now))
                self.send_page(HTTPStatus.UNAUTHORIZED, "Acesso não autorizado", self.login_error())
                return
            db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
            raw_token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            db.execute("DELETE FROM sessions WHERE user_id=?", (account["id"],))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (token_hash(raw_token), account["id"], csrf, now + SESSION_SECONDS))
        self.session_cookie = raw_token
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/dashboard")
        self.send_header("Set-Cookie", f"portal_session={raw_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}" + ("; Secure" if os.environ.get("COOKIE_SECURE") == "1" else ""))
        self.send_header("Set-Cookie", "login_nonce=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self.end_headers()

    def login_error(self) -> bytes:
        content = "<main class='login-layout'><section class='login-brand'><div class='eyebrow'>ÁREA EXCLUSIVA</div><h1>Fiscal em dia.<br><em>Sem complicação.</em></h1><p>Seus arquivos fiscais, organizados por competência e protegidos em um só lugar.</p></section><section class='login-panel'><div class='notice notice-error'>CNPJ ou senha incorretos.</div><div class='login-heading'><span class='section-kicker'>TENTE NOVAMENTE</span><h2>Acesse seu portal</h2></div><a class='button button-outline' href='/login'>Voltar ao acesso</a></section></main>"
        return render_page("Acesso", content)

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
        file_content = data.get("file", b"")
        store_id, competence = data.get("store_id", ""), data.get("competence", "")
        document_type = data.get("document_type", "")
        original_name = data.get("file_name", "")
        if not isinstance(file_content, bytes) or not file_content or len(file_content) > MAX_UPLOAD_BYTES:
            self.redirect("/dashboard?status=invalid")
            return
        if not isinstance(store_id, str) or not store_id.isdigit() or not isinstance(competence, str) or not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", competence):
            self.redirect("/dashboard?status=invalid")
            return
        if document_type not in ("fiscal", "contribuicoes"):
            self.redirect("/dashboard?status=invalid")
            return
        try:
            header = parse_sped_header(file_content, document_type)
        except ValueError:
            self.redirect("/dashboard?status=invalid")
            return
        if header["competence"] != competence:
            self.redirect("/dashboard?status=invalid")
            return
        with connect() as db:
            store = db.execute("SELECT id, cnpj FROM users WHERE id=? AND role='store' AND active=1", (int(store_id),)).fetchone()
            if not store or store["cnpj"] != header["cnpj"]:
                self.redirect("/dashboard?status=invalid")
                return
            doc_id = str(uuid.uuid4())
            filename = Path(original_name if isinstance(original_name, str) and original_name else "sped.txt").name[:150]
            uploaded_at = datetime.now(timezone.utc).isoformat()
            document_path = DOCUMENTS / (doc_id + ".txt")
            document_path.write_bytes(file_content)
            try:
                os.chmod(document_path, 0o600)
            except OSError:
                pass
            try:
                db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (doc_id, store["id"], document_type, competence, filename, len(file_content), hashlib.sha256(file_content).hexdigest(), uploaded_at))
            except sqlite3.IntegrityError:
                document_path.unlink(missing_ok=True)
                self.redirect("/dashboard?status=exists")
                return
        self.redirect("/dashboard?status=uploaded")

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()


def main() -> None:
    configure_storage()
    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        username = input("Usuário administrador: ").strip()
        name = input("Nome do administrador: ").strip()
        password = getpass.getpass("Senha (mínimo 12 caracteres): ")
        confirm = getpass.getpass("Confirme a senha: ")
        if password != confirm:
            raise SystemExit("As senhas não conferem.")
        try:
            create_user(None, name, "admin", password, username)
        except (ValueError, sqlite3.IntegrityError) as error:
            raise SystemExit(str(error)) from error
        print("Administrador criado. Inicie o portal com: python app.py")
        return
    host = os.environ.get("PORTAL_HOST", "127.0.0.1")
    port = int(os.environ.get("PORTAL_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), PortalHandler)
    print(f"Portal Fiscal ativo em http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando portal...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()