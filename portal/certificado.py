"""Entrada pelo certificado digital da loja (e-CNPJ da ICP-Brasil).

Quem abre o link do aviso prova que pertence à loja com o certificado: os 8 primeiros dígitos do CNPJ gravado nele
precisam ser os da loja, a mesma regra de matriz e filiais do resto do portal.

Responsabilidades:
    servidor HTTPS da frente   pede o certificado ao navegador, confere a cadeia ICP-Brasil e a revogação
                               e repassa o certificado ao portal;
    este módulo                lê o CNPJ e a validade do certificado repassado e decide qual loja abre.

Este módulo NÃO confere assinatura nem cadeia. Por isso o handler só aceita certificado que chega junto com o segredo
do servidor da frente (config.CERT_PROXY_SECRET): sem ele, bastaria inventar um cabeçalho para entrar como qualquer loja.
"""
from __future__ import annotations

import base64
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode

from portal.db import connect
from portal.sped import DOCUMENT_TYPES, normalize_cnpj, valid_cnpj

CERT_HEADER = "X-Client-Cert"
PROXY_HEADER = "X-Portal-Proxy"

SEQUENCE, UTC_TIME, GENERALIZED_TIME = 0x30, 0x17, 0x18
CONTEXT_0, CONTEXT_3 = 0xA0, 0xA3
OID_SUBJECT_ALT_NAME = bytes.fromhex("551D11")  # 2.5.29.17
OID_CNPJ = bytes.fromhex("604C010303")  # 2.16.76.1.3.3: CNPJ do titular, em 14 posições (DOC-ICP-04)

NO_CERTIFICATE = "Para abrir este link, o navegador precisa apresentar o certificado digital da loja."
UNREADABLE = "Não foi possível ler o certificado digital."
NO_CNPJ = "Este certificado não traz um CNPJ. Use o certificado digital da loja (e-CNPJ)."
EXPIRED = "Este certificado digital está vencido."
NOT_YET_VALID = "Este certificado digital ainda não está valendo."
NO_STORE = "Nenhuma loja ativa do portal pertence ao CNPJ deste certificado."
OTHER_STORE = "Este certificado não pertence à loja deste link. Use o certificado digital da loja."


class CertificateError(ValueError):
    """Certificado recusado, com a explicação mostrada a quem tentou entrar e o número de série, se deu para ler."""

    def __init__(self, message: str, serial: str | None = None) -> None:
        super().__init__(message)
        self.serial = serial


@dataclass(frozen=True)
class Certificate:
    cnpj: str
    serial: str
    not_before: datetime
    not_after: datetime


def _element(data: bytes, offset: int) -> tuple[int, int, int]:
    """Tag, início e fim do valor do elemento DER que começa em offset."""
    if offset + 2 > len(data):
        raise CertificateError(UNREADABLE)
    tag, first, start = data[offset], data[offset + 1], offset + 2
    length = first
    if first >= 0x80:
        size = first & 0x7F
        if not 1 <= size <= 4 or start + size > len(data):
            raise CertificateError(UNREADABLE)
        length = int.from_bytes(data[start:start + size], "big")
        start += size
    if start + length > len(data):
        raise CertificateError(UNREADABLE)
    return tag, start, start + length


def _children(data: bytes) -> list[tuple[int, bytes]]:
    """Elementos DER em sequência dentro de data, como pares (tag, valor)."""
    items, offset = [], 0
    while offset < len(data):
        tag, start, end = _element(data, offset)
        items.append((tag, data[start:end]))
        offset = end
    return items


def _moment(tag: int, value: bytes) -> datetime:
    text = value.decode("ascii")
    if tag == UTC_TIME:
        text = ("20" if int(text[:2]) < 50 else "19") + text  # regra do X.509 para o ano com dois dígitos
    elif tag != GENERALIZED_TIME:
        raise CertificateError(UNREADABLE)
    return datetime.strptime(text, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)


