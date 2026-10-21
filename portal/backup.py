"""Cópia de segurança do portal: banco, documentos publicados e segredo local.

Cada backup é uma pasta portal-AAAA-MM-DD_HHMMSS com o mesmo formato de data/ (portal.sqlite3, documentos/ e
.secret). Para restaurar, basta apontar PORTAL_DATA_DIR para uma cópia dessa pasta.

O banco é copiado com a API de backup do SQLite, que é segura com o portal rodando (copiar o arquivo direto
pode pegar uma gravação pela metade). O destino precisa ficar fora de data/: se a pasta de dados se perder,
o backup não pode ir junto.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from portal import config
from portal import db as storage

BACKUP_NAME = re.compile(r"portal-(\d{4}-\d{2}-\d{2}_\d{6})")


class BackupError(RuntimeError):
    """Backup não pôde ser feito ou não passou na conferência."""


def run_backup(destination: Path | None = None, keep_days: int | None = None, now: datetime | None = None) -> Path:
    """Faz o backup, confere a cópia e apaga os backups mais antigos que keep_days. Devolve a pasta criada."""
    destination = Path(destination or config.BACKUP_DIR).resolve()
    keep_days = config.BACKUP_KEEP_DAYS if keep_days is None else keep_days
    now = now or datetime.now()
    data_dir = storage.DATA_DIR.resolve()
    if destination == data_dir or data_dir in destination.parents:
        raise BackupError(f"O destino do backup ({destination}) não pode ficar dentro da pasta de dados ({data_dir}).")
    target = destination / f"portal-{now:%Y-%m-%d_%H%M%S}"
    partial = target.with_name(target.name + ".parcial")
    if target.exists():
        raise BackupError(f"Já existe um backup em {target}.")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        source = sqlite3.connect(storage.DATABASE, timeout=30)
        copy = sqlite3.connect(partial / "portal.sqlite3")
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        shutil.copytree(storage.DOCUMENTS, partial / "documentos")
        shutil.copy2(storage.DATA_DIR / ".secret", partial / ".secret")
        summary = verify(partial)
        (partial / "LEIA-ME.txt").write_text(
            f"Backup do Portal Fiscal {config.PORTAL_VERSION}\n"
            f"Feito em: {now:%d/%m/%Y %H:%M:%S}\n"
            f"Migração do banco: {summary['migracao']}\n"
            f"Documentos: {summary['documentos']} registros, {summary['arquivos']} arquivos\n\n"
            "Para restaurar: copie esta pasta para um lugar protegido e inicie o portal com\n"
            "PORTAL_DATA_DIR apontando para a cópia (veja o README, seção Backup).\n", encoding="utf-8")
        partial.rename(target)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    prune(destination, keep_days, now, keep=target)
    return target


def verify(folder: Path) -> dict[str, int]:
    """Confere um backup: banco íntegro e um arquivo para cada documento registrado."""
    connection = sqlite3.connect(folder / "portal.sqlite3")
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("A cópia do banco não passou na verificação de integridade.")
        ids = [row[0] for row in connection.execute("SELECT id FROM documents")]
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        version = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0] if exists else 0
    finally:
        connection.close()
    missing = [doc_id for doc_id in ids if not (folder / "documentos" / f"{doc_id}.txt").is_file()]
    if missing:
        raise BackupError(f"{len(missing)} documento(s) registrados no banco não estão na cópia (ex.: {missing[0]}).")
    if not (folder / ".secret").is_file():
        raise BackupError("O segredo local (.secret) não foi copiado.")
    return {"migracao": version, "documentos": len(ids), "arquivos": len(list((folder / "documentos").glob("*.txt")))}


def prune(destination: Path, keep_days: int, now: datetime, keep: Path) -> list[Path]:
    """Apaga backups do portal (só pastas portal-AAAA-MM-DD_HHMMSS) mais antigos que keep_days. O recém-feito fica sempre."""
    removed = []
    limit = now - timedelta(days=keep_days)
    for folder in destination.iterdir():
        match = BACKUP_NAME.fullmatch(folder.name)
        if not match or not folder.is_dir() or folder == keep:
            continue
        if datetime.strptime(match[1], "%Y-%m-%d_%H%M%S") < limit:
            shutil.rmtree(folder)
            removed.append(folder)
    return removed
