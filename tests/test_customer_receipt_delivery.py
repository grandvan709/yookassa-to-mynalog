import asyncio
import sys
import unittest
from pathlib import Path

import httpx


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from customer_receipt_delivery import (
    CustomerReceiptDelivery,
    extract_telegram_user_id,
)


class CustomerReceiptDeliveryTests(unittest.TestCase):
    def test_telegram_id_is_extracted_from_controlled_description(self):
        description = "Интернет-сервис - Пополнение на 349 ₽ (ID 1929069704)"

        self.assertEqual(1929069704, extract_telegram_user_id(description))
        self.assertIsNone(extract_telegram_user_id("ID 1929069704"))

    def test_custom_pattern_extracts_telegram_id(self):
        self.assertEqual(
            777, extract_telegram_user_id("оплата tg:777", r"tg:(\d{1,13})")
        )
        self.assertIsNone(
            extract_telegram_user_id("оплата (ID 777)", r"tg:(\d{1,13})")
        )

    def _jpeg_delivery(self, requests, **kwargs):
        def handler(request):
            requests.append(request)
            if request.url.host == "lknpd.nalog.ru":
                return httpx.Response(200, content=b"\xff\xd8\xffjpeg")
            return httpx.Response(200, json={"ok": True})

        return CustomerReceiptDelivery(
            "secret-token",
            "123456789012",
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    def test_jpeg_receipt_is_sent_without_menu_button_by_default(self):
        requests = []
        delivery = self._jpeg_delivery(requests)

        result = asyncio.run(delivery.deliver({
            "receipt_uuid": "receipt-1",
            "telegram_user_id": 1929069704,
            "amount": "349.00",
        }))

        self.assertEqual("delivered", result.status)
        self.assertEqual(2, len(requests))
        telegram_request = requests[1]
        self.assertTrue(telegram_request.url.path.endswith("/sendPhoto"))
        self.assertNotIn(b"callback_data", telegram_request.content)
        self.assertIn(b"1929069704", telegram_request.content)

    def test_menu_callback_adds_second_button(self):
        requests = []
        delivery = self._jpeg_delivery(requests, menu_callback="back_to_menu")

        result = asyncio.run(delivery.deliver({
            "receipt_uuid": "receipt-1",
            "telegram_user_id": 1929069704,
            "amount": "349.00",
        }))

        self.assertEqual("delivered", result.status)
        self.assertIn(b"back_to_menu", requests[1].content)

    def test_download_failure_sends_link_once_and_keeps_file_retry(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "lknpd.nalog.ru":
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        delivery = CustomerReceiptDelivery(
            "secret-token",
            "123456789012",
            transport=httpx.MockTransport(handler),
        )
        job = {
            "receipt_uuid": "receipt-1",
            "telegram_user_id": 1929069704,
            "amount": "349.00",
            "link_sent": False,
        }

        first = asyncio.run(delivery.deliver(job))
        job["link_sent"] = first.link_sent
        second = asyncio.run(delivery.deliver(job))

        self.assertEqual("retry", first.status)
        self.assertTrue(first.link_sent)
        self.assertEqual("retry", second.status)
        telegram_requests = [
            request for request in requests if request.url.host == "api.telegram.org"
        ]
        self.assertEqual(1, len(telegram_requests))
        self.assertTrue(telegram_requests[0].url.path.endswith("/sendMessage"))
        self.assertIn(b"receipt-1/print", telegram_requests[0].content)

    def test_blocked_bot_marks_delivery_as_undeliverable(self):
        def handler(request):
            if request.url.host == "lknpd.nalog.ru":
                return httpx.Response(200, content=b"%PDF-1.4 receipt")
            return httpx.Response(
                403,
                json={"ok": False, "description": "bot was blocked by the user"},
            )

        delivery = CustomerReceiptDelivery(
            "secret-token",
            "123456789012",
            transport=httpx.MockTransport(handler),
        )

        result = asyncio.run(delivery.deliver({
            "receipt_uuid": "receipt-1",
            "telegram_user_id": 1929069704,
            "amount": "349.00",
        }))

        self.assertEqual("undeliverable", result.status)
        self.assertIn("blocked", result.error)


if __name__ == "__main__":
    unittest.main()
