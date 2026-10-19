import tempfile
import sqlite3
import hashlib
import threading
import time
import unittest
import uuid
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import resumo_sped


def restore_storage():
    """Volta os caminhos do portal para data/ sem abrir nem alterar o banco real."""
    app.DATA_DIR = app.ROOT / "data"
    app.DATABASE = app.DATA_DIR / "portal.sqlite3"
    app.DOCUMENTS = app.DATA_DIR / "documentos"
    app.APP_SECRET = None


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


class SupportAccessTests(unittest.TestCase):
    """Perfil de suporte: vê todas as lojas, só consulta e baixa; tudo fica registrado para a administração."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        app.configure_storage(Path(self.temporary.name))
        app.create_user("11222333000181", "Loja Exemplo A", "store", "senha-loja-a-123")
        app.create_user("11444777000161", "Loja Exemplo B", "store", "senha-loja-b-123")
        app.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        self.support_password = app.create_support_user("suporte.teste", "Suporte Exemplo")
        with app.connect() as db:
            ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM users")}
            self.store_b = ids["Loja Exemplo B"]
            for user_id, token in ((ids["Suporte Exemplo"], "support-session"), (ids["Admin Exemplo"], "admin-session"), (ids["Loja Exemplo A"], "store-session")):
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (app.token_hash(token), user_id, "csrf-" + token, int(time.time()) + 3600))
            content = b"conteudo do sped da loja b"
            db.execute("INSERT INTO documents VALUES ('doc-b', ?, 'fiscal', '2026-09', 'sped-loja-b.txt', ?, ?, ?)", (self.store_b, len(content), hashlib.sha256(content).hexdigest(), "2026-10-12T00:00:00+00:00"))
        (app.DOCUMENTS / "doc-b.txt").write_bytes(content)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        restore_storage()
        self.temporary.cleanup()

    def request(self, token, path, data=None):
        from urllib.parse import urlencode
        headers = {"Cookie": f"portal_session={token}", "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Edg/130.0"}
        body = urlencode(data).encode() if data is not None else None
        try:
            with urlopen(Request(self.base + path, data=body, headers=headers)) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def log(self):
        with app.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit_log ORDER BY id")]

    def test_support_sees_every_store_and_downloads_but_cannot_publish(self):
        status, page = self.request("support-session", "/dashboard?type=fiscal")
        self.assertEqual(status, 200)
        page = page.decode()
        self.assertIn("Loja Exemplo A", page)
        self.assertIn("Loja Exemplo B", page)
        self.assertIn("Acesso de suporte", page)
        self.assertNotIn("Enviar arquivo SPED", page)
        self.assertNotIn("Cadastrar loja", page)
        self.assertEqual(self.request("support-session", "/download/doc-b"), (200, b"conteudo do sped da loja b"))
        self.assertEqual(self.request("support-session", "/upload", {"csrf": "csrf-support-session"})[0], 403)
        self.assertEqual(self.request("support-session", "/stores", {"csrf": "csrf-support-session"})[0], 403)
        self.assertEqual(self.request("support-session", "/monitoramento")[0], 403)
        actions = [entry["action"] for entry in self.log()]
        self.assertEqual(actions, ["abriu_central", "baixou_arquivo", "acao_bloqueada", "acao_bloqueada", "acao_bloqueada"])

    def test_every_action_records_store_file_time_machine_and_browser(self):
        self.request("support-session", f"/dashboard?type=fiscal&store_id={self.store_b}&competence=2026-09")
        self.request("support-session", f"/dashboard?type=fiscal&store_id={self.store_b}&competence=2026-09")  # recarregar não repete
        self.request("support-session", "/download/doc-b")
        visit, download = self.log()
        self.assertEqual((visit["action"], visit["store_name"], visit["competence"]), ("abriu_loja", "Loja Exemplo B", "2026-09"))
        self.assertEqual((download["action"], download["file_name"], download["store_cnpj"]), ("baixou_arquivo", "sped-loja-b.txt", "11444777000161"))
        self.assertEqual(download["machine"], app.socket.gethostname().upper())
        self.assertEqual(download["ip"], "127.0.0.1")
        self.assertEqual(download["browser"], "Edge no Windows")
        self.assertEqual(download["user_name"], "Suporte Exemplo")
        self.assertRegex(download["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_login_and_wrong_password_of_support_are_recorded_but_store_actions_are_not(self):
        import http.cookiejar
        import re
        from urllib.parse import urlencode
        from urllib.request import HTTPCookieProcessor, build_opener
        for password in ("senha-errada-000", self.support_password):
            opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
            csrf = re.search(r"name='csrf' value='([^']+)'", opener.open(self.base + "/login").read().decode()).group(1)
            try:
                opener.open(self.base + "/login", urlencode({"csrf": csrf, "cnpj": "suporte.teste", "password": password}).encode()).read()
            except HTTPError as error:
                error.close()
        self.request("store-session", "/dashboard?type=fiscal")
        self.request("admin-session", "/dashboard?type=fiscal")
        self.assertEqual([entry["action"] for entry in self.log()], ["login_falhou", "login", "abriu_central"])

    def test_admin_sees_clear_history_and_exports_csv(self):
        self.request("support-session", f"/dashboard?type=fiscal&store_id={self.store_b}&competence=2026-09")
        self.request("support-session", "/download/doc-b")
        status, page = self.request("admin-session", "/monitoramento?periodo=hoje")
        page = page.decode()
        self.assertEqual(status, 200)
        self.assertIn("Monitoramento do suporte", page)
        self.assertIn("Baixou o arquivo sped-loja-b.txt", page)
        self.assertIn("Consultou a loja", page)
        self.assertIn("SPED Fiscal · Setembro/2026", page)
        self.assertIn("Hoje · ", page)
        status, csv_body = self.request("admin-session", "/monitoramento.csv?periodo=hoje&acao=downloads")
        text = csv_body.decode("utf-8-sig").splitlines()
        self.assertEqual(text[0].split(";")[:3], ["Data", "Hora", "Usuário"])
        self.assertEqual(len(text), 2)
        self.assertIn("sped-loja-b.txt", text[1])

    def test_old_database_is_migrated_to_accept_support_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy = sqlite3.connect(Path(temporary) / "portal.sqlite3")
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
            try:
                app.configure_storage(Path(temporary))
                app.create_support_user("suporte.migrado", "Suporte Migrado")
                with app.connect() as db:
                    self.assertEqual(db.execute("SELECT name FROM users WHERE id=7").fetchone()[0], "Loja Antiga")
                    self.assertEqual(db.execute("SELECT store_id FROM documents WHERE id='d1'").fetchone()[0], 7)
                    self.assertEqual(db.execute("SELECT role FROM users WHERE login_name='suporte.migrado'").fetchone()[0], "support")
            finally:
                restore_storage()


class PasswordResetTests(unittest.TestCase):
    def test_reset_replaces_password_and_clears_sessions_and_lockouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            app.configure_storage(Path(temporary))
            try:
                app.create_user("11222333000181", "Loja Exemplo", "store", "senha-antiga-123")
                with app.connect() as db:
                    store_id = db.execute("SELECT id FROM users").fetchone()["id"]
                    db.execute("INSERT INTO sessions VALUES ('hash', ?, 'csrf', ?)", (store_id, int(time.time()) + 3600))
                    db.execute("INSERT INTO login_attempts VALUES ('127.0.0.1', 10, ?)", (int(time.time()),))
                name, password = app.reset_store_password("11.222.333/0001-81")
                self.assertEqual(name, "Loja Exemplo")
                with app.connect() as db:
                    stored = db.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
                    self.assertTrue(app.verify_password(password, stored))
                    self.assertFalse(app.verify_password("senha-antiga-123", stored))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 0)
                with self.assertRaises(ValueError):
                    app.reset_store_password("11444777000161")
            finally:
                restore_storage()


class LoginFormTests(unittest.TestCase):
    """O formulário de login não pode ser descartado em silêncio (aba extra, pré-carregamento, página antiga)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        app.configure_storage(Path(self.temporary.name))
        app.create_user("11222333000181", "Loja Exemplo", "store", "senha-correta-123")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        restore_storage()
        self.temporary.cleanup()

    def open_login(self, opener):
        import re
        return re.search(r"name='csrf' value='([^']+)'", opener.open(self.base + "/login").read().decode()).group(1)

    def submit(self, opener, csrf, password):
        from urllib.parse import urlencode
        try:
            response = opener.open(self.base + "/login", urlencode({"csrf": csrf, "cnpj": "11.222.333/0001-81", "password": password}).encode())
            return response.geturl(), response.read().decode()
        except HTTPError as error:
            with error:
                return error.url, error.read().decode()

    def opener(self):
        import http.cookiejar
        from urllib.request import HTTPCookieProcessor, build_opener
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def test_form_from_first_tab_still_works_after_page_loads_again(self):
        opener = self.opener()
        first = self.open_login(opener)
        self.open_login(opener)  # outra aba ou pré-carregamento do navegador
        url, body = self.submit(opener, first, "senha-correta-123")
        self.assertTrue(url.endswith("/dashboard"), url)
        self.assertIn("Loja Exemplo", body)

    def test_wrong_password_shows_form_with_message_and_cnpj(self):
        opener = self.opener()
        _, body = self.submit(opener, self.open_login(opener), "senha-errada-000")
        self.assertIn("CNPJ ou senha incorretos", body)
        self.assertIn("value='11.222.333/0001-81'", body)
        self.assertIn("name='password'", body)

    def test_expired_form_explains_instead_of_silently_reloading(self):
        _, body = self.submit(self.opener(), "token-antigo", "senha-correta-123")
        self.assertIn("A página de acesso expirou", body)
        self.assertIn("value='11.222.333/0001-81'", body)

    def test_generated_password_is_readable_and_valid(self):
        _, password = app.reset_store_password("11222333000181")
        self.assertRegex(password, r"^[A-HJ-NP-Za-km-z2-9]{4}-[A-HJ-NP-Za-km-z2-9]{4}-[A-HJ-NP-Za-km-z2-9]{4}$")
        opener = self.opener()
        url, _ = self.submit(opener, self.open_login(opener), password)
        self.assertTrue(url.endswith("/dashboard"), url)


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
        restore_storage()
        self.temporary.cleanup()

    def request_as_matrix(self, path):
        request = Request(self.base_url + path, headers={"Cookie": "portal_session=matrix-test-session"})
        return urlopen(request)

    def test_matrix_can_switch_to_branch_and_download_its_document(self):
        default_page = self.request_as_matrix("/dashboard?type=fiscal").read().decode()
        self.assertIn(f"store_id={self.branch_id}", default_page)
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


