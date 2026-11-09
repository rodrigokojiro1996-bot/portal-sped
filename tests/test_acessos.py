import csv
import http.client
import io
import socket
import sqlite3
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import urlparse

from portal import acessos, auth, certificado, config, publicacao
from portal import db as storage
from test_certificado import OID_CPF, OID_RESPONSIBLE, SECRET, as_header, build_certificate, filial_of, other_name
from util import FISCAL_SAMPLE, STORE_A_CNPJ, STORE_B_CNPJ, TestServer, sped, temporary_storage

EDGE = "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Edg/130.0"


def records():
    with storage.connect() as db:
        return [dict(item) for item in db.execute("SELECT * FROM certificate_access ORDER BY id")]


class AccessCase(unittest.TestCase):
    """Entrada ligada, atrás de proxy, com uma loja de cada raiz e uma sessão de administração e uma de suporte."""

    def setUp(self):
        temporary_storage(self)
        for name, value in (("CERT_PROXY_SECRET", SECRET), ("BEHIND_PROXY", True)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Os testes nunca consultam o DNS de verdade: os IPs de exemplo não têm nome reverso, e a espera deixaria tudo lento.
        self.lookup = mock.patch.object(acessos, "_lookup", return_value="")
        self.lookup.start()
        self.addCleanup(self.lookup.stop)
        acessos._hostnames.clear()
        self.addCleanup(acessos._hostnames.clear)
        auth.create_user(STORE_A_CNPJ, "Loja Centro", "store", "senha-da-loja-123")
        auth.create_user(STORE_B_CNPJ, "Outra Rede", "store", "senha-da-loja-123")
        auth.create_user(None, "Admin Exemplo", "admin", "senha-admin-123", "admin.teste")
        auth.create_support_user("suporte.teste", "Suporte Exemplo")
        with storage.connect() as db:
            self.ids = {item["name"]: item["id"] for item in db.execute("SELECT id, name FROM users")}
            for name, token in (("Admin Exemplo", "admin"), ("Suporte Exemplo", "suporte"), ("Loja Centro", "senha-loja")):
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (auth.token_hash(token), self.ids[name], "csrf-" + token, int(time.time()) + 3600))
        self.server = TestServer()
        self.addCleanup(self.server.stop)

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", urlparse(self.server.base).port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.getheader("Set-Cookie"), response.read()

    def enter(self, certificate=None, query="", secret=SECRET, ip="203.0.113.7", cookie=None):
        headers = {"User-Agent": EDGE, "X-Forwarded-For": ip}
        if secret:
            headers[certificado.PROXY_HEADER] = secret
        if certificate is not None:
            headers[certificado.CERT_HEADER] = as_header(certificate)
        if cookie:
            headers["Cookie"] = cookie
        status, set_cookie, _ = self.get("/certificado" + query, headers)
        return status, set_cookie.split(";")[0] if set_cookie else None

    def page(self, token, path):
        status, _, body = self.get(path, {"Cookie": f"portal_session={token}"})
        return status, body.decode("utf-8")


