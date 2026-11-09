"""Tela e exportação CSV do registro de acessos por certificado."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from portal.acessos import EVENTS, detail
from portal.auditoria import AUDIT_PERIODS, csv_cell
from portal.sped import format_cnpj
from portal.views.layout import h, icon, page_heading

EVENT_BADGES = {"entrou": "badge-acessos", "recusado": "badge-alertas", "baixou": "badge-downloads"}
LIMIT = 500


def access_row(entry: sqlite3.Row) -> str:
    moment = datetime.fromisoformat(entry["created_at"])
    certificate = (f"<strong>{h(format_cnpj(entry['certificate_cnpj']))}</strong><small>Série {h(entry['certificate_serial'])}</small>" if entry["certificate_cnpj"]
                   else "<span class='muted'>Não identificado</span>")
    store = f"<strong>{h(entry['store_name'])}</strong><small>{h(format_cnpj(entry['store_cnpj']))}</small>" if entry["store_name"] else "<span class='muted'>—</span>"
    host = f"<small class='audit-host'>{h(entry['hostname'])}</small>" if entry["hostname"] else ""
    return (f"<tr><td class='mono'>{moment:%d/%m/%Y %H:%M}</td><td class='audit-cell'><span class='audit-badge {EVENT_BADGES[entry['event']]}'>{h(EVENTS[entry['event']])}</span></td>"
            f"<td class='audit-cell'>{certificate}</td><td class='audit-cell'>{store}</td><td class='audit-cell audit-what'><span>{h(detail(entry))}</span></td>"
            f"<td class='audit-cell audit-machine'><strong>{h(entry['ip'])}</strong>{host}<small>{h(entry['browser'])}</small></td></tr>")


def access_main(stores: list[sqlite3.Row], entries: list[sqlite3.Row], store_filter: str, period: str) -> str:
    """Filtros, histórico e exportação. entries pode vir com um item além de LIMIT, que indica que há mais registros."""
    store_options = "<option value=''>Todas as lojas</option>" + "".join(
        f"<option value='{store['id']}'{' selected' if str(store['id']) == store_filter else ''}>{h(store['name'])}</option>" for store in stores)
    period_options = "".join(f"<option value='{h(key)}'{' selected' if key == period else ''}>{h(label)}</option>" for key, (label, _) in AUDIT_PERIODS.items())
    export = "/acessos.csv?" + "&amp;".join(f"{key}={h(value)}" for key, value in (("loja", store_filter), ("periodo", period)) if value)
    filters = ("<form class='audit-filters' method='get' action='/acessos'>"
               f"<label>Loja<select name='loja'>{store_options}</select></label><label>Período<select name='periodo'>{period_options}</select></label>"
               f"<div class='audit-filter-actions'><button class='btn btn-primary btn-sm' type='submit'>{icon('filter')}<span>Filtrar</span></button>"
               f"<a class='btn btn-outline btn-sm' href='{export}'>{icon('download')}<span>Exportar para Excel</span></a></div></form>")
    shown = entries[:LIMIT]
    if shown:
        rows = "".join(access_row(entry) for entry in shown)
        history = ("<div class='table-wrap audit-table'><table><thead><tr><th>DATA</th><th>O QUE ACONTECEU</th><th>CERTIFICADO</th><th>LOJA</th><th>DETALHE</th><th>ORIGEM</th></tr></thead>"
                   f"<tbody>{rows}</tbody></table></div>")
        if len(entries) > LIMIT:
            history += f"<p class='summary-note'>Mostrando os {LIMIT} registros mais recentes. Use os filtros ou exporte para Excel para ver todos.</p>"
    else:
        history = (f"<div class='empty-state'><span class='empty-mark'>{icon('lock')}</span><strong>Nenhum acesso neste período</strong>"
                   "<span>Mude a loja ou o período para ver outros registros.</span></div>")
    note = ("<p class='summary-note'>O certificado é identificado pelo CNPJ e pelo número de série; o CPF do responsável não é guardado. "
            "Abaixo do IP vem o nome reverso, que pela internet costuma ser o da operadora, e não o do computador. "
            "Visitas sem certificado e downloads de quem entrou com senha não aparecem aqui.</p>")
    return (page_heading("Administração", "Acessos por certificado", "Quem entrou pelo certificado digital, o que baixou e o que foi recusado.") + filters
            + "<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>HISTÓRICO</span><h2>O que aconteceu</h2></div>"
            + f"<span class='count-badge'>{len(shown)} {'registro' if len(shown) == 1 else 'registros'}</span></div>{history}{note}</section>")


def access_csv(entries: list[sqlite3.Row]) -> bytes:
    """Histórico filtrado em CSV (separador ; e acentos corretos no Excel)."""
    header = ("Data", "Hora", "O que aconteceu", "CNPJ do certificado", "Série do certificado", "Loja", "CNPJ da loja", "Detalhe", "IP", "Nome reverso", "Navegador")
    lines = [";".join(csv_cell(value) for value in header)]
    for entry in entries:
        moment = datetime.fromisoformat(entry["created_at"])
        values = (f"{moment:%d/%m/%Y}", f"{moment:%H:%M:%S}", EVENTS[entry["event"]], format_cnpj(entry["certificate_cnpj"]) if entry["certificate_cnpj"] else "", entry["certificate_serial"],
                  entry["store_name"], format_cnpj(entry["store_cnpj"]) if entry["store_cnpj"] else "", detail(entry), entry["ip"], entry["hostname"], entry["browser"])
        lines.append(";".join(csv_cell(value) for value in values))
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")