FISCAL_SAMPLE = "\r\n".join((
    "|0000|020|0|01092026|30092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|0000000000|4314902|00000||A|1|",
    "|0150|11444777000161|DISTRIBUIDORA EXEMPLO SA|1058|11444777000161||||||||",
    "|C100|1|0||65|00|1|101||02092026|02092026|150|0|0|0|150|9|0|0|0|100|17|0|0|0|0|0|0|0|",
    "|C190|060|5405|0|100|0|0|0|0|0|0||",
    "|C190|000|5102|17|50|50|8,5|0|0|0|0||",
    "|C100|1|0||65|02|1|102||02092026|02092026|999|0|0|0|999|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C190|000|5102|17|999|999|169,83|0|0|0|0||",
    "|C100|0|1|11444777000161|55|00|1|555||01092026|03092026|80|0|0|0|80|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C190|060|1403|0|60|0|0|0|0|0|0||",
    "|C190|000|1102|17|20|20|3,4|0|0|0|0||",
    "|C100|1|0||55|00|1|103||05092026|05092026|30|0|0|0|30|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C190|000|5929|17|30|30|5,1|0|0|0|0||",
    "|E110|8,5|0|0|0|3,4|0|0|0|0|5,1|0|5,1|0|0|",
    "|E116|000|5,1|12102026|0221|||||092026|",
    "|E210|1|0|0|0|0|0|0|0|0|0|0|1,5|0|0|",
))

