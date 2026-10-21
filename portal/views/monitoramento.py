"""Tela e exportação CSV do monitoramento do suporte (só aparece com config.MONITORING_ENABLED)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import resumo_sped
from portal.auditoria import AUDIT_ACTIONS, AUDIT_FILTERS, AUDIT_PERIODS, WEEKDAYS, audit_sentence, csv_cell
from portal.sped import format_cnpj
from portal.views.layout import h, icon, page_heading


def monitoring_main(support_users: list[sqlite3.Row], everything: list[sqlite3.Row], entries: list[sqlite3.Row], truncated: bool,
                    user_filter: str, period: str, category: str) -> str:
    """Conteúdo da tela: filtros, indicadores, último acesso e histórico agrupado por dia."""

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
                   "<span>Crie um com o arquivo scripts\\windows\\criar-usuario-suporte.bat.</span></div>")
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
    return (page_heading("Administração", "Monitoramento do suporte", "Tudo o que os usuários do suporte fazem no portal: entradas, lojas consultadas e arquivos baixados.")
            + filters + tiles + last
            + "<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>HISTÓRICO</span><h2>O que o suporte fez</h2></div>"
            + f"<span class='count-badge'>{len(entries)} {'ação' if len(entries) == 1 else 'ações'}</span></div>{history}{note}</section>")


def monitoring_csv(entries: list[sqlite3.Row]) -> bytes:
    """Histórico filtrado em CSV (separador ; e acentos corretos no Excel)."""
    header = ("Data", "Hora", "Usuário", "Login", "Tipo", "O que fez", "Detalhe", "Loja", "CNPJ da loja", "Arquivo", "Máquina", "IP", "Navegador")
    lines = [";".join(csv_cell(value) for value in header)]
    for entry in entries:
        moment = datetime.fromisoformat(entry["created_at"])
        sentence, extra = audit_sentence(entry)
        values = (f"{moment:%d/%m/%Y}", f"{moment:%H:%M:%S}", entry["user_name"], entry["login_name"], AUDIT_ACTIONS.get(entry["action"], ("Ação", ""))[0], sentence, extra,
                  entry["store_name"], format_cnpj(entry["store_cnpj"]) if entry["store_cnpj"] else "", entry["file_name"], entry["machine"] or "Não identificada", entry["ip"], entry["browser"])
        lines.append(";".join(csv_cell(value) for value in values))
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")
