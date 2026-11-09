"""Registro de quem usa a entrada por certificado: entradas, recusas e downloads.

Responde "quem acessou os arquivos desta loja?". Regras do registro:
    - a entrada grava junto com a sessão, e o download registra antes de enviar: não existe acesso sem rastro;
    - identifica o certificado pelo CNPJ e pelo número de série, sem guardar o CPF do responsável, que também vem nele;
    - visita sem certificado não é registrada: não identifica ninguém, e qualquer robô encheria a tabela.

Downloads de quem entrou com senha ficam de fora: só as sessões abertas por certificado têm entrada neste registro.
Por isso, quem tem sessão de senha e abre o link do aviso ganha uma sessão nova, pelo certificado.

O nome reverso do IP (veja hostname_of) é consultado antes de abrir a transação: uma resposta lenta do DNS não pode
deixar o banco preso.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import resumo_sped
from portal.auditoria import AUDIT_PERIODS, browser_label
from portal.certificado import Certificate, stores_of
from portal.db import connect
from portal.sped import DOCUMENT_TYPES

EVENTS = {"entrou": "Entrou", "recusado": "Recusado", "baixou": "Baixou arquivo"}
DEFAULT_PERIOD = "30d"
REPEAT_SECONDS = 60
LOOKUP_SECONDS = 1.5
NAME_TTL, FAILURE_TTL = 3600, 300  # segundos: um IP muda de dono, e uma falha do DNS passa
MAX_PENDING = 8
UNCONFIRMED = " (não confirmado)"
_hostnames: dict[str, tuple[str, float]] = {}
_hostnames_lock = threading.Lock()
_pending = threading.BoundedSemaphore(MAX_PENDING)
_resolver = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nome-reverso")


def _canonical(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """O mesmo endereço escrito de um jeito só: IPv6 em minúsculas e IPv4 mapeado em IPv6 como IPv4."""
    address = ipaddress.ip_address(ip.split("%")[0])
    return getattr(address, "ipv4_mapped", None) or address


def _lookup(ip: str) -> str:
    """Nome reverso confirmado de volta: quem controla o IP escolhe o nome reverso, então ele só vale se apontar para o mesmo IP."""
    name = socket.gethostbyaddr(ip)[0].lower()
    try:
        addresses = {_canonical(str(info[4][0])) for info in socket.getaddrinfo(name, None)}
    except (OSError, ValueError):
        addresses = set()  # nome que não resolve de volta continua aparecendo, marcado
    return name if _canonical(ip) in addresses else name + UNCONFIRMED


def hostname_of(ip: str) -> str:
    """Nome reverso do IP, ou vazio quando a rede não informa ou demora. Pela internet costuma ser o nome da operadora.

    A conexão local não é consultada: atrás de um proxy sem PORTAL_ATRAS_DE_PROXY, ela daria o nome do próprio servidor.
    O nome fica guardado por IP durante NAME_TTL, e a falta dele durante FAILURE_TTL. Com o DNS congestionado
    (MAX_PENDING consultas em andamento), nem consulta: o acesso não espera na fila.
    """
    try:
        address = _canonical(ip)
    except ValueError:
        return ""
    if address.is_loopback or address.is_unspecified:
        return ""
    now = time.monotonic()
    with _hostnames_lock:
        cached = _hostnames.get(ip)
    if cached and cached[1] > now:
        return cached[0]
    if not _pending.acquire(blocking=False):
        return ""
    future = _resolver.submit(_lookup, ip)
    future.add_done_callback(lambda _: _pending.release())
    try:
        name = future.result(timeout=LOOKUP_SECONDS)
    except Exception:  # noqa: BLE001 - sem nome reverso, o acesso segue normalmente
        future.cancel()
        name = ""
    with _hostnames_lock:
        if len(_hostnames) > 4096:
            _hostnames.clear()
        _hostnames[ip] = (name, now + (NAME_TTL if name else FAILURE_TTL))
    return name


def _insert(db: sqlite3.Connection, event: str, cnpj: str | None, serial: str | None, store: sqlite3.Row | None, ip: str, hostname: str, agent: str,
            session_hash: str | None = None, document: sqlite3.Row | None = None, reason: str | None = None) -> None:
    db.execute("INSERT INTO certificate_access (created_at, event, certificate_cnpj, certificate_serial, store_id, store_name, store_cnpj, document_type, competence, file_name, reason, session_hash, ip, hostname, browser) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (datetime.now().astimezone().isoformat(timespec="seconds"), event, cnpj, serial, store["id"] if store else None, store["name"] if store else None, store["cnpj"] if store else None,
                document["document_type"] if document else None, document["competence"] if document else None, document["original_name"] if document else None,
                reason, session_hash, ip, hostname, browser_label(agent)))


def record_entry(db: sqlite3.Connection, certificate: Certificate, store: sqlite3.Row, session_hash: str, ip: str, hostname: str, agent: str) -> None:
    """Registra a entrada dentro da transação que abre a sessão: as duas coisas acontecem juntas ou nenhuma acontece.

    O nome reverso vem pronto (hostname_of), consultado antes de a transação começar.
    """
    _insert(db, "entrou", certificate.cnpj, certificate.serial, store, ip, hostname, agent, session_hash)


def record_refusal(certificate: Certificate | None, serial: str | None, reason: str, requested: str, ip: str, agent: str) -> None:
    """Registra uma recusa, na loja do link ou, sem link, na loja do certificado.

    A mesma recusa repetida em pouco tempo conta uma vez: recarregar a página não pode encher o registro.
    Uma falha aqui não impede a explicação ao usuário: a recusa não dá acesso a nada.
    """
    cnpj, serial = (certificate.cnpj, certificate.serial) if certificate else (None, serial)
    since = (datetime.now().astimezone() - timedelta(seconds=REPEAT_SECONDS)).isoformat(timespec="seconds")
    try:
        with connect() as db:
            repeated = db.execute("SELECT 1 FROM certificate_access WHERE event='recusado' AND certificate_serial IS ? AND reason=? AND ip=? AND created_at>=?", (serial, reason, ip, since)).fetchone()
            store = db.execute("SELECT id, name, cnpj FROM users WHERE role='store' AND id=?", (requested,)).fetchone() if requested else None
        if repeated:
            return
        store = store or next(iter(stores_of(cnpj)), None) if cnpj else store
        hostname = hostname_of(ip)  # só depois de saber que vai gravar, e fora da transação
        with connect() as db:
            _insert(db, "recusado", cnpj, serial, store, ip, hostname, agent, reason=reason)
    except sqlite3.Error as error:
        print(f"[acessos] recusa não registrada: {type(error).__name__}", file=sys.stderr)


def certificate_session(session_hash: str) -> bool:
    """A sessão foi aberta por certificado? As de senha não têm entrada no registro."""
    with connect() as db:
        return db.execute("SELECT 1 FROM certificate_access WHERE session_hash=? AND event='entrou'", (session_hash,)).fetchone() is not None


def record_download(session_hash: str, document: sqlite3.Row, ip: str, agent: str) -> None:
    """Registra o download de quem entrou por certificado, atribuído ao certificado que abriu a sessão."""
    with connect() as db:
        entry = db.execute("SELECT certificate_cnpj, certificate_serial FROM certificate_access WHERE session_hash=? AND event='entrou'", (session_hash,)).fetchone()
        store = db.execute("SELECT id, name, cnpj FROM users WHERE id=?", (document["store_id"],)).fetchone() if entry else None
    if not entry:
        return
    hostname = hostname_of(ip)  # fora da transação
    with connect() as db:
        _insert(db, "baixou", entry["certificate_cnpj"], entry["certificate_serial"], store, ip, hostname, agent, session_hash, document)


def entries(store_id: str = "", period: str = DEFAULT_PERIOD, limit: int | None = None) -> list[sqlite3.Row]:
    clauses, params = [], []
    if re.fullmatch(r"[0-9]{1,18}", store_id):
        clauses.append("store_id=?")
        params.append(int(store_id))
    days = AUDIT_PERIODS.get(period, AUDIT_PERIODS[DEFAULT_PERIOD])[1]
    if days is not None:
        clauses.append("created_at >= ?")
        params.append((date.today() - timedelta(days=days)).isoformat())
    sql = "SELECT * FROM certificate_access" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as db:
        return db.execute(sql, params).fetchall()


def detail(entry: sqlite3.Row) -> str:
    """O que aconteceu, em linguagem simples: o motivo da recusa ou o arquivo baixado."""
    if entry["event"] == "recusado":
        return entry["reason"] or ""
    if entry["event"] == "baixou":
        return f"{entry['file_name']} · {DOCUMENT_TYPES[entry['document_type']][0]} · {resumo_sped.month_label(entry['competence']).capitalize()}"
    return "Sessão aberta pelo certificado"
