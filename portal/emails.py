"""Fila de saída dos avisos de publicação por e-mail.

A regra que orienta o módulo: e-mail nunca derruba uma publicação.
    - A linha da fila nasce na mesma transação do documento. Se o processo cair logo depois, ela continua pendente.
    - O processamento roda depois da resposta ao usuário, numa thread separada, e nunca lança exceção.
    - O que ficou para trás é retomado ao iniciar o servidor ou pelo botão da tela /emails.

MODO DE TESTE (o padrão): nada sai do portal. Cada aviso é montado e registrado com a situação 'ensaio', que é final:
um aviso de teste nunca vira e-mail de verdade depois.

ENVIO REAL (PORTAL_EMAIL_ENVIO_REAL=1): o aviso sai pelo Resend (portal/envio.py) e fica 'enviado', com o id da
mensagem. A chave anti-duplicação é estável por aviso, então tentar de novo depois de uma resposta perdida não duplica.

Loja sem contato cadastrado não entra na fila: a maioria das lojas não recebe aviso, e isso não é erro.
"""
from __future__ import annotations

import html
import sqlite3
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import resumo_sped
from portal import config, envio
from portal.contatos import unique_emails
from portal.db import connect
from portal.sped import DOCUMENT_TYPES, format_cnpj, period_label, sped_label

MAX_ATTEMPTS = 5
MANUAL_LIMIT = 25
SWEEP_LIMIT = 20
SWEEP_MAX_HOURS = 48
# O Resend guarda a chave anti-duplicação por 24 h, contadas do primeiro uso. Depois disso, reenviar um aviso que ficou
# sem resposta poderia mandar o e-mail duas vezes: ele passa a esperar alguém conferir no painel do Resend.
UNANSWERED_LIMIT_HOURS = 23
# Aviso mais antigo que isto não sai sozinho no envio real: ligar o envio não pode despejar nas lojas avisos de semanas atrás.
STALE_DAYS = 7
DRY_RUN_NOTE = "Modo de teste: nada foi enviado."
UNANSWERED_NOTE = ("Pode já ter saído: o Resend não respondeu e já passou o prazo da chave anti-duplicação. "
                   "Confira no painel do Resend antes de reenviar.")
CONFLICT_NOTE = ("Pode já ter saído: uma tentativa anterior ficou sem resposta e o conteúdo do aviso mudou depois. "
                 "Confira no painel do Resend antes de reenviar.")
STALE_NOTE = f"Aviso com mais de {STALE_DAYS} dias: não sai sozinho. Confira se ainda faz sentido e reenvie pela tela."
NO_ADDRESS = "O contato da loja não tem nenhum endereço de e-mail válido."
STOP_NOTES = (UNANSWERED_NOTE, CONFLICT_NOTE, STALE_NOTE)
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


def consult_link(document: sqlite3.Row) -> tuple[str, str] | None:
    """Frase e endereço do link do aviso. Sem endereço público, o aviso sai sem link."""
    if not config.PUBLIC_URL:
        return None
    page = "certificado" if config.CERT_PROXY_SECRET else "dashboard"
    intro = "Para consultar, abra este link no computador que tem o certificado digital da loja:" if config.CERT_PROXY_SECRET else "Para consultar:"
    return intro, f"{config.PUBLIC_URL}/{page}?store_id={document['store_id']}&type={document['document_type']}&competence={document['competence']}"


def message_facts(document: sqlite3.Row) -> tuple[str, str, list[tuple[str, str]]]:
    """Assunto, frase principal e detalhes do aviso, comuns ao texto e ao HTML."""
    month = resumo_sped.month_label(document["competence"]).capitalize()
    period = period_label(document["period_start"], document["period_end"])
    subject = f"{DOCUMENT_TYPES[document['document_type']][0]} publicado: {document['store_name']} - {month}"
    lead = f"O arquivo {sped_label(document['document_type'])} da loja {document['store_name']} (CNPJ {format_cnpj(document['store_cnpj'])}) foi publicado no Portal Fiscal."
    facts = [("Competência", month + (f" (período parcial de {period})" if period else "")), ("Arquivo", document["original_name"])]
    return subject, lead, facts


def build_message(document: sqlite3.Row) -> tuple[str, str]:
    """Assunto e texto do aviso de um arquivo publicado."""
    subject, lead, facts = message_facts(document)
    lines = ["Olá,", "", lead, ""] + [f"{label}: {value}" for label, value in facts]
    link = consult_link(document)
    if link:
        lines += ["", *link]
    return subject, "\n".join(lines + ["", "Mensagem automática do Portal Fiscal das Farmácias Associadas."])


