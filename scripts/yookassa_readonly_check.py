import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values


API_BASE = "https://api.yookassa.ru/v3"


def check_collection(client, name):
    response = client.get(f"{API_BASE}/{name}", params={"limit": 5})
    if response.status_code != 200:
        print(f"{name}: HTTP {response.status_code}")
        return False

    data = response.json()
    items = data.get("items", [])
    print(
        f"{name}: OK, objects_in_page={len(items)}, "
        f"next_page={bool(data.get('next_cursor'))}"
    )
    if items:
        print(
            f"{name}: schema_ok={all(key in items[0] for key in ('id', 'status', 'created_at'))}, "
            f"test_object={items[0].get('test', 'not_reported')}"
        )
        object_response = client.get(f"{API_BASE}/{name}/{items[0]['id']}")
        print(
            f"{name}: object_get_ok={object_response.status_code == 200}, "
            f"id_matches={object_response.status_code == 200 and object_response.json().get('id') == items[0]['id']}"
        )
    next_cursor = data.get("next_cursor")
    if next_cursor:
        next_response = client.get(
            f"{API_BASE}/{name}", params={"limit": 1, "cursor": next_cursor}
        )
        print(f"{name}: pagination_get_ok={next_response.status_code == 200}")
        if next_response.status_code != 200:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Read-only проверка подключения к API ЮKassa"
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="явно разрешить GET-запросы с ключом реального магазина",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    values = dotenv_values(repo_root / ".env")
    shop_id = values.get("YOOKASSA_SHOP_ID")
    api_key = values.get("YOOKASSA_API_KEY")
    if not shop_id or not api_key:
        print("Не заданы YOOKASSA_SHOP_ID или YOOKASSA_API_KEY.")
        return 2
    if api_key.startswith("live_") and not args.allow_live:
        print("Live-ключ заблокирован. Для read-only проверки нужен --allow-live.")
        return 2

    proxy = values.get("YOOKASSA_NALOG_PROXY") or os.getenv(
        "YOOKASSA_NALOG_PROXY"
    )
    try:
        with httpx.Client(
            auth=(shop_id, api_key),
            timeout=20,
            proxy=proxy,
            trust_env=not bool(proxy),
        ) as client:
            payments_ok = check_collection(client, "payments")
            refunds_ok = check_collection(client, "refunds")
    except Exception as e:
        print(f"connection: ERROR [{type(e).__name__}]")
        return 1

    return 0 if payments_ok and refunds_ok else 1


if __name__ == "__main__":
    sys.exit(main())
