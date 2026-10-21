"""Painel principal: barra lateral, meses, arquivos, resumo, previsão e ferramentas da administração."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

import resumo_sped
from portal import config
from portal.auth import STAFF_ROLES
from portal.calendario import LARGE_GROUP_BUSINESS_DAY, LARGE_GROUP_NOTE, SMALL_GROUP_BUSINESS_DAY, SMALL_GROUP_UNITS, publication_deadline
from portal.sped import DOCUMENT_TYPES, format_cnpj, normalize_cnpj, period_label
from portal.views.layout import dashboard_link, file_size, h, help_tip, icon, short_store_names

CURRENT = " active' aria-current='page"


def support_notice() -> str:
    recorded = " As ações deste acesso são registradas." if config.MONITORING_ENABLED else ""
    return (f"<div class='notice notice-info' role='note'>{icon('eye')}<span><strong>Acesso de suporte:</strong> consulta de todas as lojas e download de arquivos. "
            f"Para publicar arquivos ou cadastrar lojas, fale com a administração.{recorded}</span></div>")


def sidebar(user: sqlite3.Row, stores: list[sqlite3.Row], selected_store: sqlite3.Row | None, document_type: str, section: str = "documentos") -> str:
    """Barra lateral: loja aberta, troca de loja (lista para o grupo, seletor para administração e suporte),
    tipo de SPED e, para a administração, o monitoramento do suporte."""
    admin = user["role"] == "admin"
    staff = user["role"] in STAFF_ROLES
    if selected_store:
        identity = f"<strong>{h(selected_store['name'])}</strong><small>CNPJ {h(format_cnpj(selected_store['cnpj']))}</small>"
    else:
        identity = f"<strong>Todas as lojas</strong><small>Visão {'da administração' if admin else 'do suporte'}</small>"
    picker = ""
    if staff:
        options = "<option value=''>Todas as lojas</option>" + "".join(
            f"<option value='{store['id']}'{' selected' if selected_store and store['id'] == selected_store['id'] else ''}>{h(store['name'])}</option>" for store in stores)
        picker = ("<form class='store-picker' method='get' action='/dashboard'><input type='hidden' name='type' value='" + h(document_type) + "'>"
                  "<label for='store_id'>Escolher loja</label><select id='store_id' name='store_id'>" + options + "</select>"
                  f"<button class='btn btn-outline btn-sm btn-block' type='submit'><span>Abrir loja</span>{icon('arrow')}</button></form>")
    card = (f"<div class='side-store{' with-picker' if picker else ''}'><div class='side-store-heading'><span class='side-store-mark'>{icon('store')}</span><span>{'LOJA ABERTA' if selected_store else 'REDE'}</span></div>"
            f"<div class='side-store-identity'>{identity}</div>{picker}</div>")
    branches = ""
    if not staff and len(stores) > 1:
        short = short_store_names(stores)
        links = ""
        for store in stores:
            active = bool(selected_store) and store["id"] == selected_store["id"]
            cnpj = normalize_cnpj(store["cnpj"] or "")
            tag = "Matriz" if cnpj[8:12] == "0001" else f"/{cnpj[8:12]}-{cnpj[12:]}"
            state = CURRENT if active else ""
            links += (f"<a class='branch-link{state}' href='{dashboard_link(store['id'], document_type)}' title='{h(store['name'])}'>"
                      f"<span>{h(short[store['id']])}</span><small>{h(tag)}</small></a>")
        branches = f"<nav class='branch-nav' aria-label='Lojas do grupo'><span class='side-section-label'>LOJAS DO GRUPO</span><div class='branch-list'>{links}</div></nav>"
    store_id = selected_store["id"] if selected_store else None
    tabs = "".join(
        f"<a class='sped-tab{CURRENT if section == 'documentos' and key == document_type else ''}' href='{dashboard_link(store_id, key)}'>"
        f"<span class='tab-mark'>{mark}</span><span class='tab-text'>{label}<small>{subject}</small></span></a>"
        for key, (label, subject, mark) in DOCUMENT_TYPES.items())
    navigation = f"<nav class='sped-navigation' aria-label='Tipo de arquivo'><span class='side-section-label'>ARQUIVOS</span>{tabs}</nav>"
    if admin and config.MONITORING_ENABLED:
        navigation += (f"<nav class='sped-navigation' aria-label='Administração'><span class='side-section-label'>ADMINISTRAÇÃO</span>"
                       f"<a class='sped-tab{CURRENT if section == 'monitoramento' else ''}' href='/monitoramento'><span class='tab-mark'>{icon('eye')}</span>"
                       "<span class='tab-text'>Monitoramento<small>Ações do suporte</small></span></a></nav>")
    return f"<aside class='workspace-sidebar'><div class='side-overline'>PORTAL DA REDE</div>{card}{branches}{navigation}<div class='side-bottom'><span class='secure-dot'></span> Acesso protegido</div></aside>"


def period_nav(store: sqlite3.Row, document_type: str, months: list[str], published: list[str], competence: str, units: int) -> str:
    """Meses em ordem cronológica. Os ainda não publicados aparecem como "previsto", com o prazo no tooltip."""
    pills = ""
    for comp in months:
        pending = comp not in published
        hint = f" title='Previsto até {publication_deadline(comp, units)[0]:%d/%m/%Y}'" if pending else ""
        pills += (f"<a class='pill{' pending' if pending else ''}{CURRENT if comp == competence else ''}' href='{dashboard_link(store['id'], document_type, comp)}'{hint}>"
                  f"{h(resumo_sped.month_label(comp, short=True).capitalize())}</a>")
    note = ""
    upcoming = [comp for comp in months if comp not in published and (not published or comp > max(published))]
    if upcoming and competence in published:
        deadline, nth = publication_deadline(upcoming[0], units)
        note = (f"<p class='period-next'>{icon('calendar')}<span>Próximo arquivo: <strong>{h(resumo_sped.month_label(upcoming[0]).capitalize())}</strong>, "
                f"previsto até <strong>{deadline:%d/%m/%Y}</strong> ({nth}º dia útil)</span>{help_tip(LARGE_GROUP_NOTE) if units > SMALL_GROUP_UNITS else ''}</p>")
    return (f"<nav class='period-nav' aria-label='Mês de referência'><span class='period-label'>Mês de referência</span><div class='pill-list'>{pills}</div></nav>{note}")


def forecast_card(document_type: str, competence: str, units: int) -> str:
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


def period_note(document: sqlite3.Row) -> str:
    """Texto do período para arquivos de parte do mês (ex.: quinzenal); vazio para o mês inteiro."""
    period = period_label(document["period_start"], document["period_end"])
    return f"Período {period}" if period else ""


def files_card(documents: list[sqlite3.Row], competence: str) -> str:
    files = sorted((doc for doc in documents if doc["competence"] == competence), key=lambda doc: (doc["period_start"] or "", doc["uploaded_at"]))
    rows = "".join(
        f"<li class='file-row'><span class='file-icon'>{icon('file')}</span><div class='file-info'><strong title='{h(doc['original_name'])}'>{h(doc['original_name'])}</strong>"
        f"<small>{h(period_note(doc) + ' · ') if period_note(doc) else ''}Publicado em {h(datetime.fromisoformat(doc['uploaded_at']).strftime('%d/%m/%Y'))} · {h(file_size(doc['size']))}</small></div>"
        f"<a class='btn btn-primary btn-sm' href='/download/{h(doc['id'])}' aria-label='Baixar {h(doc['original_name'])}'>{icon('download')}<span>Baixar</span></a></li>"
        for doc in files)
    total = f"{len(files)} {'arquivo' if len(files) == 1 else 'arquivos'}"
    return f"<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>ARQUIVOS DO MÊS</span><h2>Pronto para baixar</h2></div><span class='count-badge'>{total}</span></div><ul class='file-list'>{rows}</ul></section>"


def summary_section(competence: str, summary: dict | None, history: list[tuple[str, dict]]) -> str:
    return ("<section class='section-block summary-block'><div class='section-heading'><div><span class='section-kicker'>RESUMO DO MÊS</span>"
            f"<h2>{h(resumo_sped.month_label(competence).capitalize())}</h2><p class='section-copy'>Números calculados automaticamente a partir do arquivo deste mês.</p></div></div>"
            + resumo_sped.render(summary, competence, history) + "</section>")


def matrix_notice(document_type: str, matrix: sqlite3.Row) -> str:
    """Filial sem SPED Contribuições próprio: o arquivo é entregue pela matriz e inclui todas as lojas do grupo."""
    label = DOCUMENT_TYPES[document_type][0]
    return (f"<div class='empty-state'><span class='empty-mark'>{icon('file')}</span><strong>O {h(label)} desta loja é entregue pela matriz</strong>"
            "<span>O arquivo da matriz reúne todas as lojas do grupo, inclusive esta.</span>"
            f"<a class='btn btn-primary btn-sm' href='{dashboard_link(matrix['id'], document_type)}'><span>Abrir arquivo da matriz</span>{icon('arrow')}</a></div>")


def admin_tools(user: sqlite3.Row, selected_store: sqlite3.Row | None) -> str:
    """Formulários da administração: publicar SPED e cadastrar loja. Recolhidos quando uma loja está aberta."""
    csrf = h(user["csrf_token"])
    upload = (f"<div class='panel'><div class='panel-title'><span class='panel-icon upload-icon'>{icon('upload')}</span><div><span class='section-kicker'>PUBLICAR · FISCAL OU CONTRIBUIÇÕES</span><h2>Enviar arquivo SPED</h2></div></div>"
              "<p class='panel-copy'>Escolha o arquivo e publique. O portal lê o registro 0000 e identifica sozinho o tipo (Fiscal ICMS/IPI ou Contribuições PIS/COFINS), "
              "a loja pelo CNPJ e a competência. Depois anexa o arquivo na aba certa da loja. Arquivos de parte do mês (ex.: SPED Fiscal quinzenal) "
              "também são aceitos; a competência é o mês do período.</p>"
              f"<form method='post' enctype='multipart/form-data' action='/upload' class='stack-form'><input type='hidden' name='csrf' value='{csrf}'>"
              "<label>Arquivo .txt<input class='file-input' name='file' type='file' accept='.txt,text/plain' required><small class='field-help'>Até 40 MB. A loja precisa estar cadastrada com o CNPJ do arquivo.</small></label>"
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


def stores_table(store_rows: list[sqlite3.Row], document_type: str) -> str:
    rows = "".join(
        f"<tr><td class='store-name'>{h(store['name'])}</td><td class='mono'>{h(format_cnpj(store['cnpj']))}</td><td>{store['total']}</td>"
        f"<td class='cell-action'><a class='btn btn-outline btn-sm' href='{dashboard_link(store['id'], document_type)}'><span>Abrir</span>{icon('arrow')}</a></td></tr>"
        for store in store_rows) or "<tr><td colspan='4' class='empty-cell'>Nenhuma loja cadastrada ainda.</td></tr>"
    return (f"<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>REDE</span><h2>Lojas cadastradas</h2></div><span class='count-badge'>{len(store_rows)} lojas</span></div>"
            f"<div class='table-wrap'><table><thead><tr><th>LOJA</th><th>CNPJ</th><th>ARQUIVOS</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>")


def recent_uploads(documents: list[sqlite3.Row]) -> str:
    if not documents:
        return ""
    rows = "".join(
        f"<tr><td class='store-name'>{h(doc['store_name'])}</td><td><span class='month-tag'>{h(doc['competence'][5:7] + '/' + doc['competence'][:4])}</span>"
        f"{f'<small class=muted> {h(period_note(doc))}</small>' if period_note(doc) else ''}</td>"
        f"<td class='file-name' title='{h(doc['original_name'])}'>{h(doc['original_name'])}</td><td class='muted'>{h(datetime.fromisoformat(doc['uploaded_at']).strftime('%d/%m/%Y'))}</td>"
        f"<td class='cell-action'><a class='btn btn-ghost btn-sm' href='/download/{h(doc['id'])}' aria-label='Baixar {h(doc['original_name'])}'>{icon('download')}<span>Baixar</span></a></td></tr>"
        for doc in documents)
    return (f"<section class='section-block'><div class='section-heading'><div><span class='section-kicker'>ÚLTIMAS PUBLICAÇÕES</span><h2>Arquivos enviados recentemente</h2></div></div>"
            f"<div class='table-wrap'><table><thead><tr><th>LOJA</th><th>MÊS</th><th>ARQUIVO</th><th>PUBLICADO</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>")
