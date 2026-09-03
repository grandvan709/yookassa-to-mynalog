import asyncio
import argparse
import os
import logging
import httpx
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from yookassa import Configuration, Payment, Refund
import config
from version import __version__
from logging_config import setup_logging, colorize, ANSI
from nalog_api import MoyNalogAPI
from telegram_notifier import TelegramNotifier
from email_notifier import EmailNotifier
from utils import build_template_vars
from state_store import ConcurrentRunError, StateStore
from health_state import write_status
from customer_receipt_delivery import (
    CustomerReceiptDelivery,
    extract_telegram_user_id,
)

LOG_DIR = os.getenv("LOG_DIR", "logs")
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

setup_logging(LOG_DIR)


class SyncManager:
    def __init__(self):
        try:
            config.validate_config()
        except ValueError as e:
            logging.error(f"Ошибка конфигурации: {e}")
            raise

        Configuration.configure(config.YOOKASSA_SHOP_ID, config.YOOKASSA_API_KEY)
        self.state_store = StateStore(
            f"{DATA_DIR}/sync_state.db",
            legacy_json_path=f"{LOG_DIR}/sync_state.json",
        )
        self.state = self.load_state()
        refresh_token = self.state.get("refresh_token") or config.MOY_NALOG_REFRESH_TOKEN
        self.nalog = MoyNalogAPI(
            config.MOY_NALOG_LOGIN,
            config.MOY_NALOG_PASSWORD,
            auth_method=config.MOY_NALOG_AUTH_METHOD,
            refresh_token=refresh_token,
            on_refresh_token=self._save_refresh_token,
        )
        if config.TELEGRAM_CUSTOMER_RECEIPTS_ENABLED:
            self.customer_receipt_delivery = CustomerReceiptDelivery(
                config.TELEGRAM_CUSTOMER_BOT_TOKEN,
                config.MOY_NALOG_RECEIPT_INN,
                telegram_proxy=config.TELEGRAM_PROXY,
                nalog_proxy=config.YOOKASSA_NALOG_PROXY,
                max_bytes=int(config.TELEGRAM_CUSTOMER_RECEIPT_MAX_MB * 1024 * 1024),
            )
        else:
            self.customer_receipt_delivery = None

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
        else:
            self.notifier = None

        report = self.state.get("receipt_reports", {})
        report_chat_id = report.get("chat_id")
        report_thread_id = report.get("thread_id")
        if config.TELEGRAM_BOT_TOKEN and report_chat_id:
            try:
                report_thread_id = int(report_thread_id) if report_thread_id else None
            except (TypeError, ValueError):
                logging.warning("Некорректный ID темы отчётов; используется основной чат.")
                report_thread_id = None
            self.receipt_notifier = TelegramNotifier(
                bot_token=config.TELEGRAM_BOT_TOKEN,
                chat_id=str(report_chat_id),
                thread_id=report_thread_id,
                proxy=config.TELEGRAM_PROXY,
            )
        else:
            self.receipt_notifier = None

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
        else:
            self.email_notifier = None

        self.event_notifiers = [
            n for n in (self.notifier, self.receipt_notifier, self.email_notifier) if n
        ]

    def _emit(self, method, *args):
        success_methods = {
            "on_payment_success", "on_payment_verified",
            "on_refund_cancelled", "on_refund_adjusted",
        }
        for n in self.event_notifiers:
            if n is self.notifier:
                if method in success_methods or not self._telegram_event_enabled(method):
                    continue
            if n is getattr(self, "receipt_notifier", None):
                report = self.state.get("receipt_reports", {})
                if method not in success_methods and method != "on_sync_start":
                    continue
                if method in success_methods and not report.get("enabled", True):
                    continue
            getattr(n, method)(*args)

    def _telegram_event_enabled(self, method):
        preferences = self.state.get("notification_preferences", {})
        if method in {
            "on_payment_error",
            "on_refund_error",
            "on_yookassa_error",
            "on_pending_found",
            "on_pending_refunds_found",
        }:
            return preferences.get("receipt_errors", True)
        return True

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
            "watched_payments": [],
            "expired_unpaid_payments": [],
            "skipped_payments": [],
            "receipt_map": {},
            "receipt_deliveries": [],
            "processed_refunds": [],
            "pending_refunds": [],
            "payment_balances": {},
            "payment_event_times": {},
            "refund_event_times": {},
            "last_refund_sync_time": None,
            "refund_tracking_started_at": None,
            "notification_preferences": {
                "receipt_success": True,
                "receipt_errors": True,
            },
            "receipt_reports": {
                "enabled": state.get("notification_preferences", {}).get(
                    "receipt_success", True
                ),
                "chat_id": config.TELEGRAM_RECEIPT_REPORT_CHAT_ID
                or config.TELEGRAM_CHAT_ID,
                "thread_id": config.TELEGRAM_RECEIPT_REPORT_THREAD_ID
                or config.TELEGRAM_THREAD_ID,
            },
        }
        for key, default in defaults.items():
            if key not in state:
                state[key] = default

        if config.REFUNDS_ENABLED and not state.get("refund_tracking_started_at"):
            now = datetime.now(timezone.utc).isoformat()
            # Старый set-sync-start заполнял last_refund_sync_time даже при
            # выключенной функции. Считаем прежний checkpoint действительным
            # только тогда, когда в state уже есть возвраты.
            was_used = bool(
                state.get("processed_refunds") or state.get("pending_refunds")
            )
            started_at = (
                state.get("last_refund_sync_time") if was_used else None
            ) or now
            state["refund_tracking_started_at"] = started_at
            state["last_refund_sync_time"] = started_at
            logging.info("Наблюдение за возвратами включено с %s.", started_at)

        # Старые версии принимали ответ авторизации "Не найдено" во время
        # техработ ЛК ФЛ за постоянный отказ. Возвращаем только такие записи в
        # очередь; остальные rejected по-прежнему требуют ручной проверки.
        restored = 0
        for workflow in state.get("pending_payments", []):
            if not isinstance(workflow, dict):
                continue
            error = str(workflow.get("error") or "").strip().casefold()
            if workflow.get("status") == "rejected" and error in {
                "не найдено",
                "not found",
            }:
                workflow["status"] = "ready"
                workflow["last_error_retryable"] = True
                workflow.pop("last_notified_error", None)
                restored += 1
        if restored:
            logging.warning(
                "Возвращено в очередь после ложного rejected при техработах "
                "ЛК ФЛ: %s.",
                restored,
            )
        return state

    def load_state(self):
        state = self.state_store.load()
        if state is not None:
            state = self._ensure_state_fields(state)
            self.state_store.save(state)
            return state

        base = {
            "last_sync_time": config.parse_sync_start(config.SYNC_START_DATE)
            or (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "processed_payments": [],
            "pending_payments": [],
            "watched_payments": [],
            "expired_unpaid_payments": [],
            "skipped_payments": [],
            "receipt_map": {},
            "receipt_deliveries": [],
            "processed_refunds": [],
            "pending_refunds": [],
            "payment_balances": {},
            "payment_event_times": {},
            "refund_event_times": {},
            "last_refund_sync_time": None,
            "refund_tracking_started_at": None,
            "notification_preferences": {
                "receipt_success": True,
                "receipt_errors": True,
            },
            "receipt_reports": {
                "enabled": True,
                "chat_id": config.TELEGRAM_RECEIPT_REPORT_CHAT_ID
                or config.TELEGRAM_CHAT_ID,
                "thread_id": config.TELEGRAM_RECEIPT_REPORT_THREAD_ID
                or config.TELEGRAM_THREAD_ID,
            },
        }
        base = self._ensure_state_fields(base)
        self.state_store.save(base)
        return base

    def save_state(self):
        self.state_store.save(self.state)

    def _save_refresh_token(self, token):
        self.state["refresh_token"] = token
        self.save_state()

    def check_for_updates(self):
        last_check = self.state.get("last_update_check")
        if last_check:
            try:
                if datetime.now() - datetime.fromisoformat(last_check) < timedelta(hours=24):
                    return
            except ValueError:
                pass

        try:
            url = "https://api.github.com/repos/zavul0nn/yookassa-to-mynalog/releases/latest"
            with httpx.Client(trust_env=False, timeout=10.0) as client:
                resp = client.get(url, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 200:
                latest = resp.json().get("tag_name", "")
                if latest and _parse_version(latest) > _parse_version(__version__):
                    logging.warning(
                        f"⚠️ Доступна новая версия {latest} (текущая: {__version__}). "
                        f"https://github.com/zavul0nn/yookassa-to-mynalog/releases/latest"
                    )
                    self._emit("on_update_available", latest.lstrip("vV"))
                else:
                    logging.info(f"✓ Установлена актуальная версия ({__version__}).")
            else:
                logging.warning(f"Не удалось проверить обновления (GitHub вернул {resp.status_code}).")
        except Exception as e:
            logging.warning(f"Не удалось проверить обновления: [{type(e).__name__}]")
        finally:
            self.state["last_update_check"] = datetime.now().isoformat()
            self.save_state()

    async def get_new_yookassa_payments(self):
        new_payments = []
        last_sync = self.state.get("last_sync_time")
        now = datetime.now(timezone.utc)
        last_sync_time = _parse_timestamp(last_sync) or now
        query_start = min(
            last_sync_time,
            now - timedelta(minutes=config.PENDING_PAYMENT_WATCH_MINUTES),
        )
        configured_start = _parse_timestamp(
            config.parse_sync_start(config.SYNC_START_DATE)
        )
        if configured_start:
            query_start = max(query_start, configured_start)
        pending_ids = {
            item if isinstance(item, str) else item.get("payment_id")
            for item in self.state["pending_payments"]
        }
        pending_ids.discard(None)
        watched_ids = {
            item.get("payment_id")
            for item in self.state.get("watched_payments", [])
        }
        expired_ids = {
            item.get("payment_id")
            for item in self.state.get("expired_unpaid_payments", [])
        }
        skip_ids = set(self.state["processed_payments"]) | pending_ids | expired_ids

        params = {
            "created_at.gte": query_start.isoformat().replace("+00:00", "Z"),
            "created_at.lte": now.isoformat().replace("+00:00", "Z"),
        }

        try:
            res = await asyncio.wait_for(asyncio.to_thread(Payment.list, params), timeout=120)
            while True:
                for payment in res.items:
                    status = getattr(payment, "status", None) or "succeeded"
                    if status == "succeeded":
                        if payment.id in watched_ids:
                            self._remove_watched_payment(payment.id)
                            watched_ids.discard(payment.id)
                            self.save_state()
                        if payment.id not in skip_ids:
                            new_payments.append(payment)
                            skip_ids.add(payment.id)
                    elif status != "canceled" and payment.id not in skip_ids:
                        watched = self._track_unpaid_payment(payment, status)
                        created_at = _parse_timestamp(payment.created_at)
                        if created_at and now - created_at >= timedelta(
                            minutes=config.PENDING_PAYMENT_WATCH_MINUTES
                        ):
                            self._expire_unpaid_payment(watched, payment, status)
                            expired_ids.add(payment.id)
                            watched_ids.discard(payment.id)
                        else:
                            watched_ids.add(payment.id)
                if not res.next_cursor:
                    break
                params["cursor"] = res.next_cursor
                res = await asyncio.wait_for(asyncio.to_thread(Payment.list, params), timeout=120)
        except asyncio.TimeoutError:
            logging.error("Таймаут получения платежей ЮKassa (>120s)")
            return new_payments, "Таймаут API ЮКассы (>120s)", None
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "нет деталей"
            logging.error(f"Ошибка ЮKassa: [{err_type}] {err_text}")
            return new_payments, f"[{err_type}] {err_text}", None

        scan_checkpoint = now.isoformat().replace("+00:00", "Z")
        return new_payments, None, scan_checkpoint

    def _track_unpaid_payment(self, payment, status):
        watched = self.state.setdefault("watched_payments", [])
        existing = next(
            (item for item in watched if item.get("payment_id") == payment.id),
            None,
        )
        now = datetime.now(timezone.utc).isoformat()
        if existing:
            existing["last_seen_at"] = now
            existing["last_status"] = status
        else:
            watched.append({
                "payment_id": payment.id,
                "created_at": payment.created_at,
                "first_seen_at": now,
                "last_seen_at": now,
                "last_checked_at": None,
                "last_status": status,
            })
            logging.info(
                "Неоплаченный платёж %s добавлен под наблюдение на %s минут.",
                payment.id,
                config.PENDING_PAYMENT_WATCH_MINUTES,
            )
        self.save_state()
        return existing or watched[-1]

    def _remove_watched_payment(self, payment_id):
        self.state["watched_payments"] = [
            item for item in self.state.get("watched_payments", [])
            if item.get("payment_id") != payment_id
        ]

    def _expire_unpaid_payment(self, watched, payment, status):
        payment_id = watched["payment_id"]
        self._remove_watched_payment(payment_id)
        expired = self.state.setdefault("expired_unpaid_payments", [])
        expired[:] = [
            item for item in expired if item.get("payment_id") != payment_id
        ]
        expired.append({
            "payment_id": payment_id,
            "created_at": watched["created_at"],
            "expired_at": datetime.now(timezone.utc).isoformat(),
            "last_status": status,
            "amount": str(getattr(getattr(payment, "amount", None), "value", "")),
            "currency": getattr(getattr(payment, "amount", None), "currency", None),
            "status": "unpaid_expired",
        })
        self.save_state()
        logging.info(
            "Неоплаченный платёж %s снят с наблюдения спустя %s минут.",
            payment_id,
            config.PENDING_PAYMENT_WATCH_MINUTES,
        )

    async def _resume_watched_payments(self):
        completed_amounts = []
        failures = 0
        now = datetime.now(timezone.utc)
        for watched in list(self.state.get("watched_payments", [])):
            payment_id = watched.get("payment_id")
            payment, error = await self.get_yookassa_payment(payment_id)
            if error:
                failures += 1
                logging.warning(
                    "Не удалось проверить неоплаченный платёж %s: %s",
                    payment_id,
                    error,
                )
                self._emit(
                    "on_yookassa_error",
                    f"ЮKassa (проверка платежа {payment_id}): {error}",
                )
                continue

            status = getattr(payment, "status", None) or "pending"
            watched["last_checked_at"] = now.isoformat()
            watched["last_status"] = status
            if status == "succeeded":
                self._remove_watched_payment(payment_id)
                self.save_state()
                if payment_id in self.state["processed_payments"]:
                    continue
                workflow = self._prepare_payment_workflow(payment)
                result, amount = await self._resume_payment_workflow(workflow)
                if result == "completed":
                    completed_amounts.append(amount)
                    logging.info(
                        "Отложенный платёж %s успешно оплачен и обработан.",
                        payment_id,
                    )
                elif result == "skipped":
                    self._emit(
                        "on_payment_error",
                        payment_id,
                        f"валюта {workflow.get('currency')} не поддерживается",
                    )
                else:
                    failures += 1
                continue

            created_at = _parse_timestamp(watched.get("created_at"))
            age = now - created_at if created_at else timedelta.max
            if status == "canceled" or age >= timedelta(
                minutes=config.PENDING_PAYMENT_WATCH_MINUTES
            ):
                self._expire_unpaid_payment(watched, payment, status)
            else:
                self.save_state()
        return completed_amounts, failures

    async def get_new_refunds(self):
        if not config.REFUNDS_ENABLED:
            return [], None, None
        new_refunds = []
        last_refund_sync = (
            self.state.get("last_refund_sync_time")
            or self.state.get("refund_tracking_started_at")
        )
        if not last_refund_sync:
            # Защита для вызова без обычной инициализации SyncManager.
            last_refund_sync = datetime.now(timezone.utc).isoformat()
            self.state["refund_tracking_started_at"] = last_refund_sync
            self.state["last_refund_sync_time"] = last_refund_sync
            self.save_state()
        scan_checkpoint = datetime.now(timezone.utc).isoformat()
        processed_ids = set(self.state["processed_refunds"])
        pending_ids = {item["refund_id"] for item in self.state["pending_refunds"]}
        skip_ids = processed_ids | pending_ids

        params = {
            "status": "succeeded",
            "created_at.gte": last_refund_sync
        }

        try:
            res = await asyncio.wait_for(asyncio.to_thread(Refund.list, params), timeout=120)
            for refund in res.items:
                if refund.id not in skip_ids:
                    new_refunds.append(refund)

            while res.next_cursor:
                params["cursor"] = res.next_cursor
                res = await asyncio.wait_for(asyncio.to_thread(Refund.list, params), timeout=120)
                for refund in res.items:
                    if refund.id not in skip_ids:
                        new_refunds.append(refund)
        except asyncio.TimeoutError:
            logging.error("Таймаут получения возвратов ЮKassa (>120s)")
            return new_refunds, "Таймаут API ЮКассы (>120s)", None
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "нет деталей"
            logging.error(f"Ошибка получения возвратов ЮKassa: [{err_type}] {err_text}")
            return new_refunds, f"[{err_type}] {err_text}", None

        return new_refunds, None, scan_checkpoint

    async def get_yookassa_payment(self, payment_id):
        try:
            payment = await asyncio.wait_for(
                asyncio.to_thread(Payment.find_one, payment_id),
                timeout=120,
            )
            return payment, None
        except asyncio.TimeoutError:
            return None, "таймаут API ЮКассы (>120s)"
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "нет деталей"
            return None, f"[{err_type}] {err_text}"

    def _prepare_payment_workflow(self, payment):
        amount = Decimal(str(payment.amount.value))
        currency = getattr(payment.amount, "currency", None)
        description = config.INCOME_DESCRIPTION_TEMPLATE.format_map(
            build_template_vars(payment)
        )
        if payment.id not in description:
            description = (
                f"{description} [{config.PAYMENT_ID_PREFIX}:{payment.id}]"
            )

        workflow = {
            "payment_id": payment.id,
            "amount": str(amount),
            "currency": currency,
            "created_at": payment.created_at,
            "description": description,
            "payment_description": payment.description or "",
            "telegram_user_id": extract_telegram_user_id(payment.description),
            "status": "ready" if currency == "RUB" else "unsupported_currency",
            "attempts": 0,
            "queue_attempts": 0,
        }
        self.state["pending_payments"].append(workflow)
        self.save_state()
        return workflow

    async def _resume_payment_workflow(self, workflow, queue_attempt=False):
        if isinstance(workflow, str):
            return "manual", None

        status = workflow.get("status")
        if status == "unsupported_currency":
            self._complete_skipped_payment_workflow(
                workflow,
                f"валюта {workflow.get('currency') or 'не указана'} не поддерживается",
            )
            return "skipped", None
        if status not in ("ready", "creating", "unknown"):
            return "manual", None

        amount = Decimal(workflow["amount"])
        payment_date = datetime.fromisoformat(
            workflow["created_at"].replace('Z', '+00:00')
        )
        workflow["attempts"] = int(workflow.get("attempts", 0)) + 1
        workflow["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        if status == "ready" and queue_attempt:
            maximum = config.FNS_QUEUE_MAX_ATTEMPTS
            current = int(workflow.get("queue_attempts", 0))
            if maximum and current >= maximum:
                workflow["status"] = "retry_exhausted"
                workflow["error"] = (
                    f"достигнут лимит повторов очереди ФНС: {maximum}"
                )
                self.save_state()
                return "manual", None
            workflow["queue_attempts"] = current + 1
        self.save_state()

        if status == "ready":
            workflow["status"] = "creating"
            self.save_state()
            receipt_uuid = await self.nalog.add_income(
                workflow["description"],
                amount,
                payment_date,
            )
            if receipt_uuid:
                self._complete_payment_workflow(workflow, receipt_uuid)
                return "completed", amount
            write_uncertain = getattr(
                self.nalog, "last_operation_uncertain", True
            )
            write_retryable = getattr(
                self.nalog, "last_error_retryable", False
            )
            write_error = self.nalog.last_error
        else:
            write_uncertain = True
            write_retryable = False
            write_error = self.nalog.last_error

        # Если чек точно не мог быть создан (не прошла авторизация, не установлено
        # соединение или ФНС явно отклонила запрос), сверка через find_income лишь
        # повторно авторизуется и создаёт лишнюю нагрузку на ФНС.
        if not write_uncertain:
            return self._defer_payment_workflow(
                workflow,
                write_error,
                write_retryable,
                uncertain=False,
                queue_attempt=queue_attempt,
            )

        receipt_uuid = await self.nalog.find_income(
            workflow["description"],
            amount,
            payment_date,
        )
        if receipt_uuid:
            self._complete_payment_workflow(workflow, receipt_uuid)
            return "completed", amount

        return self._defer_payment_workflow(
            workflow,
            write_error or self.nalog.last_error,
            write_retryable or getattr(self.nalog, "last_error_retryable", False),
            uncertain=True,
            queue_attempt=queue_attempt,
        )

    def _defer_payment_workflow(
        self, workflow, error, retryable, *, uncertain, queue_attempt
    ):
        workflow["error"] = error
        workflow["last_error_retryable"] = bool(retryable)
        if uncertain:
            workflow["status"] = "unknown"
        elif retryable:
            workflow["status"] = "ready"
        else:
            workflow["status"] = "rejected"
        if (
            queue_attempt
            and workflow["status"] == "ready"
            and config.FNS_QUEUE_MAX_ATTEMPTS
            and workflow.get("queue_attempts", 0)
            >= config.FNS_QUEUE_MAX_ATTEMPTS
        ):
            workflow["status"] = "retry_exhausted"
            workflow["error"] = (
                f"{workflow.get('error') or 'ФНС недоступна'}; достигнут лимит "
                f"повторов: {config.FNS_QUEUE_MAX_ATTEMPTS}"
            )
        self.save_state()
        return "manual", None

    def _complete_payment_workflow(self, workflow, receipt_uuid):
        payment_id = workflow["payment_id"]
        if payment_id not in self.state["processed_payments"]:
            self.state["processed_payments"].append(payment_id)
        self.state["receipt_map"][payment_id] = receipt_uuid
        self.state["payment_balances"][payment_id] = workflow["amount"]
        self.state["payment_event_times"][payment_id] = workflow["created_at"]
        self.state["pending_payments"] = [
            item for item in self.state["pending_payments"]
            if (
                item != payment_id
                if isinstance(item, str)
                else item.get("payment_id") != payment_id
            )
        ]
        self._enqueue_customer_receipt(workflow, receipt_uuid)
        self.save_state()

    def _enqueue_customer_receipt(self, workflow, receipt_uuid):
        if not getattr(self, "customer_receipt_delivery", None):
            return
        telegram_user_id = workflow.get("telegram_user_id")
        if not telegram_user_id:
            telegram_user_id = extract_telegram_user_id(
                workflow.get("payment_description") or workflow.get("description")
            )
        if not telegram_user_id:
            logging.warning(
                "Платёж %s зарегистрирован, но Telegram ID не найден в описании.",
                workflow.get("payment_id", "unknown"),
            )
            return

        deliveries = self.state.setdefault("receipt_deliveries", [])
        if any(item.get("receipt_uuid") == receipt_uuid for item in deliveries):
            return
        deliveries.append({
            "payment_id": workflow.get("payment_id"),
            "receipt_uuid": receipt_uuid,
            "telegram_user_id": telegram_user_id,
            "amount": workflow.get("amount"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "attempts": 0,
            "link_sent": False,
        })

    async def _process_customer_receipt_deliveries(self):
        delivery = getattr(self, "customer_receipt_delivery", None)
        if not delivery:
            return {"delivered": 0, "pending": 0, "undeliverable": 0}

        counts = {"delivered": 0, "pending": 0, "undeliverable": 0}
        for job in self.state.setdefault("receipt_deliveries", []):
            if job.get("status") != "pending":
                continue
            job["attempts"] = int(job.get("attempts", 0)) + 1
            job["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            try:
                result = await delivery.deliver(job)
            except Exception as exc:
                result = None
                job["last_error"] = f"[{type(exc).__name__}] {str(exc)[:200]}"

            if result and result.status == "delivered":
                job["status"] = "delivered"
                job["delivered_at"] = datetime.now(timezone.utc).isoformat()
                job.pop("last_error", None)
                counts["delivered"] += 1
            elif result and result.status == "undeliverable":
                job["status"] = "undeliverable"
                job["last_error"] = result.error
                job["failed_at"] = datetime.now(timezone.utc).isoformat()
                counts["undeliverable"] += 1
                logging.error(
                    "Чек %s нельзя доставить пользователю Telegram %s: %s",
                    job.get("receipt_uuid"),
                    job.get("telegram_user_id"),
                    result.error,
                )
            else:
                if result:
                    job["last_error"] = result.error
                    job["link_sent"] = bool(
                        job.get("link_sent") or result.link_sent
                    )
                counts["pending"] += 1
            self.save_state()

        if any(counts.values()):
            logging.info(
                "Доставка чеков покупателям: отправлено=%s, ожидает=%s, "
                "недоставимо=%s",
                counts["delivered"],
                counts["pending"],
                counts["undeliverable"],
            )
        return counts

    def _complete_skipped_payment_workflow(self, workflow, reason):
        payment_id = workflow["payment_id"]
        if payment_id not in self.state["processed_payments"]:
            self.state["processed_payments"].append(payment_id)
        self.state["payment_event_times"][payment_id] = workflow["created_at"]
        skipped = self.state.setdefault("skipped_payments", [])
        skipped[:] = [item for item in skipped if item.get("payment_id") != payment_id]
        skipped.append({
            "payment_id": payment_id,
            "amount": workflow["amount"],
            "currency": workflow.get("currency"),
            "created_at": workflow["created_at"],
            "reason": reason,
        })
        self.state["pending_payments"] = [
            item for item in self.state["pending_payments"]
            if (
                item != payment_id
                if isinstance(item, str)
                else item.get("payment_id") != payment_id
            )
        ]
        self.save_state()

    async def _resume_pending_payments(
        self, stop_on_unavailable=False, delay_seconds=0
    ):
        completed_amounts = []
        manual = 0
        workflows = list(self.state.get("pending_payments", []))
        for index, workflow in enumerate(workflows):
            try:
                result, amount = await self._resume_payment_workflow(
                    workflow, queue_attempt=True
                )
            except Exception as e:
                if isinstance(workflow, dict):
                    workflow["status"] = "manual_error"
                    workflow["error"] = f"[{type(e).__name__}] {str(e)[:160]}"
                    self.save_state()
                logging.error(f"Ошибка восстановления pending-платежа: {e}")
                result, amount = "manual", None
            if result == "completed":
                completed_amounts.append(amount)
            elif result == "skipped":
                if isinstance(workflow, dict):
                    reason = (
                        f"валюта {workflow.get('currency') or 'не указана'} "
                        "не поддерживается"
                    )
                    logging.warning(
                        "Платёж %s пропущен: %s.",
                        workflow.get("payment_id", "unknown"),
                        reason,
                    )
                    self._emit(
                        "on_payment_error",
                        workflow.get("payment_id", "unknown"),
                        reason,
                    )
            else:
                manual += 1
                if isinstance(workflow, dict):
                    error = workflow.get("error")
                    signature = f"{workflow.get('status')}:{error}"
                    if error and workflow.get("last_notified_error") != signature:
                        self._emit(
                            "on_payment_error",
                            workflow.get("payment_id", "unknown"),
                            f"Мой Налог: {error} "
                            f"(статус: {workflow.get('status')})",
                        )
                        workflow["last_notified_error"] = signature
                        self.save_state()
                if (
                    stop_on_unavailable
                    and isinstance(workflow, dict)
                    and workflow.get("last_error_retryable")
                ):
                    logging.warning(
                        "ФНС временно недоступна: оставшаяся очередь будет "
                        "обработана в следующем цикле."
                    )
                    break
            if delay_seconds and index + 1 < len(workflows):
                await asyncio.sleep(delay_seconds)
        return completed_amounts, manual

    async def retry_fns_queue(self):
        """Обработать только сохранённую очередь ФНС, не опрашивая ЮKassa."""
        self.state_store.acquire_lock()
        write_status(DATA_DIR, "running", filename="fns_retry_status.json")
        try:
            completed, manual = await self._resume_pending_payments(
                stop_on_unavailable=True,
                delay_seconds=config.FNS_RETRY_DELAY_SECONDS,
            )
            await self._process_customer_receipt_deliveries()
            for amount in completed:
                self._emit("on_payment_success", amount)
            remaining = len(self.state.get("pending_payments", []))
            write_status(
                DATA_DIR,
                "ok" if not remaining else "pending",
                filename="fns_retry_status.json",
                completed=len(completed),
                remaining=remaining,
                manual=manual,
            )
            logging.info(
                "Очередь ФНС обработана: зарегистрировано=%s, осталось=%s",
                len(completed),
                remaining,
            )
        except Exception as e:
            write_status(
                DATA_DIR,
                "error",
                filename="fns_retry_status.json",
                error=f"[{type(e).__name__}] {str(e)[:160]}",
            )
            raise
        finally:
            self.state_store.release_lock()
            await self.nalog.close()
            if self.notifier:
                await self.notifier.send_summary()
            if getattr(self, "receipt_notifier", None):
                await self.receipt_notifier.send_summary()
            if self.email_notifier:
                await self.email_notifier.send_summary()

    def _prepare_refund_adjustment(self, refund, payment, current_amount):
        receipt_uuid = self.state["receipt_map"].get(refund.payment_id)
        refund_amount = Decimal(str(refund.amount.value))
        remaining_amount = current_amount - refund_amount
        description = config.INCOME_DESCRIPTION_TEMPLATE.format_map(
            build_template_vars(payment)
        )

        adjustment = {
            "refund_id": refund.id,
            "payment_id": refund.payment_id,
            "refund_amount": str(refund_amount),
            "payment_amount": str(payment.amount.value),
            "previous_amount": str(current_amount),
            "remaining_amount": str(remaining_amount),
            "created_at": refund.created_at,
            "payment_created_at": payment.created_at,
            "receipt_uuid": receipt_uuid,
            "replacement_description": (
                f"{description} [остаток после возврата {refund.id}]"
            ),
            "status": "ready" if receipt_uuid else "missing_receipt",
        }
        self.state["pending_refunds"].append(adjustment)
        self.save_state()
        return adjustment

    async def _resume_refund_adjustment(self, adjustment):
        status = adjustment.get("status")
        remaining_amount = Decimal(adjustment["remaining_amount"])
        payment_date = datetime.fromisoformat(
            adjustment["payment_created_at"].replace('Z', '+00:00')
        )

        if status == "cancellation_unknown":
            income_status = await self.nalog.get_income_status(
                adjustment["receipt_uuid"], payment_date
            )
            adjustment["last_verification_at"] = datetime.now(timezone.utc).isoformat()
            if income_status == "cancelled":
                adjustment["status"] = "cancelled"
                adjustment.pop("error", None)
                self.save_state()
                status = "cancelled"
            elif income_status == "active":
                adjustment["status"] = "ready"
                adjustment["error"] = "исходный чек активен; аннулирование будет повторено"
                self.save_state()
                return "manual"
            else:
                adjustment["error"] = self.nalog.last_error or "исходный чек не найден при сверке"
                self.save_state()
                return "manual"

        if status == "replacement_unknown":
            receipt_uuid = await self.nalog.find_income(
                adjustment["replacement_description"], remaining_amount, payment_date
            )
            adjustment["last_verification_at"] = datetime.now(timezone.utc).isoformat()
            if receipt_uuid:
                self._complete_refund_adjustment(adjustment, receipt_uuid)
                return "adjusted"
            adjustment["error"] = self.nalog.last_error or (
                "чек на остаток не найден; повторная запись заблокирована до сверки"
            )
            self.save_state()
            return "manual"

        if status == "ready":
            adjustment["status"] = "cancelling"
            self.save_state()
            success = await self.nalog.cancel_income(adjustment["receipt_uuid"])
            if not success:
                adjustment["error"] = self.nalog.last_error
                if getattr(self.nalog, "last_operation_uncertain", True):
                    adjustment["status"] = "cancellation_unknown"
                elif getattr(self.nalog, "last_error_retryable", False):
                    adjustment["status"] = "ready"
                else:
                    adjustment["status"] = "cancellation_rejected"
                self.save_state()
                return "manual"
            adjustment["status"] = "cancelled"
            self.save_state()
            status = "cancelled"

        if status == "cancelled" and remaining_amount == 0:
            self._complete_refund_adjustment(adjustment, None)
            return "cancelled"

        if status == "cancelled":
            adjustment["status"] = "creating_replacement"
            self.save_state()
            receipt_uuid = await self.nalog.add_income(
                adjustment["replacement_description"],
                remaining_amount,
                payment_date,
            )
            write_uncertain = getattr(
                self.nalog, "last_operation_uncertain", True
            )
            write_retryable = getattr(
                self.nalog, "last_error_retryable", False
            )
            write_error = self.nalog.last_error
            if not receipt_uuid:
                receipt_uuid = await self.nalog.find_income(
                    adjustment["replacement_description"],
                    remaining_amount,
                    payment_date,
                )
            if receipt_uuid:
                self._complete_refund_adjustment(adjustment, receipt_uuid)
                return "adjusted"
            adjustment["error"] = write_error or self.nalog.last_error
            if write_uncertain:
                adjustment["status"] = "replacement_unknown"
            elif write_retryable:
                adjustment["status"] = "cancelled"
            else:
                adjustment["status"] = "replacement_rejected"
            self.save_state()
            return "manual"

        if status == "creating_replacement":
            receipt_uuid = await self.nalog.find_income(
                adjustment["replacement_description"],
                remaining_amount,
                payment_date,
            )
            if receipt_uuid:
                self._complete_refund_adjustment(adjustment, receipt_uuid)
                return "adjusted"
            adjustment["status"] = "replacement_unknown"
            self.save_state()

        return "manual"

    def _complete_refund_adjustment(self, adjustment, replacement_receipt_uuid):
        refund_id = adjustment["refund_id"]
        payment_id = adjustment["payment_id"]
        remaining_amount = Decimal(adjustment["remaining_amount"])

        if refund_id not in self.state["processed_refunds"]:
            self.state["processed_refunds"].append(refund_id)
        self.state["refund_event_times"][refund_id] = adjustment["created_at"]
        if replacement_receipt_uuid:
            self.state["receipt_map"][payment_id] = replacement_receipt_uuid
            self.state["payment_balances"][payment_id] = str(remaining_amount)
            self.state["payment_event_times"][payment_id] = adjustment["created_at"]
        else:
            self.state["receipt_map"].pop(payment_id, None)
            self.state["payment_balances"].pop(payment_id, None)
        self.state["pending_refunds"] = [
            item for item in self.state["pending_refunds"]
            if item.get("refund_id") != refund_id
        ]
        self.save_state()
        if replacement_receipt_uuid:
            logging.info(
                "✓ Частичный возврат обработан: возврат=%s, платёж=%s, "
                "сумма=%s руб., исходный чек=%s аннулирован, остаток=%s руб., "
                "новый чек=%s.",
                refund_id,
                payment_id,
                adjustment["refund_amount"],
                adjustment.get("receipt_uuid") or "неизвестен",
                adjustment["remaining_amount"],
                replacement_receipt_uuid,
            )
        else:
            logging.info(
                "✓ Полный возврат обработан: возврат=%s, платёж=%s, "
                "сумма=%s руб., чек=%s аннулирован.",
                refund_id,
                payment_id,
                adjustment["refund_amount"],
                adjustment.get("receipt_uuid") or "неизвестен",
            )

    def _prune_processed_history(self):
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=config.STATE_RETENTION_DAYS
        )
        payment_checkpoint = _parse_timestamp(self.state.get("last_sync_time"))
        refund_checkpoint = _parse_timestamp(
            self.state.get("last_refund_sync_time")
        )
        changed = False

        removable_payments = {
            payment_id
            for payment_id, created_at in self.state["payment_event_times"].items()
            if (
                (event_time := _parse_timestamp(created_at)) is not None
                and payment_checkpoint is not None
                and event_time < cutoff
                and event_time < payment_checkpoint
            )
        }
        if removable_payments:
            self.state["processed_payments"] = [
                payment_id for payment_id in self.state["processed_payments"]
                if payment_id not in removable_payments
            ]
            for payment_id in removable_payments:
                self.state["payment_event_times"].pop(payment_id, None)
                self.state["receipt_map"].pop(payment_id, None)
                self.state["payment_balances"].pop(payment_id, None)
            self.state["skipped_payments"] = [
                item for item in self.state.get("skipped_payments", [])
                if item.get("payment_id") not in removable_payments
            ]
            self.state["receipt_deliveries"] = [
                item for item in self.state.get("receipt_deliveries", [])
                if (
                    item.get("payment_id") not in removable_payments
                    or item.get("status") == "pending"
                )
            ]
            changed = True

        removable_refunds = {
            refund_id
            for refund_id, created_at in self.state["refund_event_times"].items()
            if (
                (event_time := _parse_timestamp(created_at)) is not None
                and refund_checkpoint is not None
                and event_time < cutoff
                and event_time < refund_checkpoint
            )
        }
        if removable_refunds:
            self.state["processed_refunds"] = [
                refund_id for refund_id in self.state["processed_refunds"]
                if refund_id not in removable_refunds
            ]
            for refund_id in removable_refunds:
                self.state["refund_event_times"].pop(refund_id, None)
            changed = True

        expired_before = len(self.state.get("expired_unpaid_payments", []))
        self.state["expired_unpaid_payments"] = [
            item for item in self.state.get("expired_unpaid_payments", [])
            if not (
                (expired_at := _parse_timestamp(item.get("expired_at")))
                and expired_at < cutoff
            )
        ]
        if len(self.state["expired_unpaid_payments"]) != expired_before:
            changed = True

        if changed:
            self.save_state()
            logging.info(
                f"Очищена история state: платежей={len(removable_payments)}, "
                f"возвратов={len(removable_refunds)}"
            )

    async def _resume_pending_refunds(self):
        results = {"adjusted": 0, "cancelled": 0, "manual": 0}
        if not config.REFUNDS_ENABLED:
            return results
        for adjustment in list(self.state.get("pending_refunds", [])):
            try:
                result = await self._resume_refund_adjustment(adjustment)
            except Exception as e:
                adjustment["status"] = "manual_error"
                adjustment["error"] = f"[{type(e).__name__}] {str(e)[:160]}"
                self.save_state()
                logging.error(
                    f"Ошибка восстановления возврата "
                    f"{adjustment.get('refund_id')}: {e}"
                )
                result = "manual"
            results[result] += 1
        return results

    async def sync(self):
        state_store = getattr(self, "state_store", None)
        if state_store:
            state_store.acquire_lock()

        try:
            await self._sync_locked()
        finally:
            if state_store:
                state_store.release_lock()

    async def _sync_locked(self):
        sync_ok = True
        write_status(DATA_DIR, "running")
        logging.info("="*60)
        logging.info("Начало синхронизации...")
        logging.info(f"Последняя синхронизация: {self.state.get('last_sync_time')}")

        self.check_for_updates()

        try:
            resumed_payments, _ = await self._resume_pending_payments()

            watched_payments, watched_failures = await self._resume_watched_payments()
            if watched_failures:
                sync_ok = False

            pending = self.state.get("pending_payments", [])
            if pending:
                sync_ok = False
                automatic = sum(
                    1 for item in pending
                    if isinstance(item, dict) and item.get("status") == "ready"
                )
                manual = len(pending) - automatic
                if automatic:
                    logging.warning(
                        f"⚠ Платежей, ожидающих автоматического повтора: {automatic}"
                    )
                if manual:
                    logging.warning(
                        f"⚠ Платежей, требующих ручной сверки: {manual}"
                    )
                    self._emit("on_pending_found", manual)

            (
                new_payments,
                payments_error,
                payment_scan_checkpoint,
            ) = await self.get_new_yookassa_payments()

            if payments_error:
                sync_ok = False
                logging.warning(f"⚠ Ошибка получения платежей из ЮКассы: {payments_error}")
                self._emit("on_yookassa_error", f"ЮKassa (платежи): {payments_error}")

            if not new_payments:
                if not payments_error:
                    logging.info("✓ Новых платежей не найдено.")
            else:
                logging.info(f"✓ Найдено новых платежей: {len(new_payments)}")
                self._emit("on_sync_start", len(new_payments))

            for amount in resumed_payments + watched_payments:
                self._emit("on_payment_success", amount)

            successful = 0
            failed = 0
            skipped = 0

            for payment in new_payments:
                try:
                    workflow = self._prepare_payment_workflow(payment)
                    result, amount = await self._resume_payment_workflow(workflow)
                    if result == "completed":
                        successful += 1
                        self._emit("on_payment_success", amount)
                    elif result == "skipped":
                        skipped += 1
                        reason = (
                            f"валюта {workflow.get('currency') or 'не указана'} "
                            "не поддерживается"
                        )
                        logging.warning(f"Платёж {payment.id} пропущен: {reason}.")
                        self._emit("on_payment_error", payment.id, reason)
                    else:
                        sync_ok = False
                        failed += 1
                        if workflow["status"] == "ready":
                            logging.warning(
                                f"Платёж {payment.id}: ФНС временно недоступна; "
                                "повтор будет выполнен при следующей синхронизации."
                            )
                        else:
                            logging.warning(
                                f"Платёж {payment.id} остановлен в фазе "
                                f"{workflow['status']} и требует ручной сверки."
                            )
                        reason = self.nalog.last_error or workflow["status"]
                        self._emit(
                            "on_payment_error",
                            payment.id,
                            f"Мой Налог: {reason}",
                        )
                except Exception as e:
                    sync_ok = False
                    failed += 1
                    logging.error(f"Ошибка при обработке платежа {payment.id}: {e}")
                    self._emit("on_payment_error", payment.id, str(e)[:80])

            if new_payments:
                logging.info(
                    f"Результат платежей: успешно={successful}, "
                    f"пропущено={skipped}, ошибок={failed}"
                )
                if payments_error or failed:
                    logging.warning(
                        "Checkpoint платежей не обновлён: следующий запуск повторно "
                        "проверит незавершённый диапазон."
                    )
            if payment_scan_checkpoint and not payments_error and failed == 0:
                self.state["last_sync_time"] = payment_scan_checkpoint
                self.save_state()

            resumed = await self._resume_pending_refunds()
            if resumed["adjusted"]:
                for _ in range(resumed["adjusted"]):
                    self._emit("on_refund_adjusted")
            if resumed["cancelled"]:
                for _ in range(resumed["cancelled"]):
                    self._emit("on_refund_cancelled")

            pending_refunds = self.state.get("pending_refunds", [])
            if pending_refunds:
                sync_ok = False
                automatic = sum(
                    1 for item in pending_refunds
                    if item.get("status") in ("ready", "cancelled")
                )
                manual = len(pending_refunds) - automatic
                if automatic:
                    logging.warning(
                        f"⚠ Возвратов, ожидающих автоматического повтора: {automatic}"
                    )
                if manual:
                    logging.warning(
                        f"⚠ Возвратов, требующих ручной сверки: {manual}"
                    )
                    self._emit("on_pending_refunds_found", manual)

            new_refunds, refunds_error, refund_scan_checkpoint = (
                await self.get_new_refunds()
            )

            if refunds_error:
                sync_ok = False
                logging.warning(f"⚠ Ошибка получения возвратов из ЮКассы: {refunds_error}")
                self._emit("on_yookassa_error", f"ЮKassa (возвраты): {refunds_error}")

            if new_refunds:
                logging.info(f"✓ Найдено новых возвратов: {len(new_refunds)}")

                cancelled = 0
                adjusted = 0
                cancel_failed = 0

                for refund in new_refunds:
                    try:
                        payment, payment_error = await self.get_yookassa_payment(
                            refund.payment_id
                        )
                        if payment_error:
                            sync_ok = False
                            cancel_failed += 1
                            logging.warning(
                                f"Возврат {refund.id}: не удалось получить исходный "
                                f"платёж {refund.payment_id}: {payment_error}"
                            )
                            self._emit("on_refund_error")
                            continue

                        try:
                            refund_amount = Decimal(str(refund.amount.value))
                            payment_amount = Decimal(str(payment.amount.value))
                        except (InvalidOperation, AttributeError, TypeError) as e:
                            raise ValueError(
                                f"некорректная сумма возврата или платежа: {e}"
                            ) from e

                        refund_currency = getattr(refund.amount, "currency", None)
                        payment_currency = getattr(payment.amount, "currency", None)
                        if (
                            refund_currency
                            and payment_currency
                            and refund_currency != payment_currency
                        ):
                            raise ValueError(
                                f"валюта возврата {refund_currency} не совпадает с "
                                f"валютой платежа {payment_currency}"
                            )

                        current_amount = Decimal(
                            self.state["payment_balances"].get(
                                refund.payment_id,
                                str(payment_amount),
                            )
                        )

                        if refund_amount <= 0 or refund_amount > current_amount:
                            raise ValueError(
                                f"сумма возврата {refund_amount} вне допустимого "
                                f"диапазона для остатка платежа {current_amount}"
                            )

                        prior_adjustment = next(
                            (
                                item for item in self.state["pending_refunds"]
                                if item.get("payment_id") == refund.payment_id
                            ),
                            None,
                        )
                        if prior_adjustment:
                            sync_ok = False
                            adjustment = self._prepare_refund_adjustment(
                                refund,
                                payment,
                                current_amount,
                            )
                            adjustment["status"] = "blocked_by_prior_adjustment"
                            adjustment["blocked_by"] = prior_adjustment["refund_id"]
                            self.save_state()
                            cancel_failed += 1
                            logging.warning(
                                f"Возврат {refund.id} ожидает ручной сверки "
                                f"предыдущего возврата {prior_adjustment['refund_id']}."
                            )
                            self._emit("on_refund_error")
                            continue

                        adjustment = self._prepare_refund_adjustment(
                            refund,
                            payment,
                            current_amount,
                        )
                        result = await self._resume_refund_adjustment(adjustment)

                        if result == "cancelled":
                            cancelled += 1
                            self._emit("on_refund_cancelled")
                        elif result == "adjusted":
                            adjusted += 1
                            self._emit("on_refund_adjusted")
                        else:
                            sync_ok = False
                            cancel_failed += 1
                            if adjustment["status"] in ("ready", "cancelled"):
                                logging.warning(
                                    f"Возврат {refund.id}: ФНС временно недоступна; "
                                    "повтор будет выполнен при следующей синхронизации."
                                )
                            else:
                                logging.warning(
                                    f"Возврат {refund.id} остановлен в фазе "
                                    f"{adjustment['status']} и требует ручной сверки."
                                )
                            self._emit("on_refund_error")
                    except Exception as e:
                        sync_ok = False
                        cancel_failed += 1
                        logging.error(f"Ошибка при обработке возврата {refund.id}: {e}")
                        self._emit("on_refund_error")

                logging.info(
                    f"Результат возвратов: аннулировано={cancelled}, "
                    f"скорректировано={adjusted}, ошибок={cancel_failed}"
                )
                if refunds_error or cancel_failed:
                    logging.warning(
                        "Checkpoint возвратов не обновлён: следующий запуск повторно "
                        "проверит незавершённый диапазон."
                    )
            else:
                if not refunds_error:
                    if config.REFUNDS_ENABLED:
                        logging.info("✓ Новых возвратов не найдено.")
                    else:
                        logging.info(
                            "Обработка возвратов отключена "
                            "(REFUNDS_ENABLED=false)."
                        )

            if (
                config.REFUNDS_ENABLED
                and refund_scan_checkpoint
                and not refunds_error
                and (not new_refunds or cancel_failed == 0)
            ):
                self.state["last_refund_sync_time"] = refund_scan_checkpoint
                self.save_state()

        except Exception as e:
            sync_ok = False
            logging.error(f"Критическая ошибка при синхронизации: {e}", exc_info=True)
        finally:
            try:
                delivery_counts = await self._process_customer_receipt_deliveries()
                if delivery_counts["pending"] or delivery_counts["undeliverable"]:
                    sync_ok = False
            except Exception as e:
                sync_ok = False
                logging.error(f"Не удалось обработать доставку чеков: {e}")
            try:
                self._prune_processed_history()
            except Exception as e:
                sync_ok = False
                logging.error(f"Не удалось очистить историю state: {e}")
            write_status(
                DATA_DIR,
                "ok" if sync_ok else "degraded",
                pending_payments=len(self.state.get("pending_payments", [])),
                watched_payments=len(self.state.get("watched_payments", [])),
                pending_refunds=len(self.state.get("pending_refunds", [])),
                pending_receipt_deliveries=sum(
                    1 for item in self.state.get("receipt_deliveries", [])
                    if item.get("status") == "pending"
                ),
                undeliverable_receipts=sum(
                    1 for item in self.state.get("receipt_deliveries", [])
                    if item.get("status") == "undeliverable"
                ),
            )
            await self.nalog.close()
            if self.notifier:
                await self.notifier.send_summary()
            if getattr(self, "receipt_notifier", None):
                await self.receipt_notifier.send_summary()
            if self.email_notifier:
                await self.email_notifier.send_summary()
            logging.info("Синхронизация завершена.")
            logging.info("="*60)


def _latest_created_at(items):
    return max(
        items,
        key=lambda item: datetime.fromisoformat(item.created_at.replace('Z', '+00:00')),
    ).created_at


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_version(v: str) -> tuple:
    parts = []
    for chunk in v.strip().lstrip("vV").split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def print_banner():
    bar = "━" * 48
    title = f"🧾  YooKassa → Мой Налог  v{__version__}"
    telegram_on = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
    email_on = bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD and config.SMTP_TO_EMAIL)
    telegram_status = colorize("✓ включён", "green") if telegram_on else colorize("· выключен", "gray")
    email_status = colorize("✓ включён", "green") if email_on else colorize("· выключен", "gray")
    customer_receipts_status = (
        colorize("✓ включена", "green")
        if config.TELEGRAM_CUSTOMER_RECEIPTS_ENABLED
        else colorize("· выключена", "gray")
    )

    rows = [
        ("Часовой пояс", config.TZ or "—"),
        ("Авторизация", config.MOY_NALOG_AUTH_METHOD),
        ("Расписание", config.CRON_SCHEDULE),
        ("Повторы ФНС", config.FNS_RETRY_SCHEDULE),
        (
            "Возвраты",
            colorize("✓ включены", "green")
            if config.REFUNDS_ENABLED
            else colorize("· выключены", "gray"),
        ),
        ("Ожидание оплаты", f"{config.PENDING_PAYMENT_WATCH_MINUTES} мин."),
        (
            "Лимит очереди",
            str(config.FNS_QUEUE_MAX_ATTEMPTS)
            if config.FNS_QUEUE_MAX_ATTEMPTS
            else "без ограничений",
        ),
        ("Telegram", telegram_status),
        ("Доставка чеков", customer_receipts_status),
        ("Email", email_status),
    ]

    print(colorize(bar, "cyan"))
    print("  " + colorize(title, "bold"))
    print(colorize(bar, "cyan"))
    for label, value in rows:
        print(f"  {label:<14} {value}")
    print(colorize(bar, "cyan"))


async def main(retry_fns_only=False):
    manager = None
    try:
        print_banner()
        manager = SyncManager()
        if retry_fns_only:
            await manager.retry_fns_queue()
        else:
            await manager.startup_notify()
            await manager.sync()
    except ConcurrentRunError:
        job = "обработка очереди ФНС" if retry_fns_only else "основная синхронизация"
        logging.info(
            "%s пропущена: другой процесс синхронизации уже работает.",
            job.capitalize(),
        )
        if manager is not None:
            await manager.nalog.close()
    except Exception as e:
        logging.critical(f"Критическая ошибка: {e}", exc_info=True)
        exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--retry-fns-only",
        action="store_true",
        help="обработать сохранённую очередь ФНС без запроса списка ЮKassa",
    )
    args = parser.parse_args()
    asyncio.run(main(retry_fns_only=args.retry_fns_only))
