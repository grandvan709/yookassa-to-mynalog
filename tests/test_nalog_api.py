import asyncio
import logging
import sys
import unittest
import httpx
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from tenacity import wait_none


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from nalog_api import MoyNalogAPI


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content_type="application/json"):
        self.status_code = status_code
        self.payload = payload if payload is not None else {
            "approvedReceiptUuid": "receipt-1"
        }
        self.text = ""
        self.headers = {"content-type": content_type}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeClient:
    def __init__(self, outcome=None):
        self.payload = None
        self.outcome = outcome or FakeResponse()

    async def post(self, url, json, headers):
        self.payload = json
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def get(self, url, params, headers):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class PagedFakeClient(FakeClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.offsets = []

    async def get(self, url, params, headers):
        self.offsets.append(params["offset"])
        return self.responses.pop(0)


class AuthFakeClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0
        self.headers = {}

    async def post(self, url, json):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

class NalogMoneyTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_add_income_serializes_decimal_without_float_conversion(self):
        api = self.create_api()
        api.client = FakeClient()

        receipt = asyncio.run(api.add_income(
            "Услуга",
            Decimal("10.10"),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))

        self.assertEqual("receipt-1", receipt)
        self.assertEqual("10.10", api.client.payload["services"][0]["amount"])
        self.assertEqual("10.10", api.client.payload["totalAmount"])

    def create_api(self, outcome=None):
        api = MoyNalogAPI.__new__(MoyNalogAPI)
        api.token = "token"
        api.last_error = None
        api.last_error_kind = None
        api.last_error_retryable = False
        api.last_operation_uncertain = False
        api.headers = {}
        api.client = FakeClient(outcome)
        return api

    def create_auth_api(self, outcome):
        api = self.create_api()
        api.token = None
        api.login = "login"
        api.password = "password"
        api.device_id = "device"
        api.user_agent = "test"
        api.client = AuthFakeClient(outcome)
        return api

    def add_income(self, outcome):
        api = self.create_api(outcome)
        result = asyncio.run(api.add_income(
            "Услуга",
            Decimal("10.10"),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        return api, result

    def test_connect_timeout_is_safe_to_retry(self):
        request = httpx.Request("POST", "https://lknpd.nalog.ru/api/v1/income")
        api, result = self.add_income(httpx.ConnectTimeout("timeout", request=request))
        self.assertIsNone(result)
        self.assertTrue(api.last_error_retryable)
        self.assertFalse(api.last_operation_uncertain)
        self.assertEqual("connect_timeout", api.last_error_kind)

    def test_read_timeout_after_post_is_uncertain(self):
        request = httpx.Request("POST", "https://lknpd.nalog.ru/api/v1/income")
        api, result = self.add_income(httpx.ReadTimeout("timeout", request=request))
        self.assertIsNone(result)
        self.assertTrue(api.last_error_retryable)
        self.assertTrue(api.last_operation_uncertain)
        self.assertEqual("timeout", api.last_error_kind)

    def test_maintenance_503_after_post_is_uncertain(self):
        api, result = self.add_income(
            FakeResponse(503, ValueError("html"), "text/html")
        )
        self.assertIsNone(result)
        self.assertTrue(api.last_error_retryable)
        self.assertTrue(api.last_operation_uncertain)
        self.assertEqual("maintenance", api.last_error_kind)

    def test_rate_limit_is_safe_to_retry_later(self):
        api, result = self.add_income(FakeResponse(429, {"message": "limit"}))
        self.assertIsNone(result)
        self.assertTrue(api.last_error_retryable)
        self.assertFalse(api.last_operation_uncertain)
        self.assertEqual("rate_limited", api.last_error_kind)

    def test_html_with_200_is_treated_as_uncertain_bad_response(self):
        api, result = self.add_income(
            FakeResponse(200, ValueError("not json"), "text/html")
        )
        self.assertIsNone(result)
        self.assertTrue(api.last_operation_uncertain)
        self.assertEqual("bad_response", api.last_error_kind)

    def test_validation_400_is_permanent_and_not_uncertain(self):
        api, result = self.add_income(
            FakeResponse(400, {"message": "validation failed"})
        )
        self.assertIsNone(result)
        self.assertFalse(api.last_error_retryable)
        self.assertFalse(api.last_operation_uncertain)

    def test_cancel_connect_timeout_is_safe_to_retry(self):
        request = httpx.Request("POST", "https://lknpd.nalog.ru/api/v1/cancel")
        api = self.create_api(httpx.ConnectTimeout("timeout", request=request))
        result = asyncio.run(api.cancel_income("receipt"))
        self.assertFalse(result)
        self.assertTrue(api.last_error_retryable)
        self.assertFalse(api.last_operation_uncertain)

    def test_cancel_read_timeout_is_uncertain(self):
        request = httpx.Request("POST", "https://lknpd.nalog.ru/api/v1/cancel")
        api = self.create_api(httpx.ReadTimeout("timeout", request=request))
        result = asyncio.run(api.cancel_income("receipt"))
        self.assertFalse(result)
        self.assertTrue(api.last_error_retryable)
        self.assertTrue(api.last_operation_uncertain)

    def test_receipt_lookup_handles_connection_reset(self):
        request = httpx.Request("GET", "https://lknpd.nalog.ru/api/v1/incomes")
        api = self.create_api(httpx.RemoteProtocolError("reset", request=request))
        result = asyncio.run(api.find_income("Услуга", Decimal("10.10")))
        self.assertIsNone(result)
        self.assertTrue(api.last_error_retryable)
        self.assertFalse(api.last_operation_uncertain)
        self.assertEqual("connection_reset", api.last_error_kind)

    def test_receipt_lookup_checks_following_pages(self):
        first_page = [
            {
                "approvedReceiptUuid": f"other-{index}",
                "name": "Другая услуга",
                "totalAmount": "10.10",
            }
            for index in range(50)
        ]
        api = self.create_api()
        api.client = PagedFakeClient([
            FakeResponse(200, {"content": first_page}),
            FakeResponse(200, {"content": [{
                "approvedReceiptUuid": "wanted-receipt",
                "name": "Услуга [yookassa:payment-1]",
                "totalAmount": "10.10",
            }]}),
        ])

        result = asyncio.run(api.find_income(
            "Услуга [yookassa:payment-1]",
            Decimal("10.10"),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))

        self.assertEqual("wanted-receipt", result)
        self.assertEqual([0, 50], api.client.offsets)

    def test_auth_401_is_not_retried(self):
        api = self.create_auth_api(FakeResponse(401, {"message": "unauthorized"}))
        with self.assertRaises(Exception):
            asyncio.run(api._authenticate_password())
        self.assertEqual(1, api.client.calls)
        self.assertEqual("auth", api.last_error_kind)
        self.assertFalse(api.last_error_retryable)

    def test_auth_503_is_retried_three_times(self):
        api = self.create_auth_api(FakeResponse(503, None, "text/html"))
        with patch.object(
            MoyNalogAPI._authenticate_password.retry,
            "wait",
            wait_none(),
        ):
            with self.assertRaises(Exception):
                asyncio.run(api._authenticate_password())
        self.assertEqual(3, api.client.calls)
        self.assertEqual("maintenance", api.last_error_kind)
        self.assertTrue(api.last_error_retryable)


if __name__ == "__main__":
    unittest.main()
