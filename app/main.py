import asyncio
import json
import os
import logging
from datetime import datetime, timedelta
from yookassa import Configuration, Payment, Refund
import config
from nalog_api import MoyNalogAPI
from telegram_notifier import TelegramNotifier
from email_notifier import EmailNotifier
from utils import build_template_vars

LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f"{LOG_DIR}/sync.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logging.getLogger("httpx").setLevel(logging.WARNING)


class SyncManager:
    def __init__(self):
        try:
            config.validate_config()
        except ValueError as e:
            logging.error(f"Ошибка конфигурации: {e}")
            raise

        Configuration.configure(config.YOOKASSA_SHOP_ID, config.YOOKASSA_API_KEY)
        self.state_file = f"{LOG_DIR}/sync_state.json"
        self.state = self.load_state()
        refresh_token = self.state.get("refresh_token") or config.MOY_NALOG_REFRESH_TOKEN
        self.nalog = MoyNalogAPI(
            config.MOY_NALOG_LOGIN,
            config.MOY_NALOG_PASSWORD,
            auth_method=config.MOY_NALOG_AUTH_METHOD,
            refresh_token=refresh_token,
            on_refresh_token=self._save_refresh_token,
        )

        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            thread_id = None
            if config.TELEGRAM_THREAD_ID:
                try:
                    thread_id = int(config.TELEGRAM_THREAD_ID)
                except ValueError:
                    logging.warning(f"TELEGRAM_THREAD_ID имеет некорректное значение: '{config.TELEGRAM_THREAD_ID}'. Сообщения будут отправляться в основной чат.")
            self.notifier = TelegramNotifier(
                bot_token=config.TELEGRAM_BOT_TOKEN,
                chat_id=config.TELEGRAM_CHAT_ID,
                thread_id=thread_id,
                proxy=config.TELEGRAM_PROXY,
            )
            logging.info("✓ Telegram-уведомления включены.")
        else:
            self.notifier = None

        if config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD and config.SMTP_TO_EMAIL:
            self.email_notifier = EmailNotifier(
                host=config.SMTP_HOST,
                port=config.SMTP_PORT,
                user=config.SMTP_USER,
                password=config.SMTP_PASSWORD,
                to_email=config.SMTP_TO_EMAIL,
                from_email=config.SMTP_FROM_EMAIL,
                from_name=config.SMTP_FROM_NAME,
                use_tls=config.SMTP_USE_TLS,
                subject=config.EMAIL_SUBJECT,
            )
            logging.info("✓ Email-уведомления включены.")
        else:
            self.email_notifier = None

        self.event_notifiers = [n for n in (self.notifier, self.email_notifier) if n]

    def _emit(self, method, *args):
        for n in self.event_notifiers:
            getattr(n, method)(*args)

    async def startup_notify(self):
        if os.environ.get("STARTUP_NOTIFY") != "1":
            return
        if self.notifier:
            await self.notifier.send_startup()
        if self.email_notifier:
            await self.email_notifier.send_startup()

    def _ensure_state_fields(self, state):
        defaults = {
            "pending_payments": [],
            "receipt_map": {},
            "processed_refunds": [],
            "last_refund_sync_time": None
        }
        for key, default in defaults.items():
            if key not in state:
                state[key] = default
        return state

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                return self._ensure_state_fields(state)
            except:
                pass

        base = {
            "last_sync_time": f"{config.SYNC_START_DATE}T00:00:00Z" if config.SYNC_START_DATE else (datetime.now() - timedelta(days=1)).isoformat(),
            "processed_payments": [],
            "pending_payments": [],
            "receipt_map": {},
            "processed_refunds": [],
            "last_refund_sync_time": None
        }
        return base

    def save_state(self):
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f)

    def _save_refresh_token(self, token):
        self.state["refresh_token"] = token
        self.save_state()

    async def get_new_yookassa_payments(self):
        new_payments = []
        last_sync = self.state.get("last_sync_time")
        skip_ids = set(self.state["processed_payments"]) | set(self.state["pending_payments"])

        params = {
            "status": "succeeded",
            "created_at.gte": last_sync
        }

        try:
            res = await asyncio.wait_for(asyncio.to_thread(Payment.list, params), timeout=120)
            for payment in res.items:
                if payment.id not in skip_ids:
                    new_payments.append(payment)

            while res.next_cursor:
                params["cursor"] = res.next_cursor
                res = await asyncio.wait_for(asyncio.to_thread(Payment.list, params), timeout=120)
                for payment in res.items:
                    if payment.id not in skip_ids:
                        new_payments.append(payment)
        except asyncio.TimeoutError:
            logging.error("Таймаут получения платежей ЮKassa (>120s)")
            return new_payments, "Таймаут API ЮКассы (>120s)"
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "нет деталей"
            logging.error(f"Ошибка ЮKassa: [{err_type}] {err_text}")
            return new_payments, f"[{err_type}] {err_text}"

        return new_payments, None

    async def get_new_refunds(self):
        new_refunds = []
        last_refund_sync = self.state.get("last_refund_sync_time") or self.state.get("last_sync_time")

        params = {
            "status": "succeeded",
            "created_at.gte": last_refund_sync
        }

        try:
            res = await asyncio.wait_for(asyncio.to_thread(Refund.list, params), timeout=120)
            for refund in res.items:
                if refund.id not in self.state["processed_refunds"]:
                    new_refunds.append(refund)

            while res.next_cursor:
                params["cursor"] = res.next_cursor
                res = await asyncio.wait_for(asyncio.to_thread(Refund.list, params), timeout=120)
                for refund in res.items:
                    if refund.id not in self.state["processed_refunds"]:
                        new_refunds.append(refund)
        except asyncio.TimeoutError:
            logging.error("Таймаут получения возвратов ЮKassa (>120s)")
            return new_refunds, "Таймаут API ЮКассы (>120s)"
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "нет деталей"
            logging.error(f"Ошибка получения возвратов ЮKassa: [{err_type}] {err_text}")
            return new_refunds, f"[{err_type}] {err_text}"

        return new_refunds, None

    async def sync(self):
        logging.info("="*60)
        logging.info("Начало синхронизации...")
        logging.info(f"Последняя синхронизация: {self.state.get('last_sync_time')}")

        pending = self.state.get("pending_payments", [])
        if pending:
            logging.warning(f"⚠ Обнаружено {len(pending)} платежей в статусе 'pending' (возможно, были отправлены в налоговую, но статус неизвестен): {pending}")
            logging.warning("Эти платежи пропущены для предотвращения дублей. Проверьте их вручную в ЛК налоговой.")
            self._emit("on_pending_found", len(pending))

        try:
            new_payments, payments_error = await self.get_new_yookassa_payments()

            if payments_error:
                logging.warning(f"⚠ Ошибка получения платежей из ЮКассы: {payments_error}")
                self._emit("on_yookassa_error", f"Платежи: {payments_error}")

            if not new_payments:
                if not payments_error:
                    logging.info("✓ Новых платежей не найдено.")
            else:
                logging.info(f"✓ Найдено новых платежей: {len(new_payments)}")
                self._emit("on_sync_start", len(new_payments))

            successful = 0
            failed = 0

            for payment in new_payments:
                try:
                    amount = float(payment.amount.value)
                    payment_date = datetime.fromisoformat(payment.created_at.replace('Z', '+00:00'))

                    template_vars = build_template_vars(payment)
                    description = config.INCOME_DESCRIPTION_TEMPLATE.format_map(template_vars)

                    receipt_uuid = None

                    for attempt in range(1, 4):
                        receipt_uuid = await self.nalog.add_income(description, amount, payment_date)
                        if receipt_uuid:
                            break

                        logging.warning(f"Попытка {attempt}/3: add_income не вернул receiptUuid для {payment.id}, проверяю наличие чека в налоговой...")
                        receipt_uuid = await self.nalog.find_income(description, amount)
                        if receipt_uuid:
                            logging.info(f"✓ Чек найден в налоговой (был создан несмотря на ошибку ответа)")
                            self._emit("on_payment_verified")
                            break

                        if attempt < 3:
                            logging.info(f"Чек не найден, повторная попытка...")

                    if receipt_uuid:
                        self.state["processed_payments"].append(payment.id)
                        self.state["receipt_map"][payment.id] = receipt_uuid
                        self.state["last_sync_time"] = payment.created_at
                        self.save_state()
                        successful += 1
                        self._emit("on_payment_success", amount)
                    else:
                        failed += 1
                        logging.warning(f"Пропуск платежа {payment.id}: не удалось зарегистрировать после 3 попыток. "
                                        f"Повторная попытка при следующей синхронизации.")
                        self._emit("on_payment_error", payment.id, "ошибка регистрации дохода")
                except Exception as e:
                    failed += 1
                    logging.error(f"Ошибка при обработке платежа {payment.id}: {e}")
                    self._emit("on_payment_error", payment.id, str(e)[:80])

            if new_payments:
                logging.info(f"Результат платежей: успешно={successful}, ошибок={failed}")

            new_refunds, refunds_error = await self.get_new_refunds()

            if refunds_error:
                logging.warning(f"⚠ Ошибка получения возвратов из ЮКассы: {refunds_error}")
                self._emit("on_yookassa_error", f"Возвраты: {refunds_error}")

            if new_refunds:
                logging.info(f"✓ Найдено новых возвратов: {len(new_refunds)}")

                cancelled = 0
                cancel_failed = 0

                for refund in new_refunds:
                    try:
                        receipt_uuid = self.state["receipt_map"].get(refund.payment_id)

                        if not receipt_uuid:
                            logging.warning(f"Возврат {refund.id}: чек для платежа {refund.payment_id} не найден в receipt_map, пропуск")
                            self.state["processed_refunds"].append(refund.id)
                            self.state["last_refund_sync_time"] = refund.created_at
                            self.save_state()
                            self._emit("on_refund_skipped")
                            continue

                        success = await self.nalog.cancel_income(receipt_uuid)

                        if success:
                            self.state["processed_refunds"].append(refund.id)
                            self.state["last_refund_sync_time"] = refund.created_at
                            del self.state["receipt_map"][refund.payment_id]
                            self.save_state()
                            cancelled += 1
                            self._emit("on_refund_cancelled")
                        else:
                            cancel_failed += 1
                            logging.warning(f"Не удалось аннулировать чек {receipt_uuid} для возврата {refund.id}")
                            self._emit("on_refund_error")
                    except Exception as e:
                        cancel_failed += 1
                        logging.error(f"Ошибка при обработке возврата {refund.id}: {e}")
                        self._emit("on_refund_error")

                logging.info(f"Результат возвратов: аннулировано={cancelled}, ошибок={cancel_failed}")
            else:
                if not refunds_error:
                    logging.info("✓ Новых возвратов не найдено.")

            if (not new_payments and not new_refunds and self.notifier
                    and not pending and not payments_error and not refunds_error):
                await self.notifier.send_no_payments()

        except Exception as e:
            logging.error(f"Критическая ошибка при синхронизации: {e}", exc_info=True)
        finally:
            await self.nalog.close()
            if self.notifier:
                await self.notifier.send_summary()
            if self.email_notifier:
                await self.email_notifier.send_summary()
            logging.info("Синхронизация завершена.")
            logging.info("="*60)


async def main():
    try:
        manager = SyncManager()
        await manager.startup_notify()
        await manager.sync()
    except Exception as e:
        logging.critical(f"Критическая ошибка: {e}", exc_info=True)
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