CONTRIBUTIONS_SAMPLE = "\r\n".join((
    "|0000|006|0|0||01092026|30092026|FARMACIA EXEMPLO LTDA|11222333000181||RS|4314902||00|",
    "|0140|0001|LOJA MATRIZ|11222333000181|RS||4314902|||",
    "|C010|11222333000181|2|",
    "|C100|1|0||65|00|1|1||02092026|02092026|200|0|0|0|200|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|C175|5405|200|10|04|0|0|||0|04|0|0|||0||",
    "|C100|0|1|11444777000161|55|00|1|2||01092026|01092026|120|0|0|0|120|9|0|0|0|0|0|0|0|0|0|0|0|0|",
    "|M200|0|0|0|0|0|0|0|0,65|0|0|0,65|0,65|",
    "|M210|51|100|100|0|0|100|0,65|0||0,65|0|0|0|0|0,65|",
    "|M400|04|300||",
    "|M600|0|0|0|0|0|0|0|3|0|0|3|3|",
))


class SpedSummaryTests(unittest.TestCase):
    def test_fiscal_summary_skips_cancelled_and_x929_and_matches_assessment(self):
        summary = resumo_sped.summarize_fiscal(FISCAL_SAMPLE.splitlines())
        operations = summary["operacoes"]
        self.assertEqual(operations["saidas"]["valor"], 150)
        self.assertEqual(operations["saidas"]["documentos"], 2)
        self.assertAlmostEqual(operations["saidas"]["icms"], 8.5)
        self.assertEqual(operations["entradas"]["valor"], 80)
        self.assertEqual(operations["cfop_x929"], 30)
        self.assertEqual(operations["tributacao_saidas"], {"Substituição tributária": 100, "Tributadas": 50})
        self.assertEqual(operations["diario"]["2026-09-03"], [0, 80])
        self.assertEqual(operations["fornecedores"], [["DISTRIBUIDORA EXEMPLO SA", 80]])
        self.assertEqual(summary["apuracao"]["icms"]["recolher"], 5.1)
        self.assertEqual(summary["apuracao"]["icms_st_recolher"], 1.5)
        self.assertEqual(summary["apuracao"]["obrigacoes"], [[5.1, "2026-10-12"]])

    def test_contributions_summary_reads_revenue_and_taxes(self):
        summary = resumo_sped.summarize_contributions(CONTRIBUTIONS_SAMPLE.splitlines())
        self.assertEqual(summary["operacoes"]["estabelecimentos"], [["11222333000181", "LOJA MATRIZ", 200, 120]])
        self.assertEqual(summary["operacoes"]["cfops_saida"], {"5405": 190})
        self.assertEqual(summary["apuracao"]["receita_tributada"], 100)
        self.assertEqual(summary["apuracao"]["receita_sem_incidencia"], {"04": 300})
        self.assertEqual(summary["apuracao"]["pis"]["recolher"], 0.65)
        self.assertEqual(summary["apuracao"]["cofins"]["recolher"], 3)

    def test_merge_keeps_main_file_when_complement_only_has_inventory(self):
        main = resumo_sped.summarize_fiscal(FISCAL_SAMPLE.splitlines())
        inventory = resumo_sped.summarize_fiscal(["|H005|30092026|1234,5|01|"])
        merged = resumo_sped.merge([main, inventory])
        self.assertEqual(merged["operacoes"]["saidas"]["valor"], 150)
        self.assertEqual(merged["inventario"], {"data": "2026-09-30", "valor": 1234.5})

    def test_render_escapes_names_and_outputs_charts(self):
        summary = resumo_sped.summarize_fiscal(FISCAL_SAMPLE.replace("DISTRIBUIDORA EXEMPLO SA", "<script>x</script>").splitlines())
        page = resumo_sped.render(summary, "2026-09", [("2026-08", {"saidas": 100, "entradas": 50, "imposto": 4}), ("2026-09", resumo_sped.headline(summary))])
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("<svg", page)
        self.assertIn("Evolução mês a mês", page)
        self.assertIn("cresceram", page)
        self.assertLessEqual(page.count("<li>"), resumo_sped.MAX_INSIGHTS)
        contributions = resumo_sped.render(resumo_sped.summarize_contributions(CONTRIBUTIONS_SAMPLE.splitlines()), "2026-09", [])
        self.assertIn("75,0%", contributions)


