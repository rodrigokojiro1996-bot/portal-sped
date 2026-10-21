"""Banco SQLite, pasta dos documentos privados e segredo local.

O estado de armazenamento (DATA_DIR, DATABASE, DOCUMENTS, APP_SECRET) mora neste módulo. Os outros módulos
o leem como db.DOCUMENTS etc., na hora do uso, para que configure_storage() (e os testes) possam trocar a pasta.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from portal import config

DATA_DIR = config.DATA_DIR
DATABASE = DATA_DIR / "portal.sqlite3"
DOCUMENTS = DATA_DIR / "documentos"
APP_SECRET: bytes | None = None

MIGRATIONS_DIR = config.ROOT / "migrations"
# Esquema da versão 1.5, a última sem migrações numeradas: equivale a 001 + 002.
PRE_MIGRATION_VERSION = 2

USERS_TABLE_SQL = """CREATE TABLE {name} (
    id INTEGER PRIMARY KEY,
    cnpj TEXT UNIQUE,
    login_name TEXT UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'support', 'store')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)"""


def use_paths(data_dir: Path) -> None:
    """Aponta banco, documentos e segredo para outra pasta, sem criar nem abrir nada."""
    global DATA_DIR, DATABASE, DOCUMENTS, APP_SECRET
    DATA_DIR = data_dir
    DATABASE = DATA_DIR / "portal.sqlite3"
    DOCUMENTS = DATA_DIR / "documentos"
    APP_SECRET = None


def configure_storage(data_dir: Path | None = None) -> None:
    global APP_SECRET
    if data_dir is not None:
        use_paths(data_dir)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(DOCUMENTS, 0o700)
    except OSError:
        pass
    secret_path = DATA_DIR / ".secret"
    if not secret_path.exists():
        secret_path.write_bytes(secrets.token_bytes(48))
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
    APP_SECRET = secret_path.read_bytes()
    migrate()


def ensure_secret() -> bytes:
    if APP_SECRET is None:
        configure_storage()
    assert APP_SECRET is not None
    return APP_SECRET


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def migration_files() -> list[tuple[int, str, Path]]:
    """Arquivos migrations/NNN_nome.sql, em ordem. A numeração precisa ser contínua a partir de 001."""
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.fullmatch(r"(\d{3})_([a-z0-9_]+)\.sql", path.name)
        if not match:
            raise RuntimeError(f"Nome de migração fora do padrão NNN_nome.sql: {path.name}")
        files.append((int(match[1]), match[2], path))
    if [number for number, _, _ in files] != list(range(1, len(files) + 1)):
        raise RuntimeError("As migrações precisam ser numeradas em sequência a partir de 001.")
    return files


def latest_version() -> int:
    return len(migration_files())


def schema_version() -> int:
    """Versão de migração do banco atual (0 se o banco não tiver a tabela schema_version)."""
    connection = sqlite3.connect(DATABASE, timeout=10)
    try:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        return connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0] if exists else 0
    finally:
        connection.close()


def migrate() -> list[int]:
    """Aplica, em ordem, as migrações com número maior que a versão do banco. Devolve as que aplicou.

    Cada migração roda numa transação, com as chaves estrangeiras desligadas (como o SQLite pede para recriar
    tabelas) e conferidas antes do COMMIT. Se algo falhar, a migração inteira é desfeita.
    """
    files = migration_files()
    connection = sqlite3.connect(DATABASE, timeout=10, isolation_level=None)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        has_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if current == 0 and has_tables:
            _adopt_pre_migration_database()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for number, name, _ in files[:PRE_MIGRATION_VERSION]:
                connection.execute("INSERT INTO schema_version VALUES (?, ?, ?)", (number, name, f"{now} (banco anterior às migrações)"))
            current = PRE_MIGRATION_VERSION
        applied = []
        for number, name, path in files:
            if number <= current:
                continue
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                script = path.read_text(encoding="utf-8")
                connection.executescript(f"BEGIN IMMEDIATE;\n{script}\nINSERT INTO schema_version VALUES ({number}, '{name}', '{now}');\n")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.IntegrityError(f"Referências inválidas depois da migração {path.name}.")
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            applied.append(number)
        return applied
    finally:
        connection.close()


# --------------------------------------------------------------------------- bancos anteriores às migrações

def _legacy_user_roles() -> None:
    """Bancos antigos só aceitavam os perfis admin e store: recria a tabela de usuários com o perfil support.

    Segue o procedimento do SQLite para alterar uma restrição (chaves estrangeiras desligadas durante a troca).
    """
    connection = sqlite3.connect(DATABASE, timeout=10, isolation_level=None)
    try:
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if row is None or "'support'" in row[0]:
            return
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(USERS_TABLE_SQL.format(name="users_migration"))
            connection.execute("INSERT INTO users_migration (id, cnpj, login_name, name, role, password_hash, active, created_at) "
                               "SELECT id, cnpj, login_name, name, role, password_hash, active, created_at FROM users")
            connection.execute("DROP TABLE users")
            connection.execute("ALTER TABLE users_migration RENAME TO users")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("Referências inválidas após migrar a tabela de usuários.")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
    finally:
        connection.close()


def _adopt_pre_migration_database() -> None:
    """Leva um banco criado antes das migrações numeradas (até a 1.5) ao esquema da 1.5.

    É o reparo que a 1.5 fazia a cada abertura; agora roda uma única vez, e o banco passa a
    ser controlado por schema_version a partir da versão PRE_MIGRATION_VERSION.
    """
    with connect() as db:
        db.execute(USERS_TABLE_SQL.format(name="IF NOT EXISTS users"))
    _legacy_user_roles()
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES users(id),
                document_type TEXT NOT NULL CHECK(document_type IN ('fiscal', 'contribuicoes')),
                competence TEXT NOT NULL,
                original_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                UNIQUE(store_id, document_type, sha256)
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                window_start INTEGER NOT NULL
            );
            """
        )
        document_columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
        unique_indexes = []
        for index in db.execute("PRAGMA index_list(documents)"):
            if index["unique"]:
                unique_indexes.append(tuple(row["name"] for row in db.execute(f"PRAGMA index_info('{index['name']}')")))
        must_migrate = "document_type" not in document_columns or ("store_id", "document_type", "competence") in unique_indexes
        if must_migrate:
            db.execute("ALTER TABLE documents RENAME TO documents_legacy")
            db.execute("""CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES users(id),
                document_type TEXT NOT NULL CHECK(document_type IN ('fiscal', 'contribuicoes')),
                competence TEXT NOT NULL,
                original_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                UNIQUE(store_id, document_type, sha256)
            )""")
            if "document_type" in document_columns:
                db.execute("""INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at)
                    SELECT id, store_id, document_type, competence, original_name, size, sha256, uploaded_at FROM documents_legacy""")
            else:
                db.execute("""INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at)
                    SELECT id, store_id, 'fiscal', competence, original_name, size, sha256, uploaded_at FROM documents_legacy""")
            db.execute("DROP TABLE documents_legacy")
        db.execute("""CREATE TABLE IF NOT EXISTS document_summaries (
            document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        # Monitoramento: cada ação dos perfis auditados. Nomes ficam copiados no registro
        # para o histórico continuar legível mesmo se uma loja ou usuário mudar depois.
        db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT NOT NULL,
            login_name TEXT,
            action TEXT NOT NULL,
            store_id INTEGER,
            store_name TEXT,
            store_cnpj TEXT,
            document_type TEXT,
            competence TEXT,
            file_name TEXT,
            detail TEXT,
            ip TEXT,
            machine TEXT,
            browser TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS audit_log_created ON audit_log(created_at)")
