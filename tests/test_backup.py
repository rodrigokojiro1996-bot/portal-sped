import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from portal import auth, backup, publicacao
from portal import db as storage
from util import FISCAL_SAMPLE, STORE_A_CNPJ, TestServer, restore_storage, sped, temporary_storage


class BackupTests(unittest.TestCase):
    """Backup: banco pela API do SQLite, documentos e segredo, fora de data/, conferido e com retenção."""

    def setUp(self):
        self.data = temporary_storage(self)
        self.destination = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.destination, True)
        auth.create_user(STORE_A_CNPJ, "Farmácia Centro", "store", "senha-centro-123")
        self.published = publicacao.publish_sped(sped(FISCAL_SAMPLE), "setembro.txt")

    def test_backup_copies_database_documents_and_secret(self):
        target = backup.run_backup(self.destination, now=datetime(2026, 10, 20, 3, 0, 0))
        self.assertEqual(target.name, "portal-2026-10-20_030000")
        self.assertTrue((target / "documentos" / f"{self.published['id']}.txt").is_file())
        self.assertEqual((target / ".secret").read_bytes(), (self.data / ".secret").read_bytes())
        copy = sqlite3.connect(target / "portal.sqlite3")
        self.assertEqual(copy.execute("SELECT original_name FROM documents").fetchone()[0], "setembro.txt")
        copy.close()
        self.assertIn("Documentos: 1 registros, 1 arquivos", (target / "LEIA-ME.txt").read_text(encoding="utf-8"))
        self.assertEqual([path.name for path in self.destination.iterdir()], [target.name])   # sem pasta .parcial

    def test_backup_is_refused_inside_the_data_folder(self):
        with self.assertRaisesRegex(backup.BackupError, "não pode ficar dentro da pasta de dados"):
            backup.run_backup(self.data / "backups")

    def test_old_backups_are_removed_after_retention_but_other_folders_stay(self):
        now = datetime(2026, 10, 20, 3, 0, 0)
        old = self.destination / f"portal-{now - timedelta(days=15):%Y-%m-%d_%H%M%S}"
        recent = self.destination / f"portal-{now - timedelta(days=13):%Y-%m-%d_%H%M%S}"
        unrelated = self.destination / "outra-pasta"
        for folder in (old, recent, unrelated):
            folder.mkdir()
        backup.run_backup(self.destination, keep_days=14, now=now)
        names = sorted(path.name for path in self.destination.iterdir())
        self.assertNotIn(old.name, names)
        self.assertIn(recent.name, names)
        self.assertIn("outra-pasta", names)

    def test_backup_with_missing_document_file_fails_and_leaves_nothing_behind(self):
        (storage.DOCUMENTS / f"{self.published['id']}.txt").unlink()
        with self.assertRaisesRegex(backup.BackupError, "não estão na cópia"):
            backup.run_backup(self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_restored_backup_starts_the_portal_with_the_same_data(self):
        """Como o arquivo de ideias pede: restaurar numa pasta separada e subir o portal apontando para ela."""
        target = backup.run_backup(self.destination)
        restored = Path(tempfile.mkdtemp()) / "restaurado"
        self.addCleanup(shutil.rmtree, restored.parent, True)
        shutil.copytree(target, restored)
        storage.configure_storage(restored)
        server = TestServer()
        self.addCleanup(server.stop)
        from urllib.request import urlopen
        with urlopen(server.base + "/saude") as response:
            self.assertEqual(response.status, 200)
        with storage.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM users WHERE role='store'").fetchone()[0], "Farmácia Centro")
        self.assertTrue((restored / "documentos" / f"{self.published['id']}.txt").is_file())


class BackupCommandTests(unittest.TestCase):
    def test_command_line_backup_with_destination_and_retention(self):
        from test_cli import run_portal
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as destination:
            result = run_portal("backup", "--destino", destination, "--manter", "7", PORTAL_DATA_DIR=data)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Backup concluído e conferido", result.stdout)
            self.assertEqual(len(list(Path(destination).glob("portal-*"))), 1)
            refused = run_portal("backup", "--manter", "sempre", PORTAL_DATA_DIR=data)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--manter precisa ser um número", refused.stderr)


if __name__ == "__main__":
    unittest.main()
