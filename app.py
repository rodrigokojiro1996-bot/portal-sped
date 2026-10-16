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

import resumo_sped


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PORTAL_DATA_DIR", ROOT / "data"))
DATABASE = DATA_DIR / "portal.sqlite3"
DOCUMENTS = DATA_DIR / "documentos"
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
LOGIN_FORM_SECONDS = 2 * 60 * 60
# Senhas geradas pelo portal: sem caracteres que se confundem ao ler ou digitar (0/O, 1/l/I).
READABLE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
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
        db.execute("""CREATE TABLE IF NOT EXISTS document_summaries (
            document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")


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


def reset_store_password(cnpj: str) -> tuple[str, str]:
    """Gera uma senha nova para a loja, encerra as sessões dela e libera bloqueios de tentativas de login."""
    normalized = normalize_cnpj(cnpj)
    password = "-".join("".join(secrets.choice(READABLE_ALPHABET) for _ in range(4)) for _ in range(3))
    with connect() as db:
        store = db.execute("SELECT id, name FROM users WHERE role='store' AND cnpj=?", (normalized,)).fetchone()
        if not store:
            raise ValueError("Nenhuma loja cadastrada com esse CNPJ.")
        db.execute("UPDATE users SET password_hash=?, active=1 WHERE id=?", (password_digest(password), store["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (store["id"],))
        db.execute("DELETE FROM login_attempts")
    return store["name"], password


def document_summary(document: sqlite3.Row) -> dict | None:
    """Resumo agregado de um SPED publicado, calculado uma vez e guardado no banco."""
    with connect() as db:
        cached = db.execute("SELECT summary FROM document_summaries WHERE document_id=? AND version=?", (document["id"], resumo_sped.SUMMARY_VERSION)).fetchone()
    if cached:
        return json.loads(cached["summary"])
    path = DOCUMENTS / (document["id"] + ".txt")
    if not path.is_file():
        return None
    try:
        summary = resumo_sped.summarize(path, document["document_type"])
    except (OSError, ValueError, IndexError):
        return None
    with connect() as db:
        db.execute("INSERT INTO document_summaries (document_id, version, summary, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(document_id) DO UPDATE SET version=excluded.version, summary=excluded.summary, created_at=excluded.created_at",
                   (document["id"], resumo_sped.SUMMARY_VERSION, json.dumps(summary, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    return summary


def competence_summary(documents: list[sqlite3.Row], competence: str) -> dict | None:
    published = sorted((doc for doc in documents if doc["competence"] == competence), key=lambda doc: doc["uploaded_at"])
    return resumo_sped.merge([summary for summary in (document_summary(doc) for doc in published) if summary])


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


# Tipos de SPED: rótulo, o que o arquivo apura e a letra do ícone da aba.
DOCUMENT_TYPES = {
    "fiscal": ("SPED Fiscal", "ICMS e IPI", "F"),
    "contribuicoes": ("SPED Contribuições", "PIS e COFINS", "C"),
}

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
}


def icon(name: str) -> str:
    return f"<svg class='icon' viewBox='0 0 24 24' aria-hidden='true' focusable='false'>{ICONS[name]}</svg>"


def format_cnpj(value: str | None) -> str:
    digits = normalize_cnpj(value or "")
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}" if len(digits) == 14 else (value or "")


def dashboard_link(store_id: int | None = None, document_type: str = "fiscal", competence: str = "") -> str:
    """Endereço do painel para uma loja, tipo de SPED e mês (já escapado para uso em atributo HTML)."""
    params = ([f"store_id={store_id}"] if store_id else []) + [f"type={document_type}"] + ([f"competence={competence}"] if competence else [])
    return "/dashboard?" + "&amp;".join(params)


# Previsão de publicação: os meses aparecem no painel até FORECAST_UNTIL, e cada arquivo
# do mês tem prazo no N-ésimo dia útil do mês seguinte, conforme o tamanho do grupo.
FORECAST_UNTIL = os.environ.get("PORTAL_PREVISAO_ATE", "2026-12")
SMALL_GROUP_UNITS = 3
SMALL_GROUP_BUSINESS_DAY = 3
LARGE_GROUP_BUSINESS_DAY = 5
LARGE_GROUP_NOTE = ("Por conta do número de lojas, a demanda requer mais tempo para conclusão e postagem. "
                    "Se algum arquivo precisar de correção, ele entra na fila e é postado após a conclusão das postagens obrigatórias e regulares.")


def easter(year: int) -> date:
    """Domingo de Páscoa (algoritmo gregoriano anônimo)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h_ = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h_ - k) % 7
    m = (a + 11 * h_ + 22 * l_) // 451
    month, day = divmod(h_ + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def non_business_days(year: int) -> set[date]:
    """Feriados nacionais, Carnaval e Sexta-feira Santa. Feriados estaduais e municipais não entram."""
    fixed = {date(year, month, day) for month, day in ((1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25))}
    sunday = easter(year)
    return fixed | {sunday - timedelta(days=48), sunday - timedelta(days=47), sunday - timedelta(days=2)}


def business_day(year: int, month: int, nth: int) -> date:
    holidays = non_business_days(year)
    day, found = date(year, month, 1), 0
    while True:
        if day.weekday() < 5 and day not in holidays:
            found += 1
            if found == nth:
                return day
        day += timedelta(days=1)


def next_competence(competence: str) -> str:
    year, month = int(competence[:4]), int(competence[5:7])
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def publication_deadline(competence: str, units: int) -> tuple[date, int]:
    """Prazo de publicação do SPED de um mês: 3º dia útil do mês seguinte para grupos de até 3 lojas, 5º para os maiores."""
    nth = SMALL_GROUP_BUSINESS_DAY if units <= SMALL_GROUP_UNITS else LARGE_GROUP_BUSINESS_DAY
    following = next_competence(competence)
    return business_day(int(following[:4]), int(following[5:7]), nth), nth


def competence_timeline(published: list[str], today: date | None = None) -> list[str]:
    """Meses exibidos no painel: dos publicados (ou do mês anterior) até o fim da previsão, no máximo 15."""
    today = today or date.today()
    previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month, end = min(published + [previous]), max(published + [FORECAST_UNTIL])
    months = []
    while month <= end:
        months.append(month)
        month = next_competence(month)
    return months[-15:]


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


def render_page(title: str, content: str, user: sqlite3.Row | None = None) -> bytes:
    nav = ""
    if user:
        nav = (f"<div class='user-chip'><span class='user-mark'>{h(user['name'][:1].upper())}</span><span class='user-text'>{h(user['name'])}<small>{'Administração' if user['role'] == 'admin' else 'Acesso da loja'}</small></span>"
               f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{h(user['csrf_token'])}'><button class='btn btn-ghost btn-sm' type='submit'>{icon('logout')}<span>Sair</span></button></form></div>")
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
        cookie = f"login_nonce={nonce}; Path=/; HttpOnly; SameSite=Strict; Max-Age={LOGIN_FORM_SECONDS}" + ("; Secure" if os.environ.get("COOKIE_SECURE") == "1" else "")
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
        else:
            self.send_page(HTTPStatus.NOT_FOUND, "Página não encontrada", "<main class='content'><h1>Página não encontrada</h1></main>", user)

    def dashboard_workspace(self, user: sqlite3.Row) -> None:
        """Tela principal: barra lateral (loja e tipo de SPED) e área de trabalho da loja ou da rede."""
        query = parse_qs(urlparse(self.path).query)
        document_type = query.get("type", ["fiscal"])[0]
        if document_type not in DOCUMENT_TYPES:
            document_type = "fiscal"
        requested_store = query.get("store_id", [""])[0]
        admin = user["role"] == "admin"

        with connect() as db:
            if admin:
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

        main = ""
        message = self.query_message()
        if message:
            main += f"<div class='notice' role='status'>{h(message)}</div>"
        if selected_store:
            main += self.page_heading(f"{label} · {subject}", selected_store["name"], f"CNPJ {format_cnpj(selected_store['cnpj'])}")
        else:
            main += self.page_heading(f"{label} · {subject}", "Central fiscal", "Publique arquivos, cadastre lojas e acompanhe a rede.")
        if admin:
            main += self.admin_tools(user, stores, selected_store, document_type, competence)
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

        content = "<div class='workspace'>" + self.sidebar(user, stores, selected_store, document_type) + "<main class='workspace-main'>" + main + "</main></div>"
        self.send_page(HTTPStatus.OK, selected_store["name"] if selected_store else "Central fiscal", content, user)

    def page_heading(self, kicker: str, title: str, text: str) -> str:
        return f"<div class='page-heading'><div><span class='section-kicker'>{h(kicker.upper())}</span><h1>{h(title)}</h1><p>{h(text)}</p></div></div>"

    def sidebar(self, user: sqlite3.Row, stores: list[sqlite3.Row], selected_store: sqlite3.Row | None, document_type: str) -> str:
        """Barra lateral: loja aberta, troca de loja (lista para o grupo, seletor para a administração) e tipo de SPED."""
        admin = user["role"] == "admin"
        if selected_store:
            identity = f"<strong>{h(selected_store['name'])}</strong><small>CNPJ {h(format_cnpj(selected_store['cnpj']))}</small>"
        else:
            identity = "<strong>Todas as lojas</strong><small>Visão da administração</small>"
        picker = ""
        if admin:
            options = "<option value=''>Todas as lojas</option>" + "".join(
                f"<option value='{store['id']}'{' selected' if selected_store and store['id'] == selected_store['id'] else ''}>{h(store['name'])}</option>" for store in stores)
            picker = ("<form class='store-picker' method='get' action='/dashboard'><input type='hidden' name='type' value='" + h(document_type) + "'>"
                      "<label for='store_id'>Escolher loja</label><select id='store_id' name='store_id'>" + options + "</select>"
                      f"<button class='btn btn-outline btn-sm btn-block' type='submit'><span>Abrir loja</span>{icon('arrow')}</button></form>")
        card = (f"<div class='side-store{' with-picker' if picker else ''}'><div class='side-store-heading'><span class='side-store-mark'>{icon('store')}</span><span>{'LOJA ABERTA' if selected_store else 'REDE'}</span></div>"
                f"<div class='side-store-identity'>{identity}</div>{picker}</div>")
        branches = ""
        if not admin and len(stores) > 1:
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
            f"<a class='sped-tab{current if key == document_type else ''}' href='{dashboard_link(store_id, key)}'>"
            f"<span class='tab-mark'>{mark}</span><span class='tab-text'>{label}<small>{subject}</small></span></a>"
            for key, (label, subject, mark) in DOCUMENT_TYPES.items())
        navigation = f"<nav class='sped-navigation' aria-label='Tipo de arquivo'><span class='side-section-label'>ARQUIVOS</span>{tabs}</nav>"
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

    def admin_tools(self, user: sqlite3.Row, stores: list[sqlite3.Row], selected_store: sqlite3.Row | None, document_type: str, competence: str) -> str:
        """Formulários da administração: publicar SPED e cadastrar loja. Recolhidos quando uma loja está aberta."""
        label = DOCUMENT_TYPES[document_type][0]
        csrf = h(user["csrf_token"])
        options = "".join(f"<option value='{store['id']}'{' selected' if selected_store and store['id'] == selected_store['id'] else ''}>{h(store['name'])} · {h(format_cnpj(store['cnpj']))}</option>" for store in stores)
        month_value = f" value='{h(competence)}'" if competence else ""
        upload = (f"<div class='panel'><div class='panel-title'><span class='panel-icon upload-icon'>{icon('upload')}</span><div><span class='section-kicker'>PUBLICAR · {h(label.upper())}</span><h2>Enviar arquivo SPED</h2></div></div>"
                  "<p class='panel-copy'>O portal confere o CNPJ e o mês no próprio arquivo antes de publicar.</p>"
                  f"<form method='post' enctype='multipart/form-data' action='/upload' class='stack-form'><input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='document_type' value='{h(document_type)}'>"
                  f"<label>Loja<select name='store_id' required><option value=''>Selecione a loja</option>{options}</select></label>"
                  f"<label>Mês de referência<input name='competence' type='month'{month_value} required></label>"
                  "<label>Arquivo .txt<input class='file-input' name='file' type='file' accept='.txt,text/plain' required><small class='field-help'>Até 40 MB. O CNPJ e o mês precisam ser os da loja escolhida.</small></label>"
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
                self.send_login_page(HTTPStatus.TOO_MANY_REQUESTS, "Muitas tentativas seguidas. Aguarde 15 minutos e tente de novo.")
                return
            credential, password = data.get("cnpj", ""), data.get("password", "")
            credential = credential.strip() if isinstance(credential, str) else ""
            cnpj = normalize_cnpj(credential)
            password = password if isinstance(password, str) else ""
            account = db.execute("SELECT * FROM users WHERE active=1 AND ((role='store' AND cnpj=?) OR (role='admin' AND login_name=?))", (cnpj, credential.lower())).fetchone()
            if not account or not verify_password(password, account["password_hash"]):
                attempts = attempt["attempts"] + 1 if attempt and attempt["window_start"] > now - 900 else 1
                db.execute("INSERT INTO login_attempts VALUES (?, ?, ?) ON CONFLICT(ip) DO UPDATE SET attempts=excluded.attempts, window_start=excluded.window_start", (ip, attempts, attempt["window_start"] if attempt and attempt["window_start"] > now - 900 else now))
                self.send_login_page(HTTPStatus.UNAUTHORIZED, "CNPJ ou senha incorretos. Confira e tente novamente.", credential)
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
                self.redirect(f"/dashboard?store_id={store['id']}&type={document_type}&competence={competence}&status=exists")
                return
        with connect() as db:
            document = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        document_summary(document)
        self.redirect(f"/dashboard?store_id={store['id']}&type={document_type}&competence={competence}&status=uploaded")

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
    if len(sys.argv) > 1 and sys.argv[1] == "redefinir-senha":
        cnpj = sys.argv[2] if len(sys.argv) > 2 else input("CNPJ da loja: ")
        try:
            name, password = reset_store_password(cnpj)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Loja: {name}")
        print(f"CNPJ: {format_cnpj(cnpj)}")
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
    print(f"Portal Fiscal ativo em http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando portal...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()