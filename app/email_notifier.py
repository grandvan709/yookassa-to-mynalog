import asyncio
import html as html_lib
import smtplib
import logging
from datetime import datetime
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

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


class EmailNotifier:
    def __init__(self, host: str, port: int, user: str, password: str, to_email: str,
                 from_email: str = None, from_name: str = None,
                 use_tls: bool = True, subject: str = "Синхронизация чеков в налоговой"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.to_email = to_email
        self.from_email = from_email or user
        self.from_name = from_name
        self.use_tls = use_tls
        self.subject = subject

        self._payments: list[float] = []
        self._errors: list[tuple[str, str]] = []
        self._start_time: datetime | None = None
        self._found_count: int = 0
        self._cancelled: int = 0
        self._cancel_errors: int = 0
        self._verified: int = 0
        self._pending_count: int = 0
        self._refund_skipped: int = 0

    def on_sync_start(self, found_count: int):
        self._start_time = datetime.now()
        self._found_count = found_count
        self._payments = []
        self._errors = []
        self._cancelled = 0
        self._cancel_errors = 0
        self._verified = 0
        self._refund_skipped = 0

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

    async def send_startup(self):
        date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width:600px; margin:20px auto; padding:24px; color:#333; background:#fff;">
  <h2 style="color:#2c3e50; border-bottom:2px solid #4CAF50; padding-bottom:8px; margin-top:0;">
    🚀 YooKassa → Мой Налог запущен
  </h2>
  <p style="color:#7f8c8d; margin:0 0 20px;">📅 {date_str}</p>
  <p style="margin:8px 0;">⚙️ Контейнер успешно стартовал</p>
  <p style="margin:8px 0;">⏰ Синхронизация по расписанию будет включена после первого запуска</p>
  <hr style="margin-top:24px; border:none; border-top:1px solid #eee;">
  <p style="color:#bbb; font-size:12px; text-align:center; margin:8px 0 0;">YooKassa → Мой Налог</p>
</body>
</html>"""
        await asyncio.to_thread(self._send_sync, body)

    async def send_summary(self):
        has_activity = (
            self._payments or self._errors or
            self._cancelled or self._cancel_errors or
            self._refund_skipped
        )
        if not has_activity:
            return

        body = self._build_html()
        await asyncio.to_thread(self._send_sync, body)

    def _send_sync(self, html_body: str):
        msg = MIMEMultipart('alternative')
        msg['Subject'] = self.subject
        msg['From'] = formataddr((self.from_name, self.from_email)) if self.from_name else self.from_email
        msg['To'] = self.to_email

        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        try:
            if self.use_tls:
                with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                    server.starttls()
                    server.login(self.user, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as server:
                    server.login(self.user, self.password)
                    server.send_message(msg)
            logger.info("✓ Email-уведомление отправлено.")
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "(нет деталей)"
            safe_msg = err_text.replace(self.password, "***")
            logger.warning(f"Не удалось отправить email [{err_type}]: {safe_msg}")

    def _build_html(self) -> str:
        successful = len(self._payments)
        failed = len(self._errors)
        total = sum(self._payments)

        date_str = self._start_time.strftime("%d.%m.%Y %H:%M") if self._start_time else "—"

        sections = []

        sections.append(f"""
        <h2 style="color:#2c3e50; border-bottom:2px solid #4CAF50; padding-bottom:8px; margin-top:0;">
          🔄 Синхронизация завершена
        </h2>
        <p style="color:#7f8c8d; margin:0 0 20px;">📅 {date_str}</p>
        """)

        if self._pending_count:
            sections.append(f"""
            <div style="background:#fff3cd; border-left:4px solid #ffc107; padding:12px 16px; margin-bottom:16px; border-radius:4px;">
              <strong>⏳ Pending-платежей: {self._pending_count}</strong><br>
              <span style="color:#856404; font-size:13px;">Требуют ручной проверки в ЛК налоговой</span>
            </div>
            """)

        if successful or failed:
            if failed == 0:
                status_line = f"<span style='color:#27ae60;'>✅ Успешно: <b>{successful}</b> из {self._found_count} {_plural(self._found_count, 'платежа', 'платежей', 'платежей')}</span>"
            else:
                status_line = f"<span style='color:#27ae60;'>✅ Успешно: <b>{successful}</b></span> | <span style='color:#e74c3c;'>❌ Ошибок: <b>{failed}</b></span>"

            total_str = f"{total:,.0f}".replace(",", "&nbsp;")

            payment_block = f"""
            <h3 style="color:#2c3e50; margin-bottom:8px;">Платежи</h3>
            <p style="margin:4px 0;">{status_line}</p>
            <p style="margin:4px 0;">💰 Итого: <b>{total_str} руб.</b></p>
            """

            if self._verified:
                payment_block += f'<p style="margin:4px 0; color:#7f8c8d; font-size:13px;">🔍 Из них верифицировано через API: {self._verified}</p>'

            breakdown: dict[float, int] = defaultdict(int)
            for amount in self._payments:
                breakdown[amount] += 1

            if breakdown:
                rows = []
                for amount in sorted(breakdown.keys(), reverse=True):
                    count = breakdown[amount]
                    word = _plural(count, "платёж", "платежа", "платежей")
                    rows.append(f"<tr><td style='padding:4px 12px 4px 0;'>{amount:g} руб.</td><td style='color:#7f8c8d;'>{count} {word}</td></tr>")
                payment_block += f"""
                <p style="margin:12px 0 4px;"><b>📊 Разбивка:</b></p>
                <table style="border-collapse:collapse; font-size:14px;">
                  {''.join(rows)}
                </table>
                """

            sections.append(payment_block)

        if self._cancelled or self._cancel_errors or self._refund_skipped:
            refund_block = '<h3 style="color:#2c3e50; margin-top:20px; margin-bottom:8px;">Возвраты</h3>'
            if self._cancelled:
                word = _plural(self._cancelled, "чек аннулирован", "чека аннулировано", "чеков аннулировано")
                refund_block += f'<p style="margin:4px 0;">↩️ <b>{self._cancelled}</b> {word}</p>'
            if self._cancel_errors:
                refund_block += f'<p style="margin:4px 0; color:#e74c3c;">⚠️ Ошибок аннулирования: <b>{self._cancel_errors}</b></p>'
            if self._refund_skipped:
                refund_block += f'<p style="margin:4px 0; color:#7f8c8d;">⏭ Пропущено возвратов (нет чека): <b>{self._refund_skipped}</b></p>'
            sections.append(refund_block)

        if self._errors:
            error_rows = []
            for pid, err in self._errors[:10]:
                safe_pid = html_lib.escape(pid)
                safe_err = html_lib.escape(err)
                error_rows.append(f"<li style='margin:4px 0;'><code style='background:#f8f9fa; padding:2px 6px; border-radius:3px; font-size:13px;'>{safe_pid}</code>: {safe_err}</li>")

            error_block = f"""
            <h3 style="color:#2c3e50; margin-top:20px; margin-bottom:8px;">⚠️ Ошибки</h3>
            <ul style="margin:0; padding-left:20px;">
              {''.join(error_rows)}
            </ul>
            """
            if len(self._errors) > 10:
                error_block += f'<p style="color:#7f8c8d; font-size:13px;">...и ещё {len(self._errors) - 10}</p>'
            sections.append(error_block)

        body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width:600px; margin:20px auto; padding:24px; color:#333; background:#fff;">
{''.join(sections)}
<hr style="margin-top:24px; border:none; border-top:1px solid #eee;">
<p style="color:#bbb; font-size:12px; text-align:center; margin:8px 0 0;">YooKassa → Мой Налог</p>
</body>
</html>"""

        return body