class PublicationForecastTests(unittest.TestCase):
    def test_deadline_is_third_business_day_for_small_groups_and_fifth_for_larger(self):
        expected = {
            "2026-09": (date(2026, 10, 5), date(2026, 10, 7)),
            "2026-10": (date(2026, 11, 5), date(2026, 11, 9)),   # 02/11 Finados
            "2026-11": (date(2026, 12, 3), date(2026, 12, 7)),
            "2026-12": (date(2027, 1, 6), date(2027, 1, 8)),     # 01/01 Confraternização
        }
        for competence, (small, large) in expected.items():
            self.assertEqual(app.publication_deadline(competence, 3), (small, 3), competence)
            self.assertEqual(app.publication_deadline(competence, 4), (large, 5), competence)

    def test_movable_holidays_follow_easter(self):
        self.assertEqual(app.easter(2026), date(2026, 4, 5))
        self.assertIn(date(2027, 2, 9), app.non_business_days(2027))   # Carnaval 2027
        self.assertIn(date(2026, 4, 3), app.non_business_days(2026))   # Sexta-feira Santa 2026

    def test_timeline_runs_until_forecast_month(self):
        self.assertEqual(app.competence_timeline(["2026-09"], today=date(2026, 10, 16)), ["2026-09", "2026-10", "2026-11", "2026-12"])

    def test_pending_month_shows_forecast_and_help_for_large_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            app.configure_storage(Path(temporary))
            try:
                for cnpj in ("11444777000161", "11444777000242", "11444777000323", "11444777000404"):
                    app.create_user(cnpj, f"Rede Exemplo - {cnpj[8:12]}", "store", "senha-exemplo-123")
                with app.connect() as db:
                    store_id = db.execute("SELECT id FROM users WHERE cnpj='11444777000161'").fetchone()["id"]
                    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (app.token_hash("forecast-session"), store_id, "csrf", int(time.time()) + 3600))
                server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    page = urlopen(Request(f"http://127.0.0.1:{server.server_port}/dashboard?type=fiscal&competence=2026-12", headers={"Cookie": "portal_session=forecast-session"})).read().decode()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                self.assertIn("Até 08/01/2027", page)
                self.assertIn("Grupo com 4 lojas", page)
                self.assertIn("entra na fila", page)
                self.assertIn("Dez/26", page)
            finally:
                restore_storage()


