import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_checkpoints as fixtures


class LatePaymentRegressionTests(unittest.TestCase):
    def test_nonterminal_payment_remains_watched_until_success(self):
        for status in ("pending", "waiting_for_capture"):
            with self.subTest(status=status):
                manager = fixtures.manager_with([], [], fixtures.FakeNalog())
                item = fixtures.payment("late", "2026-01-01T00:00:00Z", status=status)
                manager._track_unpaid_payment(item, status)
                manager.get_yookassa_payment = AsyncMock(return_value=(item, None))
                asyncio.run(manager._resume_watched_payments())
                self.assertEqual(["late"], [x["payment_id"] for x in manager.state["watched_payments"]])
                item.status = "succeeded"
                asyncio.run(manager._resume_watched_payments())
                self.assertEqual(["late"], manager.state["processed_payments"])
                self.assertEqual(1, len(manager.nalog.add_calls))

    def test_old_nonterminal_payment_from_list_is_kept(self):
        manager = fixtures.manager_with([], [], fixtures.FakeNalog())
        item = fixtures.payment("late", "2026-01-01T00:00:00Z", status="pending")
        with patch("main.Payment.list", return_value=SimpleNamespace(items=[item], next_cursor=None)):
            asyncio.run(fixtures.SyncManager.get_new_yookassa_payments(manager))
        self.assertEqual(["late"], [x["payment_id"] for x in manager.state["watched_payments"]])
        self.assertEqual([], manager.state["expired_unpaid_payments"])

    def test_old_expired_nonterminal_records_are_restored_once(self):
        manager = fixtures.manager_with([], [], fixtures.FakeNalog())
        manager.state["expired_unpaid_payments"] = [
            {"payment_id": "late", "created_at": "2026-01-01T00:00:00Z", "last_status": "pending"},
            {"payment_id": "cancelled", "created_at": "2026-01-01T00:00:00Z", "last_status": "canceled"},
        ]
        manager._ensure_state_fields(manager.state)
        manager._ensure_state_fields(manager.state)
        self.assertEqual(["late"], [x["payment_id"] for x in manager.state["watched_payments"]])
        self.assertEqual(["cancelled"], [x["payment_id"] for x in manager.state["expired_unpaid_payments"]])

    def test_confirmed_cancellation_stops_watching(self):
        manager = fixtures.manager_with([], [], fixtures.FakeNalog())
        item = fixtures.payment("cancelled", "2026-01-01T00:00:00Z", status="canceled")
        manager._track_unpaid_payment(item, "pending")
        manager.get_yookassa_payment = AsyncMock(return_value=(item, None))
        asyncio.run(manager._resume_watched_payments())
        self.assertEqual([], manager.state["watched_payments"])
        self.assertEqual([], manager.nalog.add_calls)
