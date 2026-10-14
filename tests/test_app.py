import tempfile
import sqlite3
import hashlib
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app


class CnpjTests(unittest.TestCase):
    def test_validates_check_digits(self):
        self.assertTrue(app.valid_cnpj("11.222.333/0001-81"))
        self.assertFalse(app.valid_cnpj("11.222.333/0001-82"))
        self.assertFalse(app.valid_cnpj("00.000.000/0000-00"))


class SpedHeaderTests(unittest.TestCase):
    def header(self, cnpj="11222333000181", start="01092026", end="30092026"):
        return f"|0000|020|0|{start}|{end}|FARMACIA EXEMPLO LTDA|{cnpj}||RS|0000000000|4314902|00000||A|1|\r\n|0001|0|\r\n".encode()

    def test_extracts_cnpj_and_competence_from_register_0000(self):
        parsed = app.parse_sped_header(self.header())
        self.assertEqual(parsed["cnpj"], "11222333000181")
        self.assertEqual(parsed["competence"], "2026-09")
        self.assertEqual(parsed["company"], "FARMACIA EXEMPLO LTDA")

    def test_rejects_invalid_cnpj(self):
        with self.assertRaisesRegex(ValueError, "CNPJ inválido"):
            app.parse_sped_header(self.header(cnpj="11222333000182"))

    def test_rejects_partial_month(self):
        with self.assertRaisesRegex(ValueError, "mês-calendário completo"):
            app.parse_sped_header(self.header(start="02092026"))

    def test_rejects_period_spanning_two_months(self):
        with self.assertRaisesRegex(ValueError, "único mês"):
            app.parse_sped_header(self.header(start="01092026", end="01102026"))

    def test_extracts_contributions_header_using_its_own_field_layout(self):
        content = b"|0000|006|0|0||01092026|30092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|4314902||00|\r\n"
        parsed = app.parse_sped_header(content, "contribuicoes")
        self.assertEqual(parsed["cnpj"], "11222333000181")
        self.assertEqual(parsed["competence"], "2026-09")
        self.assertEqual(parsed["company"], "FARMACIA EXEMPLO LTDA")


class PasswordTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        stored = app.password_digest("uma-senha-segura-123")
        self.assertTrue(app.verify_password("uma-senha-segura-123", stored))
        self.assertFalse(app.verify_password("senha-errada", stored))
        self.assertNotEqual(stored, app.password_digest("uma-senha-segura-123"))


class StorageTests(unittest.TestCase):
    def test_store_password_policy_and_cnpj_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            app.configure_storage(Path(temporary))
            with self.assertRaisesRegex(ValueError, "CNPJ válido"):
                app.create_user("11222333000182", "Loja", "store", "uma-senha-segura-123")
            with self.assertRaisesRegex(ValueError, "12 caracteres"):
                app.create_user("11222333000181", "Loja", "store", "curta")
            app.create_user("11222333000181", "Loja", "store", "uma-senha-segura-123")
            with app.connect() as db:
                user = db.execute("SELECT id, cnpj, role FROM users").fetchone()
                for document_type in ("fiscal", "contribuicoes"):
                    db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (document_type, user["id"], document_type, "2026-09", f"{document_type}.txt", 1, "digest", "2026-10-12T00:00:00+00:00"))
                db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("fiscal-supplement", user["id"], "fiscal", "2026-09", "bloco_h.txt", 1, "different-digest", "2026-10-12T00:00:00+00:00"))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("duplicate", user["id"], "fiscal", "2026-09", "duplicate.txt", 1, "digest", "2026-10-12T00:00:00+00:00"))
            self.assertEqual((user["cnpj"], user["role"]), ("11222333000181", "store"))


class BranchAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        app.configure_storage(Path(self.temporary.name))
        app.create_user("11444777000161", "Farmácia Matriz", "store", "senha-matriz-demo-123")
        app.create_user("11444777000242", "Farmácia Filial", "store", "senha-filial-demo-123")
        app.create_user("11222333000181", "Outra Rede", "store", "senha-outra-rede-123")
        with app.connect() as db:
            self.matrix_id = db.execute("SELECT id FROM users WHERE cnpj=?", ("11444777000161",)).fetchone()["id"]
            self.branch_id = db.execute("SELECT id FROM users WHERE cnpj=?", ("11444777000242",)).fetchone()["id"]
            other_id = db.execute("SELECT id FROM users WHERE cnpj=?", ("11222333000181",)).fetchone()["id"]
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (app.token_hash("matrix-test-session"), self.matrix_id, "csrf-test", int(time.time()) + 3600))
            self.branch_document = self.add_document(db, self.branch_id, "filial.txt", b"documento da filial")
            self.other_document = self.add_document(db, other_id, "outra-rede.txt", b"documento de outra rede")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def add_document(self, db, store_id, filename, content):
        document_id = str(uuid.uuid4())
        (app.DOCUMENTS / f"{document_id}.txt").write_bytes(content)
        db.execute("INSERT INTO documents (id, store_id, document_type, competence, original_name, size, sha256, uploaded_at) VALUES (?, ?, 'fiscal', '2026-09', ?, ?, ?, ?)", (document_id, store_id, filename, len(content), hashlib.sha256(content).hexdigest(), "2026-10-12T00:00:00+00:00"))
        return document_id

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        app.configure_storage(app.ROOT / "data")
        self.temporary.cleanup()

    def request_as_matrix(self, path):
        request = Request(self.base_url + path, headers={"Cookie": "portal_session=matrix-test-session"})
        return urlopen(request)

    def test_matrix_can_switch_to_branch_and_download_its_document(self):
        default_page = self.request_as_matrix("/dashboard?type=fiscal").read().decode()
        self.assertIn(f"value='{self.branch_id}'", default_page)
        branch_page = self.request_as_matrix(f"/dashboard?type=fiscal&store_id={self.branch_id}").read().decode()
        self.assertIn("Farmácia Filial", branch_page)
        self.assertIn("filial.txt", branch_page)
        response = self.request_as_matrix(f"/download/{self.branch_document}")
        self.assertEqual(response.read(), b"documento da filial")

    def test_matrix_cannot_download_document_from_another_cnpj_group(self):
        with self.assertRaises(HTTPError) as error:
            self.request_as_matrix(f"/download/{self.other_document}")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()