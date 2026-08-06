import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import config


class SyncStartConfigTests(unittest.TestCase):
    def test_date_uses_configured_timezone(self):
        with patch.object(config, "TZ", "Europe/Moscow"):
            result = config.parse_sync_start("2026-08-06")

        self.assertEqual("2026-08-05T21:00:00Z", result)

    def test_datetime_with_offset_is_normalized_to_utc(self):
        result = config.parse_sync_start("2026-08-06T15:42:30+03:00")

        self.assertEqual("2026-08-06T12:42:30Z", result)

    def test_utc_suffix_is_supported(self):
        result = config.parse_sync_start("2026-08-06T12:42:30Z")

        self.assertEqual("2026-08-06T12:42:30Z", result)

    def test_invalid_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ISO 8601"):
            config.parse_sync_start("06.08.2026 15:42")


if __name__ == "__main__":
    unittest.main()
