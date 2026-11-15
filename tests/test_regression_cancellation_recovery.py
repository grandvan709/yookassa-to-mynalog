import asyncio
import unittest

import test_checkpoints as fixtures


class CancellationRecoveryTests(unittest.TestCase):
    def adjustment(self, remaining):
        return {"refund_id": "r1", "payment_id": "p1", "receipt_uuid": "old",
                "remaining_amount": remaining, "refund_amount": "40.00",
                "payment_created_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-02T00:00:00Z",
                "replacement_description": "replacement", "status": "cancelling"}

    def test_interrupted_cancel_is_read_before_any_write(self):
        for status in ("active", "not_found", "error"):
            with self.subTest(status=status):
                nalog = fixtures.FakeNalog(income_status=status)
                manager = fixtures.manager_with([], [], nalog)
                item = self.adjustment("60.00")
                manager.state["pending_refunds"] = [item]
                self.assertEqual("manual", asyncio.run(manager._resume_refund_adjustment(item)))
                self.assertEqual("ready" if status == "active" else "cancellation_unknown", item["status"])
                self.assertIn("last_verification_at", item)
                self.assertEqual([], nalog.cancel_calls)
                self.assertEqual([], nalog.add_calls)

    def test_confirmed_cancel_resumes_full_and_partial_refund(self):
        for remaining, expected in (("0.00", "cancelled"), ("60.00", "adjusted")):
            with self.subTest(remaining=remaining):
                nalog = fixtures.FakeNalog(income_status="cancelled")
                manager = fixtures.manager_with([], [], nalog)
                item = self.adjustment(remaining)
                manager.state["pending_refunds"] = [item]
                result = asyncio.run(manager._resume_refund_adjustment(item))
                self.assertEqual(expected, result)
                self.assertEqual([], nalog.cancel_calls)
                self.assertEqual(0 if remaining == "0.00" else 1, len(nalog.add_calls))
                self.assertEqual([], manager.state["pending_refunds"])