def _cnpj(extensions: bytes) -> str:
    """CNPJ do campo otherName 2.16.76.1.3.3 do subjectAltName. Vazio se o certificado não tiver esse campo (e-CPF)."""
    if not extensions:
        return ""
    for _, extension in _children(_children(extensions)[0][1]):
        fields = _children(extension)
        if fields[0][1] != OID_SUBJECT_ALT_NAME:
            continue
        for tag, name in _children(_children(fields[-1][1])[0][1]):
            if tag != CONTEXT_0:
                continue
            kind, value = _children(name)
            if kind[1] == OID_CNPJ:
                return normalize_cnpj(_children(value[1])[0][1].decode("latin-1"))
    return ""


def parse_der(der: bytes) -> Certificate:
    """CNPJ e validade de um certificado X.509 em DER, sem conferir assinatura (veja o início do módulo)."""
    try:
        [(tag, body)] = _children(der)
        if tag != SEQUENCE:
            raise CertificateError(UNREADABLE)
        tbs = _children(_children(body)[0][1])
        first = 1 if tbs[0][0] == CONTEXT_0 else 0  # a versão é opcional
        serial = tbs[first][1].lstrip(b"\x00").hex().upper() or "00"  # sem o byte de sinal, como o openssl e o nginx mostram
        validity = _children(tbs[first + 3][1])
        not_before, not_after = _moment(*validity[0]), _moment(*validity[1])
        cnpj = _cnpj(next((value for tag, value in tbs if tag == CONTEXT_3), b""))
    except CertificateError:
        raise
    except (IndexError, ValueError) as error:
        raise CertificateError(UNREADABLE) from error
    if not cnpj:
        raise CertificateError(NO_CNPJ, serial)
    if not valid_cnpj(cnpj):
        raise CertificateError(UNREADABLE, serial)
    return Certificate(cnpj, serial, not_before, not_after)


def _decode(text: str) -> bytes:
    """DER a partir de um PEM ou do base64 sozinho. Numa cadeia, vale o primeiro: o certificado do próprio titular."""
    match = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.DOTALL)
    body = match[1] if match else text.split(",")[0]
    try:
        return base64.b64decode(re.sub(r"\s", "", body), validate=True)
    except ValueError as error:
        raise CertificateError(UNREADABLE) from error


def check_validity(certificate: Certificate, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if now < certificate.not_before:
        raise CertificateError(NOT_YET_VALID)
    if now > certificate.not_after:
        raise CertificateError(EXPIRED)


def read_header(value: str) -> Certificate:
    """Certificado repassado pelo servidor da frente, em PEM com escape de URL ou em base64.

    Não confere a validade: quem chama confere depois, para o registro saber de quem era um certificado vencido.
    """
    if not value:
        raise CertificateError(NO_CERTIFICATE)
    return parse_der(_decode(unquote(value)))


def read_file(path: Path) -> Certificate:
    """Certificado de um arquivo .cer ou .pem, sem conferir a validade. Só a parte pública: nunca use o .pfx."""
    content = path.read_bytes()
    return parse_der(_decode(content.decode("ascii", "replace")) if b"-----BEGIN" in content else content)


def stores_of(cnpj: str) -> list[sqlite3.Row]:
    """Lojas ativas da raiz do CNPJ: primeiro a do próprio CNPJ, depois a matriz e as filiais."""
    with connect() as db:
        return db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 AND substr(cnpj,1,8)=? "
                          "ORDER BY CASE WHEN cnpj=? THEN 0 WHEN substr(cnpj,9,4)='0001' THEN 1 ELSE 2 END, name", (cnpj[:8], cnpj)).fetchall()


def session_store(cnpj: str, requested: str = "") -> sqlite3.Row:
    """Loja dona da sessão. Com link de uma loja, a loja precisa ser da mesma raiz do certificado."""
    stores = stores_of(cnpj)
    if not stores:
        raise CertificateError(NO_STORE)
    if requested and requested not in {str(store["id"]) for store in stores}:
        raise CertificateError(OTHER_STORE)
    return stores[0]


def destination(store_id: str, document_type: str, competence: str) -> str:
    """Página que o link pediu. Montada só com valores conferidos, para o link não poder levar a outro endereço."""
    params = {"store_id": store_id} if store_id else {}
    if document_type in DOCUMENT_TYPES:
        params["type"] = document_type
    if re.fullmatch(r"\d{4}-\d{2}", competence):
        params["competence"] = competence
    return "/dashboard" + ("?" + urlencode(params) if params else "")
