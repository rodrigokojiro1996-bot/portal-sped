import sqlite3
import tempfile
import unittest
from pathlib import Path

import app
from portal import db as storage
from util import restore_storage

auth_create_user = app.create_user


class StorageTests(unittest.TestCase):
    def test_store_password_policy_and_cnpj_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage.configure_storage(Path(temporary))
            self.addCleanup(restore_storage)
            with self.assertRaisesRegex(ValueError, "CNPJ válido"):
                auth_create_user("11222333000182", "Loja", "store", "uma-senha-segura-123")
            with self.assertRaisesRegex(ValueError, "12 caracteres"):
                auth_create_user("11222333000181", "Loja", "store", "curta")
            auth_create_user("11222333000181", "Loja", "store", "uma-senha-segura-123")
            with storage.connect() as db:
                user = db.execute("SELECT id, cnpj, role FROM users").fetchone()
                for document_type in ("fiscal", "contribuicoes"):
                    db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (document_type, user["id"], document_type, "2026-09", f"{document_type}.txt", 1, "digest", "2026-10-12T00:00:00+00:00"))
                db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("fiscal-supplement", user["id"], "fiscal", "2026-09", "bloco_h.txt", 1, "different-digest", "2026-10-12T00:00:00+00:00"))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("duplicate", user["id"], "fiscal", "2026-09", "duplicate.txt", 1, "digest", "2026-10-12T00:00:00+00:00"))
            self.assertEqual((user["cnpj"], user["role"]), ("11222333000181", "store"))


if __name__ == "__main__":
    unittest.main()
