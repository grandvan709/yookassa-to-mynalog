import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import main
from state_store import ConcurrentRunError, StateStore


class StateLockRegressionTests(unittest.TestCase):
    def test_busy_constructor_cannot_modify_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "sync_state.db")
            initial = {"processed_payments": ["already-registered"]}
            store.save(initial)
            store.acquire_lock()
            with patch.object(main, "DATA_DIR", temp), patch.object(main.config, "validate_config"), \
                 patch.object(main, "MoyNalogAPI"):
                with self.assertRaises(ConcurrentRunError):
                    main.SyncManager()
            self.assertEqual(initial, store.load())
            store.release_lock()

    def test_non_owner_cannot_save_over_active_run(self):
        with tempfile.TemporaryDirectory() as temp:
            first = StateStore(Path(temp) / "sync_state.db")
            second = StateStore(first.db_path)
            first.save({"receipt_map": {"p1": "receipt-1"}})
            first.acquire_lock()
            with self.assertRaises(ConcurrentRunError):
                second.save({"receipt_map": {}})
            self.assertEqual({"receipt_map": {"p1": "receipt-1"}}, first.load())
            first.release_lock()

    def test_both_jobs_reload_state_after_acquiring_lock(self):
        for retry_only in (False, True):
            with self.subTest(retry_only=retry_only), tempfile.TemporaryDirectory() as temp:
                manager = main.SyncManager.__new__(main.SyncManager)
                manager.state_store = StateStore(Path(temp) / "sync_state.db")
                manager.state = {"processed_payments": [], "refresh_token": "stale"}
                manager.state_store.save({"processed_payments": ["p1"], "refresh_token": "rotated"})
                manager.nalog = Mock()
                manager.nalog.close = AsyncMock()
                manager.notifier = manager.email_notifier = manager.receipt_notifier = None
                manager.event_notifiers = []

                async def check_state(*args, **kwargs):
                    self.assertEqual(["p1"], manager.state["processed_payments"])
                    self.assertEqual("rotated", manager.nalog.refresh_token)
                    self.assertIsNone(manager.nalog.token)
                    return [], 0

                manager._sync_locked = check_state
                manager._resume_pending_payments = check_state
                manager._process_customer_receipt_deliveries = AsyncMock()
                with patch.object(main, "write_status"):
                    asyncio.run(manager.retry_fns_queue() if retry_only else manager.sync())
                # The run released its lock, including the refresh/read path.
                contender = StateStore(manager.state_store.db_path)
                contender.acquire_lock()
                contender.release_lock()
