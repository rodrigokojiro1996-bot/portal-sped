"""Resumo fiscal extraído dos arquivos SPED EFD publicados no portal.

O módulo lê os registros de movimento e de apuração do SPED Fiscal (ICMS/IPI) e do
SPED Contribuições (PIS/COFINS), agrega os valores em um dicionário serializável em
JSON e gera o HTML do painel (indicadores, insights e gráficos em SVG/CSS, sem
JavaScript, para respeitar a política de segurança do portal).
"""
from __future__ import annotations

import html
import math
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# 2: apurações (E110, M200/M600) de vários períodos são somadas em vez de valer só a última.
SUMMARY_VERSION = 2
CANCELLED_STATUS = {"02", "03", "04", "05"}
MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")

CFOP_LABELS = {
    "1102": "Compra para comercialização", "2102": "Compra para comercialização (interestadual)",
    "1403": "Compra para comercialização com ST", "2403": "Compra para comercialização com ST (interestadual)",
    "1202": "Devolução de venda", "1411": "Devolução de venda com ST",
    "1152": "Transferência recebida", "1409": "Transferência recebida com ST",
    "1556": "Compra de material de uso e consumo", "2556": "Compra de material de uso e consumo (interestadual)",
    "1407": "Compra de uso e consumo com ST", "1551": "Compra de ativo imobilizado", "2551": "Compra de ativo imobilizado (interestadual)",
    "1910": "Entrada em bonificação ou brinde", "2910": "Entrada em bonificação ou brinde (interestadual)",
    "1949": "Outras entradas", "2949": "Outras entradas (interestadual)",
    "1253": "Compra de energia elétrica", "1303": "Aquisição de serviço de comunicação", "1353": "Aquisição de serviço de transporte",
    "5102": "Venda de mercadoria", "6102": "Venda de mercadoria (interestadual)",
    "5405": "Venda de mercadoria com ST", "5403": "Venda com ST como substituto",
    "5202": "Devolução de compra", "5411": "Devolução de compra com ST", "6202": "Devolução de compra (interestadual)", "6411": "Devolução de compra com ST (interestadual)",
    "5152": "Transferência enviada", "5409": "Transferência enviada com ST",
    "5910": "Remessa em bonificação ou brinde", "5927": "Baixa de estoque por perda", "5949": "Outras saídas", "6949": "Outras saídas (interestadual)",
}
PIS_CST_LABELS = {
    "01": "Tributada (alíquota básica)", "02": "Tributada (alíquota diferenciada)", "03": "Tributada por unidade",
    "04": "Monofásica (revenda a alíquota zero)", "05": "Substituição tributária", "06": "Alíquota zero",
    "07": "Isenta", "08": "Sem incidência", "09": "Com suspensão",
}
TAX_GROUPS = ("Tributadas", "Substituição tributária", "Isentas ou não tributadas", "Simples Nacional", "Outras")


# --------------------------------------------------------------------------- leitura

def number(value: str) -> float:
    try:
        return float(value.replace(",", ".")) if value else 0.0
    except ValueError:
        return 0.0


def sped_date(value: str) -> str | None:
    if len(value) != 8 or not value.isdigit():
        return None
    return f"{value[4:]}-{value[2:4]}-{value[:2]}"


def read_lines(path: Path) -> list[str]:
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    return text.splitlines()


def icms_tax_group(cst: str) -> str:
    cst = cst.strip()
    if len(cst) == 4:
        csosn = cst[1:]
        if csosn in ("201", "202", "203", "500"):
            return "Substituição tributária"
        return "Outras" if csosn == "900" else "Simples Nacional"
    code = cst[-2:]
    if code in ("00", "20"):
        return "Tributadas"
    if code in ("10", "30", "60", "70"):
        return "Substituição tributária"
    if code in ("40", "41", "50"):
        return "Isentas ou não tributadas"
    return "Outras"


def summarize(path: Path, document_type: str) -> dict:
    return summarize_lines(read_lines(path), document_type)


def summarize_lines(lines: list[str], document_type: str) -> dict:
    """Resumo de um arquivo, ou de vários arquivos parciais do mesmo mês com as linhas concatenadas em ordem de período."""
    return summarize_contributions(lines) if document_type == "contribuicoes" else summarize_fiscal(lines)


