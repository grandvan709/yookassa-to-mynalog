import asyncio
import logging
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import config
import main as main_module
from main import SyncManager
from state_store import ConcurrentRunError


def payment(payment_id, created_at, amount="100.00"):
    return SimpleNamespace(
        id=payment_id,
        created_at=created_at,
        amount=SimpleNamespace(value=amount, currency="RUB"),
        description=payment_id,
        metadata={},
        invoice_details=None,
        merchant_customer_id=None,
    )


def refund(refund_id, payment_id, created_at, amount="100.00"):
    return SimpleNamespace(
        id=refund_id,
        payment_id=payment_id,
        created_at=created_at,
        amount=SimpleNamespace(value=amount),
    )


class FakeNalog:
    def __init__(
        self,
        failed_payment_ids=(),
        failed_receipts=(),
        fail_replacements=False,
        found_receipts=None,
        income_status="active",
    ):
        self.failed_payment_ids = set(failed_payment_ids)
        self.failed_receipts = set(failed_receipts)
        self.fail_replacements = fail_replacements
        self.found_receipts = found_receipts or {}
        self.income_status = income_status
        self.last_error = "temporary error"
        self.last_error_retryable = False
        self.last_operation_uncertain = True
        self.cancel_calls = []
        self.add_calls = []

    async def add_income(self, description, amount, payment_date):
        self.add_calls.append((description, Decimal(str(amount)), payment_date))
        if description in self.failed_payment_ids or (
            self.fail_replacements and "остаток после возврата" in description
        ):
            return None
        return f"receipt-{description}"

    async def find_income(self, description, amount, operation_date=None):
        return self.found_receipts.get(description)

    async def get_income_status(self, receipt_uuid, operation_date=None):
        return self.income_status

    async def cancel_income(self, receipt_uuid):
        self.cancel_calls.append(receipt_uuid)
        return receipt_uuid not in self.failed_receipts

    async def close(self):
        pass


class FakeStateStore:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    def acquire_lock(self):
        self.acquired += 1

    def release_lock(self):
        self.released += 1


