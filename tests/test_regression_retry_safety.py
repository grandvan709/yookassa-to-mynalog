import asyncio
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from state_store import StateStore
from state_cli import retry_payment, retry_refund
from telegram_admin_bot import TelegramAdminBot


class RetrySafetyTests(unittest.TestCase):
    def test_unknown_outcomes_cannot_be_reset_by_cli_or_admin(self):
        cases = [("p", s) for s in ("unknown", "creating")] + [
            ("r", s) for s in ("cancellation_unknown", "cancelling",
                               "replacement_unknown", "creating_replacement")
        ]
        for channel in ("cli", "admin"):
            for kind, status in cases:
                with self.subTest(channel=channel, kind=kind, status=status), tempfile.TemporaryDirectory() as temp:
                    store = StateStore(Path(temp) / "state.db")
                    key = "pending_payments" if kind == "p" else "pending_refunds"
                    id_key = "payment_id" if kind == "p" else "refund_id"
                    original = {key: [{id_key: "id-1", "currency": "RUB",
                                      "receipt_uuid": "receipt-1", "status": status}]}
                    store.save(original)
                    if channel == "cli":
                        with redirect_stdout(StringIO()):
                            result = (retry_payment if kind == "p" else retry_refund)(store, "id-1")
                        self.assertEqual(2, result)
                    else:
                        bot = TelegramAdminBot.__new__(TelegramAdminBot)
                        bot.store, bot.send = store, AsyncMock()
                        asyncio.run(bot.retry_queue_item(kind, "id-1"))
                    self.assertEqual(original, store.load())

    def test_definitely_rejected_operations_can_be_retried(self):
        for kind, status, expected in (("p", "rejected", "ready"),
                                       ("r", "cancellation_rejected", "ready"),
                                       ("r", "replacement_rejected", "cancelled")):
            with self.subTest(kind=kind, status=status), tempfile.TemporaryDirectory() as temp:
                store = StateStore(Path(temp) / "state.db")
                key = "pending_payments" if kind == "p" else "pending_refunds"
                id_key = "payment_id" if kind == "p" else "refund_id"
                store.save({key: [{id_key: "id-1", "currency": "RUB",
                                  "receipt_uuid": "receipt-1", "status": status}]})
                with redirect_stdout(StringIO()):
                    result = (retry_payment if kind == "p" else retry_refund)(store, "id-1")
                self.assertEqual(0, result)
                self.assertEqual(expected, store.load()[key][0]["status"])
