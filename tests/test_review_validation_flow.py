import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_checkpoints as fixtures
from state_store import StateStoreError


class ValidationFlowReviewTests(unittest.TestCase):
    def manager(self, payments=()):
        manager = fixtures.manager_with(list(payments), [], fixtures.FakeNalog())
        manager.state["last_sync_time"] = "2026-01-03T00:00:00Z"
        manager.snapshots = []
        manager.save_state = lambda: manager.snapshots.append(deepcopy(manager.state))
        return manager

    def watch(self, manager, item):
        manager._track_unpaid_payment(item, "pending")

    def test_incomplete_watched_payment_survives_and_other_work_continues(self):
        for field in ("paid", "test", "status"):
            with self.subTest(field=field):
                broken = fixtures.payment("old-broken", "2026-01-01T00:00:00Z")
                delattr(broken, field)
                good = fixtures.payment("old-good", "2026-01-01T00:00:00Z")
                fresh = fixtures.payment("fresh", "2026-01-04T00:00:00Z")
                manager = self.manager([fresh])
                for item in (broken, good):
                    self.watch(manager, item)
                responses = {item.id: item for item in (broken, good)}
                manager.get_yookassa_payment = AsyncMock(side_effect=lambda pid: (responses[pid], None))
                manager.get_new_refunds = AsyncMock(return_value=([], None, "2026-01-04T00:00:00Z"))
                with patch("main.write_status"):
                    asyncio.run(manager.sync())
                self.assertEqual(["old-broken"], [x["payment_id"] for x in manager.state["watched_payments"]])
                self.assertEqual({"old-good", "fresh"}, set(manager.state["processed_payments"]))
                manager.get_new_refunds.assert_awaited_once()
                self.assertTrue(all(any(x["payment_id"] == "old-broken" for x in s["watched_payments"])
                                    for s in manager.snapshots[2:]))
                # Corrected provider data must recover the old ID even after the scan has advanced.
                broken.status, broken.paid, broken.test = "succeeded", True, False
                completed, failures = asyncio.run(manager._resume_watched_payments())
                self.assertEqual(0, failures)
                self.assertEqual(1, len(completed))
                self.assertIn("old-broken", manager.state["processed_payments"])

    def test_explicit_test_is_durably_excluded_and_checkpoint_advances(self):
        sandbox = fixtures.payment("sandbox", "2026-01-04T00:00:00Z")
        sandbox.test = True
        live = fixtures.payment("live", "2026-01-04T00:00:00Z")
        manager = self.manager()
        manager.get_new_yookassa_payments = fixtures.SyncManager.get_new_yookassa_payments.__get__(manager)
        response = SimpleNamespace(items=[sandbox, live], next_cursor=None)
        with patch("main.Payment.list", return_value=response), patch("main.write_status"):
            asyncio.run(manager.sync())
            self.assertGreater(manager.state["last_sync_time"], "2026-01-03T00:00:00Z")
            asyncio.run(manager.sync())
        self.assertEqual({"sandbox", "live"}, set(manager.state["processed_payments"]))
        self.assertEqual(["sandbox"], [x["payment_id"] for x in manager.state["skipped_payments"]])
        self.assertEqual([], manager.state["pending_payments"])
        self.assertEqual(1, len(manager.nalog.add_calls))

    def test_watched_test_is_excluded_without_calling_fns(self):
        sandbox = fixtures.payment("sandbox", "2026-01-01T00:00:00Z")
        sandbox.test = True
        manager = self.manager()
        self.watch(manager, sandbox)
        manager.get_yookassa_payment = AsyncMock(return_value=(sandbox, None))
        completed, failures = asyncio.run(manager._resume_watched_payments())
        self.assertEqual(([], 0), (completed, failures))
        self.assertEqual([], manager.state["watched_payments"])
        self.assertEqual([], manager.nalog.add_calls)
        self.assertEqual(["sandbox"], manager.state["processed_payments"])

    def test_missing_verification_fields_in_new_scan_keep_checkpoint(self):
        broken = fixtures.payment("broken", "2026-01-04T00:00:00Z")
        del broken.test
        manager = self.manager([broken])
        before = manager.state["last_sync_time"]
        with patch("main.write_status"):
            asyncio.run(manager.sync())
        self.assertEqual(before, manager.state["last_sync_time"])
        self.assertEqual([], manager.nalog.add_calls)
        self.assertNotIn("broken", manager.state["processed_payments"])

    def test_list_scan_does_not_drop_an_incomplete_watched_payment(self):
        broken = fixtures.payment("old-broken", "2026-01-01T00:00:00Z")
        del broken.paid
        manager = self.manager()
        self.watch(manager, broken)
        manager.get_yookassa_payment = AsyncMock(return_value=(broken, None))
        manager.get_new_yookassa_payments = fixtures.SyncManager.get_new_yookassa_payments.__get__(manager)
        response = SimpleNamespace(items=[broken], next_cursor=None)
        with patch("main.Payment.list", return_value=response), patch("main.write_status"):
            asyncio.run(manager.sync())
        self.assertEqual(["old-broken"], [x["payment_id"] for x in manager.state["watched_payments"]])
        self.assertEqual([], manager.state["pending_payments"])
        self.assertEqual([], manager.nalog.add_calls)

    def test_watched_processing_error_keeps_saved_phase_and_continues(self):
        first = fixtures.payment("first", "2026-01-01T00:00:00Z")
        second = fixtures.payment("second", "2026-01-01T00:00:00Z")
        manager = self.manager()
        for item in (first, second):
            self.watch(manager, item)
        items = {p.id: p for p in (first, second)}
        manager.get_yookassa_payment = AsyncMock(side_effect=lambda pid: (items[pid], None))
        add = manager.nalog.add_income

        async def interrupted_write(description, amount, payment_date):
            if "first" in description:
                raise RuntimeError("stopped after an uncertain write")
            return await add(description, amount, payment_date)

        manager.nalog.add_income = AsyncMock(side_effect=interrupted_write)
        completed, failures = asyncio.run(manager._resume_watched_payments())
        self.assertEqual((1, 1), (len(completed), failures))
        self.assertEqual("creating", manager.state["pending_payments"][0]["status"])
        self.assertEqual(["second"], manager.state["processed_payments"])
        asyncio.run(manager._resume_pending_payments())
        self.assertEqual(2, manager.nalog.add_income.await_count)
        self.assertEqual(1, len(manager.nalog.find_calls))

    def test_watch_removal_is_saved_only_after_queue_handoff(self):
        item = fixtures.payment("p1", "2026-01-01T00:00:00Z")
        manager = self.manager()
        self.watch(manager, item)
        manager.get_yookassa_payment = AsyncMock(return_value=(item, None))
        asyncio.run(manager._resume_watched_payments())
        for snapshot in manager.snapshots:
            observed = any(x["payment_id"] == "p1" for x in snapshot["watched_payments"])
            queued = any(x["payment_id"] == "p1" for x in snapshot["pending_payments"])
            self.assertTrue(observed or queued or "p1" in snapshot["processed_payments"])

    def test_interrupted_handoff_does_not_enqueue_twice(self):
        item = fixtures.payment("p1", "2026-01-01T00:00:00Z")
        manager = self.manager()
        self.watch(manager, item)
        manager._prepare_payment_workflow(item)
        manager.get_yookassa_payment = AsyncMock(return_value=(item, None))
        asyncio.run(manager._resume_watched_payments())
        self.assertTrue(all(len(s["pending_payments"]) <= 1 for s in manager.snapshots))
        self.assertEqual(1, len(manager.nalog.add_calls))

    def test_storage_failure_is_not_swallowed_as_invalid_provider_data(self):
        item = fixtures.payment("p1", "2026-01-01T00:00:00Z")
        manager = self.manager()
        self.watch(manager, item)
        manager.get_yookassa_payment = AsyncMock(return_value=(item, None))
        manager.save_state = lambda: (_ for _ in ()).throw(StateStoreError("disk unavailable"))
        with self.assertRaises(StateStoreError):
            asyncio.run(manager._resume_watched_payments())
        self.assertEqual([], manager.nalog.add_calls)
