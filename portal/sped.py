"""Leitura do registro 0000 do SPED EFD: tipo do arquivo, CNPJ, período e competência.

Funções puras, sem banco: recebem o conteúdo do arquivo e devolvem o que o portal precisa para publicá-lo.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# Tipos de SPED: rótulo, o que o arquivo apura e a letra do ícone da aba.
DOCUMENT_TYPES = {
    "fiscal": ("SPED Fiscal", "ICMS e IPI", "F"),
    "contribuicoes": ("SPED Contribuições", "PIS e COFINS", "C"),
}

# Posições de DT_INI, DT_FIN, NOME e CNPJ no registro 0000 de cada leiaute. O Contribuições traz
# TIPO_ESCRIT, IND_SIT_ESP e NUM_REC_ANTERIOR antes do período, por isso tudo anda duas casas.
SPED_LAYOUTS = {"fiscal": (4, 5, 6, 7), "contribuicoes": (6, 7, 8, 9)}


class PublishError(ValueError):
    """Arquivo recusado na publicação, com a explicação mostrada à administração."""


def sped_label(document_type: str) -> str:
    """Nome completo do tipo, como aparece nas mensagens: "SPED Fiscal (ICMS e IPI)"."""
    label, subject, _ = DOCUMENT_TYPES[document_type]
    return f"{label} ({subject})"


def normalize_cnpj(value: str) -> str:
    return re.sub(r"\D", "", value)


def valid_cnpj(value: str) -> bool:
    digits = normalize_cnpj(value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    for length, weights in ((12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
                            (13, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))):
        remainder = sum(int(digit) * weight for digit, weight in zip(digits[:length], weights)) % 11
        check_digit = 0 if remainder < 2 else 11 - remainder
        if int(digits[length]) != check_digit:
            return False
    return True


def format_cnpj(value: str | None) -> str:
    digits = normalize_cnpj(value or "")
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}" if len(digits) == 14 else (value or "")


def detect_sped_type(content: bytes) -> str:
    """Identifica pelo registro 0000 se o arquivo é SPED Fiscal (ICMS/IPI) ou SPED Contribuições (PIS/COFINS).

    Só um dos leiautes tem datas DDMMAAAA e CNPJ de 14 dígitos nas posições esperadas: no Fiscal, o campo 4
    é a data inicial; no Contribuições, é o indicador de situação especial (vazio ou um dígito).
    """
    text = content.decode("utf-8-sig", errors="replace")
    for line in text.splitlines()[:500]:
        fields = line.strip().split("|")
        if len(fields) > 2 and fields[1] == "0000":
            matches = [kind for kind, (start, end, _name, cnpj) in SPED_LAYOUTS.items()
                       if len(fields) > cnpj and re.fullmatch(r"\d{8}", fields[start]) and re.fullmatch(r"\d{8}", fields[end]) and re.fullmatch(r"\d{14}", fields[cnpj])]
            if len(matches) == 1:
                return matches[0]
            break
    raise ValueError("O arquivo não foi reconhecido como SPED Fiscal (ICMS/IPI) nem como SPED Contribuições (PIS/COFINS).")


def parse_sped_header(content: bytes, document_type: str = "fiscal") -> dict[str, str]:
    if document_type not in SPED_LAYOUTS:
        raise ValueError("Tipo de SPED não reconhecido.")
    date_start_index, date_end_index, name_index, cnpj_index = SPED_LAYOUTS[document_type]
    text = content.decode("utf-8-sig", errors="replace")
    for line in text.splitlines()[:500]:
        fields = line.strip().split("|")
        if len(fields) > cnpj_index and fields[1] == "0000":
            cnpj = normalize_cnpj(fields[cnpj_index])
            if not valid_cnpj(cnpj):
                raise ValueError("O registro 0000 contém um CNPJ inválido.")
            try:
                start = datetime.strptime(fields[date_start_index], "%d%m%Y").date()
                end = datetime.strptime(fields[date_end_index], "%d%m%Y").date()
            except ValueError as error:
                raise ValueError("As datas do registro 0000 não são válidas.") from error
            if start > end or start.year != end.year or start.month != end.month:
                raise ValueError("O período informado no registro 0000 não corresponde a um único mês.")
            last_day = (date_after_month(start) - timedelta(days=1)).day
            if start.day != 1 or end.day != last_day:
                raise ValueError("O SPED não cobre o mês-calendário completo.")
            return {"cnpj": cnpj, "competence": start.strftime("%Y-%m"), "company": fields[name_index].strip()}
    raise ValueError("Não foi encontrado um registro 0000 válido no início do arquivo.")


def date_after_month(day: date) -> date:
    """Primeiro dia do mês seguinte ao de `day`."""
    return day.replace(year=day.year + (day.month == 12), month=day.month % 12 + 1, day=1)