class DashboardSummaryTests(unittest.TestCase):
    def test_store_dashboard_shows_summary_of_selected_competence(self):
        with tempfile.TemporaryDirectory() as temporary:
            app.configure_storage(Path(temporary))
            try:
                app.create_user("11222333000181", "Farmácia Exemplo", "store", "senha-exemplo-123")
                content = FISCAL_SAMPLE.encode()
                with app.connect() as db:
                    store_id = db.execute("SELECT id FROM users").fetchone()["id"]
                    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (app.token_hash("summary-session"), store_id, "csrf", int(time.time()) + 3600))
                    db.execute("INSERT INTO documents VALUES (?, ?, 'fiscal', '2026-09', 'sped.txt', ?, ?, ?)", ("doc-1", store_id, len(content), hashlib.sha256(content).hexdigest(), "2026-10-12T00:00:00+00:00"))
                (app.DOCUMENTS / "doc-1.txt").write_bytes(content)
                server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    page = urlopen(Request(f"http://127.0.0.1:{server.server_port}/dashboard?type=fiscal", headers={"Cookie": "portal_session=summary-session"})).read().decode()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                self.assertIn("Setembro/2026", page)
                self.assertIn("R$ 150,00", page)
                self.assertIn("href='/download/doc-1'", page)
                with app.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM document_summaries").fetchone()[0], 1)
            finally:
                restore_storage()


STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ = "11222333000181", "11444777000161", "45543915000181"


def sped(sample, cnpj=STORE_A_CNPJ, period="01092026|30092026"):
    """Amostra SPED com outro CNPJ e período no registro 0000 (as demais linhas ficam como estão)."""
    first, rest = sample.split("\r\n", 1)
    fields = first.split("|")
    start, end = period.split("|")
    if fields[2] == "006":  # Contribuições
        fields[6], fields[7], fields[9] = start, end, cnpj
    else:
        fields[4], fields[5], fields[7] = start, end, cnpj
    return ("|".join(fields) + "\r\n" + rest).encode()


class SpedTypeDetectionTests(unittest.TestCase):
    """O portal precisa saber, só pelo arquivo, se ele é SPED Fiscal (ICMS/IPI) ou SPED Contribuições (PIS/COFINS)."""

    def test_detects_fiscal_icms_ipi(self):
        self.assertEqual(app.detect_sped_type(FISCAL_SAMPLE.encode()), "fiscal")

    def test_detects_contributions_pis_cofins(self):
        self.assertEqual(app.detect_sped_type(CONTRIBUTIONS_SAMPLE.encode()), "contribuicoes")

    def test_detects_file_saved_with_utf8_bom(self):
        self.assertEqual(app.detect_sped_type(b"\xef\xbb\xbf" + CONTRIBUTIONS_SAMPLE.encode()), "contribuicoes")

    def test_rejects_other_sped_layouts_and_plain_text(self):
        ecd = b"|0000|LECD|01012026|31122026|FARMACIA EXEMPLO LTDA|11222333000181|RS||4314902|||0|0|0||0|0||N|N|0|0|\r\n"
        for content in (ecd, b"relatorio de vendas\r\nsem registro 0000\r\n", b""):
            with self.subTest(content=content[:20]):
                with self.assertRaisesRegex(ValueError, "não foi reconhecido"):
                    app.detect_sped_type(content)