def summarize_fiscal(lines: list[str]) -> dict:
    wanted = {"0150", "C100", "C190", "E110", "E116", "E210", "H005"}
    participants: dict[str, str] = {}
    totals = {side: {"valor": 0.0, "icms": 0.0, "icms_st": 0.0, "documentos": 0} for side in ("saidas", "entradas")}
    cfops: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    tax_groups: dict[str, float] = defaultdict(float)
    daily: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    suppliers: dict[str, float] = defaultdict(float)
    duplicated_x929 = 0.0
    has_operations = False
    current: dict | None = None
    assessment = None
    st_due = None
    obligations: list[list] = []
    inventory = None

    for line in lines:
        if len(line) < 6 or line[0] != "|" or line[1:5] not in wanted:
            continue
        fields = line.split("|")
        record = fields[1]
        if record == "0150" and len(fields) > 3:
            participants[fields[2]] = fields[3].strip()
        elif record == "C100" and len(fields) > 12:
            has_operations = True
            if fields[6] in CANCELLED_STATUS:
                current = None
                continue
            side = "saidas" if fields[2] == "1" else "entradas"
            totals[side]["documentos"] += 1
            day = sped_date(fields[10]) if side == "saidas" else (sped_date(fields[11]) or sped_date(fields[10]))
            current = {"participant": fields[4], "day": day}
        elif record == "C190" and current is not None and len(fields) > 9:
            cfop = fields[3]
            side = "saidas" if cfop[:1] in ("5", "6", "7") else "entradas" if cfop[:1] in ("1", "2", "3") else None
            if side is None:
                continue
            value = number(fields[5])
            if cfop[1:] == "929":
                duplicated_x929 += value
                continue
            icms, icms_st = number(fields[7]), number(fields[9])
            totals[side]["valor"] += value
            totals[side]["icms"] += icms
            totals[side]["icms_st"] += icms_st
            cfops[cfop][0] += value
            cfops[cfop][1] += icms
            if current["day"]:
                daily[current["day"]][0 if side == "saidas" else 1] += value
            if side == "saidas":
                tax_groups[icms_tax_group(fields[2])] += value
            else:
                suppliers[current["participant"]] += value
        elif record == "E110" and len(fields) > 14:
            period = {
                "debitos": number(fields[2]), "ajustes_debito": number(fields[3]) + number(fields[4]),
                "estornos_credito": number(fields[5]), "creditos": number(fields[6]),
                "ajustes_credito": number(fields[7]) + number(fields[8]), "estornos_debito": number(fields[9]),
                "saldo_credor_anterior": number(fields[10]), "saldo_apurado": number(fields[11]),
                "deducoes": number(fields[12]), "recolher": number(fields[13]), "saldo_credor": number(fields[14]),
            }
            assessment = add_period(assessment, period, first_only=("saldo_credor_anterior",), last_only=("saldo_credor",))
        elif record == "E210" and len(fields) > 13:
            st_due = (st_due or 0.0) + number(fields[13])
        elif record == "E116" and len(fields) > 4:
            obligations.append([number(fields[3]), sped_date(fields[4])])
        elif record == "H005" and len(fields) > 3:
            candidate = {"data": sped_date(fields[2]), "valor": number(fields[3])}
            if inventory is None or candidate["valor"] > inventory["valor"]:
                inventory = candidate

    top_suppliers = sorted(suppliers.items(), key=lambda item: item[1], reverse=True)[:6]
    operations = None
    if has_operations:
        operations = {
            "saidas": totals["saidas"], "entradas": totals["entradas"],
            "cfops": {code: [round(v, 2), round(i, 2)] for code, (v, i) in cfops.items()},
            "tributacao_saidas": {group: round(value, 2) for group, value in tax_groups.items()},
            "diario": {day: [round(s, 2), round(e, 2)] for day, (s, e) in sorted(daily.items())},
            "fornecedores": [[participants.get(code) or code, round(value, 2)] for code, value in top_suppliers],
            "fornecedores_qtd": len(suppliers),
            "cfop_x929": round(duplicated_x929, 2),
        }
    return {
        "tipo": "fiscal", "versao": SUMMARY_VERSION, "operacoes": operations,
        "apuracao": {"icms": assessment, "icms_st_recolher": st_due, "obrigacoes": obligations} if assessment else None,
        "inventario": inventory if inventory and inventory["valor"] > 0 else None,
    }


