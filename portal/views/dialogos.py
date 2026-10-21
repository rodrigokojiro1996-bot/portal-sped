"""Pop-up do resultado da publicação."""
from __future__ import annotations

import sqlite3

import resumo_sped
from portal.sped import format_cnpj, period_label, sped_label
from portal.views.layout import h, icon


def publish_dialog(success: bool, message: str, details: list[tuple[str, str]] | None = None) -> str:
    """Pop-up do resultado da publicação. É um <dialog> nativo que fecha pelo próprio botão, sem JavaScript;
    static/portal.js o reabre como modal para fechar também com Esc."""
    title = "Arquivo publicado com sucesso" if success else "Arquivo não publicado"
    rows = "".join(f"<div><dt>{h(term)}</dt><dd>{h(value)}</dd></div>" for term, value in details or [])
    facts = f"<dl class='publish-facts'>{rows}</dl>" if rows else ""
    return (f"<dialog open class='publish-dialog {'is-success' if success else 'is-error'}' role='{'status' if success else 'alert'}' aria-labelledby='publish-title'>"
            f"<span class='publish-mark'>{icon('upload' if success else 'file')}</span><h2 id='publish-title'>{title}</h2><p>{h(message)}</p>{facts}"
            "<form method='dialog'><button class='btn btn-primary' type='submit' autofocus><span>OK, entendi</span></button></form></dialog>")


def published_dialog(document: sqlite3.Row) -> str:
    """Confirmação depois da publicação: tipo do SPED, loja, CNPJ, competência e, se for parte do mês, o período."""
    label = sped_label(document["document_type"])
    month = resumo_sped.month_label(document["competence"]).capitalize()
    period = period_label(document["period_start"], document["period_end"])
    when = f"competência {month}" + (f" (período parcial de {period})" if period else "")
    message = f"O arquivo {label} foi publicado com sucesso na loja {document['store_name']}, CNPJ {format_cnpj(document['store_cnpj'])}, {when}."
    details = [("Tipo", label), ("Loja", document["store_name"]), ("CNPJ", format_cnpj(document["store_cnpj"])), ("Competência", month)]
    if period:
        details.append(("Período", f"{period} (parcial)"))
    return publish_dialog(True, message, details + [("Arquivo", document["original_name"])])
