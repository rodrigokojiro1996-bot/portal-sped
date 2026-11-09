"""Tela da fila de e-mails (só aparece com config.EMAIL_ENABLED)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import resumo_sped
from portal import config
from portal.emails import MAX_ATTEMPTS, STATUS_LABELS, STOP_NOTES
from portal.sped import DOCUMENT_TYPES, format_cnpj
from portal.views.layout import h, icon, page_heading

STATUS_BADGES = {"pendente": "badge-acessos", "ensaio": "badge-consultas", "enviado": "badge-downloads", "erro": "badge-alertas"}


def sentence(text: str) -> str:
    """O motivo gravado, como frase na tela: os erros do Resend ficam gravados em minúsculas e sem ponto final."""
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def queue_row(entry: sqlite3.Row, csrf: str) -> str:
    moment = datetime.fromisoformat(entry["created_at"]).astimezone()
    month = resumo_sped.month_label(entry["competence"]).capitalize()
    copies = f"<small>Cópia: {h(entry['copies'])}</small>" if entry["copies"] else ""
    message = ""
    if entry["body"]:
        message = (f"<details class='viz-table'><summary>Ver mensagem</summary><p class='email-subject'>{h(entry['subject'])}</p><pre class='email-body'>{h(entry['body'])}</pre></details>")
    if entry["html"]:
        message += f"<a class='email-preview' href='/emails/previa?id={entry['id']}' target='_blank' rel='noopener'>Ver como e-mail</a>"
    reason = sentence(entry["error"] or "") or (f"Resend: {entry['provider_id']}" if entry["provider_id"] else "")
    stopped = entry["status"] == "erro" and entry["attempts"] >= MAX_ATTEMPTS
    if stopped and entry["error"] not in STOP_NOTES:
        reason += f" Parou após {MAX_ATTEMPTS} tentativas."
    action = ""
    if stopped and config.EMAIL_SEND:
        # Se o e-mail pode já ter saído, o botão diz o que conferir antes: reenviar usa uma chave nova e pode duplicar.
        label = "Conferi no Resend: reenviar" if entry["uncertain"] else "Reenviar este aviso"
        if entry["uncertain"] and entry["error"] not in STOP_NOTES:
            reason += " Pode já ter saído: confira no painel do Resend antes de reenviar."
        action = (f"<form method='post' action='/emails/reenviar' class='email-resend'><input type='hidden' name='csrf' value='{h(csrf)}'>"
                  f"<input type='hidden' name='id' value='{entry['id']}'><button class='btn btn-outline btn-sm' type='submit'><span>{label}</span></button></form>")
    return (f"<tr><td class='mono'>{moment:%d/%m/%Y %H:%M}</td><td class='audit-cell'><strong>{h(entry['store_name'])}</strong><small>{h(format_cnpj(entry['store_cnpj']))}</small></td>"
            f"<td class='audit-cell'><strong>{h(DOCUMENT_TYPES[entry['document_type']][0])}</strong><small>{h(month)}</small></td>"
            f"<td class='audit-cell'>{h(entry['recipients'] or '—')}{copies}{message}</td>"
            f"<td class='audit-cell'><span class='audit-badge {STATUS_BADGES[entry['status']]}'>{h(STATUS_LABELS[entry['status']])}</span><small>{h(reason)}</small>{action}</td></tr>")


def emails_main(entries: list[sqlite3.Row], contacts: int, csrf: str, processed: str) -> str:
    """Conteúdo da tela: aviso do modo de teste, reprocessamento e lista da fila."""
    if config.EMAIL_SEND:
        notice = (f"<div class='notice notice-info' role='note'>{icon('mail')}<span><strong>Envio real ligado:</strong> os avisos saem pelo Resend, "
                  f"com o remetente {h(config.EMAIL_FROM)}.</span></div>")
    else:
        notice = (f"<div class='notice notice-info' role='note'>{icon('mail')}<span><strong>Modo de teste:</strong> cada publicação gera um aviso nesta lista, "
                  "mas nenhum e-mail sai do portal.</span></div>")
    if config.EMAIL_REDIRECT:
        notice += f"<div class='notice notice-info' role='note'>{icon('mail')}<span>Redirecionamento ligado: todo aviso vai para {h(config.EMAIL_REDIRECT)}.</span></div>"
    if processed == "fila":
        notice += "<div class='notice' role='status'>Processando os avisos pendentes. Atualize a página em alguns segundos para ver o resultado.</div>"
    elif processed == "reenvio":
        notice += "<div class='notice' role='status'>Reenvio confirmado. O aviso sai em instantes; atualize a página para ver o resultado.</div>"
    button = (f"<form method='post' action='/emails/reprocessar'><input type='hidden' name='csrf' value='{h(csrf)}'>"
              f"<button class='btn btn-outline btn-sm' type='submit'>{icon('mail')}<span>Reprocessar pendentes</span></button></form>")
    if entries:
        rows = "".join(queue_row(entry, csrf) for entry in entries)
        history = f"<div class='table-wrap audit-table'><table><thead><tr><th>DATA</th><th>LOJA</th><th>ARQUIVO</th><th>PARA</th><th>SITUAÇÃO</th></tr></thead><tbody>{rows}</tbody></table></div>"
    else:
        history = (f"<div class='empty-state'><span class='empty-mark'>{icon('mail')}</span><strong>Nenhum aviso na fila</strong>"
                   "<span>Publique um arquivo de uma loja com contato cadastrado para ver o aviso aqui.</span></div>")
    return (page_heading("Administração", "Fila de e-mails", f"Avisos de publicação. {contacts} {'loja tem' if contacts == 1 else 'lojas têm'} contato cadastrado.")
            + notice + "<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>HISTÓRICO</span><h2>Avisos gerados</h2></div>"
            + f"{button}</div>{history}</section>")
