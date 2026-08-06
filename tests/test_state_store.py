import json
import logging
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from state_store import ConcurrentRunError, StateStore, StateStoreError
from state_cli import (
    list_pending_payments,
    list_pending_refunds,
    resolve_payment,
    resolve_refund,
    retry_payment,
    retry_refund,
    set_sync_start,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "sync_state.db"
        self.json_path = self.root / "sync_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()
        logging.disable(logging.NOTSET)

    def test_state_round_trip(self):
        store = StateStore(self.db_path)
        state = {"last_sync_time": "2026-01-01T00:00:00Z", "данные": [1, 2]}

        store.save(state)

        self.assertEqual(state, StateStore(self.db_path).load())

    def test_legacy_json_is_imported_and_left_as_backup(self):
        state = {"processed_payments": ["payment-1"]}
        self.json_path.write_text(
            json.dumps(state),
            encoding="utf-8",
        )

        store = StateStore(self.db_path, self.json_path)

        self.assertEqual(state, store.load())
        self.assertTrue(self.json_path.exists())
        self.assertEqual(state, StateStore(self.db_path).load())

    def test_corrupt_legacy_json_stops_migration(self):
        self.json_path.write_text("{broken", encoding="utf-8")
        store = StateStore(self.db_path, self.json_path)

        with self.assertRaises(StateStoreError):
            store.load()

    def test_concurrent_run_is_rejected_until_lock_is_released(self):
        first = StateStore(self.db_path)
        second = StateStore(self.db_path)
        first.acquire_lock()

        with self.assertRaises(ConcurrentRunError):
            second.acquire_lock()

        first.release_lock()
        second.acquire_lock()
        second.release_lock()

    def test_pending_refund_can_be_listed_and_resolved(self):
        store = StateStore(self.db_path)
        store.save({
            "processed_refunds": [],
            "receipt_map": {"payment-1": "old-receipt"},
            "payment_balances": {"payment-1": "100.00"},
            "pending_refunds": [{
                "refund_id": "refund-1",
                "payment_id": "payment-1",
                "refund_amount": "40.00",
                "payment_amount": "100.00",
                "remaining_amount": "60.00",
            }]
        })

        output = StringIO()
        with redirect_stdout(output):
            list_result = list_pending_refunds(store)
            resolve_result = resolve_refund(store, "refund-1", "new-receipt")

        self.assertEqual(0, list_result)
        self.assertEqual(0, resolve_result)
        self.assertIn("refund-1", output.getvalue())
        state = store.load()
        self.assertEqual([], state["pending_refunds"])
        self.assertEqual(["refund-1"], state["processed_refunds"])
        self.assertEqual("new-receipt", state["receipt_map"]["payment-1"])
        self.assertEqual("60.00", state["payment_balances"]["payment-1"])

    def test_pending_refund_can_be_reset_for_retry(self):
        store = StateStore(self.db_path)
        store.save({
            "pending_refunds": [{
                "refund_id": "refund-1",
                "receipt_uuid": "receipt-1",
                "status": "cancellation_unknown",
            }]
        })

        with redirect_stdout(StringIO()):
            result = retry_refund(store, "refund-1")

        self.assertEqual(0, result)
        self.assertEqual("ready", store.load()["pending_refunds"][0]["status"])

    def test_pending_payment_can_be_listed_retried_and_resolved(self):
        store = StateStore(self.db_path)
        store.save({
            "processed_payments": [],
            "receipt_map": {},
            "payment_balances": {},
            "payment_event_times": {},
            "pending_payments": [{
                "payment_id": "payment-1",
                "amount": "25.50",
                "currency": "RUB",
                "created_at": "2026-01-01T00:00:00Z",
                "description": "payment-1",
                "status": "unknown",
            }],
        })

        with redirect_stdout(StringIO()):
            self.assertEqual(0, list_pending_payments(store))
            self.assertEqual(0, retry_payment(store, "payment-1"))
            self.assertEqual(
                0,
                resolve_payment(store, "payment-1", "receipt-1"),
            )

        state = store.load()
        self.assertEqual([], state["pending_payments"])
        self.assertEqual(["payment-1"], state["processed_payments"])
        self.assertEqual("receipt-1", state["receipt_map"]["payment-1"])
        self.assertEqual("25.50", state["payment_balances"]["payment-1"])

    def test_sync_start_can_be_changed_for_existing_database(self):
        store = StateStore(self.db_path)
        store.save({
            "last_sync_time": "2026-01-01T00:00:00Z",
            "last_refund_sync_time": "2026-01-01T00:00:00Z",
        })

        with redirect_stdout(StringIO()):
            result = set_sync_start(store, "2026-08-06T15:42:30+03:00")

        self.assertEqual(0, result)
        state = store.load()
        self.assertEqual("2026-08-06T12:42:30Z", state["last_sync_time"])
        self.assertEqual(
            "2026-08-06T12:42:30Z",
            state["last_refund_sync_time"],
        )


if __name__ == "__main__":
    unittest.main()
