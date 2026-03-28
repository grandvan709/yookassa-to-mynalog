import html
import httpx
import logging
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Собирает события синхронизации и отправляет итоговое сообщение в Telegram-тред.
    Использует тот же httpx, что и основной код — никаких новых зависимостей.
    """

    def __init__(self, bot_token: str, chat_id: str, thread_id: int = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        self._payments: list[float] = []
        self._errors: list[tuple[str, str]] = []
        self._start_time: datetime | None = None
        self._found_count: int = 0

    # ──────────────────────────────────────────────
    # Методы, вызываемые из SyncManager
    # ──────────────────────────────────────────────

    def on_sync_start(self, found_count: int):
        self._start_time = datetime.now()
        self._found_count = found_count
        self._payments = []
        self._errors = []

    def on_payment_success(self, amount: float):
        self._payments.append(amount)

    def on_payment_error(self, payment_id: str, error: str):
        self._errors.append((payment_id, error))

    async def send_startup(self):
        """Отправляет уведомление об успешном запуске."""
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
        """Отправляет уведомление когда новых платежей нет."""
        date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        text = (
            "🔄 <b>Синхронизация завершена</b>\n"
            f"📅 {date_str}\n"
            "\n"
            "💤 Новых платежей не найдено"
        )
        await self._send(text)

    async def send_summary(self):
        """Отправляет итоговое сообщение с платежами."""
        if not self._payments and not self._errors:
            return

        text = self._build_message()
        await self._send(text)

    # ──────────────────────────────────────────────
    # Внутренняя логика
    # ──────────────────────────────────────────────

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

        if failed == 0:
            lines.append(
                f"✅ Успешно: <b>{successful}</b> из {self._found_count} платежей"
            )
        else:
            lines.append(
                f"✅ Успешно: <b>{successful}</b> | ❌ Ошибок: <b>{failed}</b>"
            )

        # Форматируем сумму с пробелами как разделителями тысяч
        total_str = f"{total:,.0f}".replace(",", "\u00a0")
        lines.append(f"💰 Итого: <b>{total_str} руб.</b>")

        # Разбивка по суммам
        breakdown: dict[float, int] = defaultdict(int)
        for amount in self._payments:
            breakdown[amount] += 1

        if breakdown:
            lines.append("")
            lines.append("📊 Разбивка:")
            for amount in sorted(breakdown.keys(), reverse=True):
                count = breakdown[amount]
                word = _plural(count, "платёж", "платежа", "платежей")
                lines.append(f"  • {amount:g} руб. — {count} {word}")

        # Ошибки (максимум 5, остальные сворачиваем)
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
            import config as _config
            proxy_url = _config.TELEGRAM_PROXY if _config.TELEGRAM_PROXY else None
            async with httpx.AsyncClient(timeout=15.0, proxy=proxy_url) as client:
                resp = await client.post(self.api_url, json=payload)
                if resp.status_code == 200:
                    logger.info("✓ Уведомление отправлено в Telegram.")
                else:
                    logger.warning(
                        f"Telegram API вернул {resp.status_code}: {resp.text}"
                    )
        except Exception as e:
            # Ошибки Telegram не должны ломать основной процесс
            logger.warning(f"Не удалось отправить уведомление в Telegram: {e}")


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение числительных."""
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many
