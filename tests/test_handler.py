import tempfile
import sqlite3
import hashlib
import socket
import threading
import time
import unittest
import uuid
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from portal.handler import PortalHandler
from portal.views import layout
from portal import auth
from portal import config
from portal import db as storage
from util import CONTRIBUTIONS_SAMPLE, FISCAL_SAMPLE, STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ, restore_storage, sped
import resumo_sped


class SupportAccessTests(unittest.TestCase):
    """Perfil de suporte com o monitoramento ativo: vê todas as lojas, só consulta e baixa; tudo fica registrado.

    O monitoramento vem desativado desde a versão 1.5; estes testes o ligam para manter a função pronta para reativação.
    """

    def setUp(self):
        self.monitoring = config.MONITORING_ENABLED
        config.MONITORING_ENABLED = True
        self.temporary = tempfile.TemporaryDirectory()
        storage.configure_storage(Path(self.temporary.name))
        auth.create_user("11222333000181", "Loja Exemplo A", "store", "senha-loja-a-123")
        auth.create_user("11444777000161", "Loja Exemplo B", "store", "senha-loja-b-123")
        auth.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        self.support_password = auth.create_support_user("suporte.teste", "Suporte Exemplo")
        with storage.connect() as db:
            ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM users")}
            self.store_b = ids["Loja Exemplo B"]
            for user_id, token in ((ids["Suporte Exemplo"], "support-session"), (ids["Admin Exemplo"], "admin-session"), (ids["Loja Exemplo A"], "store-session")):
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash(token), user_id, "csrf-" + token, int(time.time()) + 3600))
            content = b"conteudo do sped da loja b"
            db.execute("INSERT INTO documents VALUES ('doc-b', ?, 'fiscal', '2026-09', 'sped-loja-b.txt', ?, ?, ?)", (self.store_b, len(content), hashlib.sha256(content).hexdigest(), "2026-10-12T00:00:00+00:00"))
        (storage.DOCUMENTS / "doc-b.txt").write_bytes(content)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        restore_storage()
        self.temporary.cleanup()
        config.MONITORING_ENABLED = self.monitoring

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
        with storage.connect() as db:
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
        self.assertEqual(download["machine"], socket.gethostname().upper())
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


class MonitoringDisabledTests(unittest.TestCase):
    """Versão 1.5: o monitoramento do suporte fica desligado por padrão, sem tela, sem registro e sem aviso."""

    setUp_base, tearDown, request, log = SupportAccessTests.setUp, SupportAccessTests.tearDown, SupportAccessTests.request, SupportAccessTests.log

    def setUp(self):
        self.setUp_base()
        config.MONITORING_ENABLED = False

    def test_monitoring_is_off_unless_environment_variable_turns_it_on(self):
        import os
        import subprocess
        import sys
        for value, expected in ((None, "False"), ("0", "False"), ("1", "True")):
            with self.subTest(PORTAL_MONITORAMENTO=value):
                env = {key: item for key, item in os.environ.items() if key != "PORTAL_MONITORAMENTO"}
                if value is not None:
                    env["PORTAL_MONITORAMENTO"] = value
                result = subprocess.run([sys.executable, "-c", "from portal import config; print(config.MONITORING_ENABLED)"], cwd=config.ROOT, env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), expected)

    def test_admin_has_no_monitoring_menu_and_pages_are_not_found(self):
        status, page = self.request("admin-session", "/dashboard")
        self.assertEqual(status, 200)
        self.assertNotIn("/monitoramento", page.decode())
        self.assertEqual(self.request("admin-session", "/monitoramento")[0], 404)
        self.assertEqual(self.request("admin-session", "/monitoramento.csv")[0], 404)

    def test_support_actions_are_not_recorded_and_support_is_not_told_they_are(self):
        status, page = self.request("support-session", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("Acesso de suporte:", page.decode())
        self.assertNotIn("são registradas", page.decode())
        self.assertEqual(self.request("support-session", "/download/doc-b")[1], b"conteudo do sped da loja b")
        self.assertEqual(self.request("support-session", "/upload", {"csrf": "csrf-support-session"})[0], 403)
        self.assertEqual(self.request("support-session", "/monitoramento")[0], 404)
        self.assertEqual(self.log(), [])

    def test_footer_shows_portal_version(self):
        self.assertEqual(config.PORTAL_VERSION, "1.5.0")
        self.assertIn("V1.5.0", self.request("admin-session", "/dashboard")[1].decode())


class BranchAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        storage.configure_storage(Path(self.temporary.name))
        auth.create_user("11444777000161", "Farmácia Matriz", "store", "senha-matriz-demo-123")
        auth.create_user("11444777000242", "Farmácia Filial", "store", "senha-filial-demo-123")
        auth.create_user("11222333000181", "Outra Rede", "store", "senha-outra-rede-123")
        with storage.connect() as db:
            self.matrix_id = db.execute("SELECT id FROM users WHERE cnpj=?", ("11444777000161",)).fetchone()["id"]
            self.branch_id = db.execute("SELECT id FROM users WHERE cnpj=?", ("11444777000242",)).fetchone()["id"]
            other_id = db.execute("SELECT id FROM users WHERE cnpj=?", ("11222333000181",)).fetchone()["id"]
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash("matrix-test-session"), self.matrix_id, "csrf-test", int(time.time()) + 3600))
            self.branch_document = self.add_document(db, self.branch_id, "filial.txt", b"documento da filial")
            self.other_document = self.add_document(db, other_id, "outra-rede.txt", b"documento de outra rede")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def add_document(self, db, store_id, filename, content):
        document_id = str(uuid.uuid4())
        (storage.DOCUMENTS / f"{document_id}.txt").write_bytes(content)
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