def build_html(document: sqlite3.Row) -> str:
    """O mesmo aviso em HTML de e-mail: tabelas e estilo na própria tag, que é o que os programas de e-mail respeitam.

    Todo bloco com texto declara a cor do fundo e a do texto, para o modo escuro não deixar texto escuro sobre fundo escuro.
    Nada muda de um envio para outro (sem hora de envio): o mesmo aviso gera o mesmo HTML, como a chave anti-duplicação exige.
    """
    subject, lead, facts = message_facts(document)
    e = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    font = "font-family:Arial,Helvetica,sans-serif"
    rows = "".join(f"<tr><td style='padding:6px 0;{font};font-size:13px;color:#5b6f6c;background:#ffffff;width:110px;vertical-align:top'>{e(label)}</td>"
                   f"<td style='padding:6px 0;{font};font-size:14px;color:#173a38;background:#ffffff'>{e(value)}</td></tr>" for label, value in facts)
    link = consult_link(document)
    button = ""
    if link:
        intro, url = link
        button = (f"<p style='margin:22px 0 10px;{font};font-size:14px;color:#173a38;background:#ffffff'>{e(intro)}</p>"
                  f"<table role='presentation' cellpadding='0' cellspacing='0' border='0'><tr><td style='background:#087e70;border-radius:6px'>"
                  f"<a href='{e(url)}' style='display:inline-block;padding:12px 22px;{font};font-size:14px;font-weight:bold;color:#ffffff;text-decoration:none'>Abrir no Portal Fiscal</a>"
                  f"</td></tr></table><p style='margin:10px 0 0;{font};font-size:12px;color:#5b6f6c;background:#ffffff;word-break:break-all'>{e(url)}</p>")
    return ("<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='color-scheme' content='light'><title>{e(subject)}</title></head>"
            "<body style='margin:0;padding:0;background:#eef4f3'>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' style='background:#eef4f3'><tr><td align='center' style='padding:24px 12px'>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' style='max-width:560px;background:#ffffff;border:1px solid #d6e3e0;border-radius:8px'>"
            f"<tr><td style='padding:16px 24px;background:#087e70;color:#ffffff;{font};font-size:12px;font-weight:bold;letter-spacing:1px;border-radius:8px 8px 0 0'>PORTAL FISCAL · FARMÁCIAS ASSOCIADAS</td></tr>"
            f"<tr><td style='padding:24px;background:#ffffff;color:#173a38'>"
            f"<h1 style='margin:0 0 12px;{font};font-size:20px;line-height:1.3;color:#173a38;background:#ffffff'>{e(subject)}</h1>"
            f"<p style='margin:0 0 16px;{font};font-size:15px;line-height:1.55;color:#173a38;background:#ffffff'>{e(lead)}</p>"
            f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' style='border-top:1px solid #e3ecea;background:#ffffff'>{rows}</table>{button}</td></tr>"
            f"<tr><td style='padding:16px 24px;background:#f6f9f8;color:#5b6f6c;{font};font-size:12px;line-height:1.5;border-top:1px solid #e3ecea;border-radius:0 0 8px 8px'>"
            "Mensagem automática do Portal Fiscal das Farmácias Associadas.</td></tr>"
            "</table></td></tr></table></body></html>")


