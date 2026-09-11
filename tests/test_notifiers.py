import asyncio
import logging
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from email_notifier import EmailNotifier
from telegram_notifier import TelegramNotifier


class PendingRefundNotificationTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_telegram_summary_includes_existing_pending_refunds(self):
        notifier = TelegramNotifier("token", "chat")
        notifier.on_pending_refunds_found(2)

        message = notifier._build_message()

        self.assertIn("Частичных возвратов для ручной обработки", message)
        self.assertIn("<b>2</b>", message)

    def test_email_sends_summary_for_pending_refunds_only(self):
        notifier = EmailNotifier(
            host="smtp.example.com",
            port=587,
            user="user@example.com",
            password="secret",
            to_email="owner@example.com",
        )
        notifier.on_pending_refunds_found(2)

        with patch.object(notifier, "_send_sync") as send:
            asyncio.run(notifier.send_summary())

        send.assert_called_once()
        self.assertIn(
            "Частичных возвратов для ручной обработки",
            send.call_args.args[0],
        )

    def test_adjusted_refund_is_included_in_summaries(self):
        telegram = TelegramNotifier("token", "chat")
        email = EmailNotifier(
            host="smtp.example.com",
            port=587,
            user="user@example.com",
            password="secret",
            to_email="owner@example.com",
        )
        telegram.on_refund_adjusted()
        email.on_refund_adjusted()

        self.assertIn("скорректировано", telegram._build_message())
        self.assertIn("скорректировано", email._build_html())

    def test_payment_totals_keep_kopecks(self):
        notifier = TelegramNotifier("token", "chat")
        notifier.on_sync_start(2)
        notifier.on_payment_success(Decimal("0.10"))
        notifier.on_payment_success(Decimal("0.20"))

        message = notifier._build_message()

        self.assertIn("0.30 руб.", message)

    def test_delayed_payment_summary_does_not_show_one_out_of_zero(self):
        notifier = TelegramNotifier("token", "chat")
        notifier.on_payment_success(Decimal("129.00"))

        message = notifier._build_message()

        self.assertIn("Успешно: <b>1</b> из 1", message)

    def test_update_notification_contains_versions_and_github_link(self):
        telegram = TelegramNotifier("token", "chat")
        email = EmailNotifier(
            host="smtp.example.com",
            port=587,
            user="user@example.com",
            password="secret",
            to_email="owner@example.com",
        )
        telegram.on_update_available("9.1.0")
        email.on_update_available("9.1.0")

        telegram_message = telegram._build_message()
        email_message = email._build_html()

        for message in (telegram_message, email_message):
            self.assertIn("9.1.0", message)
            self.assertIn("github.com/grandvan709/yookassa-to-mynalog", message)


if __name__ == "__main__":
    unittest.main()
