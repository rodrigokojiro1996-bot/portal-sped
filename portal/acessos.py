"""Registro de quem usa a entrada por certificado: entradas, recusas e downloads.

Responde "quem acessou os arquivos desta loja?". Regras do registro:
    - a entrada grava junto com a sessão, e o download registra antes de enviar: não existe acesso sem rastro;
    - identifica o certificado pelo CNPJ e pelo número de série, sem guardar o CPF do responsável, que também vem nele;
    - visita sem certificado não é registrada: não identifica ninguém, e qualquer robô encheria a tabela.

Downloads de quem entrou com senha ficam de fora: só as sessões abertas por certificado têm entrada neste registro.
Por isso, quem tem sessão de senha e abre o link do aviso ganha uma sessão nova, pelo certificado.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

import resumo_sped
from portal.auditoria import AUDIT_PERIODS, browser_label
from portal.certificado import Certificate, stores_of
from portal.db import connect
from portal.sped import DOCUMENT_TYPES

EVENTS = {"entrou": "Entrou", "recusado": "Recusado", "baixou": "Baixou arquivo"}
DEFAULT_PERIOD = "30d"
REPEAT_SECONDS = 60


def _insert(db: sqlite3.Connection, event: str, cnpj: str | None, serial: str | None, store: sqlite3.Row | None, ip: str, agent: str,
            session_hash: str | None = None, document: sqlite3.Row | None = None, reason: str | None = None) -> None:
    db.execute("INSERT INTO certificate_access (created_at, event, certificate_cnpj, certificate_serial, store_id, store_name, store_cnpj, document_type, competence, file_name, reason, session_hash, ip, browser) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (datetime.now().astimezone().isoformat(timespec="seconds"), event, cnpj, serial, store["id"] if store else None, store["name"] if store else None, store["cnpj"] if store else None,
                document["document_type"] if document else None, document["competence"] if document else None, document["original_name"] if document else None,
                reason, session_hash, ip, browser_label(agent)))


def record_entry(db: sqlite3.Connection, certificate: Certificate, store: sqlite3.Row, session_hash: str, ip: str, agent: str) -> None:
    """Registra a entrada dentro da transação que abre a sessão: as duas coisas acontecem juntas ou nenhuma acontece."""
    _insert(db, "entrou", certificate.cnpj, certificate.serial, store, ip, agent, session_hash)


def record_refusal(certificate: Certificate | None, serial: str | None, reason: str, requested: str, ip: str, agent: str) -> None:
    """Registra uma recusa, na loja do link ou, sem link, na loja do certificado.

    A mesma recusa repetida em pouco tempo conta uma vez: recarregar a página não pode encher o registro.
    Uma falha aqui não impede a explicação ao usuário: a recusa não dá acesso a nada.
    """
    cnpj, serial = (certificate.cnpj, certificate.serial) if certificate else (None, serial)
    since = (datetime.now().astimezone() - timedelta(seconds=REPEAT_SECONDS)).isoformat(timespec="seconds")
    try:
        with connect() as db:
            store = db.execute("SELECT id, name, cnpj FROM users WHERE role='store' AND id=?", (requested,)).fetchone() if requested else None
            store = store or next(iter(stores_of(cnpj)), None) if cnpj else store
            repeated = db.execute("SELECT 1 FROM certificate_access WHERE event='recusado' AND certificate_serial IS ? AND reason=? AND ip=? AND created_at>=?", (serial, reason, ip, since)).fetchone()
            if not repeated:
                _insert(db, "recusado", cnpj, serial, store, ip, agent, reason=reason)
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
        if entry:
            store = db.execute("SELECT id, name, cnpj FROM users WHERE id=?", (document["store_id"],)).fetchone()
            _insert(db, "baixou", entry["certificate_cnpj"], entry["certificate_serial"], store, ip, agent, session_hash, document)


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