def process(notification_id: int) -> str:
    """Monta o aviso, envia de verdade se o envio real estiver ligado e devolve a situação final.

    Nunca lança: toda falha vira registro na fila.
    """
    recipients = copies = subject = body = page = provider_id = key = key_sent_at = None
    uncertain, stop, loaded = 0, False, False
    try:
        with connect() as db:
            document = db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj, email_notifications.created_at AS notice_created, "
                                  "email_notifications.idempotency_key AS notice_key, email_notifications.key_sent_at AS notice_key_sent_at, "
                                  "email_notifications.uncertain AS notice_uncertain, email_notifications.confirmed AS notice_confirmed FROM email_notifications "
                                  "JOIN documents ON documents.id=email_notifications.document_id JOIN users ON users.id=documents.store_id WHERE email_notifications.id=?",
                                  (notification_id,)).fetchone()
            contact = db.execute("SELECT recipients, copies FROM store_contacts WHERE cnpj=?", (document["store_cnpj"],)).fetchone() if document else None
        if document:
            # Lido antes de qualquer outra falha possível: é o que volta para o banco se nada for enviado.
            key, key_sent_at, uncertain, loaded = document["notice_key"], document["notice_key_sent_at"], document["notice_uncertain"], True
        if contact is None:
            raise EmailError("A loja não tem mais contato cadastrado.")
        recipients, copies, prefix = targets(contact["recipients"], contact["copies"])
        subject, body = build_message(document)
        subject, page = prefix + subject, build_html(document)
        if not config.EMAIL_SEND:
            status, reason = "ensaio", DRY_RUN_NOTE
        else:
            now = datetime.now(timezone.utc)
            to = unique_emails(recipients)
            if not to:
                raise EmailError(NO_ADDRESS)
            # A ordem importa: um aviso que pode já ter saído nunca recebe o aviso de "antigo", que convida a reenviar.
            if uncertain and key_sent_at and now - datetime.fromisoformat(key_sent_at) > timedelta(hours=UNANSWERED_LIMIT_HOURS):
                stop = True
                raise EmailError(UNANSWERED_NOTE)
            if not key_sent_at and now - datetime.fromisoformat(document["notice_created"]) > timedelta(days=STALE_DAYS) and not document["notice_confirmed"]:
                stop = True
                raise EmailError(STALE_NOTE)
            message = {"from": config.EMAIL_FROM, "to": to, "subject": subject, "html": page, "text": body, "tags": [{"name": "aviso", "value": str(notification_id)}]}
            if copies:
                message["cc"] = unique_emails(copies)
            key = key or f"portal-aviso-{notification_id}"
            result = envio.send(message, key)
            if result.conflict:
                # A chave já foi usada com outro conteúdo (ex.: o redirecionamento mudou entre as tentativas). Se alguma
                # tentativa ficou sem confirmação, o e-mail antigo pode ter saído: trocar de chave mandaria de novo.
                if uncertain:
                    stop = True
                    raise EmailError(CONFLICT_NOTE)
                key, key_sent_at = f"portal-aviso-{notification_id}-r{int(now.timestamp())}", None
                result = envio.send(message, key)
            if result.reached and not key_sent_at:
                key_sent_at = utc_now()  # o prazo de 24 h da chave conta do primeiro uso que chegou ao Resend
            if result.uncertain:
                uncertain = 1
            if result.error:
                raise EmailError(result.error)
            status, reason, provider_id = "enviado", None, result.provider_id
    except Exception as error:  # noqa: BLE001 - nenhuma falha de e-mail pode chegar a quem publicou
        status, reason = "erro", describe_error(error)
    try:
        with connect() as db:
            # Chave, primeiro uso e "pode ter saído" nunca se perdem: uma falha antes do envio (ex.: contato removido) não pode
            # zerar a proteção contra duplicar. Só a confirmação da administração (confirm_resend) volta esses campos.
            db.execute("UPDATE email_notifications SET status=?, recipients=?, copies=?, subject=?, body=?, html=COALESCE(?, html), provider_id=COALESCE(?, provider_id), error=?, "
                       "idempotency_key=CASE WHEN ? THEN ? ELSE idempotency_key END, key_sent_at=CASE WHEN ? THEN ? ELSE key_sent_at END, "
                       "uncertain=MAX(uncertain, ?), attempts=CASE WHEN ? THEN ? ELSE attempts+1 END, processed_at=? WHERE id=?",
                       (status, recipients, copies, subject, body, page, provider_id, reason, loaded, key, loaded, key_sent_at, uncertain, stop, MAX_ATTEMPTS, utc_now(), notification_id))
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
            # Sem o envio real, um aviso que pode já ter saído fica como está: virar "Teste" diria que nada foi enviado.
            rows = db.execute("SELECT id FROM email_notifications WHERE status IN ('pendente', 'erro') AND attempts<? AND created_at>=? AND NOT (uncertain=1 AND ?) "
                              "ORDER BY created_at, id LIMIT ?", (MAX_ATTEMPTS, since, not config.EMAIL_SEND, limit)).fetchall()
        return Counter(process(row["id"]) for row in rows)


def notice_id(value: str) -> int | None:
    """Número de aviso vindo da tela; None para qualquer outra coisa."""
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 18 else None


def confirm_resend(value: str) -> bool:
    """A administração conferiu um aviso parado e pede para enviar de novo, com uma chave nova.

    Só vale para avisos parados (no teto de tentativas): os outros ainda estão sendo tentados com a chave deles, e
    trocá-la no meio tiraria a proteção contra duplicar. Como a fila nunca pega um aviso parado, não há corrida com ela.
    """
    number = notice_id(value)
    if number is None:
        return False
    with connect() as db:
        changed = db.execute("UPDATE email_notifications SET status='pendente', attempts=0, uncertain=0, confirmed=1, idempotency_key=?, key_sent_at=NULL, "
                             "error='Reenvio confirmado pela administração.' WHERE id=? AND status='erro' AND attempts>=?",
                             (f"portal-aviso-{number}-a{int(datetime.now(timezone.utc).timestamp())}", number, MAX_ATTEMPTS)).rowcount
    return changed == 1


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


def preview_html(notification_id: str) -> str | None:
    """HTML gravado de um aviso, para a prévia da administração."""
    number = notice_id(notification_id)
    if number is None:
        return None
    with connect() as db:
        row = db.execute("SELECT html FROM email_notifications WHERE id=?", (number,)).fetchone()
    return row["html"] if row and row["html"] else None


def contact_total() -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM store_contacts").fetchone()[0]
