"""Tela da fila de e-mails (só aparece com config.EMAIL_ENABLED)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import resumo_sped
from portal import config
from portal.emails import MAX_ATTEMPTS, STATUS_LABELS
from portal.sped import DOCUMENT_TYPES, format_cnpj
from portal.views.layout import h, icon, page_heading

STATUS_BADGES = {"pendente": "badge-acessos", "ensaio": "badge-consultas", "enviado": "badge-downloads", "erro": "badge-alertas"}


def queue_row(entry: sqlite3.Row) -> str:
    moment = datetime.fromisoformat(entry["created_at"]).astimezone()
    month = resumo_sped.month_label(entry["competence"]).capitalize()
    copies = f"<small>Cópia: {h(entry['copies'])}</small>" if entry["copies"] else ""
    message = ""
    if entry["body"]:
        message = (f"<details class='viz-table'><summary>Ver mensagem</summary><p class='email-subject'>{h(entry['subject'])}</p><pre class='email-body'>{h(entry['body'])}</pre></details>")
    reason = entry["error"] or ""
    if entry["status"] == "erro" and entry["attempts"] >= MAX_ATTEMPTS:
        reason += f" Parou após {MAX_ATTEMPTS} tentativas."
    return (f"<tr><td class='mono'>{moment:%d/%m/%Y %H:%M}</td><td class='audit-cell'><strong>{h(entry['store_name'])}</strong><small>{h(format_cnpj(entry['store_cnpj']))}</small></td>"
            f"<td class='audit-cell'><strong>{h(DOCUMENT_TYPES[entry['document_type']][0])}</strong><small>{h(month)}</small></td>"
            f"<td class='audit-cell'>{h(entry['recipients'] or '—')}{copies}{message}</td>"
            f"<td class='audit-cell'><span class='audit-badge {STATUS_BADGES[entry['status']]}'>{h(STATUS_LABELS[entry['status']])}</span><small>{h(reason)}</small></td></tr>")


def emails_main(entries: list[sqlite3.Row], contacts: int, csrf: str, processed: str) -> str:
    """Conteúdo da tela: aviso do modo de teste, reprocessamento e lista da fila."""
    notice = (f"<div class='notice notice-info' role='note'>{icon('mail')}<span><strong>Modo de teste:</strong> cada publicação gera um aviso nesta lista, "
              "mas nenhum e-mail sai do portal.</span></div>")
    if config.EMAIL_REDIRECT:
        notice += f"<div class='notice notice-info' role='note'>{icon('mail')}<span>Redirecionamento ligado: todo aviso vai para {h(config.EMAIL_REDIRECT)}.</span></div>"
    if processed.isdigit():
        notice += f"<div class='notice' role='status'>{h(processed)} {'aviso processado' if processed == '1' else 'avisos processados'}.</div>"
    button = (f"<form method='post' action='/emails/reprocessar'><input type='hidden' name='csrf' value='{h(csrf)}'>"
              f"<button class='btn btn-outline btn-sm' type='submit'>{icon('mail')}<span>Reprocessar pendentes</span></button></form>")
    if entries:
        rows = "".join(queue_row(entry) for entry in entries)
        history = f"<div class='table-wrap audit-table'><table><thead><tr><th>DATA</th><th>LOJA</th><th>ARQUIVO</th><th>PARA</th><th>SITUAÇÃO</th></tr></thead><tbody>{rows}</tbody></table></div>"
    else:
        history = (f"<div class='empty-state'><span class='empty-mark'>{icon('mail')}</span><strong>Nenhum aviso na fila</strong>"
                   "<span>Publique um arquivo de uma loja com contato cadastrado para ver o aviso aqui.</span></div>")
    return (page_heading("Administração", "Fila de e-mails", f"Avisos de publicação. {contacts} {'loja tem' if contacts == 1 else 'lojas têm'} contato cadastrado.")
            + notice + "<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>HISTÓRICO</span><h2>Avisos gerados</h2></div>"
            + f"{button}</div>{history}</section>")
