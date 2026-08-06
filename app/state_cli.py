import argparse
import os
from decimal import Decimal

import config
from state_store import StateStore


def create_store():
    data_dir = os.getenv("DATA_DIR", "data")
    log_dir = os.getenv("LOG_DIR", "logs")
    return StateStore(
        f"{data_dir}/sync_state.db",
        legacy_json_path=f"{log_dir}/sync_state.json",
    )


def list_pending_refunds(store):
    state = store.load() or {}
    pending = state.get("pending_refunds", [])
    if not pending:
        print("Частичных возвратов для ручной обработки нет.")
        return 0

    for item in pending:
        print(
            f"{item['refund_id']}  payment={item['payment_id']}  "
            f"refund={item['refund_amount']}  payment_total={item['payment_amount']}  "
            f"status={item.get('status', 'unknown')}"
        )
    return 0


def list_pending_payments(store):
    state = store.load() or {}
    pending = state.get("pending_payments", [])
    if not pending:
        print("Платежей для ручной сверки нет.")
        return 0
    for item in pending:
        if isinstance(item, str):
            print(f"{item}  status=legacy_unknown")
        else:
            print(
                f"{item['payment_id']}  currency={item.get('currency')}  "
                f"status={item.get('status', 'unknown')}"
            )
    return 0


def resolve_payment(store, payment_id, receipt_uuid):
    store.acquire_lock()
    try:
        state = store.load() or {}
        pending = state.get("pending_payments", [])
        workflow = next(
            (
                item for item in pending
                if (
                    item == payment_id
                    if isinstance(item, str)
                    else item.get("payment_id") == payment_id
                )
            ),
            None,
        )
        if workflow is None:
            print(f"Платёж {payment_id} не найден в pending_payments.")
            return 1
        if isinstance(workflow, str):
            print("Legacy pending-платёж не содержит суммы и даты для миграции.")
            return 2

        if payment_id not in state.setdefault("processed_payments", []):
            state["processed_payments"].append(payment_id)
        state.setdefault("receipt_map", {})[payment_id] = receipt_uuid
        state.setdefault("payment_balances", {})[payment_id] = workflow["amount"]
        state.setdefault("payment_event_times", {})[payment_id] = workflow[
            "created_at"
        ]
        state["pending_payments"] = [
            item for item in pending
            if (
                item != payment_id
                if isinstance(item, str)
                else item.get("payment_id") != payment_id
            )
        ]
        store.save(state)
        print(f"Платёж {payment_id} связан с подтверждённым чеком.")
        return 0
    finally:
        store.release_lock()


def retry_payment(store, payment_id):
    store.acquire_lock()
    try:
        state = store.load() or {}
        workflow = next(
            (
                item for item in state.get("pending_payments", [])
                if isinstance(item, dict) and item.get("payment_id") == payment_id
            ),
            None,
        )
        if not workflow:
            print(f"Платёж {payment_id} не найден или имеет legacy-формат.")
            return 1
        if workflow.get("currency") != "RUB":
            print("Нельзя повторить платёж в неподдерживаемой валюте.")
            return 2
        workflow["status"] = "ready"
        workflow.pop("error", None)
        store.save(state)
        print(f"Платёж {payment_id} поставлен на повторную обработку.")
        return 0
    finally:
        store.release_lock()


def resolve_refund(store, refund_id, replacement_receipt=None):
    store.acquire_lock()
    try:
        state = store.load() or {}
        pending = state.get("pending_refunds", [])
        adjustment = next(
            (item for item in pending if item.get("refund_id") == refund_id),
            None,
        )
        if not adjustment:
            print(f"Возврат {refund_id} не найден в pending_refunds.")
            return 1

        remaining_amount = Decimal(adjustment["remaining_amount"])
        payment_id = adjustment["payment_id"]
        if remaining_amount > 0 and not replacement_receipt:
            print(
                "Для ненулевого остатка требуется --replacement-receipt с UUID "
                "чека, созданного после ручной корректировки."
            )
            return 2
        if remaining_amount == 0 and replacement_receipt:
            print("Для нулевого остатка новый чек указывать не нужно.")
            return 2

        if refund_id not in state.setdefault("processed_refunds", []):
            state["processed_refunds"].append(refund_id)
        if replacement_receipt:
            state.setdefault("receipt_map", {})[payment_id] = replacement_receipt
            state.setdefault("payment_balances", {})[payment_id] = str(
                remaining_amount
            )
        else:
            state.setdefault("receipt_map", {}).pop(payment_id, None)
            state.setdefault("payment_balances", {}).pop(payment_id, None)
        state["pending_refunds"] = [
            item for item in pending if item.get("refund_id") != refund_id
        ]
        store.save(state)
        print(f"Возврат {refund_id} подтверждён после ручной сверки.")
        return 0
    finally:
        store.release_lock()


def retry_refund(store, refund_id):
    store.acquire_lock()
    try:
        state = store.load() or {}
        adjustment = next(
            (
                item for item in state.get("pending_refunds", [])
                if item.get("refund_id") == refund_id
            ),
            None,
        )
        if not adjustment:
            print(f"Возврат {refund_id} не найден в pending_refunds.")
            return 1
        if not adjustment.get("receipt_uuid"):
            print("Нельзя повторить: UUID исходного чека отсутствует.")
            return 2
        adjustment["status"] = "ready"
        adjustment.pop("error", None)
        store.save(state)
        print(f"Возврат {refund_id} поставлен на повторную обработку.")
        return 0
    finally:
        store.release_lock()


def set_sync_start(store, value):
    timestamp = config.parse_sync_start(value)
    if not timestamp:
        print("Необходимо указать дату или время начала синхронизации.")
        return 2

    store.acquire_lock()
    try:
        state = store.load() or {}
        state["last_sync_time"] = timestamp
        state["last_refund_sync_time"] = timestamp
        store.save(state)
        print(f"Начало синхронизации установлено: {timestamp}")
        return 0
    finally:
        store.release_lock()


def main():
    parser = argparse.ArgumentParser(description="Управление SQLite state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list-pending-payments")
    subparsers.add_parser("list-pending-refunds")
    resolve_payment_parser = subparsers.add_parser("resolve-payment")
    resolve_payment_parser.add_argument("payment_id")
    resolve_payment_parser.add_argument("--receipt", required=True)
    retry_payment_parser = subparsers.add_parser("retry-payment")
    retry_payment_parser.add_argument("payment_id")
    resolve_parser = subparsers.add_parser("resolve-refund")
    resolve_parser.add_argument("refund_id")
    resolve_parser.add_argument("--replacement-receipt")
    retry_parser = subparsers.add_parser("retry-refund")
    retry_parser.add_argument("refund_id")
    sync_start_parser = subparsers.add_parser("set-sync-start")
    sync_start_parser.add_argument("timestamp")
    args = parser.parse_args()

    store = create_store()
    if args.command == "list-pending-payments":
        return list_pending_payments(store)
    if args.command == "list-pending-refunds":
        return list_pending_refunds(store)
    if args.command == "retry-payment":
        return retry_payment(store, args.payment_id)
    if args.command == "resolve-payment":
        return resolve_payment(store, args.payment_id, args.receipt)
    if args.command == "retry-refund":
        return retry_refund(store, args.refund_id)
    if args.command == "set-sync-start":
        return set_sync_start(store, args.timestamp)
    return resolve_refund(store, args.refund_id, args.replacement_receipt)


if __name__ == "__main__":
    raise SystemExit(main())
