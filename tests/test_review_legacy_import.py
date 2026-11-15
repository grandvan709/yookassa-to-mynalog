import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from state_cli import list_pending_payments, list_pending_refunds
from state_store import ConcurrentRunError, StateStore, StateStoreError


class LegacyImportReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.legacy = self.root / "sync_state.json"
        self.state = {"pending_payments": [], "pending_refunds": []}
        self.legacy.write_text(json.dumps(self.state), encoding="utf-8")
        self.owner = StateStore(self.root / "sync_state.db")
        self.reader = StateStore(self.owner.db_path, legacy_json_path=self.legacy)

    def tearDown(self):
        self.temp.cleanup()

    def make_stale_lock(self):
        self.owner.acquire_lock()
        stale = datetime.now(timezone.utc) - self.owner.LOCK_TTL - timedelta(seconds=1)
        with closing(sqlite3.connect(self.owner.db_path)) as connection:
            connection.execute("UPDATE run_lock SET updated_at = ?", (stale.isoformat(),))
            connection.commit()

    def test_queue_listing_imports_legacy_after_crashed_owner(self):
        self.make_stale_lock()
        for command in (list_pending_payments, list_pending_refunds):
            with redirect_stdout(StringIO()):
                self.assertEqual(0, command(self.reader))
        self.assertEqual(self.state, self.reader.load())
        self.owner.acquire_lock()
        self.owner.release_lock()

    def test_legacy_import_cannot_bypass_active_foreign_lock(self):
        self.owner.acquire_lock()
        with self.assertRaises(ConcurrentRunError):
            self.reader.load()
        self.owner.release_lock()
        self.assertEqual(self.state, self.reader.load())

    def test_import_preserves_callers_existing_lock(self):
        self.reader.acquire_lock()
        self.assertEqual(self.state, self.reader.load())
        with self.assertRaises(ConcurrentRunError):
            self.owner.acquire_lock()
        self.reader.release_lock()

    def test_import_rechecks_state_after_acquiring_lock(self):
        current = {"pending_payments": ["newer-state"]}
        acquire = self.reader.acquire_lock

        def concurrent_writer_finishes():
            self.owner.acquire_lock()
            self.owner.save(current)
            self.owner.release_lock()
            acquire()

        with patch.object(self.reader, "acquire_lock", side_effect=concurrent_writer_finishes):
            self.assertEqual(current, self.reader.load())
        self.assertEqual(current, self.owner.load())

    def test_failed_import_releases_only_its_own_acquired_lock(self):
        self.make_stale_lock()
        self.legacy.write_text("not-json", encoding="utf-8")
        with self.assertRaises(StateStoreError):
            self.reader.load()
        self.owner.acquire_lock()
        self.owner.release_lock()
