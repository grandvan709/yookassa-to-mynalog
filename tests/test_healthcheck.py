import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import healthcheck


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.logs = self.root / "logs"
        self.data.mkdir()
        self.logs.mkdir()
        with closing(sqlite3.connect(self.data / "sync_state.db")) as db:
            with db:
                db.execute("CREATE TABLE state (id INTEGER)")

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, status):
        (self.data / "health.json").write_text(
            json.dumps(status), encoding="utf-8"
        )
        environment = {
            "DATA_DIR": str(self.data),
            "LOG_DIR": str(self.logs),
            "BACKUP_TARGET": "",
            "HEALTH_MAX_AGE_HOURS": "25",
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch.object(healthcheck, "_cron_is_running", return_value=True):
                with redirect_stdout(StringIO()):
                    return healthcheck.main()

    def test_healthy_database_and_recent_sync_pass(self):
        result = self._run(
            {
                "status": "ok",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.assertEqual(0, result)

    def test_stale_sync_fails(self):
        result = self._run(
            {
                "status": "ok",
                "updated_at": (
                    datetime.now(timezone.utc) - timedelta(hours=26)
                ).isoformat(),
            }
        )
        self.assertEqual(1, result)

    def test_degraded_sync_fails(self):
        result = self._run(
            {
                "status": "degraded",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.assertEqual(1, result)


if __name__ == "__main__":
    unittest.main()
