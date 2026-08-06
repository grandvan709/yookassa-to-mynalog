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
from state_store import StateStore
from health_state import write_status

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

        self.event_notifiers = [n for n in (self.notifier, self.email_notifier) if n]

    def _emit(self, method, *args):
        for n in self.event_notifiers:
            if n is self.notifier and not self._telegram_event_enabled(method):
                continue
            getattr(n, method)(*args)

    def _telegram_event_enabled(self, method):
        preferences = self.state.get("notification_preferences", {})
        if method in {
            "on_payment_success",
            "on_payment_verified",
            "on_refund_cancelled",
            "on_refund_adjusted",
        }:
            return preferences.get("receipt_success", True)
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
            "receipt_map": {},
            "processed_refunds": [],
            "pending_refunds": [],
            "payment_balances": {},
            "payment_event_times": {},
            "refund_event_times": {},
            "last_refund_sync_time": None,
            "notification_preferences": {
                "receipt_success": True,
                "receipt_errors": True,
            },
        }
        for key, default in defaults.items():
            if key not in state:
                state[key] = default
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
            "receipt_map": {},
            "processed_refunds": [],
            "pending_refunds": [],
            "payment_balances": {},
            "payment_event_times": {},
            "refund_event_times": {},
            "last_refund_sync_time": None,
            "notification_preferences": {
                "receipt_success": True,
                "receipt_errors": True,
            },
        }
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
        pending_ids = {
            item if isinstance(item, str) else item.get("payment_id")
            for item in self.state["pending_payments"]
        }
        pending_ids.discard(None)
        skip_ids = set(self.state["processed_payments"]) | pending_ids

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
            return new_refunds, "Таймаут API ЮКассы (>120s)"
        except Exception as e:
            err_type = type(e).__name__
            err_text = str(e) or "нет деталей"
            logging.error(f"Ошибка получения возвратов ЮKassa: [{err_type}] {err_text}")
            return new_refunds, f"[{err_type}] {err_text}"

        return new_refunds, None

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
            description = f"{description} [yookassa:{payment.id}]"

        workflow = {
            "payment_id": payment.id,
            "amount": str(amount),
            "currency": currency,
            "created_at": payment.created_at,
            "description": description,
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

        receipt_uuid = await self.nalog.find_income(
            workflow["description"],
            amount,
            payment_date,
        )
        if receipt_uuid:
            self._complete_payment_workflow(workflow, receipt_uuid)
            return "completed", amount

        workflow["error"] = write_error or self.nalog.last_error
        workflow["last_error_retryable"] = bool(
            write_retryable or getattr(self.nalog, "last_error_retryable", False)
        )
        if write_uncertain:
            workflow["status"] = "unknown"
        elif write_retryable:
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

        if changed:
            self.save_state()
            logging.info(
                f"Очищена история state: платежей={len(removable_payments)}, "
                f"возвратов={len(removable_refunds)}"
            )

    async def _resume_pending_refunds(self):
        results = {"adjusted": 0, "cancelled": 0, "manual": 0}
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
            for amount in resumed_payments:
                self._emit("on_payment_success", amount)

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

            new_payments, payments_error = await self.get_new_yookassa_payments()

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

            successful = 0
            failed = 0

            for payment in new_payments:
                try:
                    workflow = self._prepare_payment_workflow(payment)
                    result, amount = await self._resume_payment_workflow(workflow)
                    if result == "completed":
                        successful += 1
                        self._emit("on_payment_success", amount)
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
                logging.info(f"Результат платежей: успешно={successful}, ошибок={failed}")
                if not payments_error and failed == 0:
                    self.state["last_sync_time"] = _latest_created_at(new_payments)
                    self.save_state()
                else:
                    logging.warning(
                        "Checkpoint платежей не обновлён: следующий запуск повторно "
                        "проверит незавершённый диапазон."
                    )

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

            new_refunds, refunds_error = await self.get_new_refunds()

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
                if not refunds_error and cancel_failed == 0:
                    self.state["last_refund_sync_time"] = _latest_created_at(new_refunds)
                    self.save_state()
                else:
                    logging.warning(
                        "Checkpoint возвратов не обновлён: следующий запуск повторно "
                        "проверит незавершённый диапазон."
                    )
            else:
                if not refunds_error:
                    logging.info("✓ Новых возвратов не найдено.")

        except Exception as e:
            sync_ok = False
            logging.error(f"Критическая ошибка при синхронизации: {e}", exc_info=True)
        finally:
            try:
                self._prune_processed_history()
            except Exception as e:
                sync_ok = False
                logging.error(f"Не удалось очистить историю state: {e}")
            write_status(
                DATA_DIR,
                "ok" if sync_ok else "degraded",
                pending_payments=len(self.state.get("pending_payments", [])),
                pending_refunds=len(self.state.get("pending_refunds", [])),
            )
            await self.nalog.close()
            if self.notifier:
                await self.notifier.send_summary()
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

    rows = [
        ("Часовой пояс", config.TZ or "—"),
        ("Авторизация", config.MOY_NALOG_AUTH_METHOD),
        ("Расписание", config.CRON_SCHEDULE),
        ("Повторы ФНС", config.FNS_RETRY_SCHEDULE),
        (
            "Лимит очереди",
            str(config.FNS_QUEUE_MAX_ATTEMPTS)
            if config.FNS_QUEUE_MAX_ATTEMPTS
            else "без ограничений",
        ),
        ("Telegram", telegram_status),
        ("Email", email_status),
    ]

    print(colorize(bar, "cyan"))
    print("  " + colorize(title, "bold"))
    print(colorize(bar, "cyan"))
    for label, value in rows:
        print(f"  {label:<14} {value}")
    print(colorize(bar, "cyan"))


async def main(retry_fns_only=False):
    try:
        print_banner()
        manager = SyncManager()
        if retry_fns_only:
            await manager.retry_fns_queue()
        else:
            await manager.startup_notify()
            await manager.sync()
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