class RecordTests(AccessCase):
    def test_entry_records_who_where_and_from_where(self):
        status, _ = self.enter(build_certificate(), f"?store_id={self.ids['Loja Centro']}")
        self.assertEqual(status, 303)
        [entry] = records()
        self.assertEqual((entry["event"], entry["certificate_cnpj"], entry["certificate_serial"], entry["store_id"], entry["store_name"], entry["store_cnpj"]),
                         ("entrou", STORE_A_CNPJ, "01", self.ids["Loja Centro"], "Loja Centro", STORE_A_CNPJ))
        self.assertEqual((entry["ip"], entry["browser"]), ("203.0.113.7", "Edge no Windows"))

    def test_responsible_person_data_is_never_stored(self):
        cpf = b"01011980" + b"98765432100" + b"0" * 19
        names = [other_name(OID_RESPONSIBLE, cpf), other_name(certificado.OID_CNPJ, STORE_A_CNPJ.encode())]
        self.enter(build_certificate(names=names))
        self.assertNotIn("98765432100", str(records()))

    def test_entry_and_session_are_saved_together(self):
        with mock.patch.object(acessos, "record_entry", side_effect=sqlite3.OperationalError("disco cheio")), mock.patch("sys.stderr"):
            with self.assertRaises(http.client.HTTPException):
                self.enter(build_certificate())
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (self.ids["Loja Centro"],)).fetchone()[0], 1)  # só a da senha, do setUp

    def test_refusal_records_the_certificate_the_store_of_the_link_and_the_reason(self):
        self.enter(build_certificate(cnpj=STORE_B_CNPJ), f"?store_id={self.ids['Loja Centro']}")
        [entry] = records()
        self.assertEqual((entry["event"], entry["certificate_cnpj"], entry["store_name"], entry["reason"], entry["session_hash"]),
                         ("recusado", STORE_B_CNPJ, "Loja Centro", certificado.OTHER_STORE, None))

    def test_expired_certificate_is_identified_in_the_refusal(self):
        self.enter(build_certificate(not_after=b"250101000000Z"))
        [entry] = records()
        self.assertEqual((entry["event"], entry["certificate_cnpj"], entry["reason"]), ("recusado", STORE_A_CNPJ, certificado.EXPIRED))

    def test_unreadable_certificate_is_recorded_without_identification(self):
        status, _, _ = self.get("/certificado", {certificado.PROXY_HEADER: SECRET, certificado.CERT_HEADER: "nao-e-um-certificado"})
        self.assertEqual(status, 403)
        [entry] = records()
        self.assertEqual((entry["event"], entry["certificate_cnpj"], entry["reason"]), ("recusado", None, certificado.UNREADABLE))

    def test_link_of_a_staff_account_never_shows_it_in_the_record(self):
        self.enter(build_certificate(), f"?store_id={self.ids['Admin Exemplo']}")
        self.assertEqual(records()[0]["store_name"], "Loja Centro")  # a loja do certificado, nunca a conta da equipe

    def test_visit_without_certificate_records_nothing(self):
        self.assertEqual(self.enter()[0], 403)
        self.assertEqual(records(), [])

    def test_request_without_the_secret_records_nothing(self):
        self.assertEqual(self.enter(build_certificate(), secret="errado")[0], 403)
        self.assertEqual(records(), [])

    def test_reusing_a_session_does_not_repeat_the_entry(self):
        _, cookie = self.enter(build_certificate())
        self.assertEqual(self.enter(build_certificate(), cookie=cookie)[0], 303)
        self.assertEqual(len(records()), 1)

    def test_password_session_opening_the_link_enters_by_certificate(self):
        status, cookie = self.enter(build_certificate(), cookie="portal_session=senha-loja")
        self.assertEqual(status, 303)
        self.assertIsNotNone(cookie)
        self.assertEqual([entry["event"] for entry in records()], ["entrou"])

    def test_password_session_without_certificate_does_not_open_the_link(self):
        self.assertEqual(self.enter(cookie="portal_session=senha-loja")[0], 403)
        self.assertEqual(records(), [])

    def test_entry_is_recorded_on_the_store_of_the_link(self):
        branch = filial_of(STORE_A_CNPJ)
        auth.create_user(branch, "Loja Filial", "store", "senha-da-loja-123")
        with storage.connect() as db:
            filial = db.execute("SELECT id FROM users WHERE cnpj=?", (branch,)).fetchone()["id"]
        self.enter(build_certificate(), f"?store_id={filial}")
        self.assertEqual(records()[0]["store_name"], "Loja Filial")

    def test_personal_certificate_keeps_the_serial_in_the_refusal(self):
        self.enter(build_certificate(names=[other_name(OID_CPF, b"0101198012345678901")], serial=b"\x7f"))
        [entry] = records()
        self.assertEqual((entry["certificate_cnpj"], entry["certificate_serial"], entry["reason"]), (None, "7F", certificado.NO_CNPJ))

    def test_refusal_without_a_link_is_recorded_on_the_store_of_the_certificate(self):
        self.enter(build_certificate(not_after=b"250101000000Z"))
        self.assertEqual(records()[0]["store_id"], self.ids["Loja Centro"])

    def test_repeated_refusal_counts_once(self):
        for _ in range(3):
            self.assertEqual(self.enter(build_certificate(not_after=b"250101000000Z"))[0], 403)
        self.assertEqual(len(records()), 1)

    def test_same_refusal_later_or_from_another_address_counts_again(self):
        self.enter(build_certificate(not_after=b"250101000000Z"))
        self.enter(build_certificate(not_after=b"250101000000Z"), ip="203.0.113.8")
        self.assertEqual(len(records()), 2)
        moment = (datetime.now().astimezone() - timedelta(seconds=acessos.REPEAT_SECONDS + 5)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE certificate_access SET created_at=?", (moment,))
        self.enter(build_certificate(not_after=b"250101000000Z"))
        self.assertEqual(len(records()), 3)

    def test_failure_in_the_record_does_not_hide_the_refusal(self):
        with mock.patch.object(acessos, "_insert", side_effect=sqlite3.OperationalError("disco cheio")), mock.patch("sys.stderr"):
            self.assertEqual(self.enter(build_certificate(not_after=b"250101000000Z"))[0], 403)


class HostnameTests(AccessCase):
    def use_dns(self, names, addresses):
        """DNS simulado: names é o reverso de cada IP, addresses é o que cada nome devolve na ida."""
        self.lookup.stop()
        real_getaddrinfo = socket.getaddrinfo  # o cliente HTTP do teste também resolve nomes: o resto passa direto

        def reverse_dns(ip):
            if ip not in names:
                raise OSError("sem nome reverso")
            return names[ip], [], [ip]

        def forward_dns(name, *args):
            if name in addresses:
                return [(2, 1, 6, "", (ip, 0)) for ip in addresses[name]]
            if name.endswith(".example.net") or name.endswith(".example.gov"):
                raise OSError("nome desconhecido")
            return real_getaddrinfo(name, *args)

        for patcher in (mock.patch("socket.gethostbyaddr", side_effect=reverse_dns), mock.patch("socket.getaddrinfo", side_effect=forward_dns)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_confirmed_reverse_name_is_recorded_on_entry_refusal_and_download(self):
        self.use_dns({"203.0.113.7": "Cliente-77.Operadora.example.net"}, {"cliente-77.operadora.example.net": ["203.0.113.7"]})
        published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "sped.txt")
        _, cookie = self.enter(build_certificate())
        self.get(f"/download/{published['id']}", {"Cookie": cookie, "X-Forwarded-For": "203.0.113.7"})
        self.enter(build_certificate(not_after=b"250101000000Z"))
        self.assertEqual([(entry["event"], entry["hostname"]) for entry in records()],
                         [("entrou", "cliente-77.operadora.example.net"), ("baixou", "cliente-77.operadora.example.net"), ("recusado", "cliente-77.operadora.example.net")])

    def test_name_that_does_not_point_back_is_marked_unconfirmed(self):
        self.use_dns({"203.0.113.7": "banco.example.gov"}, {"banco.example.gov": ["198.51.100.1"]})
        self.enter(build_certificate())
        self.assertEqual(records()[0]["hostname"], "banco.example.gov (não confirmado)")

    def test_name_that_does_not_resolve_back_is_marked_unconfirmed_and_not_dropped(self):
        self.use_dns({"203.0.113.7": "banco.example.gov"}, {})
        self.enter(build_certificate())
        self.assertEqual(records()[0]["hostname"], "banco.example.gov (não confirmado)")

    def test_ipv6_and_mapped_ipv4_written_differently_still_confirm(self):
        self.use_dns({"2001:DB8::7": "v6.example.net", "::ffff:203.0.113.7": "v4.example.net"}, {"v6.example.net": ["2001:db8::7"], "v4.example.net": ["203.0.113.7"]})
        self.assertEqual(acessos.hostname_of("2001:DB8::7"), "v6.example.net")
        self.assertEqual(acessos.hostname_of("::ffff:203.0.113.7"), "v4.example.net")

    def test_loopback_in_any_form_and_invalid_addresses_are_never_looked_up(self):
        self.lookup.stop()
        with mock.patch.object(acessos, "_lookup") as lookup:
            for value in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1", "0.0.0.0", "", "nao-e-ip"):
                self.assertEqual(acessos.hostname_of(value), "", value)
        lookup.assert_not_called()

    def test_failure_is_retried_after_a_while_and_names_expire(self):
        self.lookup.stop()
        clock = [1000.0]
        with mock.patch.object(acessos.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(acessos, "_lookup", side_effect=["", "cliente.example.net", "outro.example.net"]) as lookup:
            self.assertEqual(acessos.hostname_of("203.0.113.7"), "")
            clock[0] += acessos.FAILURE_TTL - 1
            self.assertEqual(acessos.hostname_of("203.0.113.7"), "")
            clock[0] += 2
            self.assertEqual(acessos.hostname_of("203.0.113.7"), "cliente.example.net")
            clock[0] += acessos.NAME_TTL + 1
            self.assertEqual(acessos.hostname_of("203.0.113.7"), "outro.example.net")
        self.assertEqual(lookup.call_count, 3)

    def test_congested_dns_is_skipped_without_waiting_or_caching(self):
        self.lookup.stop()
        full = mock.Mock(acquire=mock.Mock(return_value=False))
        with mock.patch.object(acessos, "_pending", full), mock.patch.object(acessos, "_lookup") as lookup:
            self.assertEqual(acessos.hostname_of("203.0.113.7"), "")
        lookup.assert_not_called()
        self.assertNotIn("203.0.113.7", acessos._hostnames)

    def test_repeated_refusal_does_not_look_up_the_name_again(self):
        self.lookup.stop()
        with mock.patch.object(acessos, "_lookup", return_value="cliente.example.net") as lookup:
            self.enter(build_certificate(not_after=b"250101000000Z"))
            acessos._hostnames.clear()
            self.enter(build_certificate(not_after=b"250101000000Z"))
        self.assertEqual(lookup.call_count, 1)

    def test_ip_without_reverse_name_is_recorded_empty(self):
        self.use_dns({}, {})
        self.assertEqual(self.enter(build_certificate())[0], 303)
        self.assertEqual(records()[0]["hostname"], "")

    def test_slow_dns_does_not_hold_the_entry(self):
        self.lookup.stop()
        with mock.patch.object(acessos, "LOOKUP_SECONDS", 0.2), mock.patch.object(acessos, "_lookup", side_effect=lambda ip: time.sleep(1) or "lento.example.net"):
            started = time.monotonic()
            self.assertEqual(self.enter(build_certificate())[0], 303)
            self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(records()[0]["hostname"], "")

    def test_reverse_name_is_looked_up_before_the_transaction(self):
        def lookup_that_writes(ip):
            connection = sqlite3.connect(storage.DATABASE, timeout=0.3)
            try:
                connection.execute("BEGIN IMMEDIATE")  # falha se a entrada já estiver com o banco preso
                connection.rollback()
            finally:
                connection.close()
            return "livre.example.net"
        self.lookup.stop()
        with mock.patch.object(acessos, "_lookup", side_effect=lookup_that_writes):
            self.enter(build_certificate())
        self.assertEqual(records()[0]["hostname"], "livre.example.net")

    def test_local_connection_is_never_looked_up(self):
        self.lookup.stop()
        with mock.patch.object(config, "BEHIND_PROXY", False), mock.patch.object(acessos, "_lookup") as lookup:
            self.enter(build_certificate())
        lookup.assert_not_called()
        self.assertEqual(records()[0]["hostname"], "")

    def test_same_ip_is_looked_up_once(self):
        self.lookup.stop()
        with mock.patch.object(acessos, "_lookup", return_value="cliente.example.net") as lookup:
            self.enter(build_certificate())
            self.enter(build_certificate(cnpj=STORE_B_CNPJ))
        self.assertEqual(lookup.call_count, 1)

    def test_reverse_name_is_shown_escaped_and_exported(self):
        self.lookup.stop()
        with mock.patch.object(acessos, "_lookup", return_value="=<b>host"):
            self.enter(build_certificate())
        page = self.page("admin", "/acessos")[1]
        self.assertIn("&lt;b&gt;host", page)
        self.assertNotIn("<b>host", page)
        csv_text = self.page("admin", "/acessos.csv")[1]
        self.assertIn("Nome reverso", csv_text)
        self.assertIn("'=<b>host", csv_text)


class DownloadTests(AccessCase):
    def setUp(self):
        super().setUp()
        self.published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "sped-setembro.txt")

    def download(self, cookie, ip="203.0.113.9"):
        return self.get(f"/download/{self.published['id']}", {"Cookie": cookie, "User-Agent": EDGE, "X-Forwarded-For": ip})

    def test_download_is_attributed_to_the_certificate_of_the_session(self):
        _, cookie = self.enter(build_certificate())
        status, _, body = self.download(cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body)
        entry = records()[-1]
        self.assertEqual((entry["event"], entry["certificate_cnpj"], entry["certificate_serial"], entry["store_name"], entry["document_type"], entry["competence"], entry["file_name"], entry["ip"]),
                         ("baixou", STORE_A_CNPJ, "01", "Loja Centro", "fiscal", "2026-09", "sped-setembro.txt", "203.0.113.9"))

    def test_file_does_not_leave_if_the_record_fails(self):
        _, cookie = self.enter(build_certificate())
        with mock.patch.object(acessos, "record_download", side_effect=sqlite3.OperationalError("disco cheio")), mock.patch("sys.stderr"):
            with self.assertRaises(http.client.HTTPException):
                self.download(cookie)

    def test_password_session_is_not_recorded(self):
        self.assertEqual(self.download("portal_session=senha-loja")[0], 200)
        self.assertEqual(records(), [])

    def test_staff_download_is_not_recorded(self):
        self.assertEqual(self.download("portal_session=admin")[0], 200)
        self.assertEqual(records(), [])

    def test_refused_download_records_nothing(self):
        other = publicacao.publish_sped(sped(FISCAL_SAMPLE, STORE_B_CNPJ), "outra.txt")
        _, cookie = self.enter(build_certificate())
        self.assertEqual(self.get(f"/download/{other['id']}", {"Cookie": cookie})[0], 404)
        self.assertEqual([entry["event"] for entry in records()], ["entrou"])


class ScreenTests(AccessCase):
    def age_all(self, days):
        moment = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        with storage.connect() as db:
            db.execute("UPDATE certificate_access SET created_at=?", (moment,))

    def test_admin_sees_entries_refusals_and_downloads(self):
        published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "sped-setembro.txt")
        _, cookie = self.enter(build_certificate())
        self.get(f"/download/{published['id']}", {"Cookie": cookie})
        self.enter(build_certificate(cnpj=STORE_B_CNPJ), f"?store_id={self.ids['Loja Centro']}")
        status, page = self.page("admin", "/acessos")
        self.assertEqual(status, 200)
        for text in ("Entrou", "Baixou arquivo", "Recusado", "11.222.333/0001-81", "Série 01", "sped-setembro.txt", "Setembro/2026", "203.0.113.7", "Edge no Windows", "3 registros"):
            self.assertIn(text, page)
        self.assertIn(certificado.OTHER_STORE, page)

    def test_empty_record_explains_itself(self):
        self.assertIn("Nenhum acesso neste período", self.page("admin", "/acessos")[1])

    def test_only_the_administration_reaches_the_record(self):
        for token in ("suporte", "senha-loja"):
            self.assertEqual(self.page(token, "/acessos")[0], 403, token)
            self.assertEqual(self.page(token, "/acessos.csv")[0], 403, token)

    def test_screen_and_link_follow_the_switch(self):
        self.assertIn("href='/acessos'", self.page("admin", "/dashboard")[1])
        self.assertNotIn("href='/acessos'", self.page("senha-loja", "/dashboard")[1])
        with mock.patch.object(config, "CERT_PROXY_SECRET", ""):
            self.assertNotIn("href='/acessos'", self.page("admin", "/dashboard")[1])
            self.assertEqual(self.page("admin", "/acessos")[0], 404)

    def test_filter_by_store(self):
        self.enter(build_certificate())
        self.enter(build_certificate(cnpj=STORE_B_CNPJ))
        self.assertEqual(len(acessos.entries()), 2)
        page = self.page("admin", f"/acessos?loja={self.ids['Outra Rede']}")[1]
        self.assertIn("1 registro", page)
        self.assertNotIn("11.222.333/0001-81", page.split("<table>")[1])

    def test_filter_by_period(self):
        self.enter(build_certificate())
        self.age_all(40)
        self.assertIn("Nenhum acesso neste período", self.page("admin", "/acessos")[1])
        self.assertIn("1 registro", self.page("admin", "/acessos?periodo=tudo")[1])

    def test_invalid_filters_fall_back_to_the_default(self):
        self.enter(build_certificate())
        for value in ("abc", "%C2%B2", "9" * 30):
            self.assertIn("1 registro", self.page("admin", f"/acessos?loja={value}&periodo=invalido")[1], value)
            self.assertEqual(self.page("admin", f"/acessos.csv?loja={value}")[0], 200, value)

    def test_deactivated_store_can_still_be_chosen_in_the_filter(self):
        with storage.connect() as db:
            db.execute("UPDATE users SET active=0 WHERE name='Outra Rede'")
        self.assertIn(f"<option value='{self.ids['Outra Rede']}'", self.page("admin", "/acessos")[1])

    def test_long_history_says_it_was_cut_and_the_export_keeps_everything(self):
        with storage.connect() as db:
            moment = datetime.now().astimezone().isoformat(timespec="seconds")
            db.executemany("INSERT INTO certificate_access (created_at, event, ip, browser) VALUES (?, 'entrou', '203.0.113.7', 'Edge no Windows')", [(moment,)] * 505)
        page = self.page("admin", "/acessos")[1]
        self.assertIn("Mostrando os 500 registros mais recentes", page)
        self.assertIn("500 registros", page)
        self.assertEqual(len(self.page("admin", "/acessos.csv")[1].splitlines()), 506)

    def test_data_is_escaped_on_the_screen(self):
        published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "<b>arquivo.txt")
        _, cookie = self.enter(build_certificate())
        self.get(f"/download/{published['id']}", {"Cookie": cookie})
        page = self.page("admin", "/acessos")[1]
        self.assertNotIn("<b>arquivo", page)
        self.assertIn("&lt;b&gt;arquivo", page)

    def test_export_follows_the_filters_and_opens_in_excel(self):
        self.enter(build_certificate())
        self.enter(build_certificate(cnpj=STORE_B_CNPJ))
        status, _, body = self.get(f"/acessos.csv?loja={self.ids['Loja Centro']}", {"Cookie": "portal_session=admin"})
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0][:3], ["Data", "Hora", "O que aconteceu"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2:5], ["Entrou", "11.222.333/0001-81", "01"])

    def test_export_is_protected_against_formulas(self):
        published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "=HYPERLINK(1).txt")
        _, cookie = self.enter(build_certificate())
        self.get(f"/download/{published['id']}", {"Cookie": cookie})
        body = self.page("admin", "/acessos.csv")[1]
        self.assertIn("'=HYPERLINK", body)


if __name__ == "__main__":
    unittest.main()
