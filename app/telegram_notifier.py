import html
import httpx
import logging
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, thread_id: int = None, proxy: str = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.proxy = proxy
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        self._payments: list[float] = []
        self._errors: list[tuple[str, str]] = []
        self._start_time: datetime | None = None
        self._found_count: int = 0
        self._cancelled: int = 0
        self._cancel_errors: int = 0
        self._verified: int = 0
        self._pending_count: int = 0
        self._refund_skipped: int = 0
        self._yookassa_errors: list[str] = []

    def on_sync_start(self, found_count: int):
        self._start_time = datetime.now()
        self._found_count = found_count
        self._payments = []
        self._errors = []
        self._cancelled = 0
        self._cancel_errors = 0
        self._verified = 0
        self._refund_skipped = 0
        self._yookassa_errors = []

    def on_pending_found(self, count: int):
        self._pending_count = count

    def on_payment_success(self, amount: float):
        self._payments.append(amount)

    def on_payment_error(self, payment_id: str, error: str):
        self._errors.append((payment_id, error))

    def on_refund_cancelled(self):
        self._cancelled += 1

    def on_refund_error(self):
        self._cancel_errors += 1

    def on_payment_verified(self):
        self._verified += 1

    def on_refund_skipped(self):
        self._refund_skipped += 1

    def on_yookassa_error(self, message: str):
        self._yookassa_errors.append(message)

    async def send_startup(self):
        date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        text = (
            "🚀 <b>YooKassa → Мой Налог запущен</b>\n"
            f"📅 {date_str}\n"
            "\n"
            "⚙️ Контейнер успешно стартовал\n"
            "⏰ Синхронизация по расписанию будет включена после первого запуска"
        )
        await self._send(text)

    async def send_no_payments(self):
        date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        text = (
            "🔄 <b>Синхронизация завершена</b>\n"
            f"📅 {date_str}\n"
            "\n"
            "💤 Новых платежей не найдено"
        )
        await self._send(text)

    async def send_summary(self):
        if (not self._payments and not self._errors and not self._cancelled
                and not self._cancel_errors and not self._pending_count
                and not self._refund_skipped and not self._yookassa_errors):
            return

        text = self._build_message()
        await self._send(text)

    def _build_message(self) -> str:
        successful = len(self._payments)
        failed = len(self._errors)
        total = sum(self._payments)

        date_str = (
            self._start_time.strftime("%d.%m.%Y %H:%M")
            if self._start_time
            else "—"
        )

        lines = [
            "🔄 <b>Синхронизация завершена</b>",
            f"📅 {date_str}",
            "",
        ]

        if self._yookassa_errors:
            lines.append("⚠️ <b>Ошибка получения данных из ЮКассы</b>")
            for err in self._yookassa_errors:
                safe_err = html.escape(err)
                lines.append(f"  • <code>{safe_err}</code>")
            lines.append("")

        if self._pending_count:
            lines.append(f"⏳ Pending-платежей: <b>{self._pending_count}</b> (требуют ручной проверки)")
            lines.append("")

        if successful or failed:
            if failed == 0:
                lines.append(
                    f"✅ Успешно: <b>{successful}</b> из {self._found_count} {_plural(self._found_count, 'платежа', 'платежей', 'платежей')}"
                )
            else:
                lines.append(
                    f"✅ Успешно: <b>{successful}</b> | ❌ Ошибок: <b>{failed}</b>"
                )

            total_str = f"{total:,.0f}".replace(",", "\u00a0")
            lines.append(f"💰 Итого: <b>{total_str} руб.</b>")

            breakdown: dict[float, int] = defaultdict(int)
            for amount in self._payments:
                breakdown[amount] += 1

            if self._verified:
                lines.append(f"🔍 Из них верифицировано через API: {self._verified}")

            if breakdown:
                lines.append("")
                lines.append("📊 Разбивка:")
                for amount in sorted(breakdown.keys(), reverse=True):
                    count = breakdown[amount]
                    word = _plural(count, "платёж", "платежа", "платежей")
                    lines.append(f"  • {amount:g} руб. — {count} {word}")

        if self._cancelled or self._cancel_errors or self._refund_skipped:
            lines.append("")
            if self._cancelled:
                word = _plural(self._cancelled, "чек аннулирован", "чека аннулировано", "чеков аннулировано")
                lines.append(f"↩️ Возвраты: <b>{self._cancelled}</b> {word}")
            if self._cancel_errors:
                lines.append(f"⚠️ Ошибок аннулирования: <b>{self._cancel_errors}</b>")
            if self._refund_skipped:
                lines.append(f"⏭ Пропущено возвратов (нет чека): <b>{self._refund_skipped}</b>")

        if self._errors:
            lines.append("")
            lines.append("⚠️ Ошибки:")
            for pid, err in self._errors[:5]:
                safe_pid = html.escape(pid)
                safe_err = html.escape(err)
                lines.append(f"  • <code>{safe_pid}</code>: {safe_err}")
            if len(self._errors) > 5:
                lines.append(f"  ...и ещё {len(self._errors) - 5}")

        return "\n".join(lines)

    async def _send(self, text: str):
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id

        try:
            async with httpx.AsyncClient(timeout=15.0, proxy=self.proxy) as client:
                resp = await client.post(self.api_url, json=payload)
                if resp.status_code == 200:
                    logger.info("✓ Уведомление отправлено в Telegram.")
                else:
                    safe_text = resp.text.replace(self.bot_token, "***")
                    logger.warning(f"Telegram API вернул {resp.status_code}: {safe_text}")
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "(нет деталей)"
            safe_msg = err_text.replace(self.bot_token, "***")
            logger.warning(f"Не удалось отправить уведомление в Telegram [{err_type}]: {safe_msg}")
