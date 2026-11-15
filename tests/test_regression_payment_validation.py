import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_checkpoints as fixtures


class PaymentValidationTests(unittest.TestCase):
    def payment(self):
        item = fixtures.payment("payment-1", "2026-01-02T00:00:00Z")
        item.test, item.paid = False, True
        return item

    def test_unverified_payment_never_enters_income_queue(self):
        for field, value in (("test", None), ("paid", False),
                             ("paid", None), ("status", None), ("status", "pending")):
            with self.subTest(field=field, value=value):
                item = self.payment()
                setattr(item, field, value)
                manager = fixtures.manager_with([], [], fixtures.FakeNalog())
                with self.assertRaises(ValueError):
                    manager._prepare_payment_workflow(item)
                self.assertEqual([], manager.state["pending_payments"])
                self.assertEqual([], manager.nalog.add_calls)

    def test_missing_test_or_paid_is_rejected(self):
        for field in ("test", "paid"):
            with self.subTest(field=field):
                item = self.payment()
                delattr(item, field)
                manager = fixtures.manager_with([], [], fixtures.FakeNalog())
                with self.assertRaises(ValueError):
                    manager._prepare_payment_workflow(item)

    def test_verified_live_payment_is_processed(self):
        manager = fixtures.manager_with([], [], fixtures.FakeNalog())
        workflow = manager._prepare_payment_workflow(self.payment())
        result, _ = asyncio.run(manager._resume_payment_workflow(workflow))
        self.assertEqual("completed", result)
        self.assertEqual(1, len(manager.nalog.add_calls))

    def test_list_without_status_is_not_successful(self):
        manager = fixtures.manager_with([], [], fixtures.FakeNalog())
        item = self.payment()
        delattr(item, "status")
        response = SimpleNamespace(items=[item], next_cursor=None)
        with patch("main.Payment.list", return_value=response):
            payments, error, checkpoint = asyncio.run(
                fixtures.SyncManager.get_new_yookassa_payments(manager)
            )
        self.assertEqual([], payments)
        self.assertIsNotNone(error)
        self.assertIsNone(checkpoint)
