import contextlib
import io
import sqlite3
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from portal import auth, config, contatos, emails, publicacao
from portal import db as storage
from util import CONTRIBUTIONS_SAMPLE, FISCAL_SAMPLE, STORE_A_CNPJ, STORE_B_CNPJ, TestServer, sped, temporary_storage


def wait_for_queue():
    for thread in threading.enumerate():
        if thread.name == emails.WORKER_NAME:
            thread.join(5)


def queue():
    with storage.connect() as db:
        return [dict(item) for item in db.execute("SELECT * FROM email_notifications ORDER BY id")]


class QueueCase(unittest.TestCase):
    """Fila ligada, Loja Centro com contato e Loja Bairro sem contato."""

    def setUp(self):
        temporary_storage(self)
        patcher = mock.patch.object(config, "EMAIL_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        auth.create_user(STORE_A_CNPJ, "Loja Centro", "store", "senha-centro-123")
        auth.create_user(STORE_B_CNPJ, "Loja Bairro", "store", "senha-bairro-123")
        contatos.save_contacts({STORE_A_CNPJ: (["contador@example.com"], ["loja@example.com"])})

    def publish(self, content=None, file_name="sped.txt"):
        return publicacao.publish_sped(content or sped(FISCAL_SAMPLE), file_name)


class EnqueueTests(QueueCase):
    def test_publication_leaves_a_pending_notice(self):
        published = self.publish()
        [notice] = queue()
        self.assertEqual((notice["document_id"], notice["status"], notice["attempts"], notice["recipients"]), (published["id"], "pendente", 0, None))

    def test_store_without_contact_publishes_normally_and_generates_nothing(self):
        published = self.publish(sped(FISCAL_SAMPLE, STORE_B_CNPJ))
        self.assertEqual(published["store_name"], "Loja Bairro")
        self.assertEqual(queue(), [])

    def test_queue_turned_off_records_nothing(self):
        with mock.patch.object(config, "EMAIL_ENABLED", False):
            self.publish()
            self.assertEqual(emails.process_pending(), {})
        self.assertEqual(queue(), [])

    def test_same_file_twice_generates_one_notice(self):
        self.publish()
        with self.assertRaises(publicacao.PublishError):
            self.publish()
        self.assertEqual(len(queue()), 1)

    def test_each_published_file_has_its_own_notice(self):
        self.publish()
        self.publish(sped(CONTRIBUTIONS_SAMPLE), "contribuicoes.txt")
        self.assertEqual(len(queue()), 2)

    def test_notice_and_document_are_saved_together(self):
        with mock.patch.object(emails, "enqueue", side_effect=sqlite3.OperationalError("disco cheio")):
            with self.assertRaises(sqlite3.OperationalError):
                self.publish()
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        self.assertEqual(list(storage.DOCUMENTS.iterdir()), [])


class ProcessTests(QueueCase):
    def test_notice_is_built_and_recorded_as_test(self):
        self.publish(file_name="sped-setembro.txt")
        self.assertEqual(emails.process_pending(), {"ensaio": 1})
        [notice] = queue()
        self.assertEqual((notice["status"], notice["recipients"], notice["copies"], notice["attempts"]), ("ensaio", "contador@example.com", "loja@example.com", 1))
        self.assertEqual(notice["subject"], "SPED Fiscal publicado: Loja Centro - Setembro/2026")
        self.assertIn("Loja Centro (CNPJ 11.222.333/0001-81)", notice["body"])
        self.assertIn("Competência: Setembro/2026", notice["body"])
        self.assertIn("Arquivo: sped-setembro.txt", notice["body"])
        self.assertEqual(notice["error"], emails.DRY_RUN_NOTE)

    def test_nothing_leaves_the_portal(self):
        self.publish()
        with mock.patch("socket.socket.connect", side_effect=AssertionError("tentou abrir uma conexão")):
            self.assertEqual(emails.process_pending(), {"ensaio": 1})

    def test_test_notice_is_final_and_is_never_processed_again(self):
        self.publish()
        emails.process_pending()
        self.assertEqual(emails.process_pending(), {})
        self.assertEqual(queue()[0]["attempts"], 1)

    def test_partial_period_appears_in_the_message(self):
        self.publish(sped(FISCAL_SAMPLE, period="01092026|15092026"))
        emails.process_pending()
        self.assertIn("período parcial de 01/09/2026 a 15/09/2026", queue()[0]["body"])

    def test_link_only_appears_with_public_address(self):
        published = self.publish()
        emails.process_pending()
        self.assertNotIn("http", queue()[0]["body"])
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET status='pendente'")
        with mock.patch.object(config, "PUBLIC_URL", "https://portal.example.com"):
            emails.process_pending()
        self.assertIn(f"https://portal.example.com/dashboard?store_id={published['store_id']}&type=fiscal&competence=2026-09", queue()[0]["body"])
        self.assertNotIn("certificado", queue()[0]["body"])

    def test_link_asks_for_the_certificate_only_with_the_entry_turned_on(self):
        published = self.publish()
        with mock.patch.object(config, "PUBLIC_URL", "https://portal.example.com"), mock.patch.object(config, "CERT_PROXY_SECRET", "s" * 32):
            emails.process_pending()
        self.assertIn(f"https://portal.example.com/certificado?store_id={published['store_id']}&type=fiscal&competence=2026-09", queue()[0]["body"])
        self.assertIn("computador que tem o certificado digital da loja", queue()[0]["body"])

    def test_redirect_replaces_every_real_address(self):
        self.publish()
        with mock.patch.object(config, "EMAIL_REDIRECT", "teste@example.org; outro@example.org"):
            emails.process_pending()
        [notice] = queue()
        self.assertEqual((notice["recipients"], notice["copies"]), ("teste@example.org, outro@example.org", ""))
        self.assertTrue(notice["subject"].startswith("[TESTE para contador@example.com] "))

    def test_broken_redirect_fails_instead_of_reaching_the_real_address(self):
        self.publish()
        with mock.patch.object(config, "EMAIL_REDIRECT", "nao-e-um-endereco"):
            self.assertEqual(emails.process_pending(), {"erro": 1})
        [notice] = queue()
        self.assertEqual((notice["status"], notice["recipients"], notice["copies"]), ("erro", None, None))
        self.assertIn("nenhum endereço válido", notice["error"])

    def test_contact_removed_after_publication_becomes_an_explained_error(self):
        self.publish()
        with storage.connect() as db:
            db.execute("DELETE FROM store_contacts")
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(queue()[0]["error"], "A loja não tem mais contato cadastrado.")

    def test_unexpected_failure_is_recorded_without_repeating_its_message(self):
        self.publish()
        with mock.patch.object(emails, "build_message", side_effect=ValueError("contador@example.com recusado")):
            self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(queue()[0]["error"], "falha inesperada (ValueError)")

    def test_failed_notice_is_retried_and_can_succeed(self):
        self.publish()
        with mock.patch.object(emails, "build_message", side_effect=ValueError):
            emails.process_pending()
        self.assertEqual(emails.process_pending(), {"ensaio": 1})
        self.assertEqual(queue()[0]["attempts"], 2)

    def test_attempts_stop_at_the_limit_and_the_notice_stays_visible(self):
        self.publish()
        with mock.patch.object(emails, "build_message", side_effect=ValueError):
            for _ in range(emails.MAX_ATTEMPTS + 2):
                emails.process_pending()
        [notice] = queue()
        self.assertEqual((notice["status"], notice["attempts"]), ("erro", emails.MAX_ATTEMPTS))

    def test_limit_leaves_the_rest_for_the_next_round_oldest_first(self):
        first = self.publish()
        self.publish(sped(CONTRIBUTIONS_SAMPLE), "contribuicoes.txt")
        self.assertEqual(emails.process_pending(limit=1), {"ensaio": 1})
        self.assertEqual([(notice["document_id"] == first["id"], notice["status"]) for notice in queue()], [(True, "ensaio"), (False, "pendente")])


class SweepTests(QueueCase):
    def age_first_notice(self, hours):
        moment = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET created_at=? WHERE id=(SELECT MIN(id) FROM email_notifications)", (moment,))

    def sweep(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            emails.sweep_on_start()
            wait_for_queue()
        return output.getvalue()

    def test_sweep_resumes_what_stayed_pending_and_says_so(self):
        self.publish()
        self.assertIn("1 aviso(s) processado(s), 0 com erro", self.sweep())
        self.assertEqual(queue()[0]["status"], "ensaio")

    def test_sweep_leaves_old_notices_for_the_button(self):
        self.publish()
        self.age_first_notice(emails.SWEEP_MAX_HOURS + 1)
        self.assertEqual(self.sweep(), "")
        self.assertEqual(queue()[0]["status"], "pendente")
        self.assertEqual(emails.process_pending(), {"ensaio": 1})

    def test_sweep_does_nothing_with_queue_turned_off(self):
        self.publish()
        with mock.patch.object(config, "EMAIL_ENABLED", False):
            self.sweep()
        self.assertEqual(queue()[0]["status"], "pendente")


class EmailScreenTests(QueueCase):
    def setUp(self):
        super().setUp()
        auth.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        auth.create_support_user("suporte.teste", "Suporte Exemplo")
        with storage.connect() as db:
            ids = {item["name"]: item["id"] for item in db.execute("SELECT id, name FROM users")}
            for name, token in (("Admin Exemplo", "admin"), ("Suporte Exemplo", "suporte"), ("Loja Centro", "loja")):
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash(token), ids[name], "csrf-" + token, int(time.time()) + 3600))
        self.server = TestServer()
        self.addCleanup(self.server.stop)
        self.addCleanup(wait_for_queue)

    def request(self, token, path, data=None):
        body = urlencode(data).encode() if data is not None else None
        try:
            with urlopen(Request(self.server.base + path, data=body, headers={"Cookie": f"portal_session={token}"})) as response:
                return response.status, response.read().decode()
        except HTTPError as error:
            with error:
                return error.code, error.read().decode()

    def upload(self, content):
        boundary = "----portal" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\ncsrf-admin\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sped.txt\"\r\nContent-Type: text/plain\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = Request(self.server.base + "/upload", data=body, headers={"Cookie": "portal_session=admin", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(request) as response:
            return response.status, response.read().decode()

    def test_upload_answers_normally_and_the_notice_is_processed_in_the_background(self):
        status, page = self.upload(sped(FISCAL_SAMPLE))
        self.assertEqual(status, 200)
        self.assertIn("Arquivo publicado com sucesso", page)
        wait_for_queue()
        self.assertEqual(queue()[0]["status"], "ensaio")

    def test_failure_in_the_queue_does_not_reach_who_published(self):
        with mock.patch.object(emails, "process_pending", side_effect=sqlite3.OperationalError("banco ocupado")), mock.patch("sys.stderr"):
            status, page = self.upload(sped(FISCAL_SAMPLE))
            wait_for_queue()
        self.assertEqual(status, 200)
        self.assertIn("Arquivo publicado com sucesso", page)
        self.assertEqual(queue()[0]["status"], "pendente")

    def test_admin_sees_the_queue_and_the_test_mode_warning(self):
        self.upload(sped(FISCAL_SAMPLE))
        wait_for_queue()
        status, page = self.request("admin", "/emails")
        self.assertEqual(status, 200)
        self.assertIn("Modo de teste:", page)
        self.assertIn("nenhum e-mail sai do portal", page)
        self.assertIn("1 loja tem contato cadastrado", page)
        self.assertIn("Loja Centro", page)
        self.assertIn("contador@example.com", page)
        self.assertIn("Cópia: loja@example.com", page)
        self.assertIn("Ver mensagem", page)

    def test_button_processes_what_is_pending(self):
        self.publish()
        status, page = self.request("admin", "/emails/reprocessar", {"csrf": "csrf-admin"})
        self.assertEqual(status, 200)
        self.assertIn("1 aviso processado", page)
        self.assertEqual(queue()[0]["status"], "ensaio")

    def test_button_needs_the_form_token(self):
        self.publish()
        status, _ = self.request("admin", "/emails/reprocessar", {"csrf": "errado"})
        self.assertEqual(status, 403)
        self.assertEqual(queue()[0]["status"], "pendente")

    def test_only_the_administration_reaches_the_queue(self):
        self.publish()
        for token in ("suporte", "loja"):
            self.assertEqual(self.request(token, "/emails")[0], 403, token)
            self.assertEqual(self.request(token, "/emails/reprocessar", {"csrf": "csrf-" + token})[0], 403, token)
        self.assertEqual(queue()[0]["status"], "pendente")

    def test_sidebar_link_follows_the_switch(self):
        self.assertIn("href='/emails'", self.request("admin", "/dashboard")[1])
        self.assertNotIn("href='/emails'", self.request("loja", "/dashboard")[1])
        with mock.patch.object(config, "EMAIL_ENABLED", False):
            self.assertNotIn("href='/emails'", self.request("admin", "/dashboard")[1])
            self.assertEqual(self.request("admin", "/emails")[0], 404)

    def test_store_data_is_escaped_on_the_screen(self):
        with storage.connect() as db:
            db.execute("UPDATE users SET name='<script>alert(1)</script>' WHERE cnpj=?", (STORE_A_CNPJ,))
        self.publish()
        emails.process_pending()
        page = self.request("admin", "/emails")[1]
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert", page)


if __name__ == "__main__":
    unittest.main()