def summarize_contributions(lines: list[str]) -> dict:
    wanted = {"0140", "A010", "A100", "C010", "C100", "C170", "C175", "M100", "M200", "M210", "M400", "M500", "M600"}
    establishment_names: dict[str, str] = {}
    establishments: dict[str, list[float]] = {}
    cfops: dict[str, float] = defaultdict(float)
    revenue_taxed = 0.0
    revenue_exempt: dict[str, float] = defaultdict(float)
    taxes: dict[str, dict | None] = {"pis": None, "cofins": None}
    credits = {"pis": 0.0, "cofins": 0.0}
    has_operations = False
    current_cnpj = ""
    current_side: str | None = None

    def tax(fields: list[str]) -> dict:
        return {
            "apurada": number(fields[2]) + number(fields[9]),
            "creditos": number(fields[3]) + number(fields[4]),
            "deducoes": number(fields[6]) + number(fields[7]) + number(fields[10]) + number(fields[11]),
            "recolher": number(fields[13]),
            "nao_cumulativa": number(fields[2]), "cumulativa": number(fields[9]),
        }

    for line in lines:
        if len(line) < 6 or line[0] != "|" or line[1:5] not in wanted:
            continue
        fields = line.split("|")
        record = fields[1]
        if record == "0140" and len(fields) > 4:
            establishment_names[fields[4]] = fields[3].strip()
        elif record in ("C010", "A010") and len(fields) > 2:
            current_cnpj = fields[2]
            establishments.setdefault(current_cnpj, [0.0, 0.0])
        elif record in ("C100", "A100") and len(fields) > 12:
            has_operations = True
            status = fields[6] if record == "C100" else fields[5]
            if status in CANCELLED_STATUS:
                current_side = None
                continue
            current_side = "saidas" if fields[2] == "1" else "entradas"
            establishments.setdefault(current_cnpj, [0.0, 0.0])[0 if current_side == "saidas" else 1] += number(fields[12])
            if record == "A100" and current_side == "saidas":
                cfops["Serviços"] += number(fields[12])
        elif record == "C170" and current_side == "saidas" and len(fields) > 11:
            cfops[fields[11]] += number(fields[7]) - number(fields[8])
        elif record == "C175" and current_side == "saidas" and len(fields) > 4:
            cfops[fields[2]] += number(fields[3]) - number(fields[4])
        elif record == "M200" and len(fields) > 13:
            taxes["pis"] = add_period(taxes["pis"], tax(fields))
        elif record == "M600" and len(fields) > 13:
            taxes["cofins"] = add_period(taxes["cofins"], tax(fields))
        elif record == "M210" and len(fields) > 3:
            revenue_taxed += number(fields[3])
        elif record == "M400" and len(fields) > 3:
            revenue_exempt[fields[2]] += number(fields[3])
        elif record == "M100" and len(fields) > 14:
            credits["pis"] += number(fields[14])
        elif record == "M500" and len(fields) > 14:
            credits["cofins"] += number(fields[14])

    operations = None
    if has_operations:
        operations = {
            "estabelecimentos": [[cnpj, establishment_names.get(cnpj, ""), round(s, 2), round(e, 2)] for cnpj, (s, e) in establishments.items() if s or e],
            "saidas": round(sum(s for s, _ in establishments.values()), 2),
            "entradas": round(sum(e for _, e in establishments.values()), 2),
            "cfops_saida": {code: round(value, 2) for code, value in cfops.items()},
        }
    assessment = None
    if taxes["pis"] or taxes["cofins"]:
        assessment = {
            "pis": taxes["pis"], "cofins": taxes["cofins"],
            "receita_tributada": round(revenue_taxed, 2),
            "receita_sem_incidencia": {cst: round(value, 2) for cst, value in sorted(revenue_exempt.items())},
            "creditos_descontados": credits,
        }
    return {"tipo": "contribuicoes", "versao": SUMMARY_VERSION, "operacoes": operations, "apuracao": assessment}


def add_period(total: dict | None, period: dict, first_only: tuple[str, ...] = (), last_only: tuple[str, ...] = ()) -> dict:
    """Soma os valores de uma apuração (E110, M200, M600) aos dos períodos anteriores do mesmo arquivo ou mês.

    Saldos de transporte não se somam: o saldo anterior é o do primeiro período e o saldo a transportar, o do último.
    """
    if total is None:
        return dict(period)
    return {key: total[key] if key in first_only else value if key in last_only else round(total[key] + value, 2) for key, value in period.items()}


def merge(summaries: list[dict]) -> dict | None:
    """Combina os arquivos de uma competência (do mais antigo ao mais recente).

    Cada seção vem do arquivo mais recente que a contém; assim um arquivo
    complementar (ex.: só o bloco H) não duplica o movimento do arquivo principal.
    """
    if not summaries:
        return None
    merged: dict = {"tipo": summaries[-1]["tipo"]}
    for key in ("operacoes", "apuracao", "inventario"):
        merged[key] = next((summary.get(key) for summary in reversed(summaries) if summary.get(key)), None)
    return merged


def headline(summary: dict) -> dict[str, float]:
    """Números usados na série mensal (tendência entre competências)."""
    operations = summary.get("operacoes") or {}
    assessment = summary.get("apuracao") or {}
    if summary["tipo"] == "fiscal":
        icms = (assessment.get("icms") or {}) if assessment else {}
        return {"saidas": (operations.get("saidas") or {}).get("valor", 0.0), "entradas": (operations.get("entradas") or {}).get("valor", 0.0), "imposto": icms.get("recolher", 0.0)}
    pis, cofins = (assessment.get("pis") or {}), (assessment.get("cofins") or {})
    return {"saidas": operations.get("saidas", 0.0), "entradas": operations.get("entradas", 0.0), "imposto": pis.get("recolher", 0.0) + cofins.get("recolher", 0.0)}


