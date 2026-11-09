"""Fila de saída dos avisos de publicação por e-mail.

A regra que orienta o módulo: e-mail nunca derruba uma publicação.
    - A linha da fila nasce na mesma transação do documento. Se o processo cair logo depois, ela continua pendente.
    - O processamento roda depois da resposta ao usuário, numa thread separada, e nunca lança exceção.
    - O que ficou para trás é retomado ao iniciar o servidor ou pelo botão da tela /emails.

MODO DE TESTE: esta versão não envia nada. O módulo não tem nenhuma chamada de rede. Cada aviso é montado e registrado
com a situação 'ensaio', que é final: um aviso de teste nunca vira e-mail de verdade quando o envio real existir.

Loja sem contato cadastrado não entra na fila: a maioria das lojas não recebe aviso, e isso não é erro.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import resumo_sped
from portal import config
from portal.contatos import unique_emails
from portal.db import connect
from portal.sped import DOCUMENT_TYPES, format_cnpj, period_label, sped_label

MAX_ATTEMPTS = 5
MANUAL_LIMIT = 25
SWEEP_LIMIT = 20
SWEEP_MAX_HOURS = 48
DRY_RUN_NOTE = "Modo de teste: nada foi enviado."
WORKER_NAME = "fila-de-emails"
STATUS_LABELS = {"pendente": "Pendente", "ensaio": "Teste", "enviado": "Enviado", "erro": "Erro"}

# Uma rodada de cada vez: a publicação, a varredura e o botão podem pedir processamento ao mesmo tempo.
_processing = threading.Lock()


class EmailError(ValueError):
    """Motivo escrito pelo próprio portal, seguro para gravar e mostrar na tela."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def describe_error(error: Exception) -> str:
    """Motivo gravado na fila. Erros de fora ficam só com a classe: a mensagem pode repetir endereços e dados da loja."""
    return str(error) if isinstance(error, EmailError) else f"falha inesperada ({type(error).__name__})"


def enqueue(db: sqlite3.Connection, document_id: str, cnpj: str) -> None:
    """Registra que um aviso é devido, dentro da transação que grava o documento."""
    if config.EMAIL_ENABLED and db.execute("SELECT 1 FROM store_contacts WHERE cnpj=?", (cnpj,)).fetchone():
        db.execute("INSERT INTO email_notifications (document_id, created_at) VALUES (?, ?)", (document_id, utc_now()))


def targets(recipients: str, copies: str) -> tuple[str, str, str]:
    """Destinatários, cópias e prefixo do assunto. Com o redirecionamento, ninguém da lista real é alcançado."""
    if not config.EMAIL_REDIRECT:
        return recipients, copies, ""
    redirect = unique_emails(config.EMAIL_REDIRECT)
    if not redirect:
        raise EmailError("PORTAL_EMAIL_REDIRECIONAR está preenchido, mas não tem nenhum endereço válido.")
    return ", ".join(redirect), "", f"[TESTE para {recipients}] "


def build_message(document: sqlite3.Row) -> tuple[str, str]:
    """Assunto e texto do aviso de um arquivo publicado."""
    month = resumo_sped.month_label(document["competence"]).capitalize()
    period = period_label(document["period_start"], document["period_end"])
    subject = f"{DOCUMENT_TYPES[document['document_type']][0]} publicado: {document['store_name']} - {month}"
    lines = ["Olá,", "", f"O arquivo {sped_label(document['document_type'])} da loja {document['store_name']} (CNPJ {format_cnpj(document['store_cnpj'])}) foi publicado no Portal Fiscal.",
             "", f"Competência: {month}" + (f" (período parcial de {period})" if period else ""), f"Arquivo: {document['original_name']}"]
    if config.PUBLIC_URL:
        page = "certificado" if config.CERT_PROXY_SECRET else "dashboard"
        intro = "Para consultar, abra este link no computador que tem o certificado digital da loja:" if config.CERT_PROXY_SECRET else "Para consultar:"
        lines += ["", intro, f"{config.PUBLIC_URL}/{page}?store_id={document['store_id']}&type={document['document_type']}&competence={document['competence']}"]
    return subject, "\n".join(lines + ["", "Mensagem automática do Portal Fiscal das Farmácias Associadas."])