def manager_with(
    payments,
    refunds,
    nalog,
    payments_error=None,
    refunds_error=None,
    payment_totals=None,
):
    manager = SyncManager.__new__(SyncManager)
    manager.state = {
        "last_sync_time": "2026-01-01T00:00:00Z",
        "last_refund_sync_time": "2026-01-01T00:00:00Z",
        "processed_payments": [],
        "pending_payments": [],
        "processed_refunds": [],
        "pending_refunds": [],
        "payment_balances": {},
        "payment_event_times": {},
        "refund_event_times": {},
        "receipt_map": {
            "payment-new": "receipt-new",
            "payment-old": "receipt-old",
        },
    }
    manager.nalog = nalog
    manager.notifier = None
    manager.email_notifier = None
    manager.event_notifiers = []
    manager.check_for_updates = lambda: None
    manager.save_state = lambda: None

    async def get_payments():
        return payments, payments_error

    async def get_refunds():
        return refunds, refunds_error

    async def get_payment(payment_id):
        totals = payment_totals or {}
        total = totals.get(payment_id, "100.00")
        return payment(
            payment_id,
            "2026-01-01T12:00:00Z",
            amount=total,
        ), None

    manager.get_new_yookassa_payments = get_payments
    manager.get_new_refunds = get_refunds
    manager.get_yookassa_payment = get_payment
    return manager


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.original_template = config.INCOME_DESCRIPTION_TEMPLATE
        self.original_refunds_enabled = config.REFUNDS_ENABLED
        config.INCOME_DESCRIPTION_TEMPLATE = "{id}"
        config.REFUNDS_ENABLED = True
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        config.INCOME_DESCRIPTION_TEMPLATE = self.original_template
        config.REFUNDS_ENABLED = self.original_refunds_enabled
        logging.disable(logging.NOTSET)

    def test_legacy_state_gets_pending_refunds_field(self):
        manager = SyncManager.__new__(SyncManager)

        state = manager._ensure_state_fields({})

        self.assertEqual([], state["pending_refunds"])
        self.assertEqual({}, state["payment_balances"])

    def test_pending_refund_is_not_returned_for_processing_again(self):
        manager = SyncManager.__new__(SyncManager)
        manager.state = {
            "last_sync_time": "2026-01-01T00:00:00Z",
            "last_refund_sync_time": "2026-01-01T00:00:00Z",
            "processed_refunds": [],
            "pending_refunds": [{"refund_id": "refund-pending"}],
        }
        response = SimpleNamespace(
            items=[
                refund(
                    "refund-pending",
                    "payment-old",
                    "2026-01-02T00:00:00Z",
                    amount="40.00",
                ),
                refund(
                    "refund-new",
                    "payment-new",
                    "2026-01-03T00:00:00Z",
                ),
            ],
            next_cursor=None,
        )

        with patch("main.Refund.list", return_value=response):
            refunds, error = asyncio.run(manager.get_new_refunds())

        self.assertIsNone(error)
        self.assertEqual(["refund-new"], [item.id for item in refunds])

    def test_pending_payment_is_not_returned_for_processing_again(self):
        manager = SyncManager.__new__(SyncManager)
        manager.state = {
            "last_sync_time": "2026-01-01T00:00:00Z",
            "processed_payments": [],
            "pending_payments": [{"payment_id": "payment-pending"}],
        }
        response = SimpleNamespace(
            items=[
                payment("payment-pending", "2026-01-02T00:00:00Z"),
                payment("payment-new", "2026-01-03T00:00:00Z"),
            ],
            next_cursor=None,
        )

        with patch("main.Payment.list", return_value=response):
            payments, error = asyncio.run(manager.get_new_yookassa_payments())

        self.assertIsNone(error)
        self.assertEqual(["payment-new"], [item.id for item in payments])

    def test_payment_query_uses_exact_inclusive_start_timestamp(self):
        manager = SyncManager.__new__(SyncManager)
        manager.state = {
            "last_sync_time": "2026-08-06T12:42:30Z",
            "processed_payments": [],
            "pending_payments": [],
        }
        response = SimpleNamespace(items=[], next_cursor=None)

        with patch("main.Payment.list", return_value=response) as payment_list:
            asyncio.run(manager.get_new_yookassa_payments())

        params = payment_list.call_args.args[0]
        self.assertEqual(
            "2026-08-06T12:42:30Z",
            params["created_at.gte"],
        )

    def test_payment_failure_keeps_old_checkpoint(self):
        payments = [
            payment("new", "2026-01-03T00:00:00Z"),
            payment("old", "2026-01-02T00:00:00Z"),
        ]
        nalog = FakeNalog(failed_payment_ids={"old"})
        manager = manager_with(
            payments,
            [],
            nalog,
        )

        asyncio.run(manager.sync())

        self.assertEqual("2026-01-01T00:00:00Z", manager.state["last_sync_time"])
        self.assertEqual(["new"], manager.state["processed_payments"])
        self.assertEqual("unknown", manager.state["pending_payments"][0]["status"])
        self.assertEqual(2, len(nalog.add_calls))

    def test_interrupted_payment_creation_is_reconciled_without_retry(self):
        description = "payment-old"
        nalog = FakeNalog(found_receipts={description: "existing-receipt"})
        manager = manager_with([], [], nalog)
        manager.state["pending_payments"] = [{
            "payment_id": "payment-old",
            "amount": "100.00",
            "currency": "RUB",
            "created_at": "2026-01-02T00:00:00Z",
            "description": description,
            "status": "creating",
        }]

        asyncio.run(manager.sync())

        self.assertEqual([], nalog.add_calls)
        self.assertEqual([], manager.state["pending_payments"])
        self.assertEqual(["payment-old"], manager.state["processed_payments"])
        self.assertEqual(
            "existing-receipt",
            manager.state["receipt_map"]["payment-old"],
        )

    def test_unknown_payment_is_not_created_again_on_next_sync(self):
        nalog = FakeNalog(failed_payment_ids={"old"})
        payments = [payment("old", "2026-01-02T00:00:00Z")]
        manager = manager_with(
            payments,
            [],
            nalog,
        )

        asyncio.run(manager.sync())
        first_call_count = len(nalog.add_calls)
        payments.clear()
        asyncio.run(manager.sync())

        self.assertEqual(first_call_count, len(nalog.add_calls))
        self.assertEqual("unknown", manager.state["pending_payments"][0]["status"])

    def test_payment_retries_next_run_when_request_was_not_sent(self):
        nalog = FakeNalog(failed_payment_ids={"outage"})
        nalog.last_error_retryable = True
        nalog.last_operation_uncertain = False
        payments = [payment("outage", "2026-01-02T00:00:00Z")]
        manager = manager_with(payments, [], nalog)

        asyncio.run(manager.sync())
        self.assertEqual("ready", manager.state["pending_payments"][0]["status"])
        first_call_count = len(nalog.add_calls)

        payments.clear()
        asyncio.run(manager.sync())

        self.assertGreater(len(nalog.add_calls), first_call_count)
        self.assertEqual("ready", manager.state["pending_payments"][0]["status"])

    def test_retry_queue_stops_batch_while_fns_is_unavailable(self):
        nalog = FakeNalog(failed_payment_ids={"first", "second"})
        nalog.last_error_retryable = True
        nalog.last_operation_uncertain = False
        manager = manager_with([], [], nalog)
        manager.state["pending_payments"] = [
            {
                "payment_id": name,
                "amount": "100.00",
                "currency": "RUB",
                "created_at": "2026-01-02T00:00:00Z",
                "description": name,
                "status": "ready",
            }
            for name in ("first", "second")
        ]

        asyncio.run(manager._resume_pending_payments(stop_on_unavailable=True))

        self.assertEqual(["first"], [call[0] for call in nalog.add_calls])
        self.assertEqual(1, manager.state["pending_payments"][0]["attempts"])
        self.assertNotIn("attempts", manager.state["pending_payments"][1])

    def test_retry_queue_keeps_receipt_after_attempt_limit(self):
        nalog = FakeNalog(failed_payment_ids={"limited"})
        nalog.last_error_retryable = True
        nalog.last_operation_uncertain = False
        manager = manager_with([], [], nalog)
        manager.state["pending_payments"] = [{
            "payment_id": "limited",
            "amount": "100.00",
            "currency": "RUB",
            "created_at": "2026-01-02T00:00:00Z",
            "description": "limited",
            "status": "ready",
            "queue_attempts": 0,
        }]

        with patch.object(config, "FNS_QUEUE_MAX_ATTEMPTS", 2):
            asyncio.run(manager._resume_pending_payments())
            asyncio.run(manager._resume_pending_payments())
            call_count_at_limit = len(nalog.add_calls)
            asyncio.run(manager._resume_pending_payments())

        workflow = manager.state["pending_payments"][0]
        self.assertEqual("retry_exhausted", workflow["status"])
        self.assertEqual(2, workflow["queue_attempts"])
        self.assertEqual(2, call_count_at_limit)
        self.assertEqual(call_count_at_limit, len(nalog.add_calls))

    def test_telegram_preferences_do_not_disable_email_events(self):
        manager = manager_with([], [], FakeNalog())
        telegram = SimpleNamespace(
            on_payment_success=Mock(),
            on_payment_error=Mock(),
        )
        email = SimpleNamespace(
            on_payment_success=Mock(),
            on_payment_error=Mock(),
        )
        manager.notifier = telegram
        manager.email_notifier = email
        manager.event_notifiers = [telegram, email]
        manager.state["notification_preferences"] = {
            "receipt_success": False,
            "receipt_errors": False,
        }

        manager._emit("on_payment_success", Decimal("10.00"))
        manager._emit("on_payment_error", "payment", "error")

        telegram.on_payment_success.assert_not_called()
        telegram.on_payment_error.assert_not_called()
        email.on_payment_success.assert_called_once()
        email.on_payment_error.assert_called_once()

    def test_success_reports_and_admin_errors_use_separate_telegram_targets(self):
        manager = manager_with([], [], FakeNalog())
        admin = SimpleNamespace(
            on_payment_success=Mock(),
            on_payment_error=Mock(),
        )
        reports = SimpleNamespace(
            on_payment_success=Mock(),
            on_payment_error=Mock(),
        )
        manager.notifier = admin
        manager.receipt_notifier = reports
        manager.email_notifier = None
        manager.event_notifiers = [admin, reports]
        manager.state["notification_preferences"] = {"receipt_errors": True}
        manager.state["receipt_reports"] = {"enabled": True}

        manager._emit("on_payment_success", Decimal("10.00"))
        manager._emit("on_payment_error", "payment", "error")

        reports.on_payment_success.assert_called_once()
        reports.on_payment_error.assert_not_called()
        admin.on_payment_success.assert_not_called()
        admin.on_payment_error.assert_called_once()

    def test_fns_only_worker_does_not_fetch_yookassa(self):
        nalog = FakeNalog()
        manager = manager_with([], [], nalog)
        manager.state_store = FakeStateStore()
        manager.state["pending_payments"] = [{
            "payment_id": "queued",
            "amount": "100.00",
            "currency": "RUB",
            "created_at": "2026-01-02T00:00:00Z",
            "description": "queued",
            "status": "ready",
            "attempts": 2,
        }]
        manager.get_new_yookassa_payments = AsyncMock(
            side_effect=AssertionError("YooKassa must not be queried")
        )

        with patch("main.write_status"), patch.object(
            config, "FNS_RETRY_DELAY_SECONDS", 0
        ):
            asyncio.run(manager.retry_fns_queue())

        manager.get_new_yookassa_payments.assert_not_awaited()
        self.assertEqual([], manager.state["pending_payments"])
        self.assertEqual(1, manager.state_store.acquired)
        self.assertEqual(1, manager.state_store.released)

    def test_scheduled_collision_is_a_normal_skipped_run(self):
        manager = SimpleNamespace(
            retry_fns_queue=AsyncMock(
                side_effect=ConcurrentRunError("already running")
            ),
            nalog=SimpleNamespace(close=AsyncMock()),
        )
        with patch.object(main_module, "SyncManager", return_value=manager), patch.object(
            main_module, "print_banner"
        ), patch.object(main_module.logging, "info") as log_info:
            asyncio.run(main_module.main(retry_fns_only=True))

        manager.nalog.close.assert_awaited_once()
        log_info.assert_called_once()

    def test_permanent_payment_rejection_is_not_retried(self):
        nalog = FakeNalog(failed_payment_ids={"invalid"})
        nalog.last_error_retryable = False
        nalog.last_operation_uncertain = False
        payments = [payment("invalid", "2026-01-02T00:00:00Z")]
        manager = manager_with(payments, [], nalog)

        asyncio.run(manager.sync())
        first_call_count = len(nalog.add_calls)
        self.assertEqual(
            "rejected", manager.state["pending_payments"][0]["status"]
        )

        payments.clear()
        asyncio.run(manager.sync())

        self.assertEqual(first_call_count, len(nalog.add_calls))

    def test_payment_id_is_added_to_non_unique_description(self):
        config.INCOME_DESCRIPTION_TEMPLATE = "Оплата услуг"
        nalog = FakeNalog()
        manager = manager_with(
            [payment("payment-unique", "2026-01-02T00:00:00Z")],
            [],
            nalog,
        )

        asyncio.run(manager.sync())

        self.assertIn("payment-unique", nalog.add_calls[0][0])

    def test_non_rub_payment_is_held_without_calling_mynalog(self):
        foreign_payment = payment(
            "foreign",
            "2026-01-02T00:00:00Z",
            amount="10.00",
        )
        foreign_payment.amount.currency = "USD"
        nalog = FakeNalog()
        manager = manager_with([foreign_payment], [], nalog)

        asyncio.run(manager.sync())

        self.assertEqual([], nalog.add_calls)
        self.assertEqual(
            "unsupported_currency",
            manager.state["pending_payments"][0]["status"],
        )

    def test_old_processed_history_is_pruned_behind_checkpoints(self):
        manager = manager_with([], [], FakeNalog())
        manager.state.update({
            "last_sync_time": "2025-01-01T00:00:00Z",
            "last_refund_sync_time": "2025-01-01T00:00:00Z",
            "processed_payments": ["old-payment", "legacy-payment"],
            "processed_refunds": ["old-refund"],
            "payment_event_times": {"old-payment": "2020-01-01T00:00:00Z"},
            "refund_event_times": {"old-refund": "2020-01-01T00:00:00Z"},
            "receipt_map": {"old-payment": "receipt-old"},
            "payment_balances": {"old-payment": "100.00"},
        })

        manager._prune_processed_history()

        self.assertEqual(["legacy-payment"], manager.state["processed_payments"])
        self.assertEqual([], manager.state["processed_refunds"])
        self.assertNotIn("old-payment", manager.state["receipt_map"])
        self.assertNotIn("old-payment", manager.state["payment_balances"])

    def test_complete_payment_batch_advances_to_newest_item(self):
        payments = [
            payment("new", "2026-01-03T00:00:00Z"),
            payment("old", "2026-01-02T00:00:00Z"),
        ]
        manager = manager_with(payments, [], FakeNalog())

        asyncio.run(manager.sync())

        self.assertEqual("2026-01-03T00:00:00Z", manager.state["last_sync_time"])

    def test_partial_payment_fetch_keeps_old_checkpoint(self):
        payments = [payment("new", "2026-01-03T00:00:00Z")]
        manager = manager_with(
            payments,
            [],
            FakeNalog(),
            payments_error="second page timed out",
        )

        asyncio.run(manager.sync())

        self.assertEqual("2026-01-01T00:00:00Z", manager.state["last_sync_time"])

    def test_refund_failure_keeps_old_checkpoint(self):
        refunds = [
            refund("refund-new", "payment-new", "2026-01-03T00:00:00Z"),
            refund("refund-old", "payment-old", "2026-01-02T00:00:00Z"),
        ]
        manager = manager_with(
            [],
            refunds,
            FakeNalog(failed_receipts={"receipt-old"}),
        )

        asyncio.run(manager.sync())

        self.assertEqual(
            "2026-01-01T00:00:00Z",
            manager.state["last_refund_sync_time"],
        )
        self.assertEqual(["refund-new"], manager.state["processed_refunds"])
        self.assertEqual(
            "cancellation_unknown",
            manager.state["pending_refunds"][0]["status"],
        )

    def test_cancellation_retries_when_request_was_not_sent(self):
        refunds = [
            refund("refund-outage", "payment-old", "2026-01-02T00:00:00Z")
        ]
        nalog = FakeNalog(failed_receipts={"receipt-old"})
        nalog.last_error_retryable = True
        nalog.last_operation_uncertain = False
        manager = manager_with([], refunds, nalog)

        asyncio.run(manager.sync())
        self.assertEqual("ready", manager.state["pending_refunds"][0]["status"])
        first_call_count = len(nalog.cancel_calls)

        refunds.clear()
        asyncio.run(manager.sync())

        self.assertGreater(len(nalog.cancel_calls), first_call_count)
        self.assertEqual("ready", manager.state["pending_refunds"][0]["status"])

    def test_partial_refund_fetch_keeps_old_checkpoint(self):
        refunds = [
            refund("refund-new", "payment-new", "2026-01-03T00:00:00Z"),
        ]
        manager = manager_with(
            [],
            refunds,
            FakeNalog(),
            refunds_error="second page timed out",
        )

        asyncio.run(manager.sync())

        self.assertEqual(
            "2026-01-01T00:00:00Z",
            manager.state["last_refund_sync_time"],
        )

    def test_partial_refund_replaces_receipt_with_remaining_amount(self):
        partial = refund(
            "refund-partial",
            "payment-new",
            "2026-01-03T00:00:00Z",
            amount="40.00",
        )
        nalog = FakeNalog()
        manager = manager_with(
            [],
            [partial],
            nalog,
            payment_totals={"payment-new": "100.00"},
        )

        asyncio.run(manager.sync())

        self.assertEqual(["receipt-new"], nalog.cancel_calls)
        self.assertTrue(
            manager.state["receipt_map"]["payment-new"].startswith("receipt-")
        )
        self.assertEqual(["refund-partial"], manager.state["processed_refunds"])
        self.assertEqual([], manager.state["pending_refunds"])
        self.assertEqual("60.00", manager.state["payment_balances"]["payment-new"])
        self.assertEqual(
            "2026-01-03T00:00:00Z",
            manager.state["last_refund_sync_time"],
        )

    def test_unknown_replacement_is_kept_for_manual_reconciliation(self):
        partial = refund(
            "refund-partial",
            "payment-new",
            "2026-01-03T00:00:00Z",
            amount="40.00",
        )
        manager = manager_with(
            [],
            [partial],
            FakeNalog(fail_replacements=True),
        )

        asyncio.run(manager.sync())

        self.assertEqual([], manager.state["processed_refunds"])
        self.assertEqual(
            "replacement_unknown",
            manager.state["pending_refunds"][0]["status"],
        )
        self.assertEqual(
            "2026-01-01T00:00:00Z",
            manager.state["last_refund_sync_time"],
        )

    def test_replacement_retries_when_request_was_not_sent(self):
        partial = refund(
            "refund-partial",
            "payment-new",
            "2026-01-03T00:00:00Z",
            amount="40.00",
        )
        nalog = FakeNalog(fail_replacements=True)
        nalog.last_error_retryable = True
        nalog.last_operation_uncertain = False
        manager = manager_with([], [partial], nalog)

        asyncio.run(manager.sync())

        self.assertEqual([], manager.state["processed_refunds"])
        self.assertEqual(
            "cancelled",
            manager.state["pending_refunds"][0]["status"],
        )

    def test_interrupted_replacement_is_reconciled_without_creating_duplicate(self):
        description = "payment-new [остаток после возврата refund-partial]"
        nalog = FakeNalog(found_receipts={description: "replacement-receipt"})
        manager = manager_with([], [], nalog)
        manager.state["pending_refunds"] = [{
            "refund_id": "refund-partial",
            "payment_id": "payment-new",
            "refund_amount": "40.00",
            "payment_amount": "100.00",
            "previous_amount": "100.00",
            "remaining_amount": "60.00",
            "created_at": "2026-01-03T00:00:00Z",
            "payment_created_at": "2026-01-01T12:00:00Z",
            "receipt_uuid": "receipt-new",
            "replacement_description": description,
            "status": "creating_replacement",
        }]

        asyncio.run(manager.sync())

        self.assertEqual([], nalog.cancel_calls)
        self.assertEqual([], manager.state["pending_refunds"])
        self.assertEqual(["refund-partial"], manager.state["processed_refunds"])
        self.assertEqual(
            "replacement-receipt",
            manager.state["receipt_map"]["payment-new"],
        )

    def test_unknown_cancellation_is_reconciled_by_receipt_uuid(self):
        nalog = FakeNalog(income_status="cancelled")
        manager = manager_with([], [], nalog)
        adjustment = {
            "refund_id": "refund-unknown",
            "payment_id": "payment-new",
            "refund_amount": "100.00",
            "payment_amount": "100.00",
            "previous_amount": "100.00",
            "remaining_amount": "0.00",
            "created_at": "2026-01-03T00:00:00Z",
            "payment_created_at": "2026-01-01T12:00:00Z",
            "receipt_uuid": "receipt-new",
            "replacement_description": "unused",
            "status": "cancellation_unknown",
        }
        manager.state["pending_refunds"] = [adjustment]

        result = asyncio.run(manager._resume_refund_adjustment(adjustment))

        self.assertEqual("cancelled", result)
        self.assertEqual([], nalog.cancel_calls)
        self.assertEqual([], manager.state["pending_refunds"])

    def test_refunds_are_disabled_by_default_switch(self):
        manager = SyncManager.__new__(SyncManager)
        manager.state = {
            "last_sync_time": "2026-01-01T00:00:00Z",
            "processed_refunds": [],
            "pending_refunds": [],
        }
        with patch.object(config, "REFUNDS_ENABLED", False), patch(
            "main.Refund.list"
        ) as refund_list:
            result = asyncio.run(manager.get_new_refunds())
        self.assertEqual(([], None), result)
        refund_list.assert_not_called()

    def test_two_partial_refunds_update_running_balance(self):
        refunds = [
            refund(
                "refund-second",
                "payment-new",
                "2026-01-04T00:00:00Z",
                amount="30.00",
            ),
            refund(
                "refund-first",
                "payment-new",
                "2026-01-03T00:00:00Z",
                amount="40.00",
            ),
        ]
        manager = manager_with([], refunds, FakeNalog())

        asyncio.run(manager.sync())

        self.assertEqual("30.00", manager.state["payment_balances"]["payment-new"])
        self.assertEqual([], manager.state["pending_refunds"])
        self.assertEqual(
            ["refund-second", "refund-first"],
            manager.state["processed_refunds"],
        )

    def test_final_partial_refund_removes_receipt_and_balance(self):
        refunds = [
            refund(
                "refund-second",
                "payment-new",
                "2026-01-04T00:00:00Z",
                amount="60.00",
            ),
            refund(
                "refund-final",
                "payment-new",
                "2026-01-03T00:00:00Z",
                amount="40.00",
            ),
        ]
        manager = manager_with([], refunds, FakeNalog())

        asyncio.run(manager.sync())

        self.assertNotIn("payment-new", manager.state["receipt_map"])
        self.assertNotIn("payment-new", manager.state["payment_balances"])
        self.assertEqual([], manager.state["pending_refunds"])

    def test_complete_refund_batch_advances_to_newest_item(self):
        refunds = [
            refund("refund-new", "payment-new", "2026-01-03T00:00:00Z"),
            refund("refund-old", "payment-old", "2026-01-02T00:00:00Z"),
        ]
        manager = manager_with([], refunds, FakeNalog())

        asyncio.run(manager.sync())

        self.assertEqual(
            "2026-01-03T00:00:00Z",
            manager.state["last_refund_sync_time"],
        )


if __name__ == "__main__":
    unittest.main()
