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

    def test_pending_payment_watch_must_be_positive(self):
        with patch.object(config, "PENDING_PAYMENT_WATCH_MINUTES", 0):
            with self.assertRaisesRegex(
                ValueError, "PENDING_PAYMENT_WATCH_MINUTES"
            ):
                config.validate_config()

    def test_payment_id_prefix_rejects_unsafe_characters(self):
        with patch.object(config, "PAYMENT_ID_PREFIX", "payment prefix"):
            with self.assertRaisesRegex(ValueError, "PAYMENT_ID_PREFIX"):
                config.validate_config()

    def test_customer_receipts_require_bedolaga_bot_token(self):
        with patch.multiple(
            config,
            TELEGRAM_CUSTOMER_RECEIPTS_ENABLED=True,
            TELEGRAM_CUSTOMER_BOT_TOKEN=None,
            MOY_NALOG_RECEIPT_INN="123456789012",
            YOOKASSA_SHOP_ID="shop",
            YOOKASSA_API_KEY="key",
            MOY_NALOG_AUTH_METHOD="password",
            MOY_NALOG_LOGIN="123456789012",
            MOY_NALOG_PASSWORD="password",
        ):
            with self.assertRaisesRegex(
                ValueError, "TELEGRAM_CUSTOMER_BOT_TOKEN"
            ):
                config.validate_config()


if __name__ == "__main__":
    unittest.main()