class PublicationForecastTests(unittest.TestCase):
    def test_pending_month_shows_forecast_and_help_for_large_groups(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage.configure_storage(Path(temporary))
            try:
                for cnpj in ("11444777000161", "11444777000242", "11444777000323", "11444777000404"):
                    auth.create_user(cnpj, f"Rede Exemplo - {cnpj[8:12]}", "store", "senha-exemplo-123")
                with storage.connect() as db:
                    store_id = db.execute("SELECT id FROM users WHERE cnpj='11444777000161'").fetchone()["id"]
                    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash("forecast-session"), store_id, "csrf", int(time.time()) + 3600))
                server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
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
            storage.configure_storage(Path(temporary))
            try:
                auth.create_user("11222333000181", "Farmácia Exemplo", "store", "senha-exemplo-123")
                content = FISCAL_SAMPLE.encode()
                with storage.connect() as db:
                    store_id = db.execute("SELECT id FROM users").fetchone()["id"]
                    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash("summary-session"), store_id, "csrf", int(time.time()) + 3600))
                    db.execute("INSERT INTO documents VALUES (?, ?, 'fiscal', '2026-09', 'sped.txt', ?, ?, ?)", ("doc-1", store_id, len(content), hashlib.sha256(content).hexdigest(), "2026-10-12T00:00:00+00:00"))
                (storage.DOCUMENTS / "doc-1.txt").write_bytes(content)
                server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
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
                with storage.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM document_summaries").fetchone()[0], 1)
            finally:
                restore_storage()


STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ = "11222333000181", "11444777000161", "45543915000181"


class AutoPublishTests(unittest.TestCase):
    """Publicação automática: o portal acha a loja pelo CNPJ, a competência pelo período e a aba pelo tipo do arquivo."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        storage.configure_storage(Path(self.temporary.name))
        auth.create_user(STORE_A_CNPJ, "Farmácia Centro", "store", "senha-centro-123")
        auth.create_user(STORE_B_CNPJ, "Farmácia Bairro", "store", "senha-bairro-123")
        auth.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        with storage.connect() as db:
            ids = {row["name"]: row["id"] for row in db.execute("SELECT id, name FROM users")}
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash("admin-session"), ids["Admin Exemplo"], "csrf-admin", int(time.time()) + 3600))
        self.store_a, self.store_b = ids["Farmácia Centro"], ids["Farmácia Bairro"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
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
        with storage.connect() as db:
            return [dict(row) for row in db.execute("SELECT store_id, document_type, competence, original_name FROM documents ORDER BY uploaded_at")]

    def stored_files(self):
        return sorted(path.name for path in storage.DOCUMENTS.iterdir())

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

    def test_popup_disappears_when_closed(self):
        # "OK, entendi" fecha o <dialog> tirando o atributo open; o CSS não pode manter o pop-up visível sem ele.
        css = (config.ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".publish-dialog:not([open]){display:none}", css)
        status, _, page = self.post_file(sped(FISCAL_SAMPLE))
        self.assertEqual(status, 200)
        self.assertIn("<form method='dialog'><button", page)

    def test_stylesheet_address_changes_with_its_content_so_browser_cache_is_refreshed(self):
        with urlopen(Request(self.base + "/dashboard", headers={"Cookie": "portal_session=admin-session"})) as response:
            page = response.read().decode()
        self.assertIn(f"href='/static/styles.css?v={layout.STYLES_VERSION}'", page)
        expected = hashlib.sha256((config.ROOT / "static" / "styles.css").read_bytes()).hexdigest()[:10]
        self.assertEqual(layout.STYLES_VERSION, expected)
        with urlopen(self.base + f"/static/styles.css?v={layout.STYLES_VERSION}") as response:
            self.assertIn(b".publish-dialog:not([open])", response.read())

    def test_popup_closes_with_escape_key(self):
        # O Esc só fecha um <dialog> aberto como modal; o script reabre o pop-up com showModal() e ainda trata o Esc.
        status, _, page = self.post_file(sped(FISCAL_SAMPLE))
        self.assertEqual(status, 200)
        self.assertIn(f"<script src='/static/portal.js?v={layout.SCRIPT_VERSION}' defer></script>", page)
        with urlopen(self.base + f"/static/portal.js?v={layout.SCRIPT_VERSION}") as response:
            self.assertEqual(response.headers["Content-Type"], "text/javascript; charset=utf-8")
            script = response.read().decode()
        self.assertIn("dialog.publish-dialog[open]", script)
        self.assertIn("showModal()", script)
        self.assertIn('event.key !== "Escape"', script)
        # A CSP bloqueia script inline; o pop-up precisa funcionar só com o arquivo externo.
        self.assertNotIn("<script>", page)
        self.assertNotIn("onclick", page)

    def test_popup_only_appears_right_after_publishing(self):
        _, url, _ = self.post_file(sped(FISCAL_SAMPLE))
        plain = url.split("&published=")[0].removeprefix(self.base)
        with urlopen(Request(self.base + plain, headers={"Cookie": "portal_session=admin-session"})) as response:
            self.assertNotIn("publish-dialog", response.read().decode())


if __name__ == "__main__":
    unittest.main()