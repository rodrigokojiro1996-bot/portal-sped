"""Contatos que recebem o aviso de publicação, importados de uma planilha .xlsx.

A planilha tem uma linha por loja. Entram só as lojas marcadas com SIM em "SPED SPA" e com e-mail em "CONTATO".
Esse campo é texto livre ("manda para a@x com copia para b@y"), por isso os endereços são separados aqui em
destinatários e cópias. O .xlsx é lido só com a biblioteca padrão, como todo o portal.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from portal.db import connect
from portal.sped import normalize_cnpj, valid_cnpj

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
COPY_MARK = re.compile(r"com\s+c[oó]pia\s+para", re.IGNORECASE)
REQUIRED_COLUMNS = ("CNPJ", "SPED SPA", "CONTATO")
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class ContactFileError(ValueError):
    """Planilha que não dá para ler, com a explicação mostrada a quem está importando."""


@dataclass
class ContactImport:
    """O que a planilha trouxe: os contatos prontos e os casos que ficaram de fora."""
    contacts: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    rows: int = 0
    closed: list[str] = field(default_factory=list)
    without_email: list[str] = field(default_factory=list)
    invalid_cnpj: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    repeated: list[str] = field(default_factory=list)


def unique_emails(text: str) -> list[str]:
    """Endereços de um texto, em minúsculas e sem repetir, na ordem em que aparecem."""
    return list(dict.fromkeys(address.lower() for address in EMAIL.findall(text)))


def split_contact(text: str) -> tuple[list[str], list[str]]:
    """Separa o texto do contato em destinatários e cópias. Só "com cópia para" indica cópia."""
    main, *rest = COPY_MARK.split(text, maxsplit=1)
    recipients = unique_emails(main)
    copies = [address for address in unique_emails(rest[0]) if address not in recipients] if rest else []
    return (recipients, copies) if recipients else (copies, [])


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind, value = cell.get("t"), cell.find(SHEET + "v")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(SHEET + "t")).strip()
    if value is None or value.text is None:
        return ""
    return (shared[int(value.text)] if kind == "s" else value.text).strip()


def _column_index(reference: str) -> int:
    index = 0
    for letter in re.match(r"[A-Z]+", reference)[0]:
        index = index * 26 + ord(letter) - 64
    return index - 1


def read_xlsx(path: Path) -> list[dict[str, str]]:
    """Linhas preenchidas da primeira aba, como dicionários com os nomes da linha de cabeçalho."""
    try:
        with zipfile.ZipFile(path) as book:
            shared = []
            if "xl/sharedStrings.xml" in book.namelist():
                shared = ["".join(node.text or "" for node in item.iter(SHEET + "t")) for item in ET.fromstring(book.read("xl/sharedStrings.xml")).findall(SHEET + "si")]
            targets = {rel.get("Id"): rel.get("Target") for rel in ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))}
            first_sheet = ET.fromstring(book.read("xl/workbook.xml")).find(SHEET + "sheets")[0]
            target = targets[first_sheet.get(RELATIONSHIP_ID)].lstrip("/")
            sheet = ET.fromstring(book.read(target if target.startswith("xl/") else "xl/" + target))
    except (zipfile.BadZipFile, KeyError, IndexError, TypeError, ET.ParseError) as error:
        raise ContactFileError(f"{Path(path).name} não é uma planilha .xlsx válida.") from error
    table = [{_column_index(cell.get("r")): _cell_text(cell, shared) for cell in row.findall(SHEET + "c")} for row in sheet.iter(SHEET + "row")]
    table = [row for row in table if any(row.values())]
    if not table:
        raise ContactFileError("A planilha está vazia.")
    names = table[0]
    missing = [column for column in REQUIRED_COLUMNS if column not in names.values()]
    if missing:
        raise ContactFileError(f"Faltam colunas na planilha: {', '.join(missing)}.")
    return [{name: row.get(index, "") for index, name in names.items() if name} for row in table[1:]]


def select_contacts(rows: list[dict[str, str]]) -> ContactImport:
    """Escolhe as lojas que recebem o aviso. Nada é gravado aqui: quem importa confere o resultado antes."""
    result = ContactImport(rows=len(rows))
    occurrences = Counter(normalize_cnpj(row["CNPJ"]) for row in rows if row["CNPJ"])
    result.repeated = [cnpj for cnpj, total in occurrences.items() if total > 1]
    for row in rows:
        if not row["CNPJ"] or row["SPED SPA"].upper() != "SIM":
            continue
        cnpj, label = normalize_cnpj(row["CNPJ"]), row.get("Fantasia") or row["CNPJ"]
        recipients, copies = split_contact(row["CONTATO"])
        if any("loja fechada" in value.lower() for value in row.values()):
            result.closed.append(label)
        elif not valid_cnpj(cnpj):
            result.invalid_cnpj.append(label)
        elif not recipients:
            result.without_email.append(label)
        elif result.contacts.get(cnpj, (recipients, copies)) != (recipients, copies):
            result.conflicts.append(label)
        else:
            result.contacts[cnpj] = (recipients, copies)
    return result


def save_contacts(contacts: dict[str, tuple[list[str], list[str]]]) -> None:
    """Grava ou atualiza os contatos informados. Contatos que não estão na lista ficam como estão."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.executemany("INSERT INTO store_contacts (cnpj, recipients, copies, updated_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(cnpj) DO UPDATE SET recipients=excluded.recipients, copies=excluded.copies, updated_at=excluded.updated_at",
                       [(cnpj, ", ".join(recipients), ", ".join(copies), now) for cnpj, (recipients, copies) in contacts.items()])


def unregistered_stores(cnpjs: list[str]) -> list[str]:
    """CNPJs da lista sem loja ativa no portal: o contato fica guardado, mas só vale depois do cadastro da loja."""
    with connect() as db:
        registered = {row["cnpj"] for row in db.execute("SELECT cnpj FROM users WHERE role='store' AND active=1")}
    return [cnpj for cnpj in cnpjs if cnpj not in registered]
