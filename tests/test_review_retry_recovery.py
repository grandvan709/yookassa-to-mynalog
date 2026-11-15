import asyncio
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock

import test_checkpoints as fixtures
from state_cli import retry_payment
from state_store import StateStore
from telegram_admin_bot import TelegramAdminBot


class RetryRecoveryReviewTests(unittest.TestCase):
    def payment(self, status="ready"):
        return {"payment_id": "p1", "amount": "25.50", "currency": "RUB",
                "created_at": "2026-01-01T00:00:00Z", "description": "p1",
                "status": status, "attempts": 0, "queue_attempts": 0}

    def bot(self, store):
        bot = TelegramAdminBot.__new__(TelegramAdminBot)
        bot.store, bot.send = store, AsyncMock()
        return bot

    def test_buttons_and_handler_share_retry_policy(self):
        cases = [("p", s, allowed) for s, allowed in (
            ("ready", True), ("rejected", True), ("retry_exhausted", True),
            ("unknown", False), ("creating", False), ("manual_error", False))]
        cases += [("r", s, allowed) for s, allowed in (
            ("ready", True), ("cancelled", True), ("cancellation_rejected", True),
            ("replacement_rejected", True), ("cancellation_unknown", False),
            ("replacement_unknown", False), ("creating_replacement", False),
            ("cancelling", False))]
        for kind, status, allowed in cases:
            with self.subTest(kind=kind, status=status), tempfile.TemporaryDirectory() as temp:
                store = StateStore(Path(temp) / "state.db")
                key = "pending_payments" if kind == "p" else "pending_refunds"
                item = self.payment(status)
                item.update(refund_id="p1", receipt_uuid="receipt-1")
                initial = {key: [item]}
                store.save(initial)
                bot = self.bot(store)
                asyncio.run(bot.show_queue_item(kind, "p1"))
                buttons = bot.send.await_args.kwargs["reply_markup"]["inline_keyboard"]
                has_retry = any(b.get("callback_data", "").startswith("retry:") for row in buttons for b in row)
                self.assertEqual(allowed, has_retry)
                asyncio.run(bot.retry_queue_item(kind, "p1"))
                if not allowed:
                    self.assertEqual(initial, store.load())
                else:
                    self.assertIn("безопасной фазы", bot.send.await_args.args[0])

    def test_prewrite_failure_is_recoverable_after_repair_in_both_interfaces(self):
        for channel in ("cli", "admin"):
            for field in ("amount", "created_at"):
                with self.subTest(channel=channel, field=field), tempfile.TemporaryDirectory() as temp:
                    manager = fixtures.manager_with([], [], fixtures.FakeNalog())
                    workflow = self.payment()
                    correct = workflow[field]
                    workflow[field] = "malformed"
                    manager.state["pending_payments"] = [workflow]
                    store = StateStore(Path(temp) / "state.db")
                    manager.save_state = lambda: store.save(manager.state)
                    asyncio.run(manager._resume_pending_payments())
                    self.assertEqual("manual_error", workflow["status"])
                    self.assertEqual("before_write", workflow.get("failure_phase"))
                    self.assertEqual([], manager.nalog.add_calls)
                    # Broken parameters still do not offer a retry.
                    bot = self.bot(store)
                    asyncio.run(bot.show_queue_item("p", "p1"))
                    buttons = bot.send.await_args.kwargs["reply_markup"]["inline_keyboard"]
                    self.assertFalse(any(b.get("callback_data", "").startswith("retry:") for row in buttons for b in row))
                    repaired = store.load()
                    repaired["pending_payments"][0][field] = correct
                    store.save(repaired)
                    asyncio.run(bot.show_queue_item("p", "p1"))
                    buttons = bot.send.await_args.kwargs["reply_markup"]["inline_keyboard"]
                    self.assertTrue(any(b.get("callback_data", "").startswith("retry:") for row in buttons for b in row))
                    if channel == "cli":
                        with redirect_stdout(StringIO()):
                            self.assertEqual(0, retry_payment(store, "p1"))
                    else:
                        asyncio.run(bot.retry_queue_item("p", "p1"))
                    manager.state = store.load()
                    self.assertEqual("ready", manager.state["pending_payments"][0]["status"])
                    asyncio.run(manager._resume_pending_payments())
                    self.assertEqual(["p1"], manager.state["processed_payments"])
                    self.assertEqual(1, len(manager.nalog.add_calls))

    def test_exception_after_write_stays_reconciliation_only(self):
        manager = fixtures.manager_with([], [], fixtures.FakeNalog())
        manager.nalog.add_income = AsyncMock(side_effect=ValueError("reply lost after accepting write"))
        item = self.payment()
        manager.state["pending_payments"] = [item]
        asyncio.run(manager._resume_pending_payments())
        self.assertEqual("unknown", item["status"])
        self.assertNotEqual("before_write", item.get("failure_phase"))
        asyncio.run(manager._resume_pending_payments())
        manager.nalog.add_income.assert_awaited_once()
        self.assertEqual(1, len(manager.nalog.find_calls))

    def test_legacy_manual_error_without_evidence_cannot_be_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            state = {"pending_payments": [self.payment("manual_error")]}
            store.save(state)
            with redirect_stdout(StringIO()):
                self.assertEqual(2, retry_payment(store, "p1"))
            asyncio.run(self.bot(store).retry_queue_item("p", "p1"))
            self.assertEqual(state, store.load())