def process(notification_id: int) -> str:
    """Monta e registra um aviso e devolve a situação final. Nunca lança: toda falha vira registro na fila."""
    recipients = copies = subject = body = None
    try:
        with connect() as db:
            document = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj FROM email_notifications JOIN documents ON documents.id=email_notifications.document_id "
                                  "JOIN users ON users.id=documents.store_id WHERE email_notifications.id=?", (notification_id,)).fetchone()
            contact = db.execute("SELECT recipients, copies FROM store_contacts WHERE cnpj=?", (document["store_cnpj"],)).fetchone() if document else None
        if contact is None:
            raise EmailError("A loja não tem mais contato cadastrado.")
        recipients, copies, prefix = targets(contact["recipients"], contact["copies"])
        subject, body = build_message(document)
        subject = prefix + subject
        status, reason = "ensaio", DRY_RUN_NOTE
    except Exception as error:  # noqa: BLE001 - nenhuma falha de e-mail pode chegar a quem publicou
        status, reason = "erro", describe_error(error)
    try:
        with connect() as db:
            db.execute("UPDATE email_notifications SET status=?, recipients=?, copies=?, subject=?, body=?, error=?, attempts=attempts+1, processed_at=? WHERE id=?",
                       (status, recipients, copies, subject, body, reason, utc_now(), notification_id))
    except sqlite3.Error as error:
        print(f"[email] não foi possível registrar o aviso {notification_id}: {type(error).__name__}", file=sys.stderr)
        return "erro"
    return status


def process_pending(limit: int = MANUAL_LIMIT, max_age_hours: int | None = None) -> Counter:
    """Processa o que está pendente ou deu erro. Linhas no teto de tentativas ficam na tela, para alguém resolver."""
    if not config.EMAIL_ENABLED:
        return Counter()
    since = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat(timespec="seconds") if max_age_hours is not None else ""
    with _processing:
        with connect() as db:
            rows = db.execute("SELECT id FROM email_notifications WHERE status IN ('pendente', 'erro') AND attempts<? AND created_at>=? ORDER BY created_at, id LIMIT ?",
                              (MAX_ATTEMPTS, since, limit)).fetchall()
        return Counter(process(row["id"]) for row in rows)


def _run_quietly(limit: int = MANUAL_LIMIT, max_age_hours: int | None = None) -> Counter:
    try:
        return process_pending(limit, max_age_hours)
    except Exception as error:  # noqa: BLE001 - a thread da fila não pode derrubar nada, e a mensagem pode trazer dados
        print(f"[email] fila indisponível: {type(error).__name__}", file=sys.stderr)
        return Counter()


def process_in_background() -> None:
    """Processa a fila sem atrasar a resposta de quem publicou."""
    if config.EMAIL_ENABLED:
        threading.Thread(target=_run_quietly, name=WORKER_NAME, daemon=True).start()


def sweep_on_start() -> None:
    """Retoma, ao iniciar o servidor, o que ficou pela metade. Limitada para não virar uma rajada: o resto espera o botão."""
    def run() -> None:
        results = _run_quietly(SWEEP_LIMIT, SWEEP_MAX_HOURS)
        if results:
            print(f"[email] varredura ao iniciar: {sum(results.values())} aviso(s) processado(s), {results['erro']} com erro.")

    if config.EMAIL_ENABLED:
        threading.Thread(target=run, name=WORKER_NAME, daemon=True).start()


def queue_entries(limit: int = 200) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute("SELECT email_notifications.*, documents.document_type, documents.competence, documents.original_name, users.name AS store_name, users.cnpj AS store_cnpj "
                          "FROM email_notifications JOIN documents ON documents.id=email_notifications.document_id JOIN users ON users.id=documents.store_id "
                          "ORDER BY email_notifications.id DESC LIMIT ?", (limit,)).fetchall()


def contact_total() -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM store_contacts").fetchone()[0]
