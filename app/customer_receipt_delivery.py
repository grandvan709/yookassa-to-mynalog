import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

import httpx


TELEGRAM_USER_ID_PATTERN = re.compile(r"\(ID\s+(\d{1,13})\)")
MAX_TELEGRAM_USER_ID = 1_099_511_627_775


def extract_telegram_user_id(description):
    """Извлечь Telegram user ID только из контролируемого формата платежа."""
    match = TELEGRAM_USER_ID_PATTERN.search(str(description or ""))
    if not match:
        return None
    user_id = int(match.group(1))
    return user_id if 0 < user_id <= MAX_TELEGRAM_USER_ID else None


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    error: str | None = None
    link_sent: bool = False


class CustomerReceiptDelivery:
    """Скачивает публичную печатную форму ФНС и отправляет её BEDOLAGA-ботом."""

    def __init__(
        self,
        bot_token,
        inn,
        *,
        telegram_proxy=None,
        nalog_proxy=None,
        max_bytes=10 * 1024 * 1024,
        transport=None,
    ):
        self.bot_token = bot_token
        self.inn = str(inn)
        self.telegram_proxy = telegram_proxy
        self.nalog_proxy = nalog_proxy
        self.max_bytes = max_bytes
        self.transport = transport

    def receipt_url(self, receipt_uuid):
        safe_inn = quote(self.inn, safe="")
        safe_uuid = quote(str(receipt_uuid).strip(), safe="")
        return f"https://lknpd.nalog.ru/api/v1/receipt/{safe_inn}/{safe_uuid}/print"

    async def deliver(self, job):
        receipt_url = self.receipt_url(job["receipt_uuid"])
        caption = self._caption(job)
        keyboard = self._keyboard(receipt_url)

        try:
            content, content_type, filename, as_photo = await self._download_receipt(
                receipt_url,
                job["receipt_uuid"],
            )
        except Exception as exc:
            error = self._safe_error(exc)
            logging.warning(
                "Не удалось скачать файл чека %s: %s",
                job.get("receipt_uuid"),
                error,
            )
            if job.get("link_sent"):
                return DeliveryResult("retry", error=error, link_sent=True)
            link_result = await self._send_link(
                job["telegram_user_id"], caption, keyboard
            )
            if link_result.status == "delivered":
                return DeliveryResult("retry", error=error, link_sent=True)
            return link_result

        result = await self._send_file(
            job["telegram_user_id"],
            caption,
            keyboard,
            content,
            content_type,
            filename,
            as_photo,
        )
        if result.status == "bad_file":
            if job.get("link_sent"):
                return DeliveryResult("retry", error=result.error, link_sent=True)
            link_result = await self._send_link(
                job["telegram_user_id"], caption, keyboard
            )
            if link_result.status == "delivered":
                return DeliveryResult(
                    "retry", error=result.error, link_sent=True
                )
            return link_result
        if result.status == "delivered":
            logging.info(
                "Чек %s отправлен пользователю Telegram %s файлом.",
                job.get("receipt_uuid"),
                job.get("telegram_user_id"),
            )
        return result

    async def _download_receipt(self, url, receipt_uuid):
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            proxy=self.nalog_proxy,
            transport=self.transport,
            trust_env=self.transport is None,
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"ФНС вернула HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared and int(declared) > self.max_bytes:
                    raise ValueError("файл чека превышает допустимый размер")
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError("файл чека превышает допустимый размер")
                    chunks.append(chunk)

        content = b"".join(chunks)
        content_type, extension, as_photo = self._detect_file(content)
        filename = f"receipt_{receipt_uuid}.{extension}"
        return content, content_type, filename, as_photo

    @staticmethod
    def _detect_file(content):
        if content.startswith(b"%PDF-"):
            return "application/pdf", "pdf", False
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", "png", True
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "jpg", True
        raise ValueError("ФНС вернула не PDF/JPEG/PNG")

    async def _send_file(
        self, chat_id, caption, keyboard, content, content_type, filename, as_photo
    ):
        field = "photo" if as_photo else "document"
        method = "sendPhoto" if as_photo else "sendDocument"
        result = await self._telegram_request(
            method,
            data={
                "chat_id": str(chat_id),
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard, ensure_ascii=False),
            },
            files={field: (filename, content, content_type)},
        )
        if result.status == "bad_file" and as_photo:
            return await self._telegram_request(
                "sendDocument",
                data={
                    "chat_id": str(chat_id),
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(keyboard, ensure_ascii=False),
                },
                files={"document": (filename, content, content_type)},
            )
        return result

    async def _send_link(self, chat_id, caption, keyboard):
        return await self._telegram_request(
            "sendMessage",
            json_body={
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
                "disable_web_page_preview": True,
            },
        )

    async def _telegram_request(self, method, *, data=None, files=None, json_body=None):
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                proxy=self.telegram_proxy,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    url,
                    data=data,
                    files=files,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            return DeliveryResult("retry", error=self._safe_error(exc))

        if response.status_code == 200:
            return DeliveryResult("delivered")

        description = self._telegram_error(response)
        if response.status_code in (401, 403):
            return DeliveryResult("undeliverable", error=description)
        if response.status_code == 400 and files:
            return DeliveryResult("bad_file", error=description)
        if response.status_code == 400:
            return DeliveryResult("undeliverable", error=description)
        return DeliveryResult("retry", error=description)

    def _telegram_error(self, response):
        try:
            payload = response.json()
            description = payload.get("description")
        except Exception:
            description = response.text
        return self._safe_error(
            f"Telegram HTTP {response.status_code}: {description or 'нет описания'}"
        )

    def _safe_error(self, error):
        return str(error).replace(self.bot_token, "***")[:240]

    @staticmethod
    def _caption(job):
        amount = Decimal(str(job["amount"])).quantize(Decimal("0.01"))
        return (
            "🧾 <b>Чек по вашему платежу сформирован</b>\n\n"
            f"💰 Сумма: <b>{amount:.2f} руб.</b>\n\n"
            "Чек зарегистрирован в ФНС через сервис «Мой налог»."
        )

    @staticmethod
    def _keyboard(receipt_url):
        return {
            "inline_keyboard": [
                [{"text": "🧾 Открыть чек", "url": receipt_url}],
                [{"text": "🏠 На главную", "callback_data": "back_to_menu"}],
            ]
        }
