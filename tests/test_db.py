import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from portal import auth
from portal import db as storage
from util import restore_storage, temporary_storage


def normalized_schema(database: Path) -> list[tuple[str, str, str]]:
    """Esquema comparável entre bancos: sem aspas, sem IF NOT EXISTS e com espaços uniformes."""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite%' AND name <> 'schema_version' ORDER BY type, name").fetchall()
    finally:
        connection.close()
    clean = lambda sql: re.sub(r"\s+", " ", (sql or "").replace('"', "").lower().replace("if not exists ", "")).replace("( ", "(").replace(" )", ")").strip()
    return [(kind, name, clean(sql)) for kind, name, sql in rows]


class StorageTests(unittest.TestCase):
    def test_store_password_policy_and_cnpj_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage.configure_storage(Path(temporary))
            self.addCleanup(restore_storage)
            with self.assertRaisesRegex(ValueError, "CNPJ válido"):
                auth.create_user("11222333000182", "Loja", "store", "uma-senha-segura-123")
            with self.assertRaisesRegex(ValueError, "12 caracteres"):
                auth.create_user("11222333000181", "Loja", "store", "curta")
            auth.create_user("11222333000181", "Loja", "store", "uma-senha-segura-123")
            with storage.connect() as db:
                user = db.execute("SELECT id, cnpj, role FROM users").fetchone()
                for document_type in ("fiscal", "contribuicoes"):
                    db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (document_type, user["id"], document_type, "2026-09", f"{document_type}.txt", 1, "digest", "2026-10-12T00:00:00+00:00"))
                db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("fiscal-supplement", user["id"], "fiscal", "2026-09", "bloco_h.txt", 1, "different-digest", "2026-10-12T00:00:00+00:00"))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("duplicate", user["id"], "fiscal", "2026-09", "duplicate.txt", 1, "digest", "2026-10-12T00:00:00+00:00"))
            self.assertEqual((user["cnpj"], user["role"]), ("11222333000181", "store"))


class MigrationTests(unittest.TestCase):
    """Migrações numeradas: banco novo e banco existente terminam no mesmo esquema, cada passo numa transação."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(restore_storage)

    def open_at(self, name: str) -> Path:
        folder = self.folder / name
        storage.use_paths(folder)
        folder.mkdir()
        return folder / "portal.sqlite3"

    def test_new_database_runs_every_migration_in_order(self):
        temporary_storage(self)
        latest = storage.latest_version()
        self.assertGreaterEqual(latest, 3)
        self.assertEqual(storage.schema_version(), latest)
        with storage.connect() as db:
            versions = [row["version"] for row in db.execute("SELECT version FROM schema_version ORDER BY version")]
        self.assertEqual(versions, list(range(1, latest + 1)))

    def test_running_again_applies_nothing(self):
        temporary_storage(self)
        self.assertEqual(storage.migrate(), [])

    def test_database_from_version_1_5_is_adopted_and_ends_with_same_schema_as_a_new_one(self):
        fresh = self.open_at("novo")
        storage.migrate()
        existing = self.open_at("existente")
        storage._adopt_pre_migration_database()   # é como a 1.5 criava o banco
        sqlite3.connect(existing).execute("INSERT INTO users (cnpj, name, role, password_hash, created_at) VALUES ('11222333000181', 'Loja', 'store', 'x', 'agora')").connection.commit()
        applied = storage.migrate()
        self.assertEqual(applied, list(range(storage.PRE_MIGRATION_VERSION + 1, storage.latest_version() + 1)))
        self.assertEqual(normalized_schema(existing), normalized_schema(fresh))
        self.assertEqual(storage.schema_version(), storage.latest_version())
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM users").fetchone()[0], "Loja")

    def test_database_from_version_1_1_keeps_its_data_and_accepts_support_role(self):
        database = self.open_at("antigo")
        legacy = sqlite3.connect(database)
        legacy.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, cnpj TEXT UNIQUE, login_name TEXT UNIQUE, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'store')), password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE documents (id TEXT PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES users(id), document_type TEXT NOT NULL CHECK(document_type IN ('fiscal', 'contribuicoes')),
                competence TEXT NOT NULL, original_name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, uploaded_at TEXT NOT NULL, UNIQUE(store_id, document_type, sha256));
            INSERT INTO users VALUES (7, '11222333000181', NULL, 'Loja Antiga', 'store', 'hash', 1, '2026-01-01');
            INSERT INTO documents VALUES ('d1', 7, 'fiscal', '2026-09', 'a.txt', 1, 'x', '2026-10-01');
        """)
        legacy.commit()
        legacy.close()
        storage.configure_storage(database.parent)
        auth.create_support_user("suporte.migrado", "Suporte Migrado")
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM users WHERE id=7").fetchone()[0], "Loja Antiga")
            self.assertEqual(db.execute("SELECT store_id FROM documents WHERE id='d1'").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT role FROM users WHERE login_name='suporte.migrado'").fetchone()[0], "support")
        self.assertEqual(storage.schema_version(), storage.latest_version())

    def test_failed_migration_is_rolled_back_and_can_run_again_after_fix(self):
        migrations = self.folder / "migrations"
        shutil.copytree(storage.MIGRATIONS_DIR, migrations)
        broken = migrations / f"{storage.latest_version() + 1:03d}_quebrada.sql"
        broken.write_text("CREATE TABLE parcial (id INTEGER);\nINSERT INTO tabela_que_nao_existe VALUES (1);\n", encoding="utf-8")
        with mock.patch.object(storage, "MIGRATIONS_DIR", migrations):
            self.open_at("falha")
            with self.assertRaises(sqlite3.OperationalError):
                storage.migrate()
            version = storage.schema_version()
            with storage.connect() as db:
                partial = db.execute("SELECT 1 FROM sqlite_master WHERE name='parcial'").fetchone()
            self.assertEqual(version, storage.latest_version() - 1)
            self.assertIsNone(partial)
            broken.write_text("CREATE TABLE parcial (id INTEGER);\n", encoding="utf-8")
            self.assertEqual(storage.migrate(), [storage.latest_version()])

    def test_migration_files_must_be_numbered_in_sequence(self):
        migrations = self.folder / "migrations"
        shutil.copytree(storage.MIGRATIONS_DIR, migrations)
        (migrations / f"{storage.latest_version() + 2:03d}_pulou_um.sql").write_text("SELECT 1;", encoding="utf-8")
        with mock.patch.object(storage, "MIGRATIONS_DIR", migrations):
            with self.assertRaisesRegex(RuntimeError, "em sequência"):
                storage.migration_files()
        (migrations / "extra.sql").write_text("SELECT 1;", encoding="utf-8")
        with mock.patch.object(storage, "MIGRATIONS_DIR", migrations):
            with self.assertRaisesRegex(RuntimeError, "fora do padrão"):
                storage.migration_files()


if __name__ == "__main__":
    unittest.main()
