"""Publicação de um SPED e resumo fiscal da competência.

A publicação lê tudo do próprio arquivo (tipo, loja pelo CNPJ e competência), grava o arquivo na pasta
privada e o registro no banco, e calcula o resumo fiscal, que fica guardado em document_summaries.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import resumo_sped
from portal import config
from portal import db as storage
from portal.db import connect
from portal.sped import PublishError, detect_sped_type, format_cnpj, parse_sped_header, period_label, sped_label


def publish_sped(content: bytes, file_name: str) -> dict[str, str | int]:
    """Publicação automática: o tipo, a loja (pelo CNPJ) e a competência vêm do registro 0000 do próprio arquivo."""
    if not content:
        raise PublishError("Escolha um arquivo .txt para publicar.")
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise PublishError("O arquivo passa do limite de 40 MB.")
    try:
        document_type = detect_sped_type(content)
        header = parse_sped_header(content, document_type)
    except ValueError as error:
        raise PublishError(str(error)) from error
    label = sped_label(document_type)
    with connect() as db:
        store = db.execute("SELECT id, cnpj, name FROM users WHERE role='store' AND active=1 AND cnpj=?", (header["cnpj"],)).fetchone()
        if not store:
            raise PublishError(f"O arquivo é um {label} do CNPJ {format_cnpj(header['cnpj'])} ({header['company']}), "
                               "e nenhuma loja ativa tem esse CNPJ. Cadastre a loja e envie o arquivo de novo.")
        doc_id = str(uuid.uuid4())
        filename = Path(file_name or "sped.txt").name[:150] or "sped.txt"
        document_path = storage.DOCUMENTS / (doc_id + ".txt")
        document_path.write_bytes(content)
        try:
            os.chmod(document_path, 0o600)
        except OSError:
            pass
        try:
            db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at, period_start, period_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (doc_id, store["id"], document_type, header["competence"], filename, len(content), hashlib.sha256(content).hexdigest(), datetime.now(timezone.utc).isoformat(),
                        header["period_start"], header["period_end"]))
        except sqlite3.IntegrityError as error:
            document_path.unlink(missing_ok=True)
            raise PublishError(f"Este arquivo já está publicado no {label} da loja {store['name']} (CNPJ {format_cnpj(store['cnpj'])}). Nada foi alterado.") from error
    with connect() as db:
        document = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    document_summary(document)
    return {"id": doc_id, "store_id": store["id"], "store_name": store["name"], "cnpj": store["cnpj"], "document_type": document_type, "competence": header["competence"],
            "period_start": header["period_start"], "period_end": header["period_end"], "partial": header["partial"]}


def document_summary(document: sqlite3.Row) -> dict | None:
    """Resumo agregado de um SPED publicado, calculado uma vez e guardado no banco."""
    with connect() as db:
        cached = db.execute("SELECT summary FROM document_summaries WHERE document_id=? AND version=?", (document["id"], resumo_sped.SUMMARY_VERSION)).fetchone()
    if cached:
        return json.loads(cached["summary"])
    path = storage.DOCUMENTS / (document["id"] + ".txt")
    if not path.is_file():
        return None
    try:
        summary = resumo_sped.summarize(path, document["document_type"])
    except (OSError, ValueError, IndexError):
        return None
    with connect() as db:
        db.execute("INSERT INTO document_summaries (document_id, version, summary, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(document_id) DO UPDATE SET version=excluded.version, summary=excluded.summary, created_at=excluded.created_at",
                   (document["id"], resumo_sped.SUMMARY_VERSION, json.dumps(summary, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    return summary


def is_partial(document: sqlite3.Row) -> bool:
    """Arquivo de parte do mês (ex.: quinzenal). Arquivos sem período gravado (anteriores à 1.6) contam como mês inteiro."""
    return bool(period_label(document["period_start"], document["period_end"]))


def partial_set(documents: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Arquivos parciais que formam o mês, sem sobreposição: o mais recente de cada período vale.

    Um reenvio corrigido da 1ª quinzena substitui o anterior em vez de somar duas vezes.
    """
    chosen: list[sqlite3.Row] = []
    for document in sorted((doc for doc in documents if is_partial(doc)), key=lambda doc: doc["uploaded_at"], reverse=True):
        if all(document["period_end"] < other["period_start"] or document["period_start"] > other["period_end"] for other in chosen):
            chosen.append(document)
    return sorted(chosen, key=lambda doc: doc["period_start"])


_combined_cache: dict[tuple, dict | None] = {}


def combined_summary(documents: list[sqlite3.Row]) -> dict | None:
    """Resumo de vários arquivos parciais do mesmo mês, somados como se fossem um arquivo só (em ordem de período)."""
    if len(documents) == 1:
        return document_summary(documents[0])
    key = (resumo_sped.SUMMARY_VERSION, *(doc["id"] for doc in documents))
    if key not in _combined_cache:
        lines: list[str] = []
        try:
            for document in documents:
                lines += resumo_sped.read_lines(storage.DOCUMENTS / (document["id"] + ".txt"))
            summary = resumo_sped.summarize_lines(lines, documents[0]["document_type"])
        except (OSError, ValueError, IndexError):
            summary = None
        if len(_combined_cache) > 256:
            _combined_cache.clear()
        _combined_cache[key] = summary
    return _combined_cache[key]


def competence_summary(documents: list[sqlite3.Row], competence: str) -> dict | None:
    """Resumo do mês. Arquivos do mês inteiro entram como antes (cada seção vem do mais recente que a tem);
    os parciais do mês são somados e entram como se fossem um arquivo, na posição do mais recente deles."""
    published = sorted((doc for doc in documents if doc["competence"] == competence), key=lambda doc: doc["uploaded_at"])
    entries = [(doc["uploaded_at"], document_summary(doc)) for doc in published if not is_partial(doc)]
    partial = partial_set(published)
    if partial:
        entries.append((max(doc["uploaded_at"] for doc in partial), combined_summary(partial)))
    entries.sort(key=lambda entry: entry[0])
    return resumo_sped.merge([summary for _, summary in entries if summary])
