from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import resumo_sped
from portal.publicacao import competence_summary, document_summary, publish_sped
from portal import auditoria, config
from portal.auditoria import AUDIT_ACTIONS, AUDIT_FILTERS, AUDIT_PERIODS, AUDITED_ROLES, WEEKDAYS, audit_entries, audit_sentence, browser_label, csv_cell, machine_name
from portal.auth import (READABLE_ALPHABET, ROLE_LABELS, STAFF_ROLES, USER_ROLES, create_support_user, create_user, password_digest, readable_password,
                         reset_store_password, token_hash, verify_password)
from portal import db as storage
from portal.db import configure_storage, connect, ensure_secret, initialize_database
from portal.sped import DOCUMENT_TYPES, SPED_LAYOUTS, PublishError, detect_sped_type, format_cnpj, normalize_cnpj, parse_sped_header, sped_label, valid_cnpj
from portal.calendario import (LARGE_GROUP_BUSINESS_DAY, LARGE_GROUP_NOTE, SMALL_GROUP_BUSINESS_DAY, SMALL_GROUP_UNITS, business_day, competence_timeline, easter,
                               next_competence, non_business_days, publication_deadline)


ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- monitoramento do suporte


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def publish_dialog(success: bool, message: str, details: list[tuple[str, str]] | None = None) -> str:
    """Pop-up do resultado da publicação. É um <dialog> nativo que fecha pelo próprio botão, sem JavaScript."""
    title = "Arquivo publicado com sucesso" if success else "Arquivo não publicado"
    rows = "".join(f"<div><dt>{h(term)}</dt><dd>{h(value)}</dd></div>" for term, value in details or [])
    facts = f"<dl class='publish-facts'>{rows}</dl>" if rows else ""
    return (f"<dialog open class='publish-dialog {'is-success' if success else 'is-error'}' role='{'status' if success else 'alert'}' aria-labelledby='publish-title'>"
            f"<span class='publish-mark'>{icon('upload' if success else 'file')}</span><h2 id='publish-title'>{title}</h2><p>{h(message)}</p>{facts}"
            "<form method='dialog'><button class='btn btn-primary' type='submit' autofocus><span>OK, entendi</span></button></form></dialog>")

# Ícones de traço (24x24) desenhados em SVG inline: nítidos em qualquer tela e permitidos pela CSP.
ICONS = {
    "download": "<path d='M12 4v11'/><path d='m7 10 5 5 5-5'/><path d='M5 20h14'/>",
    "logout": "<path d='M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3'/><path d='m10 16 4-4-4-4'/><path d='M14 12H4'/>",
    "store": "<path d='M4 10v10h16V10'/><path d='m3 10 2-6h14l2 6'/><path d='M9 20v-6h6v6'/>",
    "file": "<path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/>",
    "arrow": "<path d='M5 12h14'/><path d='m13 6 6 6-6 6'/>",
    "upload": "<path d='M12 20V9'/><path d='m7 14 5-5 5 5'/><path d='M5 4h14'/>",
    "plus": "<path d='M12 5v14'/><path d='M5 12h14'/>",
    "lock": "<rect x='5' y='11' width='14' height='9' rx='2'/><path d='M8 11V8a4 4 0 0 1 8 0v3'/>",
    "calendar": "<rect x='4' y='5' width='16' height='15' rx='2'/><path d='M4 10h16'/><path d='M9 3v4'/><path d='M15 3v4'/>",
    "eye": "<path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>",
    "filter": "<path d='M4 5h16l-6 7.5V19l-4-2v-4.5z'/>",
}


def icon(name: str) -> str:
    return f"<svg class='icon' viewBox='0 0 24 24' aria-hidden='true' focusable='false'>{ICONS[name]}</svg>"


def dashboard_link(store_id: int | None = None, document_type: str = "fiscal", competence: str = "") -> str:
    """Endereço do painel para uma loja, tipo de SPED e mês (já escapado para uso em atributo HTML)."""
    params = ([f"store_id={store_id}"] if store_id else []) + [f"type={document_type}"] + ([f"competence={competence}"] if competence else [])
    return "/dashboard?" + "&amp;".join(params)


def help_tip(text: str) -> str:
    """Ícone "?" que mostra a explicação ao passar o mouse, tocar ou focar pelo teclado."""
    return f"<span class='help' tabindex='0' role='note' aria-label='{h(text)}'>?<span class='help-tip' aria-hidden='true'>{h(text)}</span></span>"


def file_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB".replace(".", ",") if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


def short_store_names(stores: list[sqlite3.Row]) -> dict[int, str]:
    """Nomes curtos para as lojas de um mesmo grupo, sem a parte repetida da razão social.

    "FARMACIA EXEMPLO LTDA - VR" vira "VR"; "LOJA 2 - DROGARIA X LTDA" vira "LOJA 2".
    """
    return dict(zip((store["id"] for store in stores), resumo_sped.short_labels([store["name"].strip() for store in stores])))


# Versão do CSS e do script no endereço de cada um: muda quando o arquivo muda, e o navegador não usa a cópia antiga do cache.
STYLES_VERSION = hashlib.sha256((ROOT / "static" / "styles.css").read_bytes()).hexdigest()[:10]
SCRIPT_VERSION = hashlib.sha256((ROOT / "static" / "portal.js").read_bytes()).hexdigest()[:10]


