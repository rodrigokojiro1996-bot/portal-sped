import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import app
from portal import auth
from portal import db as storage
from util import restore_storage


class PasswordTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        stored = auth.password_digest("uma-senha-segura-123")
        self.assertTrue(auth.verify_password("uma-senha-segura-123", stored))
        self.assertFalse(auth.verify_password("senha-errada", stored))
        self.assertNotEqual(stored, auth.password_digest("uma-senha-segura-123"))


class PasswordResetTests(unittest.TestCase):
    def test_reset_replaces_password_and_clears_sessions_and_lockouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage.configure_storage(Path(temporary))
            try:
                auth.create_user("11222333000181", "Loja Exemplo", "store", "senha-antiga-123")
                with storage.connect() as db:
                    store_id = db.execute("SELECT id FROM users").fetchone()["id"]
                    db.execute("INSERT INTO sessions VALUES ('hash', ?, 'csrf', ?)", (store_id, int(time.time()) + 3600))
                    db.execute("INSERT INTO login_attempts VALUES ('127.0.0.1', 10, ?)", (int(time.time()),))
                name, password = auth.reset_store_password("11.222.333/0001-81")
                self.assertEqual(name, "Loja Exemplo")
                with storage.connect() as db:
                    stored = db.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
                    self.assertTrue(auth.verify_password(password, stored))
                    self.assertFalse(auth.verify_password("senha-antiga-123", stored))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 0)
                with self.assertRaises(ValueError):
                    auth.reset_store_password("11444777000161")
            finally:
                restore_storage()


class LoginFormTests(unittest.TestCase):
    """O formulário de login não pode ser descartado em silêncio (aba extra, pré-carregamento, página antiga)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        storage.configure_storage(Path(self.temporary.name))
        auth.create_user("11222333000181", "Loja Exemplo", "store", "senha-correta-123")
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
        _, password = auth.reset_store_password("11222333000181")
        self.assertRegex(password, r"^[A-HJ-NP-Za-km-z2-9]{4}-[A-HJ-NP-Za-km-z2-9]{4}-[A-HJ-NP-Za-km-z2-9]{4}$")
        opener = self.opener()
        url, _ = self.submit(opener, self.open_login(opener), password)
        self.assertTrue(url.endswith("/dashboard"), url)


if __name__ == "__main__":
    unittest.main()
