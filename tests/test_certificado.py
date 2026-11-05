import base64
import contextlib
import http.client
import io
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import quote, urlencode, urlparse

from portal import auth, certificado, cli, config, publicacao
from portal import db as storage
from portal.sped import valid_cnpj
from portal.views import acesso
from util import FISCAL_SAMPLE, STORE_A_CNPJ, STORE_B_CNPJ, UNREGISTERED_CNPJ, TestServer, sped, temporary_storage

SECRET = "segredo-do-servidor-da-frente-para-testes"
OID_RESPONSIBLE = bytes.fromhex("604C010304")  # 2.16.76.1.3.4: dados do responsável, presente no e-CNPJ
OID_CPF = bytes.fromhex("604C010301")  # 2.16.76.1.3.1: dados do titular do e-CPF
OID_BASIC_CONSTRAINTS = bytes.fromhex("551D13")
PRINTABLE, UTF8, IA5, OCTET = 0x13, 0x0C, 0x16, 0x04


def der(tag, *parts):
    content = b"".join(parts)
    size = len(content)
    if size < 0x80:
        return bytes([tag, size]) + content
    encoded = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(encoded)]) + encoded + content


def other_name(oid, value, tag=PRINTABLE):
    return der(0xA0, der(0x06, oid), der(0xA0, der(tag, value)))


def build_certificate(cnpj=STORE_A_CNPJ, not_before=b"260101000000Z", not_after=b"360101000000Z", time_tag=0x17, value_tag=PRINTABLE, with_version=True, names=None):
    """Certificado X.509 mínimo com a mesma estrutura de um e-CNPJ. A assinatura é falsa: o portal não a confere."""
    if names is None:
        names = [other_name(OID_RESPONSIBLE, b"01011980" + b"1" * 30), other_name(certificado.OID_CNPJ, cnpj.encode(), value_tag), der(0x81, b"loja@example.com")]
    alt_names = der(0x30, der(0x06, certificado.OID_SUBJECT_ALT_NAME), der(0x04, der(0x30, *names)))
    extensions = der(0xA3, der(0x30, der(0x30, der(0x06, OID_BASIC_CONSTRAINTS), der(0x04, der(0x30))), alt_names))
    algorithm = der(0x30, der(0x06, bytes.fromhex("2A864886F70D01010B")), der(0x05))
    name = der(0x30, der(0x31, der(0x30, der(0x06, bytes.fromhex("550403")), der(UTF8, b"Teste"))))
    version = [der(0xA0, der(0x02, b"\x02"))] if with_version else []
    tbs = der(0x30, *version, der(0x02, b"\x01"), algorithm, name, der(0x30, der(time_tag, not_before), der(time_tag, not_after)), name, der(0x30, algorithm, der(0x03, b"\x00")), extensions)
    return der(0x30, tbs, algorithm, der(0x03, b"\x00\x01\x02"))


def as_pem(certificate):
    return "-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(certificate).decode() + "-----END CERTIFICATE-----\n"


def as_header(certificate):
    """Como o nginx repassa em $ssl_client_escaped_cert: o PEM com escape de URL."""
    return quote(as_pem(certificate), safe="")


def filial_of(cnpj, branch="0002"):
    """CNPJ válido da filial, com os dígitos verificadores calculados."""
    return next(candidate for candidate in (f"{cnpj[:8]}{branch}{digits:02d}" for digits in range(100)) if valid_cnpj(candidate))


