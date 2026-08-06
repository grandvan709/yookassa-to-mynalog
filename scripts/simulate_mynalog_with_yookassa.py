import argparse
import asyncio
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values
from yookassa import Configuration, Payment


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import config
import main as app_main
from backup import create_backup, restore_backup
from state_store import StateStore

SyncManager = app_main.SyncManager


class FakeMoyNalogAPI:
    def __init__(self):
        self.receipts = {}
        self.counter = 0
        self.last_error = None

    async def add_income(self, name, amount, date):
        self.counter += 1
        receipt_uuid = f"fake-receipt-{self.counter}"
        self.receipts[receipt_uuid] = {
            "name": name,
            "amount": Decimal(str(amount)),
            "date": date,
            "cancelled": False,
        }
        return receipt_uuid

    async def cancel_income(self, receipt_uuid):
        receipt = self.receipts.get(receipt_uuid)
        if not receipt or receipt["cancelled"]:
            return False
        receipt["cancelled"] = True
        return True

    async def find_income(self, name, amount, operation_date=None):
        expected = Decimal(str(amount))
        for receipt_uuid, receipt in self.receipts.items():
            if (
                not receipt["cancelled"]
                and receipt["name"] == name
                and receipt["amount"] == expected
            ):
                return receipt_uuid
        return None

    async def close(self):
        pass


def create_manager(nalog, payments, refunds, payment_lookup, store):
    manager = SyncManager.__new__(SyncManager)
    manager.state = {
        "last_sync_time": "2020-01-01T00:00:00Z",
        "last_refund_sync_time": "2020-01-01T00:00:00Z",
        "processed_payments": [],
        "pending_payments": [],
        "processed_refunds": [],
        "pending_refunds": [],
        "payment_balances": {},
        "payment_event_times": {},
        "refund_event_times": {},
        "receipt_map": {},
    }
    manager.nalog = nalog
    manager.notifier = None
    manager.email_notifier = None
    manager.event_notifiers = []
    manager.check_for_updates = lambda: None
    manager.state_store = store
    manager.save_state()

    async def get_payments():
        result = list(payments)
        payments.clear()
        return result, None

    async def get_refunds():
        result = list(refunds)
        refunds.clear()
        return result, None

    async def get_payment(payment_id):
        return payment_lookup[payment_id], None

    manager.get_new_yookassa_payments = get_payments
    manager.get_new_refunds = get_refunds
    manager.get_yookassa_payment = get_payment
    return manager


async def run_simulation(payment):
    original_template = config.INCOME_DESCRIPTION_TEMPLATE
    config.INCOME_DESCRIPTION_TEMPLATE = "Платёж {id}"
    original_data_dir = app_main.DATA_DIR
    try:
        amount = Decimal(str(payment.amount.value))
        refund_amount = (amount / 2).quantize(Decimal("0.01"))
        if refund_amount <= 0 or refund_amount >= amount:
            raise RuntimeError("нет платежа с суммой, подходящей для частичного возврата")

        with tempfile.TemporaryDirectory(prefix="yn-full-test-") as temp_name:
            root = Path(temp_name)
            data_dir = root / "data"
            log_dir = root / "logs"
            restore_dir = root / "restore"
            data_dir.mkdir()
            log_dir.mkdir()
            app_main.DATA_DIR = str(data_dir)

            store = StateStore(data_dir / "sync_state.db")
            nalog = FakeMoyNalogAPI()
            payments = [payment]
            refunds = []
            manager = create_manager(
                nalog,
                payments,
                refunds,
                {payment.id: payment},
                store,
            )

            await manager.sync()
            first_receipt = manager.state["receipt_map"].get(payment.id)
            if not first_receipt:
                raise RuntimeError("симулятор не создал исходный чек")

            refunds.append(SimpleNamespace(
                id="synthetic-partial-refund",
                payment_id=payment.id,
                created_at=payment.created_at,
                amount=SimpleNamespace(
                    value=str(refund_amount),
                    currency=payment.amount.currency,
                ),
            ))
            await manager.sync()

            expected_balance = amount - refund_amount
            actual_balance = Decimal(manager.state["payment_balances"][payment.id])
            replacement = manager.state["receipt_map"].get(payment.id)
            active_receipts = [
                receipt for receipt in nalog.receipts.values()
                if not receipt["cancelled"]
            ]
            if actual_balance != expected_balance:
                raise RuntimeError("остаток платежа рассчитан неверно")
            if not replacement or replacement == first_receipt:
                raise RuntimeError("чек на остаток не был создан")
            if len(active_receipts) != 1:
                raise RuntimeError("ожидался ровно один активный чек")

            persisted = store.load()
            if persisted["receipt_map"].get(payment.id) != replacement:
                raise RuntimeError("SQLite не сохранила UUID заменяющего чека")

            backup_path = create_backup(
                data_dir,
                log_dir,
                "temporary-full-test-password",
                include_logs=False,
            )
            restore_backup(
                backup_path,
                restore_dir,
                "temporary-full-test-password",
            )
            restored_store = StateStore(restore_dir / "data" / "sync_state.db")
            if restored_store.load()["receipt_map"].get(payment.id) != replacement:
                raise RuntimeError("восстановленный backup не совпадает со state")

            print("simulation: OK")
            print("yookassa_calls: GET list + GET existing payment only")
            print("mynalog_calls: in-memory fake only")
            print("sqlite_persistence: OK")
            print("partial_refund_workflow: original_cancelled_and_replaced")
            print("encrypted_backup_restore: OK")
            print("sensitive_payment_data_printed: false")
    finally:
        app_main.DATA_DIR = original_data_dir
        config.INCOME_DESCRIPTION_TEMPLATE = original_template


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-live-readonly", action="store_true")
    args = parser.parse_args()
    values = dotenv_values(REPO_ROOT / ".env")
    shop_id = values.get("YOOKASSA_SHOP_ID")
    api_key = values.get("YOOKASSA_API_KEY")
    if not shop_id or not api_key:
        print("Не заданы YOOKASSA_SHOP_ID или YOOKASSA_API_KEY.")
        return 2
    if api_key.startswith("live_") and not args.allow_live_readonly:
        print("Live-ключ заблокирован без --allow-live-readonly.")
        return 2

    Configuration.configure(shop_id, api_key)
    try:
        response = Payment.list({"status": "succeeded", "limit": 20})
    except Exception as e:
        print(f"yookassa: ERROR [{type(e).__name__}]")
        return 1

    payment = next(
        (
            item for item in response.items
            if Decimal(str(item.amount.value)) >= Decimal("0.02")
        ),
        None,
    )
    if not payment:
        print("Нет подходящего успешного платежа для симуляции.")
        return 1

    try:
        payment = Payment.find_one(payment.id)
    except Exception as e:
        print(f"yookassa payment GET: ERROR [{type(e).__name__}]")
        return 1

    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run_simulation(payment))
    finally:
        logging.disable(logging.NOTSET)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