def render_page(title: str, content: str, user: sqlite3.Row | None = None) -> bytes:
    nav = ""
    if user:
        nav = (f"<div class='user-chip'><span class='user-mark'>{h(user['name'][:1].upper())}</span><span class='user-text'>{h(user['name'])}<small>{ROLE_LABELS.get(user['role'], '')}</small></span>"
               f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{h(user['csrf_token'])}'><button class='btn btn-ghost btn-sm' type='submit'>{icon('logout')}<span>Sair</span></button></form></div>")
    brand = "<a class='brand' href='/dashboard'><img class='brand-logo' src='/static/farmacias-associadas-logo.png' alt='Farmácias Associadas'><span class='brand-context'>PORTAL FISCAL</span></a>"
    return (f"<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='theme-color' content='#087e70'><title>{h(title)} · Portal Fiscal</title><link rel='stylesheet' href='/static/styles.css?v={STYLES_VERSION}'><script src='/static/portal.js?v={SCRIPT_VERSION}' defer></script></head><body><header class='topbar'>{brand}{nav}</header>{content}<footer class='site-footer'><span>PORTAL FISCAL <i></i> FARMÁCIAS ASSOCIADAS <i></i> V{config.PORTAL_VERSION}</span><span>Documentos acessíveis somente à loja autorizada.</span></footer></body></html>").encode("utf-8")


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
            cookie = f"portal_session={self.session_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={config.SESSION_SECONDS}"
            if config.COOKIE_SECURE:
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
        """Tela de acesso. Com mensagem, mostra o erro acima do formulário e mantém o CNPJ digitado."""
        nonce, csrf = self.login_token()
        notice = f"<div class='notice notice-error' role='alert'>{h(message)}</div>" if message else ""
        kicker = "TENTE NOVAMENTE" if message else "BEM-VINDA, LOJA"
        content = ("<main class='login-layout'><section class='login-brand'><div class='eyebrow'>ÁREA EXCLUSIVA</div><h1>Fiscal em dia.<br><em>Sem complicação.</em></h1>"
                   "<p>Seus arquivos fiscais, organizados por competência e protegidos em um só lugar.</p><div class='login-foot'>ACESSO SEGURO <i></i> FARMÁCIAS ASSOCIADAS</div></section>"
                   f"<section class='login-panel'>{notice}<div class='login-heading'><span class='section-kicker'>{kicker}</span><h2>Acesse seu portal</h2><p>Entre com o CNPJ cadastrado e sua senha.</p></div>"
                   f"<form method='post' action='/login' class='stack-form'><input type='hidden' name='csrf' value='{h(csrf)}'>"
                   f"<label>CNPJ ou usuário<input name='cnpj' autocomplete='username' placeholder='CNPJ da loja ou usuário admin' maxlength='40' value='{h(credential)}' required></label>"
                   "<label>Senha<input name='password' type='password' autocomplete='current-password' placeholder='Sua senha de acesso' required></label>"
                   "<button class='button button-primary button-wide'>Entrar no portal <span>→</span></button></form>"
                   "<div class='login-note'><span class='lock-icon'>▣</span> Conexão protegida. Seus arquivos são privados.</div></section></main>")
        cookie = f"login_nonce={nonce}; Path=/; HttpOnly; SameSite=Strict; Max-Age={config.LOGIN_FORM_SECONDS}" + ("; Secure" if config.COOKIE_SECURE else "")
        self.send_bytes(status, render_page("Acesso", content), headers={"Set-Cookie": cookie})

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
        if path == "/static/portal.js":
            script = (ROOT / "static" / "portal.js").read_bytes()
            self.send_bytes(HTTPStatus.OK, script, "text/javascript; charset=utf-8", {"Cache-Control": "public, max-age=3600"})
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
            self.send_login_page()
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
        elif config.MONITORING_ENABLED and path in ("/monitoramento", "/monitoramento.csv"):
            if user["role"] != "admin":
                self.audit(user, "acao_bloqueada", detail="abrir o monitoramento do suporte")
                self.send_page(HTTPStatus.FORBIDDEN, "Acesso negado", "<main class='content'><h1>Área restrita à administração.</h1></main>", user)
            elif path.endswith(".csv"):
                self.monitoring_csv()
            else:
                self.monitoring_page(user)
        else:
            self.send_page(HTTPStatus.NOT_FOUND, "Página não encontrada", "<main class='content'><h1>Página não encontrada</h1></main>", user)

    def dashboard_workspace(self, user: sqlite3.Row, dialog: str = "", status: int = HTTPStatus.OK) -> None:
        """Tela principal: barra lateral (loja e tipo de SPED) e área de trabalho da loja ou da rede."""
        query = parse_qs(urlparse(self.path).query)
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
        if selected_store:
            self.audit(user, "abriu_loja", selected_store, document_type, competence)
        else:
            self.audit(user, "abriu_central", document_type=document_type)

        main = ""
        message = self.query_message()
        if message:
            main += f"<div class='notice' role='status'>{h(message)}</div>"
        if user["role"] == "support":
            recorded = " As ações deste acesso são registradas." if config.MONITORING_ENABLED else ""
            main += (f"<div class='notice notice-info' role='note'>{icon('eye')}<span><strong>Acesso de suporte:</strong> consulta de todas as lojas e download de arquivos. "
                     f"Para publicar arquivos ou cadastrar lojas, fale com a administração.{recorded}</span></div>")
        if selected_store:
            main += self.page_heading(f"{label} · {subject}", selected_store["name"], f"CNPJ {format_cnpj(selected_store['cnpj'])}")
        elif admin:
            main += self.page_heading(f"{label} · {subject}", "Central fiscal", "Publique arquivos, cadastre lojas e acompanhe a rede.")
        else:
            main += self.page_heading(f"{label} · {subject}", "Central fiscal", "Escolha uma loja para consultar ou baixe os arquivos mais recentes.")
        if admin:
            main += self.admin_tools(user, selected_store)
        if selected_store and matrix_with_file:
            main += (f"<div class='empty-state'><span class='empty-mark'>{icon('file')}</span><strong>O {h(label)} desta loja é entregue pela matriz</strong>"
                     "<span>O arquivo da matriz reúne todas as lojas do grupo, inclusive esta.</span>"
                     f"<a class='btn btn-primary btn-sm' href='{dashboard_link(matrix_with_file['id'], document_type)}'><span>Abrir arquivo da matriz</span>{icon('arrow')}</a></div>")
        elif selected_store:
            main += self.period_nav(selected_store, document_type, months, competences, competence, units)
            if competence in competences:
                main += self.files_card(documents, competence)
                main += self.summary_section(documents, competence)
            else:
                main += self.forecast_card(document_type, competence, units)
        else:
            main += self.stores_table(store_rows, document_type) + self.recent_uploads(documents)

        if admin and not dialog:
            dialog = self.published_dialog(query.get("published", [""])[0])
        content = dialog + "<div class='workspace'>" + self.sidebar(user, stores, selected_store, document_type) + "<main class='workspace-main'>" + main + "</main></div>"
        self.send_page(status, selected_store["name"] if selected_store else "Central fiscal", content, user)

    def published_dialog(self, document_id: str) -> str:
        """Confirmação depois da publicação: tipo do SPED, loja, CNPJ e competência lidos do arquivo."""
        if not document_id:
            return ""
        with connect() as db:
            document = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.id=?", (document_id,)).fetchone()
        if not document:
            return ""
        label = sped_label(document["document_type"])
        month = resumo_sped.month_label(document["competence"]).capitalize()
        message = f"O arquivo {label} foi publicado com sucesso na loja {document['store_name']}, CNPJ {format_cnpj(document['store_cnpj'])}, competência {month}."
        return publish_dialog(True, message, [("Tipo", label), ("Loja", document["store_name"]), ("CNPJ", format_cnpj(document["store_cnpj"])),
                                              ("Competência", month), ("Arquivo", document["original_name"])])

    def page_heading(self, kicker: str, title: str, text: str) -> str:
        return f"<div class='page-heading'><div><span class='section-kicker'>{h(kicker.upper())}</span><h1>{h(title)}</h1><p>{h(text)}</p></div></div>"

    def sidebar(self, user: sqlite3.Row, stores: list[sqlite3.Row], selected_store: sqlite3.Row | None, document_type: str, section: str = "documentos") -> str:
        """Barra lateral: loja aberta, troca de loja (lista para o grupo, seletor para administração e suporte),
        tipo de SPED e, para a administração, o monitoramento do suporte."""
        admin = user["role"] == "admin"
        staff = user["role"] in STAFF_ROLES
        if selected_store:
            identity = f"<strong>{h(selected_store['name'])}</strong><small>CNPJ {h(format_cnpj(selected_store['cnpj']))}</small>"
        else:
            identity = f"<strong>Todas as lojas</strong><small>Visão {'da administração' if admin else 'do suporte'}</small>"
        picker = ""
        if staff:
            options = "<option value=''>Todas as lojas</option>" + "".join(
                f"<option value='{store['id']}'{' selected' if selected_store and store['id'] == selected_store['id'] else ''}>{h(store['name'])}</option>" for store in stores)
            picker = ("<form class='store-picker' method='get' action='/dashboard'><input type='hidden' name='type' value='" + h(document_type) + "'>"
                      "<label for='store_id'>Escolher loja</label><select id='store_id' name='store_id'>" + options + "</select>"
                      f"<button class='btn btn-outline btn-sm btn-block' type='submit'><span>Abrir loja</span>{icon('arrow')}</button></form>")
        card = (f"<div class='side-store{' with-picker' if picker else ''}'><div class='side-store-heading'><span class='side-store-mark'>{icon('store')}</span><span>{'LOJA ABERTA' if selected_store else 'REDE'}</span></div>"
                f"<div class='side-store-identity'>{identity}</div>{picker}</div>")
        branches = ""
        if not staff and len(stores) > 1:
            short = short_store_names(stores)
            links = ""
            for store in stores:
                active = bool(selected_store) and store["id"] == selected_store["id"]
                cnpj = normalize_cnpj(store["cnpj"] or "")
                tag = "Matriz" if cnpj[8:12] == "0001" else f"/{cnpj[8:12]}-{cnpj[12:]}"
                state = " active' aria-current='page" if active else ""
                links += (f"<a class='branch-link{state}' href='{dashboard_link(store['id'], document_type)}' title='{h(store['name'])}'>"
                          f"<span>{h(short[store['id']])}</span><small>{h(tag)}</small></a>")
            branches = f"<nav class='branch-nav' aria-label='Lojas do grupo'><span class='side-section-label'>LOJAS DO GRUPO</span><div class='branch-list'>{links}</div></nav>"
        store_id = selected_store["id"] if selected_store else None
        current = " active' aria-current='page"
        tabs = "".join(
            f"<a class='sped-tab{current if section == 'documentos' and key == document_type else ''}' href='{dashboard_link(store_id, key)}'>"
            f"<span class='tab-mark'>{mark}</span><span class='tab-text'>{label}<small>{subject}</small></span></a>"
            for key, (label, subject, mark) in DOCUMENT_TYPES.items())
        navigation = f"<nav class='sped-navigation' aria-label='Tipo de arquivo'><span class='side-section-label'>ARQUIVOS</span>{tabs}</nav>"
        if admin and config.MONITORING_ENABLED:
            navigation += (f"<nav class='sped-navigation' aria-label='Administração'><span class='side-section-label'>ADMINISTRAÇÃO</span>"
                           f"<a class='sped-tab{current if section == 'monitoramento' else ''}' href='/monitoramento'><span class='tab-mark'>{icon('eye')}</span>"
                           "<span class='tab-text'>Monitoramento<small>Ações do suporte</small></span></a></nav>")
        return f"<aside class='workspace-sidebar'><div class='side-overline'>PORTAL DA REDE</div>{card}{branches}{navigation}<div class='side-bottom'><span class='secure-dot'></span> Acesso protegido</div></aside>"

    def period_nav(self, store: sqlite3.Row, document_type: str, months: list[str], published: list[str], competence: str, units: int) -> str:
        """Meses em ordem cronológica. Os ainda não publicados aparecem como "previsto", com o prazo no tooltip."""
        current = " active' aria-current='page"
        pills = ""
        for comp in months:
            pending = comp not in published
            hint = f" title='Previsto até {publication_deadline(comp, units)[0]:%d/%m/%Y}'" if pending else ""
            pills += (f"<a class='pill{' pending' if pending else ''}{current if comp == competence else ''}' href='{dashboard_link(store['id'], document_type, comp)}'{hint}>"
                      f"{h(resumo_sped.month_label(comp, short=True).capitalize())}</a>")
        note = ""
        upcoming = [comp for comp in months if comp not in published and (not published or comp > max(published))]
        if upcoming and competence in published:
            deadline, nth = publication_deadline(upcoming[0], units)
            note = (f"<p class='period-next'>{icon('calendar')}<span>Próximo arquivo: <strong>{h(resumo_sped.month_label(upcoming[0]).capitalize())}</strong>, "
                    f"previsto até <strong>{deadline:%d/%m/%Y}</strong> ({nth}º dia útil)</span>{help_tip(LARGE_GROUP_NOTE) if units > SMALL_GROUP_UNITS else ''}</p>")
        return (f"<nav class='period-nav' aria-label='Mês de referência'><span class='period-label'>Mês de referência</span><div class='pill-list'>{pills}</div></nav>{note}")

    def forecast_card(self, document_type: str, competence: str, units: int) -> str:
        """Mês ainda sem arquivo: mostra até quando ele deve ser publicado e a regra do prazo."""
        label = DOCUMENT_TYPES[document_type][0]
        deadline, nth = publication_deadline(competence, units)
        month = resumo_sped.month_label(competence)
        following = resumo_sped.MONTHS[deadline.month - 1]
        if date.today() > deadline:
            kicker, text = "EM PREPARAÇÃO", f"O prazo previsto era {deadline:%d/%m/%Y}. O arquivo está em preparação e aparece aqui assim que for publicado."
        else:
            kicker, text = "PREVISÃO DE PUBLICAÇÃO", f"O {label} de {month} é publicado depois do fechamento do mês, até o {nth}º dia útil de {following}."
        if units > SMALL_GROUP_UNITS:
            rule = f"<p class='forecast-rule'><span>Grupo com {units} lojas: prazo de {LARGE_GROUP_BUSINESS_DAY}º dia útil do mês seguinte.</span>{help_tip(LARGE_GROUP_NOTE)}</p>"
        else:
            rule = f"<p class='forecast-rule'><span>Grupos com até {SMALL_GROUP_UNITS} lojas: prazo de {SMALL_GROUP_BUSINESS_DAY}º dia útil do mês seguinte.</span></p>"
        return (f"<section class='section-block'><div class='forecast-card'><span class='forecast-icon'>{icon('calendar')}</span><div class='forecast-body'>"
                f"<span class='section-kicker'>{kicker}</span><h2>Até {deadline:%d/%m/%Y}</h2><p>{h(text)}</p>{rule}</div></div></section>")

    def files_card(self, documents: list[sqlite3.Row], competence: str) -> str:
        files = [doc for doc in documents if doc["competence"] == competence]
        rows = "".join(
            f"<li class='file-row'><span class='file-icon'>{icon('file')}</span><div class='file-info'><strong title='{h(doc['original_name'])}'>{h(doc['original_name'])}</strong>"
            f"<small>Publicado em {h(datetime.fromisoformat(doc['uploaded_at']).strftime('%d/%m/%Y'))} · {h(file_size(doc['size']))}</small></div>"
            f"<a class='btn btn-primary btn-sm' href='/download/{h(doc['id'])}' aria-label='Baixar {h(doc['original_name'])}'>{icon('download')}<span>Baixar</span></a></li>"
            for doc in files)
        total = f"{len(files)} {'arquivo' if len(files) == 1 else 'arquivos'}"
        return f"<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>ARQUIVOS DO MÊS</span><h2>Pronto para baixar</h2></div><span class='count-badge'>{total}</span></div><ul class='file-list'>{rows}</ul></section>"

    def summary_section(self, documents: list[sqlite3.Row], competence: str) -> str:
        competences = sorted({doc["competence"] for doc in documents})
        summaries = {comp: competence_summary(documents, comp) for comp in sorted(set(competences[-12:]) | {competence})}
        history = [(comp, resumo_sped.headline(summary)) for comp, summary in summaries.items() if summary and summary.get("operacoes")]
        return ("<section class='section-block summary-block'><div class='section-heading'><div><span class='section-kicker'>RESUMO DO MÊS</span>"
                f"<h2>{h(resumo_sped.month_label(competence).capitalize())}</h2><p class='section-copy'>Números calculados automaticamente a partir do arquivo deste mês.</p></div></div>"
                + resumo_sped.render(summaries.get(competence), competence, history) + "</section>")

    def admin_tools(self, user: sqlite3.Row, selected_store: sqlite3.Row | None) -> str:
        """Formulários da administração: publicar SPED e cadastrar loja. Recolhidos quando uma loja está aberta."""
        csrf = h(user["csrf_token"])
        upload = (f"<div class='panel'><div class='panel-title'><span class='panel-icon upload-icon'>{icon('upload')}</span><div><span class='section-kicker'>PUBLICAR · FISCAL OU CONTRIBUIÇÕES</span><h2>Enviar arquivo SPED</h2></div></div>"
                  "<p class='panel-copy'>Escolha o arquivo e publique. O portal lê o registro 0000 e identifica sozinho o tipo (Fiscal ICMS/IPI ou Contribuições PIS/COFINS), "
                  "a loja pelo CNPJ e a competência. Depois anexa o arquivo na aba certa da loja.</p>"
                  f"<form method='post' enctype='multipart/form-data' action='/upload' class='stack-form'><input type='hidden' name='csrf' value='{csrf}'>"
                  "<label>Arquivo .txt<input class='file-input' name='file' type='file' accept='.txt,text/plain' required><small class='field-help'>Até 40 MB. A loja precisa estar cadastrada com o CNPJ do arquivo.</small></label>"
                  f"<button class='btn btn-primary' type='submit'>{icon('upload')}<span>Validar e publicar</span></button></form></div>")
        register = (f"<div class='panel'><div class='panel-title'><span class='panel-icon store-icon'>{icon('plus')}</span><div><span class='section-kicker'>ACESSOS</span><h2>Cadastrar loja</h2></div></div>"
                    "<p class='panel-copy'>Crie o acesso da farmácia. Ela entra com o CNPJ e esta senha.</p>"
                    f"<form method='post' action='/stores' class='stack-form'><input type='hidden' name='csrf' value='{csrf}'>"
                    "<label>Nome da loja<input name='name' maxlength='120' placeholder='Ex.: Farmácia Central' required></label>"
                    "<label>CNPJ<input name='cnpj' inputmode='numeric' maxlength='18' placeholder='00.000.000/0000-00' required></label>"
                    "<label>Senha inicial<input name='password' type='password' minlength='12' autocomplete='new-password' placeholder='Mínimo de 12 caracteres' required></label>"
                    f"<button class='btn btn-outline' type='submit'>{icon('plus')}<span>Criar acesso</span></button></form></div>")
        grid = f"<div class='admin-grid'>{upload}{register}</div>"
        if selected_store:
            return f"<details class='more-details admin-tools'><summary><span class='more-title'>Publicar arquivo ou cadastrar loja</span><span class='more-hint'>Ferramentas da administração</span></summary>{grid}</details>"
        return f"<section class='admin-section'>{grid}</section>"

    def stores_table(self, store_rows: list[sqlite3.Row], document_type: str) -> str:
        rows = "".join(
            f"<tr><td class='store-name'>{h(store['name'])}</td><td class='mono'>{h(format_cnpj(store['cnpj']))}</td><td>{store['total']}</td>"
            f"<td class='cell-action'><a class='btn btn-outline btn-sm' href='{dashboard_link(store['id'], document_type)}'><span>Abrir</span>{icon('arrow')}</a></td></tr>"
            for store in store_rows) or "<tr><td colspan='4' class='empty-cell'>Nenhuma loja cadastrada ainda.</td></tr>"
        return (f"<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>REDE</span><h2>Lojas cadastradas</h2></div><span class='count-badge'>{len(store_rows)} lojas</span></div>"
                f"<div class='table-wrap'><table><thead><tr><th>LOJA</th><th>CNPJ</th><th>ARQUIVOS</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>")

    def recent_uploads(self, documents: list[sqlite3.Row]) -> str:
        if not documents:
            return ""
        rows = "".join(
            f"<tr><td class='store-name'>{h(doc['store_name'])}</td><td><span class='month-tag'>{h(doc['competence'][5:7] + '/' + doc['competence'][:4])}</span></td>"
            f"<td class='file-name' title='{h(doc['original_name'])}'>{h(doc['original_name'])}</td><td class='muted'>{h(datetime.fromisoformat(doc['uploaded_at']).strftime('%d/%m/%Y'))}</td>"
            f"<td class='cell-action'><a class='btn btn-ghost btn-sm' href='/download/{h(doc['id'])}' aria-label='Baixar {h(doc['original_name'])}'>{icon('download')}<span>Baixar</span></a></td></tr>"
            for doc in documents)
        return (f"<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>ÚLTIMAS PUBLICAÇÕES</span><h2>Arquivos enviados recentemente</h2></div></div>"
                f"<div class='table-wrap'><table><thead><tr><th>LOJA</th><th>MÊS</th><th>ARQUIVO</th><th>PUBLICADO</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>")

    def monitoring_filters(self) -> tuple[str, str, str]:
        query = parse_qs(urlparse(self.path).query)
        period = query.get("periodo", ["7d"])[0]
        category = query.get("acao", [""])[0]
        return query.get("usuario", [""])[0], period if period in AUDIT_PERIODS else "7d", category if category in AUDIT_FILTERS else ""

    def monitoring_page(self, user: sqlite3.Row) -> None:
        """Tela da administração com tudo o que o suporte fez: resumo, filtros e histórico agrupado por dia."""
        user_filter, period, category = self.monitoring_filters()
        with connect() as db:
            support_users = db.execute("SELECT id, name, login_name FROM users WHERE role='support' ORDER BY name").fetchall()
            stores = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 ORDER BY name").fetchall()
        everything = audit_entries(user_filter, period, "", limit=None)
        entries = audit_entries(user_filter, period, category, limit=501)
        truncated, entries = len(entries) > 500, entries[:500]

        def options(choices: dict[str, str], selected: str) -> str:
            return "".join(f"<option value='{h(value)}'{' selected' if value == selected else ''}>{h(label)}</option>" for value, label in choices.items())

        user_choices = {"": "Todos os usuários do suporte"} | {str(item["id"]): f"{item['name']} ({item['login_name']})" for item in support_users}
        export = "/monitoramento.csv?" + "&amp;".join(f"{key}={h(value)}" for key, value in (("usuario", user_filter), ("periodo", period), ("acao", category)) if value)
        filters = ("<form class='audit-filters' method='get' action='/monitoramento'>"
                   f"<label>Usuário<select name='usuario'>{options(user_choices, user_filter)}</select></label>"
                   f"<label>Período<select name='periodo'>{options({key: label for key, (label, _) in AUDIT_PERIODS.items()}, period)}</select></label>"
                   f"<label>O que fez<select name='acao'>{options(AUDIT_FILTERS, category)}</select></label>"
                   f"<div class='audit-filter-actions'><button class='btn btn-primary btn-sm' type='submit'>{icon('filter')}<span>Filtrar</span></button>"
                   f"<a class='btn btn-outline btn-sm' href='{export}'>{icon('download')}<span>Exportar para Excel</span></a></div></form>")

        period_label = AUDIT_PERIODS[period][0].lower()
        stores_seen = len({entry["store_id"] for entry in everything if entry["action"] == "abriu_loja"})
        downloads = sum(1 for entry in everything if entry["action"] == "baixou_arquivo")
        alerts = sum(1 for entry in everything if AUDIT_ACTIONS.get(entry["action"], ("", ""))[1] == "alertas")
        tiles = ("<div class='kpi-grid'>"
                 + resumo_sped.kpi("Ações registradas", str(len(everything)), period_label.capitalize())
                 + resumo_sped.kpi("Lojas consultadas", str(stores_seen), "Lojas diferentes abertas")
                 + resumo_sped.kpi("Arquivos baixados", str(downloads), "Downloads de SPED")
                 + resumo_sped.kpi("Alertas", str(alerts), "Senha errada ou ação bloqueada")
                 + "</div>")
        last_login = next((entry for entry in everything if entry["action"] == "login"), None)
        last = ""
        if last_login:
            moment = datetime.fromisoformat(last_login["created_at"])
            last = (f"<p class='period-next'>{icon('eye')}<span>Último acesso: <strong>{h(last_login['user_name'])}</strong> em <strong>{moment:%d/%m/%Y} às {moment:%H:%M}</strong>, "
                    f"na máquina <strong>{h(last_login['machine'] or 'não identificada')}</strong> ({h(last_login['ip'])})</span></p>")

        if not support_users:
            history = ("<div class='empty-state'><span class='empty-mark'>" + icon("eye") + "</span><strong>Nenhum usuário de suporte cadastrado</strong>"
                       "<span>Crie um com o arquivo criar-usuario-suporte.bat na pasta do portal.</span></div>")
        elif not entries:
            history = ("<div class='empty-state'><span class='empty-mark'>" + icon("eye") + "</span><strong>Nenhuma ação do suporte neste período</strong>"
                       "<span>Mude o período ou o filtro para ver outros registros.</span></div>")
        else:
            today, rows, current_day = date.today(), "", None
            for entry in entries:
                moment = datetime.fromisoformat(entry["created_at"])
                if moment.date() != current_day:
                    current_day = moment.date()
                    prefix = "Hoje · " if current_day == today else "Ontem · " if current_day == today - timedelta(days=1) else ""
                    rows += f"<tr class='day-row'><td colspan='6'>{prefix}{WEEKDAYS[current_day.weekday()]}, {current_day:%d/%m/%Y}</td></tr>"
                badge, group = AUDIT_ACTIONS.get(entry["action"], ("Ação", "consultas"))
                sentence, extra = audit_sentence(entry)
                store = (f"<strong>{h(entry['store_name'])}</strong><small>{h(format_cnpj(entry['store_cnpj']))}</small>" if entry["store_name"] else "<span class='muted'>—</span>")
                machine = f"<strong>{h(entry['machine'])}</strong>" if entry["machine"] else "<span class='muted'>Não identificada</span>"
                rows += (f"<tr><td class='mono'>{moment:%H:%M:%S}</td><td class='audit-cell'><strong>{h(entry['user_name'])}</strong><small>{h(entry['login_name'] or '')}</small></td>"
                         f"<td class='audit-cell audit-what'><span class='audit-badge badge-{group}'>{h(badge)}</span><span><strong>{h(sentence)}</strong>{f'<small>{h(extra)}</small>' if extra else ''}</span></td>"
                         f"<td class='audit-cell'>{store}</td><td class='audit-cell audit-machine'>{machine}<small>IP {h(entry['ip'])}</small></td><td class='audit-cell muted'>{h(entry['browser'])}</td></tr>")
            more = "<p class='summary-note'>Mostrando as 500 ações mais recentes. Use os filtros ou exporte para Excel para ver todas.</p>" if truncated else ""
            history = ("<div class='table-wrap audit-table'><table><thead><tr><th>HORA</th><th>USUÁRIO</th><th>O QUE FEZ</th><th>LOJA</th><th>MÁQUINA</th><th>NAVEGADOR</th></tr></thead>"
                       f"<tbody>{rows}</tbody></table></div>{more}")

        note = ("<p class='summary-note'>Horários de Brasília (horário deste servidor). A máquina é o nome do computador informado pela rede; "
                "quando a rede não informa, aparece só o IP. Abrir a mesma tela de novo em menos de 1 minuto não gera registro repetido.</p>")
        main = (self.page_heading("Administração", "Monitoramento do suporte", "Tudo o que os usuários do suporte fazem no portal: entradas, lojas consultadas e arquivos baixados.")
                + filters + tiles + last
                + "<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>HISTÓRICO</span><h2>O que o suporte fez</h2></div>"
                + f"<span class='count-badge'>{len(entries)} {'ação' if len(entries) == 1 else 'ações'}</span></div>{history}{note}</section>")
        content = "<div class='workspace'>" + self.sidebar(user, stores, None, "fiscal", section="monitoramento") + "<main class='workspace-main'>" + main + "</main></div>"
        self.send_page(HTTPStatus.OK, "Monitoramento do suporte", content, user)

    def monitoring_csv(self) -> None:
        """Exporta o histórico filtrado em CSV (separador ; e acentos corretos no Excel)."""
        user_filter, period, category = self.monitoring_filters()
        header = ("Data", "Hora", "Usuário", "Login", "Tipo", "O que fez", "Detalhe", "Loja", "CNPJ da loja", "Arquivo", "Máquina", "IP", "Navegador")
        lines = [";".join(csv_cell(value) for value in header)]
        for entry in audit_entries(user_filter, period, category, limit=None):
            moment = datetime.fromisoformat(entry["created_at"])
            sentence, extra = audit_sentence(entry)
            values = (f"{moment:%d/%m/%Y}", f"{moment:%H:%M:%S}", entry["user_name"], entry["login_name"], AUDIT_ACTIONS.get(entry["action"], ("Ação", ""))[0], sentence, extra,
                      entry["store_name"], format_cnpj(entry["store_cnpj"]) if entry["store_cnpj"] else "", entry["file_name"], entry["machine"] or "Não identificada", entry["ip"], entry["browser"])
            lines.append(";".join(csv_cell(value) for value in values))
        body = ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")
        self.send_bytes(HTTPStatus.OK, body, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="monitoramento-suporte-{date.today():%Y-%m-%d}.csv"'})

    def query_message(self) -> str:
        code = parse_qs(urlparse(self.path).query).get("status", [""])[0]
        return {"store-created": "Acesso da loja criado com sucesso.", "invalid": "Não foi possível concluir. Confira os dados informados."}.get(code, "")

    def download(self, user: sqlite3.Row, document_id: str) -> None:
        with connect() as db:
            if user["role"] in STAFF_ROLES:
                document = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.id=?", (document_id,)).fetchone()
            else:
                cnpj_root = normalize_cnpj(user["cnpj"] or "")[:8]
                document = db.execute("SELECT documents.* FROM documents JOIN users AS document_store ON document_store.id=documents.store_id WHERE documents.id=? AND document_store.role='store' AND substr(document_store.cnpj,1,8)=?", (document_id, cnpj_root)).fetchone()
        if not document:
            self.send_page(HTTPStatus.NOT_FOUND, "Arquivo indisponível", "<main class='content'><h1>Arquivo não encontrado</h1></main>", user)
            return
        path = storage.DOCUMENTS / (document["id"] + ".txt")
        if not path.is_file():
            self.send_page(HTTPStatus.NOT_FOUND, "Arquivo indisponível", "<main class='content'><h1>Arquivo não encontrado</h1></main>", user)
            return
        body = path.read_bytes()
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(document["original_name"]).name)[:150] or "sped.txt"
        if user["role"] in AUDITED_ROLES:
            # Registra antes de enviar: o download nunca deixa de constar no histórico.
            store = {"id": document["store_id"], "name": document["store_name"], "cnpj": document["store_cnpj"]}
            self.audit(user, "baixou_arquivo", store, document["document_type"], document["competence"], file_name=document["original_name"])
        self.send_bytes(HTTPStatus.OK, body, "text/plain; charset=utf-8", {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})

    def audit(self, user: sqlite3.Row | dict | None, action: str, store: sqlite3.Row | dict | None = None, document_type: str | None = None,
              competence: str | None = None, file_name: str | None = None, detail: str | None = None) -> None:
        auditoria.record(user, action, self.client_address[0], self.headers.get("User-Agent", ""), store, document_type, competence, file_name, detail)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.form()
        except (ValueError, OSError):
            self.send_page(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Envio inválido", "<main class='content'><h1>Arquivo acima do limite de 40 MB.</h1></main>")
            return
        user = self.get_user()
        if path == "/login" and not self.check_csrf(data, None):
            # Nunca volta ao login em silêncio: mostra o formulário de novo, com o CNPJ já preenchido.
            credential = data.get("cnpj", "")
            self.send_login_page(HTTPStatus.OK, "A página de acesso expirou. Digite a senha e clique em Entrar novamente.", credential if isinstance(credential, str) else "")
            return
        if path != "/login" and not self.check_csrf(data, user):
            self.send_page(HTTPStatus.FORBIDDEN, "Sessão expirada", "<main class='content'><h1>Sessão expirada. Atualize a página e tente novamente.</h1></main>", user)
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
            self.send_page(HTTPStatus.FORBIDDEN, "Acesso negado", "<main class='content'><h1>Ação não autorizada.</h1></main>", user)

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
    if len(sys.argv) > 1 and sys.argv[1] == "criar-suporte":
        # python app.py criar-suporte [usuario] [nome] [--salvar-em arquivo.txt]
        args = sys.argv[2:]
        target = None
        if "--salvar-em" in args:
            position = args.index("--salvar-em")
            target = Path(args[position + 1]) if position + 1 < len(args) else None
            args = args[:position] + args[position + 2:]
        login_name = (args[0] if args else input("Usuário do suporte (ex.: suporte): ")).strip().lower() or "suporte"
        name = args[1] if len(args) > 1 else "Suporte Farmácias Associadas"
        try:
            password = create_support_user(login_name, name)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Usuário de suporte criado: {login_name} ({name})")
        print(f"Senha: {password}")
        print("Acesso: todas as lojas, somente consulta e download." + (" As ações ficam registradas no monitoramento da administração." if config.MONITORING_ENABLED else ""))
        if target:
            block = (f"\nPORTAL FISCAL - ACESSO SUPORTE\nUsuário: {login_name}\nSenha: {password}\n"
                     "Perfil: consulta de todas as lojas e download de arquivos. Não publica nem cadastra." + (" Ações registradas no monitoramento." if config.MONITORING_ENABLED else "") + "\n")
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(block)
            print(f"Credencial adicionada em: {target}")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "redefinir-senha":
        cnpj = sys.argv[2] if len(sys.argv) > 2 else input("CNPJ da loja: ")
        try:
            name, password = reset_store_password(cnpj)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Loja: {name}")
        print(f"CNPJ: {format_cnpj(cnpj)}" if len(normalize_cnpj(cnpj)) == 14 else f"Usuário: {cnpj.strip().lower()}")
        print(f"Nova senha: {password}")
        print("Sessões antigas encerradas e bloqueios de tentativa liberados.")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "gerar-resumos":
        with connect() as db:
            documents = db.execute("SELECT * FROM documents ORDER BY uploaded_at").fetchall()
        failures = sum(1 for document in documents if document_summary(document) is None)
        print(f"Resumos prontos: {len(documents) - failures} de {len(documents)} arquivos.")
        return
    host = os.environ.get("PORTAL_HOST", "127.0.0.1")
    port = int(os.environ.get("PORTAL_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), PortalHandler)
    print(f"Portal Fiscal {config.PORTAL_VERSION} ativo em http://{host}:{port} (monitoramento do suporte {'ativo' if config.MONITORING_ENABLED else 'desativado'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando portal...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()