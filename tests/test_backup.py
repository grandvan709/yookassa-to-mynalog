import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from backup import create_backup, decrypt_bytes, restore_backup, run_backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.logs = self.root / "logs"
        self.data.mkdir()
        self.logs.mkdir()
        with closing(sqlite3.connect(self.data / "sync_state.db")) as db:
            with db:
                db.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                db.execute("INSERT INTO sample VALUES ('состояние')")
        (self.logs / "sync.log").write_text("test log\n", encoding="utf-8")
        self.password = "very-strong-test-password"

    def tearDown(self):
        self.temporary.cleanup()

    def test_encrypted_backup_contains_consistent_database_and_logs(self):
        archive = create_backup(self.data, self.logs, self.password)
        payload = decrypt_bytes(archive.read_bytes(), self.password)
        zip_path = self.root / "plain.zip"
        zip_path.write_bytes(payload)

        with zipfile.ZipFile(zip_path) as bundle:
            self.assertIn("data/sync_state.db", bundle.namelist())
            self.assertIn("logs/sync.log", bundle.namelist())
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertTrue(manifest["logs_included"])

    def test_restore_uses_separate_directory_and_validates_sqlite(self):
        archive = create_backup(self.data, self.logs, self.password)
        restored = restore_backup(archive, self.root / "restored", self.password)
        with closing(sqlite3.connect(restored / "data" / "sync_state.db")) as db:
            value = db.execute("SELECT value FROM sample").fetchone()[0]
        self.assertEqual("состояние", value)

    def test_wrong_password_does_not_decrypt(self):
        archive = create_backup(self.data, self.logs, self.password)
        with self.assertRaises(Exception):
            decrypt_bytes(archive.read_bytes(), "different-password")

    def test_short_password_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "12"):
            create_backup(self.data, self.logs, "short")

    def _run_delivery_test(self, target, sender_name):
        environment = {
            "DATA_DIR": str(self.data),
            "LOG_DIR": str(self.logs),
            "BACKUP_TARGET": target,
            "BACKUP_PASSWORD": self.password,
            "BACKUP_INCLUDE_LOGS": "false",
            "BACKUP_RETENTION_COUNT": "2",
            "BACKUP_MAX_MB": "45",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch(f"backup.{sender_name}") as sender:
                archive = run_backup()
        sender.assert_called_once()
        status = json.loads(
            (self.data / "backup_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual("ok", status["status"])
        self.assertEqual(target, status["target"])
        self.assertTrue(archive.is_file())

    def test_telegram_delivery_route(self):
        self._run_delivery_test("telegram", "_send_telegram")

    def test_email_delivery_route(self):
        self._run_delivery_test("email", "_send_email")


if __name__ == "__main__":
    unittest.main()
