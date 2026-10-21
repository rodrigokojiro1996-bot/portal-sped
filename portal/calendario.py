"""Dias úteis e prazos de publicação.

Os meses aparecem no painel até config.FORECAST_UNTIL, e cada arquivo do mês tem prazo no N-ésimo dia útil
do mês seguinte, conforme o tamanho do grupo (lojas com a mesma raiz de CNPJ).
"""
from __future__ import annotations

from datetime import date, timedelta

from portal import config

SMALL_GROUP_UNITS = 3
SMALL_GROUP_BUSINESS_DAY = 3
LARGE_GROUP_BUSINESS_DAY = 5
LARGE_GROUP_NOTE = ("Por conta do número de lojas, a demanda requer mais tempo para conclusão e postagem. "
                    "Se algum arquivo precisar de correção, ele entra na fila e é postado após a conclusão das postagens obrigatórias e regulares.")


def easter(year: int) -> date:
    """Domingo de Páscoa (algoritmo gregoriano anônimo)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h_ = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h_ - k) % 7
    m = (a + 11 * h_ + 22 * l_) // 451
    month, day = divmod(h_ + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def non_business_days(year: int) -> set[date]:
    """Feriados nacionais, Carnaval e Sexta-feira Santa. Feriados estaduais e municipais não entram."""
    fixed = {date(year, month, day) for month, day in ((1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25))}
    sunday = easter(year)
    return fixed | {sunday - timedelta(days=48), sunday - timedelta(days=47), sunday - timedelta(days=2)}


def business_day(year: int, month: int, nth: int) -> date:
    holidays = non_business_days(year)
    day, found = date(year, month, 1), 0
    while True:
        if day.weekday() < 5 and day not in holidays:
            found += 1
            if found == nth:
                return day
        day += timedelta(days=1)


def next_competence(competence: str) -> str:
    year, month = int(competence[:4]), int(competence[5:7])
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def publication_deadline(competence: str, units: int) -> tuple[date, int]:
    """Prazo de publicação do SPED de um mês: 3º dia útil do mês seguinte para grupos de até 3 lojas, 5º para os maiores."""
    nth = SMALL_GROUP_BUSINESS_DAY if units <= SMALL_GROUP_UNITS else LARGE_GROUP_BUSINESS_DAY
    following = next_competence(competence)
    return business_day(int(following[:4]), int(following[5:7]), nth), nth


def competence_timeline(published: list[str], today: date | None = None) -> list[str]:
    """Meses exibidos no painel: dos publicados (ou do mês anterior) até o fim da previsão, no máximo 15."""
    today = today or date.today()
    previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month, end = min(published + [previous]), max(published + [config.FORECAST_UNTIL])
    months = []
    while month <= end:
        months.append(month)
        month = next_competence(month)
    return months[-15:]
