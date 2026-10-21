import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

from portal import cli, config
from portal import db as storage
from util import TestServer, restore_storage, temporary_storage


def run_portal(*args, **env):
    """Roda python app.py ... num processo separado, com variáveis de ambiente próprias."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PORTAL_")}
    environment.update(env, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "app.py", *args], cwd=config.ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)


class StartupCheckTests(unittest.TestCase):
    """Checagem no boot: o portal se recusa a subir, com mensagem clara e código 2, se algo essencial falhar."""

    def test_everything_ok_reports_no_problem(self):
        folder = temporary_storage(self)
        with mock.patch.object(config, "DATA_DIR", folder):
            self.assertEqual(cli.startup_problems(), [])

    def test_invalid_port_stops_with_clear_message(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_portal("gerar-resumos", PORTAL_PORT="porta", PORTAL_DATA_DIR=folder)
        self.assertEqual(result.returncode, 2)
        self.assertIn("O Portal Fiscal não pode iniciar", result.stderr)
        self.assertIn("PORTAL_PORT inválida: 'porta'", result.stderr)

    def test_data_folder_that_cannot_be_created_stops_the_portal(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "arquivo-no-lugar-da-pasta"
            blocker.write_text("não é uma pasta", encoding="utf-8")
            result = run_portal("gerar-resumos", PORTAL_DATA_DIR=str(blocker / "dados"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Não foi possível abrir a pasta de dados", result.stderr)

    def test_database_behind_expected_migration_is_reported(self):
        temporary_storage(self)
        with mock.patch.object(storage, "latest_version", return_value=storage.latest_version() + 1), \
                mock.patch.object(storage, "migrate", return_value=[]):
            problems = cli.startup_problems()
        self.assertTrue(any("espera a" in problem for problem in problems), problems)

    def test_valid_command_runs_after_check(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_portal("gerar-resumos", PORTAL_DATA_DIR=folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resumos prontos: 0 de 0 arquivos.", result.stdout)

    def test_unknown_command_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_portal("comando-que-nao-existe", PORTAL_DATA_DIR=folder)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Comando desconhecido", result.stderr)


class EnvFileTests(unittest.TestCase):
    def test_env_file_fills_missing_variables_without_overriding_the_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("# comentário\nPORTAL_TESTE_A=do-arquivo\nPORTAL_TESTE_B = 'com aspas'\nPORTAL_TESTE_C=do-arquivo\nlinha sem igual\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PORTAL_TESTE_C": "do-ambiente"}):
                config.load_env_file(env_file)
                self.assertEqual(os.environ["PORTAL_TESTE_A"], "do-arquivo")
                self.assertEqual(os.environ["PORTAL_TESTE_B"], "com aspas")
                self.assertEqual(os.environ["PORTAL_TESTE_C"], "do-ambiente")
            for key in ("PORTAL_TESTE_A", "PORTAL_TESTE_B"):
                os.environ.pop(key, None)


class HealthRouteTests(unittest.TestCase):
    """GET /saude, sem login: 200 com a versão quando o banco responde na migração esperada, 503 quando não."""

    def setUp(self):
        temporary_storage(self)
        self.server = TestServer()
        self.addCleanup(self.server.stop)

    def get(self):
        try:
            with urlopen(self.server.base + "/saude") as response:
                return response.status, response.headers["Content-Type"], json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, error.headers["Content-Type"], json.loads(error.read())

    def test_healthy_portal_answers_without_login(self):
        status, content_type, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        self.assertEqual(body, {"ok": True, "versao": config.PORTAL_VERSION, "migracao": storage.latest_version()})

    def test_database_behind_code_answers_503(self):
        with mock.patch.object(storage, "latest_version", return_value=storage.latest_version() + 1):
            status, _, body = self.get()
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertIn("espera a", body["erro"])

    def test_broken_database_answers_503_without_leaking_details(self):
        broken = Path(tempfile.mkdtemp()) / "nao-existe" / "portal.sqlite3"
        with mock.patch.object(storage, "DATABASE", broken), mock.patch("sys.stderr"):
            status, _, body = self.get()
        self.assertEqual(status, 503)
        self.assertEqual(body["erro"], "Banco de dados indisponível.")
        self.assertNotIn("nao-existe", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
