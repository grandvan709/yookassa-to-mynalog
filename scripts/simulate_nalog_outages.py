import asyncio
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from nalog_api import MoyNalogAPI


class FakeResponse:
    def __init__(self, status_code, payload, content_type="application/json"):
        self.status_code = status_code
        self.payload = payload
        self.text = ""
        self.headers = {"content-type": content_type}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome

    async def post(self, url, json, headers):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def get(self, url, params, headers):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def create_api(outcome):
    api = MoyNalogAPI.__new__(MoyNalogAPI)
    api.token = "fake-token"
    api.last_error = None
    api.last_error_kind = None
    api.last_error_retryable = False
    api.last_operation_uncertain = False
    api.headers = {}
    api.client = FakeClient(outcome)
    return api


async def simulate(name, outcome, expected_kind, retryable, uncertain):
    api = create_api(outcome)
    result = await api.add_income(
        "Эмуляция недоступности",
        Decimal("10.00"),
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    actual = (
        api.last_error_kind,
        api.last_error_retryable,
        api.last_operation_uncertain,
    )
    expected = (expected_kind, retryable, uncertain)
    if result is not None or actual != expected:
        raise RuntimeError(f"{name}: ожидалось {expected}, получено {actual}")
    action = "manual reconciliation" if uncertain else (
        "retry next schedule" if retryable else "permanent rejection"
    )
    print(f"{name}: OK -> {action}")


async def main():
    request = httpx.Request("POST", "https://lknpd.nalog.ru/api/v1/income")
    scenarios = [
        (
            "connect timeout",
            httpx.ConnectTimeout("timeout", request=request),
            "connect_timeout", True, False,
        ),
        (
            "DNS / connection refused",
            httpx.ConnectError("connection failed", request=request),
            "connection", True, False,
        ),
        (
            "no response after POST",
            httpx.ReadTimeout("timeout", request=request),
            "timeout", True, True,
        ),
        (
            "maintenance 503",
            FakeResponse(503, ValueError("html"), "text/html"),
            "maintenance", True, True,
        ),
        (
            "rate limit 429",
            FakeResponse(429, {"message": "rate limit"}),
            "rate_limited", True, False,
        ),
        (
            "HTML maintenance page with HTTP 200",
            FakeResponse(200, ValueError("not json"), "text/html"),
            "bad_response", True, True,
        ),
        (
            "validation error 400",
            FakeResponse(400, {"message": "invalid payload"}),
            "http", False, False,
        ),
    ]
    for scenario in scenarios:
        await simulate(*scenario)
    print("real_fns_calls: 0")
    print("duplicate_safe_state_transitions: covered by unit tests")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