class ParseCertificateTests(unittest.TestCase):
    def test_reads_cnpj_among_the_other_names(self):
        self.assertEqual(certificado.parse_der(build_certificate()).cnpj, STORE_A_CNPJ)

    def test_reads_cnpj_in_every_string_encoding_used_by_the_authorities(self):
        for tag in (PRINTABLE, UTF8, IA5, OCTET):
            self.assertEqual(certificado.parse_der(build_certificate(value_tag=tag)).cnpj, STORE_A_CNPJ, hex(tag))

    def test_reads_the_layout_of_a_real_certificate(self):
        """Mesma ordem e mesmos tipos de um e-CNPJ real conferido em 05/11/2026, com valores fictícios."""
        names = [other_name(bytes.fromhex("604C010302"), b"X" * 17, OCTET), other_name(certificado.OID_CNPJ, STORE_A_CNPJ.encode(), OCTET),
                 other_name(OID_RESPONSIBLE, b"0" * 55, OCTET), other_name(bytes.fromhex("604C010307"), b"0" * 12, OCTET), der(0x81, b"loja@example.com")]
        self.assertEqual(certificado.parse_der(build_certificate(names=names)).cnpj, STORE_A_CNPJ)

    def test_certificate_without_version_field(self):
        self.assertEqual(certificado.parse_der(build_certificate(with_version=False)).cnpj, STORE_A_CNPJ)

    def test_reads_validity_in_both_time_formats(self):
        short = certificado.parse_der(build_certificate(not_before=b"260102030405Z", not_after=b"491231235959Z"))
        self.assertEqual(short.not_before, datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(short.not_after.year, 2049)
        self.assertEqual(certificado.parse_der(build_certificate(not_before=b"500101000000Z", not_after=b"510101000000Z")).not_before.year, 1950)
        full = certificado.parse_der(build_certificate(not_before=b"20260102030405Z", not_after=b"20510101000000Z", time_tag=0x18))
        self.assertEqual(full.not_after.year, 2051)

    def test_personal_certificate_has_no_cnpj(self):
        with self.assertRaisesRegex(certificado.CertificateError, "não traz um CNPJ"):
            certificado.parse_der(build_certificate(names=[other_name(OID_CPF, b"0101198012345678901")]))

    def test_cnpj_with_wrong_check_digits_is_refused(self):
        with self.assertRaisesRegex(certificado.CertificateError, "Não foi possível ler"):
            certificado.parse_der(build_certificate(cnpj="11222333000199"))

    def test_broken_certificates_are_refused_without_crashing(self):
        good = build_certificate()
        for broken in (b"", b"\x30", b"nao e um certificado", good[:40], good[:-5], good + b"\x00", b"\x31" + good[1:], der(0x30), der(0x30, der(0x30))):
            with self.assertRaises(certificado.CertificateError, msg=broken[:12]):
                certificado.parse_der(broken)

    def test_every_truncation_is_refused_without_crashing(self):
        good = build_certificate()
        for size in range(len(good)):
            with self.assertRaises(certificado.CertificateError):
                certificado.parse_der(good[:size])

    def test_every_single_corrupted_byte_ends_in_a_result_or_a_refusal(self):
        good = build_certificate()
        for index in range(len(good)):
            damaged = good[:index] + bytes([good[index] ^ 0xFF]) + good[index + 1:]
            try:
                certificado.parse_der(damaged)
            except certificado.CertificateError:
                pass


class HeaderTests(unittest.TestCase):
    now = datetime(2027, 6, 1, tzinfo=timezone.utc)

    def test_reads_url_escaped_pem(self):
        self.assertEqual(certificado.read_header(as_header(build_certificate()), self.now).cnpj, STORE_A_CNPJ)

    def test_reads_plain_base64_and_keeps_the_first_of_a_chain(self):
        leaf, other = (base64.b64encode(item).decode() for item in (build_certificate(), build_certificate(cnpj=STORE_B_CNPJ)))
        self.assertEqual(certificado.read_header(leaf, self.now).cnpj, STORE_A_CNPJ)
        self.assertEqual(certificado.read_header(f"{leaf},{other}", self.now).cnpj, STORE_A_CNPJ)

    def test_keeps_the_first_of_a_pem_chain(self):
        chain = as_pem(build_certificate()) + as_pem(build_certificate(cnpj=STORE_B_CNPJ))
        self.assertEqual(certificado.read_header(quote(chain, safe=""), self.now).cnpj, STORE_A_CNPJ)

    def test_expired_and_future_certificates_are_refused(self):
        with self.assertRaisesRegex(certificado.CertificateError, "vencido"):
            certificado.read_header(as_header(build_certificate(not_after=b"270101000000Z")), self.now)
        with self.assertRaisesRegex(certificado.CertificateError, "ainda não está valendo"):
            certificado.read_header(as_header(build_certificate(not_before=b"280101000000Z")), self.now)

    def test_empty_and_invalid_values_are_refused(self):
        for value in ("", "!!!não é base64!!!", "A" * 20000):
            with self.assertRaises(certificado.CertificateError):
                certificado.read_header(value, self.now)


class DestinationTests(unittest.TestCase):
    def test_keeps_only_checked_values(self):
        self.assertEqual(certificado.destination("7", "fiscal", "2026-09"), "/dashboard?store_id=7&type=fiscal&competence=2026-09")
        self.assertEqual(certificado.destination("", "", ""), "/dashboard")

    def test_invalid_values_never_reach_the_address(self):
        self.assertEqual(certificado.destination("", "//evil.example.com", "2026-09\r\nSet-Cookie: x=1"), "/dashboard")


class EntryTests(unittest.TestCase):
    """Matriz e filial da mesma raiz e uma loja de outra raiz."""

    def setUp(self):
        temporary_storage(self)
        patcher = mock.patch.object(config, "CERT_PROXY_SECRET", SECRET)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.filial = filial_of(STORE_A_CNPJ)
        for cnpj, name in ((STORE_A_CNPJ, "Loja Matriz"), (self.filial, "Loja Filial"), (STORE_B_CNPJ, "Outra Rede")):
            auth.create_user(cnpj, name, "store", "senha-da-loja-123")
        with storage.connect() as db:
            self.ids = {item["name"]: item["id"] for item in db.execute("SELECT id, name FROM users")}
        self.server = TestServer()
        self.addCleanup(self.server.stop)

    def enter(self, certificate=None, query="", secret=SECRET, headers=None):
        """GET sem seguir o redirecionamento: devolve a situação, o endereço de destino, o cookie e a página."""
        sent = dict(headers or {})
        if secret is not None:
            sent[certificado.PROXY_HEADER] = secret
        if certificate is not None:
            sent[certificado.CERT_HEADER] = as_header(certificate)
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
        self.addCleanup(connection.close)
        connection.request("GET", "/certificado" + query, headers=sent)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.getheader("Set-Cookie"), response.read().decode()

    def get(self, path, cookie):
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Cookie": cookie.split(";")[0]})
        response = connection.getresponse()
        return response.status, response.read().decode()

    def sessions(self):
        with storage.connect() as db:
            return [item["user_id"] for item in db.execute("SELECT user_id FROM sessions")]

    def test_certificate_of_the_store_opens_the_page_of_the_link(self):
        store = self.ids["Loja Matriz"]
        status, location, cookie, _ = self.enter(build_certificate(), f"?store_id={store}&type=fiscal&competence=2026-09")
        self.assertEqual((status, location), (303, f"/dashboard?store_id={store}&type=fiscal&competence=2026-09"))
        self.assertIn("HttpOnly", cookie)
        page_status, page = self.get(location, cookie)
        self.assertEqual(page_status, 200)
        self.assertIn("Loja Matriz", page)

    def test_only_the_first_8_digits_need_to_match(self):
        store = self.ids["Loja Matriz"]
        status, location, cookie, _ = self.enter(build_certificate(cnpj=self.filial), f"?store_id={store}&type=fiscal")
        self.assertEqual(status, 303)
        self.assertIn("Loja Matriz", self.get(location, cookie)[1])

    def test_store_of_another_root_is_refused_and_no_session_is_created(self):
        status, location, cookie, page = self.enter(build_certificate(cnpj=STORE_B_CNPJ), f"?store_id={self.ids['Loja Matriz']}")
        self.assertEqual((status, location, cookie), (403, None, None))
        self.assertIn("não pertence à loja deste link", page)
        self.assertEqual(self.sessions(), [])

    def test_link_with_unknown_or_invalid_store_is_refused(self):
        for value in ("999999", "abc", "1%20OR%201=1"):
            self.assertEqual(self.enter(build_certificate(), f"?store_id={value}")[0], 403, value)
        self.assertEqual(self.sessions(), [])

    def test_staff_account_is_never_a_valid_link_target(self):
        auth.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        with storage.connect() as db:
            admin = db.execute("SELECT id FROM users WHERE role='admin'").fetchone()["id"]
        self.assertEqual(self.enter(build_certificate(), f"?store_id={admin}")[0], 403)

    def test_without_store_in_the_link_enters_the_store_of_the_certificate(self):
        status, location, _, _ = self.enter(build_certificate(cnpj=self.filial))
        self.assertEqual((status, location), (303, "/dashboard"))
        self.assertEqual(self.sessions(), [self.ids["Loja Filial"]])

    def test_certificate_of_a_branch_not_registered_uses_the_head_office(self):
        self.enter(build_certificate(cnpj=filial_of(STORE_A_CNPJ, "0003")))
        self.assertEqual(self.sessions(), [self.ids["Loja Matriz"]])

    def test_root_without_active_store_is_refused(self):
        status, _, _, page = self.enter(build_certificate(cnpj=UNREGISTERED_CNPJ))
        self.assertEqual(status, 403)
        self.assertIn("Nenhuma loja ativa", page)

    def test_inactive_store_does_not_open(self):
        with storage.connect() as db:
            db.execute("UPDATE users SET active=0 WHERE name='Outra Rede'")
        self.assertEqual(self.enter(build_certificate(cnpj=STORE_B_CNPJ))[0], 403)

    def test_expired_certificate_is_refused(self):
        status, _, _, page = self.enter(build_certificate(not_after=b"250101000000Z"))
        self.assertEqual(status, 403)
        self.assertIn("vencido", page)

    def test_personal_certificate_is_refused_with_explanation(self):
        status, _, _, page = self.enter(build_certificate(names=[other_name(OID_CPF, b"0101198012345678901")]))
        self.assertEqual(status, 403)
        self.assertIn("e-CNPJ", page)

    def test_request_that_did_not_come_from_the_front_server_is_refused(self):
        for secret in (None, "", "segredo-errado"):
            status, _, cookie, page = self.enter(build_certificate(), secret=secret)
            self.assertEqual((status, cookie), (403, None), secret)
            self.assertNotIn("Loja Matriz", page)
        self.assertEqual(self.sessions(), [])

    def test_missing_certificate_explains_what_to_do(self):
        status, _, _, page = self.enter()
        self.assertEqual(status, 403)
        self.assertIn("Nenhum certificado recebido", page)
        self.assertIn("celular", page)

    def test_every_refusal_has_a_title_and_a_next_step(self):
        for message in (certificado.NO_CERTIFICATE, certificado.UNREADABLE, certificado.NO_CNPJ, certificado.EXPIRED, certificado.NOT_YET_VALID, certificado.NO_STORE, certificado.OTHER_STORE):
            title, steps = acesso.REFUSALS[message]
            self.assertTrue(title and steps, message)
            self.assertIn("entry-steps", acesso.certificate_refused(message))

    def test_refusal_never_shows_a_password_form(self):
        self.assertNotIn("type='password'", self.enter(build_certificate(cnpj=STORE_B_CNPJ), f"?store_id={self.ids['Loja Matriz']}")[3])

    def open_path(self, path, certificate=None, secret=SECRET):
        headers = {} if secret is None else {certificado.PROXY_HEADER: secret}
        if certificate is not None:
            headers[certificado.CERT_HEADER] = as_header(certificate)
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()

    def test_lost_session_enters_again_and_keeps_the_page_of_the_link(self):
        status, location, _ = self.open_path("/dashboard?store_id=1&type=fiscal&competence=2026-09", build_certificate())
        self.assertEqual((status, location), (303, "/certificado?store_id=1&type=fiscal&competence=2026-09"))

    def test_lost_session_on_another_page_enters_again_without_the_query(self):
        self.assertEqual(self.open_path("/download/abc?x=1", build_certificate())[:2], (303, "/certificado"))

    def test_automatic_browser_requests_never_open_a_session(self):
        for path in ("/favicon.ico", "/robots.txt", "/apple-touch-icon.png", "/qualquer-coisa"):
            self.assertEqual(self.open_path(path, build_certificate())[:2], (404, None), path)
        self.assertEqual(self.sessions(), [])

    def test_empty_secret_never_trusts_the_header(self):
        with mock.patch.object(config, "CERT_PROXY_SECRET", ""):
            self.assertIn("type='password'", self.open_path("/login", build_certificate(), secret="")[2])

    def test_valid_session_is_reused_and_needs_no_certificate(self):
        store = self.ids["Loja Matriz"]
        _, _, cookie, _ = self.enter(build_certificate())
        status, location, new_cookie, _ = self.enter(None, f"?store_id={store}&type=fiscal", headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual((status, location, new_cookie), (303, f"/dashboard?store_id={store}&type=fiscal", None))
        self.assertEqual(len(self.sessions()), 1)

    def test_session_of_another_root_does_not_open_this_link(self):
        _, _, cookie, _ = self.enter(build_certificate(cnpj=STORE_B_CNPJ))
        link, session = f"?store_id={self.ids['Loja Matriz']}", {"Cookie": cookie.split(";")[0]}
        self.assertEqual(self.enter(None, link, headers=session)[0], 403)
        self.assertEqual(self.enter(build_certificate(), link, headers=session)[0], 303)
        self.assertIn(self.ids["Loja Matriz"], self.sessions())

    def test_login_page_is_skipped_with_a_certificate(self):
        for path in ("/", "/login"):
            self.assertEqual(self.open_path(path, build_certificate())[:2], (303, "/certificado"), path)

    def test_login_page_stays_without_a_certificate_and_with_a_forged_one(self):
        self.assertIn("type='password'", self.open_path("/login")[2])
        self.assertIn("type='password'", self.open_path("/login", secret=None)[2])
        self.assertIn("type='password'", self.open_path("/login", build_certificate(), secret=None)[2])
        self.assertEqual(self.open_path("/dashboard", build_certificate(), secret=None)[:2], (303, "/login"))

    def log_out(self, cookie, page, headers):
        csrf = page.split("name='csrf' value='")[1].split("'")[0]
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
        self.addCleanup(connection.close)
        connection.request("POST", "/logout", body=urlencode({"csrf": csrf}), headers={"Cookie": cookie.split(";")[0], "Content-Type": "application/x-www-form-urlencoded", **headers})
        response = connection.getresponse()
        response.read()
        return response.getheader("Location")

    def test_leaving_shows_the_exit_page_and_does_not_enter_again_by_itself(self):
        certificate = build_certificate()
        _, location, cookie, _ = self.enter(certificate)
        page = self.get(location, cookie)[1]
        target = self.log_out(cookie, page, {certificado.PROXY_HEADER: SECRET, certificado.CERT_HEADER: as_header(certificate)})
        self.assertEqual(target, "/login?saiu=1")
        status, redirected, exit_page = self.open_path(target, certificate)
        self.assertEqual((status, redirected), (200, None))
        self.assertIn("Você saiu do portal", exit_page)
        self.assertIn("feche todas as janelas do navegador", exit_page)
        self.assertIn("href='/certificado'", exit_page)
        self.assertNotIn("type='password'", exit_page)
        self.assertEqual(self.sessions(), [])

    def test_leaving_by_password_keeps_the_login_page(self):
        _, location, cookie, _ = self.enter(build_certificate())
        page = self.get(location, cookie)[1]
        self.assertEqual(self.log_out(cookie, page, {}), "/login")

    def test_route_does_not_exist_without_the_secret(self):
        with mock.patch.object(config, "CERT_PROXY_SECRET", ""):
            status, location, cookie, _ = self.enter(build_certificate(), secret="")
        self.assertEqual((status, location, cookie), (303, "/login", None))

    def test_link_cannot_redirect_to_another_site(self):
        _, location, _, _ = self.enter(build_certificate(), "?type=//evil.example.com&competence=https://evil.example.com")
        self.assertEqual(location, "/dashboard")

    def test_session_stays_read_only(self):
        _, location, cookie, _ = self.enter(build_certificate())
        page = self.get(location, cookie)[1]
        self.assertNotIn("Enviar arquivo SPED", page)
        self.assertEqual(self.get("/emails", cookie)[0], 404)

    def test_session_downloads_files_of_the_root_and_not_of_others(self):
        own = publicacao.publish_sped(sped(FISCAL_SAMPLE, self.filial), "filial.txt")
        other = publicacao.publish_sped(sped(FISCAL_SAMPLE, STORE_B_CNPJ), "outra.txt")
        _, _, cookie, _ = self.enter(build_certificate())
        self.assertEqual(self.get(f"/download/{own['id']}", cookie)[0], 200)
        self.assertEqual(self.get(f"/download/{other['id']}", cookie)[0], 404)

    def test_second_entry_keeps_the_first_session_alive(self):
        _, location, first, _ = self.enter(build_certificate())
        self.enter(build_certificate())
        self.assertEqual(self.get(location, first)[0], 200)
        self.assertEqual(len(self.sessions()), 2)

    def test_expired_sessions_of_the_store_are_cleaned(self):
        with storage.connect() as db:
            db.execute("INSERT INTO sessions VALUES ('velha', ?, 'csrf', ?)", (self.ids["Loja Matriz"], int(time.time()) - 10))
        self.enter(build_certificate())
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE token_hash='velha'").fetchone()[0], 0)


class ClientAddressTests(unittest.TestCase):
    """Atrás do servidor da frente, cada pessoa continua com o próprio limite de tentativas de login."""

    def setUp(self):
        temporary_storage(self)
        self.server = TestServer()
        self.addCleanup(self.server.stop)

    def failed_login(self, headers):
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
        self.addCleanup(connection.close)
        connection.request("GET", "/login")
        response = connection.getresponse()
        page = response.read().decode()
        cookie = response.getheader("Set-Cookie").split(";")[0]
        csrf = page.split("name='csrf' value='")[1].split("'")[0]
        body = urlencode({"csrf": csrf, "cnpj": "ninguem", "password": "errada"})
        connection.request("POST", "/login", body=body, headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", **headers})
        connection.getresponse().read()
        with storage.connect() as db:
            return [item["ip"] for item in db.execute("SELECT ip FROM login_attempts")]

    def test_proxy_informs_the_real_address(self):
        with mock.patch.object(config, "BEHIND_PROXY", True):
            attempts = self.failed_login({"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
        self.assertEqual(attempts, ["203.0.113.7"])

    def test_address_is_not_believed_without_a_proxy(self):
        self.assertEqual(self.failed_login({"X-Forwarded-For": "203.0.113.7"}), ["127.0.0.1"])

    def test_certificate_secret_gives_no_trust_in_the_address(self):
        with mock.patch.object(config, "CERT_PROXY_SECRET", SECRET):
            attempts = self.failed_login({certificado.PROXY_HEADER: SECRET, "X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(attempts, ["127.0.0.1"])

    def test_trusting_the_address_gives_no_right_to_enter_as_a_store(self):
        auth.create_user(STORE_A_CNPJ, "Loja Matriz", "store", "senha-da-loja-123")  # sem a loja, a recusa viria de outra regra
        with mock.patch.object(config, "BEHIND_PROXY", True), mock.patch.object(config, "CERT_PROXY_SECRET", SECRET):
            connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
            self.addCleanup(connection.close)
            connection.request("GET", "/certificado", headers={"X-Forwarded-For": "203.0.113.7", certificado.CERT_HEADER: as_header(build_certificate())})
            response = connection.getresponse()
            response.read()
        self.assertEqual(response.status, 403)


class CheckCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = temporary_storage(self)
        auth.create_user(STORE_A_CNPJ, "Loja Matriz", "store", "senha-da-loja-123")

    def run_command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.check_certificate(list(args))
        return output.getvalue()

    def test_reads_der_and_pem_files_and_lists_the_stores(self):
        der_file, pem_file = self.folder / "loja.cer", self.folder / "loja.pem"
        der_file.write_bytes(build_certificate())
        pem_file.write_text(as_pem(build_certificate()), encoding="ascii")
        for path in (der_file, pem_file):
            output = self.run_command(str(path))
            self.assertIn("CNPJ: 11.222.333/0001-81 (raiz 11222333)", output)
            self.assertIn("Lojas ativas no portal com esta raiz: 1 (Loja Matriz)", output)
            self.assertIn("dentro da validade", output)

    def test_expired_certificate_is_shown_with_its_situation(self):
        path = self.folder / "vencido.cer"
        path.write_bytes(build_certificate(not_after=b"250101000000Z"))
        self.assertIn("Situação: Este certificado digital está vencido.", self.run_command(str(path)))

    def test_file_that_is_not_a_certificate_stops_with_clear_message(self):
        path = self.folder / "lixo.cer"
        path.write_bytes(b"nao e certificado")
        with self.assertRaisesRegex(SystemExit, "Certificado não conferido"):
            self.run_command(str(path))

    def test_command_needs_exactly_one_file(self):
        with self.assertRaisesRegex(SystemExit, "Uso: python app.py conferir-certificado"):
            self.run_command()


if __name__ == "__main__":
    unittest.main()