# --------------------------------------------------------------------------- formatação

def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def money(value: float) -> str:
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + text


def compact(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"R$ {value / 1_000_000:.1f} mi".replace(".", ",")
    if abs(value) >= 1_000:
        return f"R$ {value / 1_000:.0f} mil"
    return f"R$ {value:.0f}"


def count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def percent(part: float, whole: float) -> str:
    return f"{(part / whole * 100 if whole else 0):.1f}%".replace(".", ",")


def br_date(iso: str | None) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""


def month_label(competence: str, short: bool = False) -> str:
    name = MONTHS[int(competence[5:7]) - 1]
    return f"{name[:3]}/{competence[2:4]}" if short else f"{name}/{competence[:4]}"


def short_labels(names: list[str]) -> list[str]:
    """Remove a parte da razão social repetida em todas as lojas: "EMPRESA LTDA - VR" vira "VR"."""
    if len(names) < 2:
        return names
    prefix = os.path.commonprefix(names)
    prefix = prefix[:prefix.rfind(" - ") + 3] if " - " in prefix else ""
    suffix = os.path.commonprefix([name[::-1] for name in names])[::-1]
    suffix = suffix[suffix.find(" - "):] if " - " in suffix else ""
    return [name[len(prefix):len(name) - len(suffix) if suffix else None].strip(" -") or name for name in names]


def width_class(value: float, maximum: float) -> str:
    if maximum <= 0 or value <= 0:
        return "w-0"
    step = max(2, round(value / maximum * 50) * 2)
    return f"w-{min(step, 100)}"


def cfop_label(code: str) -> str:
    if code == "Serviços":
        return "Serviços prestados"
    return f"{code[0]}.{code[1:]} · {CFOP_LABELS.get(code, 'CFOP ' + code)}" if len(code) == 4 else code


# --------------------------------------------------------------------------- componentes

def kpi(label: str, value: str, detail: str = "", swatch: str = "") -> str:
    mark = f"<i class='swatch {swatch}'></i>" if swatch else ""
    return f"<div class='kpi'><span class='kpi-label'>{mark}{h(label)}</span><strong class='kpi-value' title='{h(value)}'>{h(value)}</strong><span class='kpi-detail'>{h(detail)}</span></div>"


def legend(*items: tuple[str, str]) -> str:
    return "<div class='legend'>" + "".join(f"<span><i class='swatch {css}'></i>{h(label)}</span>" for label, css in items) + "</div>"


def bar_list(rows: list[tuple[str, float, str]], detail: str = "") -> str:
    """Barras horizontais: (rótulo, valor, classe da série). O rótulo e o valor ficam em texto."""
    rows = [row for row in rows if row[1] > 0]
    if not rows:
        return "<p class='viz-empty'>Sem valores neste período.</p>"
    maximum = max(value for _, value, _ in rows)
    total = sum(value for _, value, _ in rows)
    body = "".join(
        f"<div class='bar-row' title='{h(label)}: {h(money(value))} ({h(percent(value, total))})'><div class='bar-text'><span>{h(label)}</span><strong>{h(money(value))}</strong></div><div class='bar-track'><span class='bar-fill {css} {width_class(value, maximum)}'></span></div></div>"
        for label, value, css in rows
    )
    return f"<div class='bar-list'>{body}</div>" + (f"<p class='viz-foot'>{h(detail)}</p>" if detail else "")


def data_table(headers: tuple[str, ...], rows: list[tuple]) -> str:
    head = "".join(f"<th>{h(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(f"<td>{h(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<details class='viz-table'><summary>Ver dados em tabela</summary><div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></details>"


def column_path(x: float, y: float, width: float, height: float) -> str:
    """Coluna com topo arredondado (4px) e base reta, crescendo da linha de base."""
    if height <= 0:
        return ""
    radius = min(4.0, width / 2, height)
    return (f"M{x:.1f},{y + height:.1f}V{y + radius:.1f}Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f}"
            f"H{x + width - radius:.1f}Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + radius:.1f}V{y + height:.1f}Z")


def nice_ticks(maximum: float) -> list[float]:
    """Marcações redondas do eixo (0, 25 mil, 50 mil...) cobrindo o maior valor em até 5 passos."""
    if maximum <= 0:
        return [0.0]
    magnitude = 10 ** math.floor(math.log10(maximum / 4))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude * 4 >= maximum)
    return [step * i for i in range(math.ceil(maximum / step) + 1)]


def paired_columns(groups: list[tuple[str, str, float, float]], axis_every: int = 1) -> str:
    """Colunas pareadas (saídas × entradas). groups: (rótulo do eixo, rótulo do tooltip, saídas, entradas)."""
    width, height, left, right, top, bottom = 900, 240, 64, 10, 18, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    maximum = max([max(s, e) for _, _, s, e in groups] + [0.0])
    ticks = nice_ticks(maximum)
    scale_max = max(ticks[-1], maximum) or 1.0
    band = plot_w / max(len(groups), 1)
    bar = min(24.0, max(3.0, band * 0.36))
    parts = [f"<svg class='viz-svg' viewBox='0 0 {width} {height}' role='img' aria-label='Saídas e entradas por período'>"]
    for tick in ticks:
        y = top + plot_h - tick / scale_max * plot_h
        parts.append(f"<line class='viz-gridline' x1='{left}' x2='{width - right}' y1='{y:.1f}' y2='{y:.1f}'/><text class='viz-axis' x='{left - 8}' y='{y + 3:.1f}' text-anchor='end'>{h(compact(tick) if tick else 'R$ 0')}</text>")
    peak_index = max(range(len(groups)), key=lambda i: groups[i][2]) if groups and maximum > 0 else -1
    for index, (axis_label, tip_label, outgoing, incoming) in enumerate(groups):
        x0 = left + index * band
        center = x0 + band / 2
        heights = [value / scale_max * plot_h for value in (outgoing, incoming)]
        tip = f"{tip_label} · Saídas {money(outgoing)} · Entradas {money(incoming)}"
        parts.append(f"<g class='viz-group'><title>{h(tip)}</title><rect class='viz-hit' x='{x0:.1f}' y='{top}' width='{band:.1f}' height='{plot_h}'/>")
        for offset, value_h, css in ((-bar - 1, heights[0], "fill-saida"), (1, heights[1], "fill-entrada")):
            path = column_path(center + offset, top + plot_h - value_h, bar, value_h)
            if path:
                parts.append(f"<path class='{css}' d='{path}'/>")
        parts.append("</g>")
        if index % axis_every == 0 or index == len(groups) - 1:
            parts.append(f"<text class='viz-axis' x='{center:.1f}' y='{height - 9}' text-anchor='middle'>{h(axis_label)}</text>")
        if index == peak_index:
            label_x = min(max(center - bar / 2, left + 30), width - right - 30)
            parts.append(f"<text class='viz-label' x='{label_x:.1f}' y='{top + plot_h - max(heights) - 6:.1f}' text-anchor='middle'>{h(compact(outgoing))}</text>")
    parts.append(f"<line class='viz-baseline' x1='{left}' x2='{width - right}' y1='{top + plot_h}' y2='{top + plot_h}'/></svg>")
    return "<div class='viz-scroll'>" + "".join(parts) + "</div>"


def daily_chart(competence: str, daily: dict[str, list[float]]) -> str:
    first = date(int(competence[:4]), int(competence[5:7]), 1)
    days = []
    current = first
    while current.month == first.month:
        values = daily.get(current.isoformat(), [0.0, 0.0])
        days.append((str(current.day), br_date(current.isoformat()), values[0], values[1]))
        current += timedelta(days=1)
    return paired_columns(days, axis_every=3)


def insight_list(items: list[str]) -> str:
    if not items:
        return ""
    return "<div class='insights'><span class='section-kicker'>O QUE CHAMA ATENÇÃO</span><ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul></div>"


def b(value: str) -> str:
    return f"<strong>{h(value)}</strong>"


# --------------------------------------------------------------------------- painéis

MAX_INSIGHTS = 4
EMPTY_TAX = {"apurada": 0.0, "creditos": 0.0, "deducoes": 0.0, "recolher": 0.0, "nao_cumulativa": 0.0, "cumulativa": 0.0}
TAX_GROUP_LABELS = {
    "Tributadas": "Tributadas (ICMS normal)",
    "Substituição tributária": "Substituição tributária (ICMS pago na compra)",
    "Isentas ou não tributadas": "Isentas ou não tributadas",
    "Simples Nacional": "Simples Nacional",
    "Outras": "Outras",
}


def render(summary: dict | None, competence: str, history: list[tuple[str, dict]]) -> str:
    """Conteúdo do resumo do mês: indicadores, insights, gráficos principais e detalhes recolhidos."""
    if not summary or not (summary.get("operacoes") or summary.get("apuracao")):
        return "<div class='empty-state'><span class='empty-mark'>—</span><strong>Resumo indisponível</strong><span>O arquivo deste mês não traz registros de movimento ou de apuração.</span></div>"
    body = render_fiscal(summary, competence, history) if summary["tipo"] == "fiscal" else render_contributions(summary, competence, history)
    return body + "<p class='summary-note'>Resumo gerencial calculado a partir do arquivo SPED. Não substitui a apuração oficial nem a validação do PVA.</p>"


def panel(title: str, subtitle: str, body: str, wide: bool = False) -> str:
    return f"<div class='viz-panel{' viz-wide' if wide else ''}'><h3>{h(title)}</h3><p class='viz-sub'>{h(subtitle)}</p>{body}</div>"


def layout(tiles: str, insights: list[str], main_panels: list[str], extra_panels: list[str], extra_hint: str) -> str:
    content = tiles + insight_list(insights[:MAX_INSIGHTS]) + "<div class='viz-grid'>" + "".join(main_panels) + "</div>"
    if extra_panels:
        content += (f"<details class='more-details'><summary><span class='more-title'>Ver mais detalhes</span><span class='more-hint'>{h(extra_hint)}</span></summary>"
                    "<div class='viz-grid'>" + "".join(extra_panels) + "</div></details>")
    return content


def previous_change(history: list[tuple[str, dict]], competence: str, key: str) -> tuple[str, float] | None:
    earlier = [(comp, values) for comp, values in history if comp < competence]
    current = next((values for comp, values in history if comp == competence), None)
    if not earlier or not current or not earlier[-1][1].get(key):
        return None
    comp, values = earlier[-1]
    return comp, (current[key] - values[key]) / values[key] * 100


def trend_panel(history: list[tuple[str, dict]], tax_label: str) -> str:
    if len(history) < 2:
        return ""
    groups = [(month_label(comp, True), month_label(comp), values["saidas"], values["entradas"]) for comp, values in history]
    rows = [(month_label(comp), money(values["saidas"]), money(values["entradas"]), money(values["imposto"])) for comp, values in history]
    body = legend(("Saídas", "fill-saida"), ("Entradas", "fill-entrada")) + paired_columns(groups) + data_table(("Mês", "Saídas", "Entradas", tax_label), rows)
    return panel("Evolução mês a mês", "Saídas e entradas de cada mês publicado", body, wide=True)


def render_fiscal(summary: dict, competence: str, history: list[tuple[str, dict]]) -> str:
    operations = summary.get("operacoes") or {}
    assessment = summary.get("apuracao") or {}
    icms = assessment.get("icms") or {}
    inventory = summary.get("inventario")
    empty_side = {"valor": 0.0, "icms": 0.0, "icms_st": 0.0, "documentos": 0}
    outgoing = operations.get("saidas") or empty_side
    incoming = operations.get("entradas") or empty_side
    obligations = assessment.get("obrigacoes") or []
    due = next((when for _, when in sorted(obligations, key=lambda item: item[1] or "")), None)
    st_due = assessment.get("icms_st_recolher") or 0.0

    if icms.get("recolher", 0) > 0 or not icms.get("saldo_credor"):
        icms_tile = kpi("ICMS a pagar", money(icms.get("recolher", 0.0)), f"Vence em {br_date(due)}" if due else "Apurado no mês")
    else:
        icms_tile = kpi("Saldo credor de ICMS", money(icms["saldo_credor"]), "Fica para abater no próximo mês")
    if inventory:
        fourth = kpi("Estoque declarado", money(inventory["valor"]), f"Inventário em {br_date(inventory['data'])}")
    else:
        fourth = kpi("ICMS-ST a pagar", money(st_due), "Substituição tributária")
    tiles = ("<div class='kpi-grid'>"
             + kpi("Vendas e saídas", money(outgoing["valor"]), f"{count(outgoing['documentos'])} notas e cupons", "fill-saida")
             + kpi("Compras e entradas", money(incoming["valor"]), f"{count(incoming['documentos'])} notas · {percent(incoming['valor'], outgoing['valor'])} das vendas", "fill-entrada")
             + icms_tile + fourth + "</div>")

    insights = []
    if icms.get("recolher", 0) > 0:
        text = f"O ICMS a pagar é {b(money(icms['recolher']))}"
        text += f", com vencimento em {b(br_date(due))}" if due else ""
        text += f": {b(percent(icms['recolher'], outgoing['valor']))} do valor vendido." if outgoing["valor"] else "."
        insights.append(text)
    elif icms.get("saldo_credor", 0) > 0:
        insights.append(f"As compras geraram mais crédito do que as vendas geraram de imposto. Sobra {b(money(icms['saldo_credor']))} para abater no próximo mês.")
    if outgoing["valor"]:
        text = f"Para cada R$ 100 vendidos, a loja comprou {b(money(incoming['valor'] / outgoing['valor'] * 100))}."
        if incoming["valor"] > outgoing["valor"]:
            text += " As compras superaram as vendas: vale acompanhar o estoque."
        insights.append(text)
    groups = operations.get("tributacao_saidas") or {}
    st_value = groups.get("Substituição tributária", 0.0)
    if outgoing["valor"] and st_value:
        insights.append(f"{b(percent(st_value, outgoing['valor']))} das vendas foram de produtos com ICMS já pago na compra (substituição tributária).")
    change = previous_change(history, competence, "saidas")
    if change:
        insights.append(f"As vendas {'cresceram' if change[1] >= 0 else 'caíram'} {b(f'{abs(change[1]):.1f}%'.replace('.', ','))} em relação a {h(month_label(change[0]))}.")
    suppliers = operations.get("fornecedores") or []
    if suppliers and incoming["valor"]:
        name, value = suppliers[0]
        insights.append(f"Maior fornecedor: {b(name)}, com {b(percent(value, incoming['valor']))} das compras.")
    daily = operations.get("diario") or {}
    sales_days = {day: values[0] for day, values in daily.items() if values[0] > 0}
    if sales_days:
        peak = max(sales_days, key=sales_days.get)
        insights.append(f"O dia de maior venda foi {b(br_date(peak))}, com {b(money(sales_days[peak]))}.")

    main_panels = []
    if daily:
        foot = ""
        if operations.get("cfop_x929"):
            foot = f"<p class='viz-foot'>Notas com CFOP x.929 ({h(money(operations['cfop_x929']))}) ficam fora da soma, pois repetem vendas já registradas em cupom ou NFC-e.</p>"
        body = (legend(("Saídas", "fill-saida"), ("Entradas", "fill-entrada")) + daily_chart(competence, daily) + foot
                + data_table(("Dia", "Saídas", "Entradas"), [(br_date(day), money(s), money(e)) for day, (s, e) in daily.items()]))
        main_panels.append(panel("Movimento do mês, dia a dia", "Passe o mouse sobre um dia para ver os valores", body, wide=True))
    if icms:
        rows = [("Gerado nas vendas (débitos)", icms["debitos"] + icms["ajustes_debito"] + icms["estornos_credito"], "fill-saida"),
                ("Abatido pelas compras (créditos)", icms["creditos"] + icms["ajustes_credito"] + icms["estornos_debito"], "fill-entrada"),
                ("Crédito que sobrou do mês anterior", icms["saldo_credor_anterior"], "fill-entrada"),
                ("Deduções", icms["deducoes"], "fill-entrada"),
                ("ICMS a pagar", icms["recolher"], "fill-neutro"),
                ("Crédito para o próximo mês", icms["saldo_credor"], "fill-neutro")]
        if st_due:
            rows.append(("ICMS-ST a pagar", st_due, "fill-neutro"))
        main_panels.append(panel("Como o ICMS foi calculado", "Imposto das vendas, menos o crédito das compras", bar_list(rows)))
    if groups:
        rows = [(TAX_GROUP_LABELS[group], groups.get(group, 0.0), "fill-saida") for group in TAX_GROUPS]
        main_panels.append(panel("Como as vendas foram tributadas", "Valor vendido por tipo de tributação do ICMS", bar_list(rows)))
    trend = trend_panel(history, "ICMS a pagar")
    if trend:
        main_panels.append(trend)

    extra_panels = []
    cfops = operations.get("cfops") or {}
    if cfops:
        top_out = sorted(((code, values[0]) for code, values in cfops.items() if code[:1] in "567"), key=lambda item: item[1], reverse=True)[:5]
        top_in = sorted(((code, values[0]) for code, values in cfops.items() if code[:1] in "123"), key=lambda item: item[1], reverse=True)[:5]
        body = ""
        if top_out:
            body += "<span class='viz-group-label'>Saídas</span>" + bar_list([(cfop_label(code), value, "fill-saida") for code, value in top_out])
        if top_in:
            body += "<span class='viz-group-label'>Entradas</span>" + bar_list([(cfop_label(code), value, "fill-entrada") for code, value in top_in])
        body += data_table(("CFOP", "Descrição", "Valor", "ICMS"), [(code, CFOP_LABELS.get(code, ""), money(values[0]), money(values[1])) for code, values in sorted(cfops.items())])
        extra_panels.append(panel("Tipos de operação (CFOP)", "As naturezas de operação com maior valor", body))
    if suppliers:
        detail = f"{operations.get('fornecedores_qtd', len(suppliers))} fornecedores no mês."
        extra_panels.append(panel("Maiores fornecedores", "Valor comprado de cada fornecedor", bar_list([(name, value, "fill-entrada") for name, value in suppliers[:5]], detail)))
    return layout(tiles, insights, main_panels, extra_panels, "Tipos de operação (CFOP) e maiores fornecedores")


def render_contributions(summary: dict, competence: str, history: list[tuple[str, dict]]) -> str:
    operations = summary.get("operacoes") or {}
    assessment = summary.get("apuracao") or {}
    pis = assessment.get("pis") or EMPTY_TAX
    cofins = assessment.get("cofins") or EMPTY_TAX
    exempt = assessment.get("receita_sem_incidencia") or {}
    taxed = assessment.get("receita_tributada", 0.0)
    exempt_total = sum(exempt.values())
    revenue = taxed + exempt_total
    total_due = pis["recolher"] + cofins["recolher"]
    establishments = operations.get("estabelecimentos") or []
    main_cst = max(exempt, key=exempt.get) if exempt else None

    tiles = ("<div class='kpi-grid'>"
             + kpi("Faturamento bruto", money(revenue), "Receita declarada no mês")
             + kpi("PIS a pagar", money(pis["recolher"]), "Apurado no mês")
             + kpi("COFINS a pagar", money(cofins["recolher"]), "Apurado no mês")
             + kpi("Receita sem PIS/COFINS", percent(exempt_total, revenue), f"{money(exempt_total)} do faturamento")
             + "</div>")

    insights = []
    if revenue:
        text = f"{b(percent(exempt_total, revenue))} do faturamento não paga PIS/COFINS"
        text += f", principalmente por ser {h(PIS_CST_LABELS.get(main_cst, 'CST ' + main_cst).lower())}." if main_cst else "."
        insights.append(text)
        insights.append(f"PIS e COFINS somam {b(money(total_due))} a pagar: {b(percent(total_due, revenue))} do faturamento.")
    change = previous_change(history, competence, "imposto")
    if change:
        insights.append(f"O PIS/COFINS a pagar {'subiu' if change[1] >= 0 else 'caiu'} {b(f'{abs(change[1]):.1f}%'.replace('.', ','))} em relação a {h(month_label(change[0]))}.")
    if pis["cumulativa"] and not pis["nao_cumulativa"]:
        insights.append("A empresa está no regime cumulativo: alíquotas de 0,65% (PIS) e 3% (COFINS), sem créditos sobre compras.")
    elif pis["creditos"] or cofins["creditos"]:
        insights.append(f"Os créditos sobre compras reduziram o PIS/COFINS em {b(money(pis['creditos'] + cofins['creditos']))}.")
    if len(establishments) > 1:
        top = max(establishments, key=lambda item: item[2])
        insights.append(f"Este arquivo reúne {b(str(len(establishments)))} lojas. A que mais vendeu foi {b(top[1] or top[0])}.")

    main_panels = []
    if revenue:
        rows = [("Receita tributada", taxed, "fill-saida")] + [(f"{PIS_CST_LABELS.get(cst, 'Outras')} (CST {cst})", value, "fill-neutro") for cst, value in sorted(exempt.items(), key=lambda item: item[1], reverse=True)]
        main_panels.append(panel("De onde veio o faturamento", "Receita que paga PIS/COFINS e receita que não paga", bar_list(rows)))
    if assessment:
        body = ""
        for name, values in (("PIS", pis), ("COFINS", cofins)):
            body += f"<span class='viz-group-label'>{name}</span>" + bar_list([
                ("Contribuição do mês", values["apurada"], "fill-saida"), ("Créditos abatidos", values["creditos"], "fill-entrada"),
                ("Retenções e deduções", values["deducoes"], "fill-entrada"), ("A pagar", values["recolher"], "fill-neutro")])
        main_panels.append(panel("Como o PIS e a COFINS foram calculados", "Contribuição do mês, abatimentos e valor a pagar", body))
    if len(establishments) > 1:
        labels = short_labels([name or cnpj for cnpj, name, _, _ in establishments])
        groups = [(label[:16], f"{name or cnpj} ({cnpj})", s, e) for label, (cnpj, name, s, e) in zip(labels, establishments)]
        body = (legend(("Saídas", "fill-saida"), ("Entradas", "fill-entrada")) + paired_columns(groups)
                + data_table(("Loja", "CNPJ", "Saídas", "Entradas"), [(name, cnpj, money(s), money(e)) for cnpj, name, s, e in establishments]))
        main_panels.append(panel("Movimento de cada loja", "Saídas e entradas de cada CNPJ deste arquivo", body, wide=True))
    trend = trend_panel(history, "PIS/COFINS a pagar")
    if trend:
        main_panels.append(trend)

    extra_panels = []
    cfops = operations.get("cfops_saida") or {}
    if cfops:
        top = sorted(cfops.items(), key=lambda item: item[1], reverse=True)[:6]
        extra_panels.append(panel("Vendas por tipo de operação (CFOP)", "Valor vendido, já descontado", bar_list([(cfop_label(code), value, "fill-saida") for code, value in top])))
    return layout(tiles, insights, main_panels, extra_panels, "Vendas por tipo de operação (CFOP)")