class AutoPublishTests(unittest.TestCase):
    """Publicação automática: o portal acha a loja pelo CNPJ, a competência pelo período e a aba pelo tipo do arquivo."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        app.configure_storage(Path(self.temporary.name))
        app.create_user(STORE_A_CNPJ, "Farmácia Centro", "store", "senha-centro-123")
        app.create_user(STORE_B_CNPJ, "Farmácia Bairro", "store", "senha-bairro-123")
        app.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        with app.connect() as db:
            ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM users")}
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (app.token_hash("admin-session"), ids["Admin Exemplo"], "csrf-admin", int(time.time()) + 3600))
        self.store_a, self.store_b = ids["Farmácia Centro"], ids["Farmácia Bairro"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.PortalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        restore_storage()
        self.temporary.cleanup()

    def documents(self):
        with app.connect() as db:
            return [dict(row) for row in db.execute("SELECT store_id, document_type, competence, original_name FROM documents ORDER BY uploaded_at")]

    def stored_files(self):
        return sorted(path.name for path in app.DOCUMENTS.iterdir())

    def post_file(self, content, file_name="sped.txt", csrf="csrf-admin"):
        """Envia o formulário de publicação como o navegador: só o csrf e o arquivo."""
        boundary = "----portal" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\nContent-Type: text/plain\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = Request(self.base + "/upload", data=body, headers={"Cookie": "portal_session=admin-session", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(request) as response:  # segue o redirecionamento até o painel da loja
                return response.status, response.geturl(), response.read().decode()
        except HTTPError as error:
            with error:
                return error.code, error.geturl(), error.read().decode()

    # ---------------------------------------------------------------- regra de publicação

    def test_fiscal_file_is_attached_to_store_of_its_cnpj_in_fiscal_tab(self):
        content = sped(FISCAL_SAMPLE, STORE_A_CNPJ)
        published = app.publish_sped(content, "sped-fiscal-centro.txt")
        self.assertEqual((published["store_id"], published["document_type"], published["competence"]), (self.store_a, "fiscal", "2026-09"))
        self.assertEqual(self.documents(), [{"store_id": self.store_a, "document_type": "fiscal", "competence": "2026-09", "original_name": "sped-fiscal-centro.txt"}])
        self.assertEqual((app.DOCUMENTS / f"{published['id']}.txt").read_bytes(), content)

    def test_contributions_file_is_attached_to_store_of_its_cnpj_in_contributions_tab(self):
        published = app.publish_sped(sped(CONTRIBUTIONS_SAMPLE, STORE_B_CNPJ), "contribuicoes.txt")
        self.assertEqual((published["store_id"], published["document_type"], published["competence"]), (self.store_b, "contribuicoes", "2026-09"))
        self.assertEqual(published["store_name"], "Farmácia Bairro")

    def test_competence_comes_from_the_period_inside_the_file(self):
        published = app.publish_sped(sped(FISCAL_SAMPLE, STORE_B_CNPJ, "01022026|28022026"), "fevereiro.txt")
        self.assertEqual(published["competence"], "2026-02")

    def test_fiscal_and_contributions_of_same_store_and_month_coexist(self):
        app.publish_sped(sped(FISCAL_SAMPLE), "fiscal.txt")
        app.publish_sped(sped(CONTRIBUTIONS_SAMPLE), "contribuicoes.txt")
        self.assertEqual([(doc["store_id"], doc["document_type"]) for doc in self.documents()], [(self.store_a, "fiscal"), (self.store_a, "contribuicoes")])

    def test_file_of_unregistered_cnpj_is_refused_and_nothing_is_saved(self):
        with self.assertRaises(app.PublishError) as error:
            app.publish_sped(sped(FISCAL_SAMPLE, UNREGISTERED_CNPJ), "outra.txt")
        self.assertIn("45.543.915/0001-81", str(error.exception))
        self.assertIn("FARMACIA EXEMPLO LTDA", str(error.exception))
        self.assertIn("SPED Fiscal (ICMS e IPI)", str(error.exception))
        self.assertEqual((self.documents(), self.stored_files()), ([], []))

    def test_file_of_inactive_store_is_refused(self):
        with app.connect() as db:
            db.execute("UPDATE users SET active=0 WHERE id=?", (self.store_a,))
        with self.assertRaisesRegex(app.PublishError, "nenhuma loja ativa"):
            app.publish_sped(sped(FISCAL_SAMPLE), "sped.txt")

    def test_same_file_twice_is_refused_without_duplicating(self):
        app.publish_sped(sped(FISCAL_SAMPLE), "sped.txt")
        with self.assertRaisesRegex(app.PublishError, "já está publicado no SPED Fiscal .* Farmácia Centro"):
            app.publish_sped(sped(FISCAL_SAMPLE), "copia.txt")
        self.assertEqual((len(self.documents()), len(self.stored_files())), (1, 1))

    def test_invalid_files_are_refused_with_the_reason(self):
        cases = {
            "período parcial": (sped(FISCAL_SAMPLE, period="02092026|30092026"), "mês-calendário completo"),
            "CNPJ inválido": (sped(FISCAL_SAMPLE, "11222333000182"), "CNPJ inválido"),
            "não é SPED": (b"qualquer texto\r\n", "não foi reconhecido"),
            "vazio": (b"", "Escolha um arquivo"),
        }
        for case, (content, reason) in cases.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(app.PublishError, reason):
                    app.publish_sped(content, "sped.txt")
        self.assertEqual(self.stored_files(), [])

    # ---------------------------------------------------------------- tela e pop-up

    def test_upload_form_has_only_the_file_and_the_publish_button(self):
        with urlopen(Request(self.base + "/dashboard", headers={"Cookie": "portal_session=admin-session"})) as response:
            page = response.read().decode()
        form = page.split("action='/upload'", 1)[1].split("</form>", 1)[0]
        self.assertIn("name='file'", form)
        self.assertIn("Validar e publicar", form)
        for removed in ("store_id", "competence", "document_type", "<select"):
            self.assertNotIn(removed, form)

    def test_publishing_fiscal_shows_popup_with_type_store_cnpj_and_competence(self):
        status, url, page = self.post_file(sped(FISCAL_SAMPLE), "sped-setembro.txt")
        self.assertEqual(status, 200)
        self.assertIn(f"store_id={self.store_a}&type=fiscal&competence=2026-09&published=", url)
        self.assertIn("<dialog open class='publish-dialog is-success'", page)
        self.assertIn("O arquivo SPED Fiscal (ICMS e IPI) foi publicado com sucesso na loja Farmácia Centro, CNPJ 11.222.333/0001-81, competência Setembro/2026.", page)
        self.assertIn("sped-setembro.txt", page)
        self.assertIn(f"class='sped-tab active' aria-current='page' href='/dashboard?store_id={self.store_a}&amp;type=fiscal'", page)

    def test_publishing_contributions_shows_popup_mentioning_contributions(self):
        status, url, page = self.post_file(sped(CONTRIBUTIONS_SAMPLE, STORE_B_CNPJ), "contrib.txt")
        self.assertEqual(status, 200)
        self.assertIn(f"store_id={self.store_b}&type=contribuicoes&competence=2026-09", url)
        self.assertIn("O arquivo SPED Contribuições (PIS e COFINS) foi publicado com sucesso na loja Farmácia Bairro, CNPJ 11.444.777/0001-61, competência Setembro/2026.", page)
        self.assertIn(f"class='sped-tab active' aria-current='page' href='/dashboard?store_id={self.store_b}&amp;type=contribuicoes'", page)

    def test_refused_file_shows_error_popup_and_saves_nothing(self):
        status, _, page = self.post_file(sped(FISCAL_SAMPLE, UNREGISTERED_CNPJ))
        self.assertEqual(status, 422)
        self.assertIn("<dialog open class='publish-dialog is-error'", page)
        self.assertIn("Arquivo não publicado", page)
        self.assertIn("45.543.915/0001-81", page)
        self.assertEqual(self.stored_files(), [])

    def test_publish_requires_valid_form_token(self):
        status, _, _ = self.post_file(sped(FISCAL_SAMPLE), csrf="token-errado")
        self.assertEqual(status, 403)
        self.assertEqual(self.documents(), [])

    def test_popup_only_appears_right_after_publishing(self):
        _, url, _ = self.post_file(sped(FISCAL_SAMPLE))
        plain = url.split("&published=")[0].removeprefix(self.base)
        with urlopen(Request(self.base + plain, headers={"Cookie": "portal_session=admin-session"})) as response:
            self.assertNotIn("publish-dialog", response.read().decode())


if __name__ == "__main__":
    unittest.main()