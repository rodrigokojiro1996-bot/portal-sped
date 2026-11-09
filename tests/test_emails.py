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

import json
import urllib.error

from portal import auth, config, contatos, emails, envio, publicacao
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


class FakeResend:
    """Faz o papel da API do Resend: guarda cada requisição e devolve as respostas na ordem combinada."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        status, body = self.responses.pop(0)
        if isinstance(status, Exception):
            raise status
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "erro", {}, io.BytesIO(json.dumps(body).encode()))
        return io.BytesIO(json.dumps(body).encode())

    def payload(self, index=-1):
        return json.loads(self.requests[index].data)

    def key(self, index=-1):
        return self.requests[index].get_header("Idempotency-key")


SENT = (200, {"id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"})


class RealSendTests(QueueCase):
    """Envio real ligado, com a API do Resend simulada: nenhum teste sai para a internet."""

    def setUp(self):
        super().setUp()
        for name, value in (("EMAIL_SEND", True), ("RESEND_API_KEY", "re_chave_de_teste"), ("EMAIL_TO_STORES", True)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sleeps = []
        patcher = mock.patch.object(envio, "_sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resend(self, *responses):
        fake = FakeResend(*responses)
        patcher = mock.patch.object(envio, "_urlopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_notice_is_sent_to_the_resend_api_with_everything_it_needs(self):
        fake = self.resend(SENT)
        published = self.publish(file_name="sped-setembro.txt")
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        [request] = fake.requests
        self.assertEqual((request.full_url, request.get_method()), ("https://api.resend.com/emails", "POST"))
        self.assertEqual(request.get_header("Authorization"), "Bearer re_chave_de_teste")
        self.assertTrue(request.get_header("User-agent").startswith("PortalFiscal/"))
        self.assertEqual(fake.key(), f"portal-aviso-{queue()[0]['id']}")
        payload = fake.payload()
        self.assertEqual((payload["from"], payload["to"], payload["cc"]), (config.EMAIL_FROM, ["contador@example.com"], ["loja@example.com"]))
        self.assertEqual(payload["subject"], "SPED Fiscal publicado: Loja Centro - Setembro/2026")
        self.assertIn("sped-setembro.txt", payload["text"])
        self.assertIn("Loja Centro", payload["html"])
        notice = queue()[0]
        self.assertEqual((notice["status"], notice["provider_id"], notice["error"], notice["document_id"]), ("enviado", SENT[1]["id"], None, published["id"]))
        self.assertEqual(notice["html"], payload["html"])

    def test_redirect_sends_only_to_the_test_addresses(self):
        fake = self.resend(SENT)
        self.publish()
        with mock.patch.object(config, "EMAIL_REDIRECT", "teste@example.org"):
            emails.process_pending()
        payload = fake.payload()
        self.assertEqual(payload["to"], ["teste@example.org"])
        self.assertNotIn("cc", payload)
        self.assertNotIn("contador@example.com", json.dumps(payload["to"]))

    def test_sent_notice_is_never_sent_again(self):
        fake = self.resend(SENT)
        self.publish()
        emails.process_pending()
        self.assertEqual(emails.process_pending(), {})
        self.assertEqual(len(fake.requests), 1)

    def test_test_mode_notice_never_becomes_a_real_email_when_sending_is_turned_on(self):
        self.publish()
        with mock.patch.object(config, "EMAIL_SEND", False):
            emails.process_pending()
        fake = self.resend()
        self.assertEqual(emails.process_pending(), {})
        self.assertEqual(fake.requests, [])

    def test_rate_limit_waits_and_tries_again_with_the_same_key(self):
        fake = self.resend((429, {"name": "rate_limit_exceeded"}), SENT)
        self.publish()
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(fake.key(0), fake.key(1))
        self.assertIn(envio.RETRY_WAITS[0], self.sleeps)

    def test_server_error_is_retried_and_then_left_for_later_as_maybe_sent(self):
        fake = self.resend(*[(500, {"name": "internal_server_error"})] * (len(envio.RETRY_WAITS) + 1))
        self.publish()
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(len(fake.requests), len(envio.RETRY_WAITS) + 1)
        notice = queue()[0]
        self.assertEqual((notice["error"], notice["uncertain"]), ("sem confirmação do Resend (internal_server_error)", 1))

    def test_persistent_rate_limit_is_not_taken_as_maybe_sent(self):
        self.resend(*[(429, {"name": "rate_limit_exceeded"})] * (len(envio.RETRY_WAITS) + 1))
        self.publish()
        emails.process_pending()
        notice = queue()[0]
        self.assertEqual((notice["error"], notice["uncertain"]), ("o Resend continua indisponível (rate_limit_exceeded)", 0))

    def test_unreachable_resend_does_not_start_the_key_window(self):
        import socket
        self.resend((urllib.error.URLError(socket.gaierror(11001, "sem nome")), None), (urllib.error.URLError(ConnectionRefusedError()), None))
        self.publish()
        emails.process_pending()
        notice = queue()[0]
        self.assertEqual((notice["error"], notice["uncertain"], notice["key_sent_at"]), ("sem conexão com o Resend (gaierror)", 0, None))
        emails.process_pending()
        self.assertEqual((queue()[0]["uncertain"], queue()[0]["key_sent_at"]), (0, None))

    def test_failure_before_sending_keeps_the_protection_against_duplicates(self):
        self.resend((urllib.error.URLError("sem rede"), None))
        self.publish()
        emails.process_pending()
        before = queue()[0]
        with storage.connect() as db:
            db.execute("DELETE FROM store_contacts")
        emails.process_pending()
        after = queue()[0]
        self.assertEqual(after["error"], "A loja não tem mais contato cadastrado.")
        self.assertEqual((after["uncertain"], after["key_sent_at"], after["idempotency_key"], after["html"]),
                         (1, before["key_sent_at"], before["idempotency_key"], before["html"]))

    def test_maybe_sent_old_notice_is_retried_with_its_key_and_not_called_old(self):
        fake = self.resend((urllib.error.URLError("sem rede"), None), SENT)
        self.publish()
        emails.process_pending()
        moment = (datetime.now(timezone.utc) - timedelta(days=emails.STALE_DAYS + 1)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET created_at=?", (moment,))
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        self.assertEqual(fake.key(0), fake.key(1))

    def test_confirmation_only_applies_to_stopped_notices(self):
        self.resend((urllib.error.URLError("sem rede"), None))
        self.publish()
        emails.process_pending()
        self.assertEqual(queue()[0]["status"], "erro")
        self.assertFalse(emails.confirm_resend(str(queue()[0]["id"])))
        self.assertEqual(queue()[0]["uncertain"], 1)

    def test_daily_quota_is_not_retried(self):
        fake = self.resend((429, {"name": "daily_quota_exceeded"}))
        self.publish()
        emails.process_pending()
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(queue()[0]["error"], "recusado pelo Resend (daily_quota_exceeded)")

    def test_refusal_is_recorded_without_the_message_of_the_provider(self):
        self.resend((403, {"name": "validation_error", "message": "You can only send testing emails to your own email address (contador@example.com)."}))
        self.publish()
        emails.process_pending()
        [notice] = queue()
        self.assertEqual((notice["status"], notice["error"]), ("erro", "recusado pelo Resend (validation_error)"))
        self.assertNotIn("contador@example.com", notice["error"])

    def test_same_key_with_other_content_rotates_the_key_once_and_keeps_it(self):
        fake = self.resend((409, {"name": "invalid_idempotent_request"}), SENT)
        self.publish()
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        notice = queue()[0]
        self.assertEqual(fake.key(0), f"portal-aviso-{notice['id']}")
        self.assertTrue(fake.key(1).startswith(f"portal-aviso-{notice['id']}-r"))
        self.assertEqual(notice["idempotency_key"], fake.key(1))

    def test_rotated_key_is_reused_in_later_attempts(self):
        fake = self.resend((409, {"name": "invalid_idempotent_request"}), (urllib.error.URLError("sem rede"), None), SENT)
        self.publish()
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        self.assertEqual(fake.key(1), fake.key(2))

    def test_conflict_after_a_lost_answer_waits_for_a_person(self):
        fake = self.resend((urllib.error.URLError("sem rede"), None), (409, {"name": "invalid_idempotent_request"}))
        self.publish()
        emails.process_pending()
        self.assertEqual(emails.process_pending(), {"erro": 1})
        [notice] = queue()
        self.assertEqual((notice["error"], notice["attempts"]), (emails.CONFLICT_NOTE, emails.MAX_ATTEMPTS))
        self.assertEqual(len(fake.requests), 2)

    def test_lost_answer_mark_survives_a_later_different_error(self):
        fake = self.resend((urllib.error.URLError("sem rede"), None), *[(429, {"name": "rate_limit_exceeded"})] * (len(envio.RETRY_WAITS) + 1))
        self.publish()
        emails.process_pending()
        emails.process_pending()
        self.assertEqual(queue()[0]["error"], "o Resend continua indisponível (rate_limit_exceeded)")
        self.assertEqual(queue()[0]["uncertain"], 1)
        old = (datetime.now(timezone.utc) - timedelta(hours=emails.UNANSWERED_LIMIT_HOURS + 1)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET key_sent_at=?", (old,))
        requests_before = len(fake.requests)
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(queue()[0]["error"], emails.UNANSWERED_NOTE)
        self.assertEqual(len(fake.requests), requests_before)

    def test_key_window_counts_from_the_first_send_not_from_the_publication(self):
        self.resend((urllib.error.URLError("sem rede"), None), SENT)
        self.publish()
        moment = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET created_at=?", (moment,))
        emails.process_pending()
        self.assertEqual(emails.process_pending(), {"enviado": 1})

    def test_connection_cut_while_reading_the_answer_counts_as_no_answer(self):
        import http.client as http_client
        self.resend((http_client.IncompleteRead(b""), None))
        self.publish()
        emails.process_pending()
        [notice] = queue()
        self.assertTrue(notice["error"].startswith(envio.NO_ANSWER))
        self.assertEqual(notice["uncertain"], 1)

    def test_turning_real_sending_off_does_not_close_a_notice_that_may_have_left(self):
        self.resend((urllib.error.URLError("sem rede"), None))
        self.publish()
        emails.process_pending()
        with mock.patch.object(config, "EMAIL_SEND", False):
            self.assertEqual(emails.process_pending(), {})
        self.assertEqual(queue()[0]["status"], "erro")

    def test_old_notice_waits_for_confirmation_and_is_sent_after_it(self):
        fake = self.resend(SENT)
        self.publish()
        moment = (datetime.now(timezone.utc) - timedelta(days=emails.STALE_DAYS + 1)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET created_at=?", (moment,))
        self.assertEqual(emails.process_pending(), {"erro": 1})
        notice = queue()[0]
        self.assertEqual((notice["error"], notice["attempts"], fake.requests), (emails.STALE_NOTE, emails.MAX_ATTEMPTS, []))
        self.assertTrue(emails.confirm_resend(str(notice["id"])))
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        self.assertTrue(fake.key().startswith(f"portal-aviso-{notice['id']}-a"))

    def test_confirmation_only_applies_to_notices_with_error(self):
        # Enviado só na última tentativa: está no teto, e mesmo assim não pode voltar para a fila.
        self.resend(*[(429, {"name": "rate_limit_exceeded"})] * (len(envio.RETRY_WAITS) + 1) * (emails.MAX_ATTEMPTS - 1), SENT)
        self.publish()
        for _ in range(emails.MAX_ATTEMPTS):
            emails.process_pending()
        notice_id = str(queue()[0]["id"])
        self.assertEqual((queue()[0]["status"], queue()[0]["attempts"]), ("enviado", emails.MAX_ATTEMPTS))
        self.assertFalse(emails.confirm_resend(notice_id))
        for value in ("abc", "9" * 30, "%C2%B2"):
            self.assertFalse(emails.confirm_resend(value))

    def test_contact_without_valid_address_fails_before_calling_resend(self):
        fake = self.resend()
        self.publish()
        with storage.connect() as db:
            db.execute("UPDATE store_contacts SET recipients='ligar na loja', copies=''")
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual((queue()[0]["error"], fake.requests), (emails.NO_ADDRESS, []))

    def test_lost_answer_is_retried_later_with_the_same_key(self):
        fake = self.resend((urllib.error.URLError("sem rede"), None), SENT)
        self.publish()
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(queue()[0]["error"], "sem confirmação do Resend (URLError)")
        self.assertEqual(emails.process_pending(), {"enviado": 1})
        self.assertEqual(fake.key(0), fake.key(1))

    def test_unanswered_notice_past_the_key_window_waits_for_a_person(self):
        fake = self.resend((TimeoutError("lento"), None))
        self.publish()
        emails.process_pending()
        moment = (datetime.now(timezone.utc) - timedelta(hours=emails.UNANSWERED_LIMIT_HOURS + 1)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET key_sent_at=?", (moment,))
        self.assertEqual(emails.process_pending(), {"erro": 1})
        [notice] = queue()
        self.assertEqual((notice["error"], notice["attempts"]), (emails.UNANSWERED_NOTE, emails.MAX_ATTEMPTS))
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(emails.process_pending(), {})

    def test_answer_without_id_is_not_taken_as_sent_but_as_maybe_sent(self):
        self.resend((200, {}))
        self.publish()
        self.assertEqual(emails.process_pending(), {"erro": 1})
        self.assertEqual(queue()[0]["uncertain"], 1)

    def test_requests_are_spaced(self):
        self.resend(SENT, SENT)
        self.publish()
        self.publish(sped(CONTRIBUTIONS_SAMPLE), "contribuicoes.txt")
        envio._last_request = 0.0
        emails.process_pending()
        self.assertTrue(any(0 < wait <= envio.MIN_INTERVAL_SECONDS for wait in self.sleeps), self.sleeps)

    def test_nothing_is_sent_outside_the_resend_api(self):
        fake = self.resend(SENT)
        self.publish()
        with mock.patch("socket.socket.connect", side_effect=AssertionError("tentou abrir uma conexão")):
            emails.process_pending()
        self.assertEqual({request.host for request in fake.requests}, {"api.resend.com"})


class EmailHtmlTests(QueueCase):
    def document(self, name="Loja Centro"):
        published = self.publish()
        with storage.connect() as db:
            db.execute("UPDATE users SET name=? WHERE cnpj=?", (name, STORE_A_CNPJ))
            return db.execute("SELECT documents.*, users.name AS store_name, users.cnpj AS store_cnpj FROM documents JOIN users ON users.id=documents.store_id WHERE documents.id=?",
                              (published["id"],)).fetchone()

    def test_store_data_is_escaped(self):
        page = emails.build_html(self.document("<script>alert(1)</script>"))
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)

    def test_same_notice_always_gives_the_same_html(self):
        document = self.document()
        first = emails.build_html(document)
        time.sleep(1.1)
        self.assertEqual(emails.build_html(document), first)

    def test_link_becomes_a_button_only_with_a_public_address(self):
        document = self.document()
        self.assertNotIn("<a ", emails.build_html(document))
        with mock.patch.object(config, "PUBLIC_URL", "https://portal.example.com"):
            page = emails.build_html(document)
        self.assertIn("href='https://portal.example.com/dashboard?store_id=", page)
        self.assertIn("Abrir no Portal Fiscal", page)

    def test_every_text_block_declares_background_and_color_for_dark_mode(self):
        page = emails.build_html(self.document())
        import re
        for tag, style in re.findall(r"<(p|h1|td)[^>]*style='([^']*)'", page):
            if tag != "td" or "font-size" in style:
                self.assertIn("background", style, (tag, style))
                self.assertIn("color", style, (tag, style))


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

    def test_button_processes_what_is_pending_in_the_background(self):
        self.publish()
        status, page = self.request("admin", "/emails/reprocessar", {"csrf": "csrf-admin"})
        self.assertEqual(status, 200)
        self.assertIn("Processando os avisos pendentes", page)
        wait_for_queue()
        self.assertEqual(queue()[0]["status"], "ensaio")

    def test_button_does_not_wait_for_a_slow_provider(self):
        self.publish()
        with mock.patch.object(emails, "process_pending", side_effect=lambda *args: time.sleep(2)):
            started = time.monotonic()
            self.request("admin", "/emails/reprocessar", {"csrf": "csrf-admin"})
            self.assertLess(time.monotonic() - started, 1.5)
            wait_for_queue()

    def stop_notice(self):
        self.publish()
        moment = (datetime.now(timezone.utc) - timedelta(days=emails.STALE_DAYS + 1)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE email_notifications SET created_at=?", (moment,))
        with mock.patch.object(config, "EMAIL_SEND", True), mock.patch.object(envio, "_urlopen", FakeResend()):
            emails.process_pending()
        return queue()[0]["id"]

    def test_stopped_notice_offers_resend_and_does_not_say_it_tried_five_times(self):
        self.stop_notice()
        with mock.patch.object(config, "EMAIL_SEND", True):
            page = self.request("admin", "/emails")[1]
        self.assertIn("Reenviar este aviso", page)
        self.assertNotIn("Parou após", page)

    def test_maybe_sent_stopped_notice_warns_before_resending(self):
        self.publish()
        fake = FakeResend(*[(urllib.error.URLError("sem rede"), None)] * emails.MAX_ATTEMPTS)
        with mock.patch.object(config, "EMAIL_SEND", True), mock.patch.object(config, "RESEND_API_KEY", "re_teste"), mock.patch.object(envio, "_urlopen", fake):
            for _ in range(emails.MAX_ATTEMPTS):
                emails.process_pending()
            page = self.request("admin", "/emails")[1]
        self.assertIn("Conferi no Resend: reenviar", page)
        # O erro do Resend vira frase na tela, para não emendar com o que vem depois.
        self.assertIn(f"Sem confirmação do Resend (URLError). Parou após {emails.MAX_ATTEMPTS} tentativas. Pode já ter saído: confira no painel do Resend antes de reenviar.", page)

    def test_resend_button_confirms_and_sends_in_the_background(self):
        notice_id = self.stop_notice()
        fake = FakeResend(SENT)
        with mock.patch.object(config, "EMAIL_SEND", True), mock.patch.object(config, "RESEND_API_KEY", "re_teste"), mock.patch.object(envio, "_urlopen", fake):
            status, page = self.request("admin", "/emails/reenviar", {"csrf": "csrf-admin", "id": str(notice_id)})
            wait_for_queue()
        self.assertEqual(status, 200)
        self.assertIn("Reenvio confirmado", page)
        self.assertEqual(queue()[0]["status"], "enviado")
        self.assertEqual(len(fake.requests), 1)

    def test_resend_needs_the_administration_the_form_token_and_real_sending(self):
        notice_id = str(self.stop_notice())
        with mock.patch.object(config, "EMAIL_SEND", True):
            for token in ("suporte", "loja"):
                self.assertEqual(self.request(token, "/emails/reenviar", {"csrf": "csrf-" + token, "id": notice_id})[0], 403, token)
            self.assertEqual(self.request("admin", "/emails/reenviar", {"csrf": "errado", "id": notice_id})[0], 403)
        self.assertEqual(self.request("admin", "/emails/reenviar", {"csrf": "csrf-admin", "id": notice_id})[0], 403)
        self.assertEqual(queue()[0]["confirmed"], 0)

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

    def preview(self, token, notice_id):
        connection = __import__("http.client").client.HTTPConnection("127.0.0.1", int(self.server.base.rsplit(":", 1)[1]))
        self.addCleanup(connection.close)
        connection.request("GET", f"/emails/previa?id={notice_id}", headers={"Cookie": f"portal_session={token}"})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Security-Policy"), response.read().decode()

    def test_admin_sees_the_notice_as_an_email_in_an_isolated_page(self):
        self.publish()
        emails.process_pending()
        notice_id = queue()[0]["id"]
        self.assertIn(f"/emails/previa?id={notice_id}", self.request("admin", "/emails")[1])
        status, policy, page = self.preview("admin", notice_id)
        self.assertEqual(status, 200)
        self.assertIn("sandbox", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("script-src", policy)
        self.assertNotIn("default-src 'self'", policy)  # a política padrão não vai junto: os estilos do e-mail ficariam bloqueados
        self.assertIn("Loja Centro", page)

    def test_preview_is_only_for_the_administration_and_known_notices(self):
        self.publish()
        emails.process_pending()
        notice_id = queue()[0]["id"]
        for token in ("suporte", "loja"):
            self.assertEqual(self.preview(token, notice_id)[0], 403, token)
        for value in ("999", "abc", "%C2%B2", "9" * 30):
            self.assertEqual(self.preview("admin", value)[0], 404, value)

    def test_screen_says_when_real_sending_is_on(self):
        self.assertIn("Modo de teste:", self.request("admin", "/emails")[1])
        with mock.patch.object(config, "EMAIL_SEND", True):
            page = self.request("admin", "/emails")[1]
        self.assertIn("Envio real ligado", page)
        self.assertNotIn("Modo de teste:", page)

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
