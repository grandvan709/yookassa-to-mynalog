import asyncio
import html
import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path

import httpx

import config
from backup import run_backup
from health_state import write_status
from logging_config import setup_logging
from state_store import ConcurrentRunError, StateStore


DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
OFFSET_PATH = DATA_DIR / "telegram_bot_offset.json"


class TelegramAdminBot:
    def __init__(self):
        if not (
            config.TELEGRAM_BOT_TOKEN
            and config.TELEGRAM_CHAT_ID
            and config.TELEGRAM_ADMIN_USER_ID
        ):
            raise ValueError(
                "Для Telegram-бота нужны TELEGRAM_BOT_TOKEN, "
                "TELEGRAM_CHAT_ID и TELEGRAM_ADMIN_USER_ID"
            )
        self.token = config.TELEGRAM_BOT_TOKEN
        self.admin_chat_id = str(config.TELEGRAM_CHAT_ID)
        self.admin_user_id = str(config.TELEGRAM_ADMIN_USER_ID)
        try:
            self.thread_id = (
                int(config.TELEGRAM_THREAD_ID)
                if config.TELEGRAM_THREAD_ID
                else None
            )
        except ValueError:
            logging.warning(
                "TELEGRAM_THREAD_ID некорректен; ответы бота пойдут в основной чат."
            )
            self.thread_id = None
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(40, connect=15),
            proxy=config.TELEGRAM_PROXY,
            trust_env=False,
        )
        self.store = StateStore(
            DATA_DIR / "sync_state.db",
            legacy_json_path=LOG_DIR / "sync_state.json",
        )
        self.offset = self._load_offset()
        self.pending_input = None
        self.pending_input_deadline = None
        self._reply_chat_id = self.admin_chat_id
        self._reply_thread_id = self.thread_id

    def _load_offset(self):
        try:
            return int(json.loads(OFFSET_PATH.read_text(encoding="utf-8"))["offset"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _save_offset(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="telegram-offset-", suffix=".tmp", dir=DATA_DIR
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"offset": self.offset}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, OFFSET_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def _api(self, method, **payload):
        response = await self.client.post(f"{self.api_url}/{method}", json=payload)
        if response.status_code != 200:
            safe = response.text.replace(self.token, "***")[:500]
            raise RuntimeError(f"Telegram API {response.status_code}: {safe}")
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram API вернул ok=false")
        return data.get("result")

    async def send(self, text, reply_markup=None):
        payload = {
            "chat_id": getattr(self, "_reply_chat_id", self.admin_chat_id),
            "text": text[:4000],
            "parse_mode": "HTML",
        }
        reply_thread_id = getattr(self, "_reply_thread_id", self.thread_id)
        if reply_thread_id:
            payload["message_thread_id"] = reply_thread_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._api("sendMessage", **payload)

    async def initialize_offset(self):
        if self.offset is not None:
            return
        updates = await self._api(
            "getUpdates",
            offset=-1,
            limit=1,
            timeout=0,
            allowed_updates=["message", "callback_query"],
        )
        self.offset = updates[-1]["update_id"] + 1 if updates else 0
        self._save_offset()

    def _state(self):
        return self.store.load() or {}

    async def update_receipt_report(self, *, toggle=False, **values):
        try:
            self.store.acquire_lock()
            state = self._state()
            report = state.setdefault("receipt_reports", {})
            if toggle:
                report["enabled"] = not report.get("enabled", True)
            report.update(values)
            self.store.save(state)
            return True
        except ConcurrentRunError:
            await self.send("Синхронизация сейчас выполняется. Повторите через минуту.")
            return False
        finally:
            self.store.release_lock()

    @staticmethod
    def _enabled(value):
        return "включены ✅" if value else "выключены ⛔"

    @staticmethod
    def _main_keyboard():
        return {
            "keyboard": [
                ["📊 Статус", "📋 Очередь"],
                ["🔔 Уведомления", "📣 Отчёты о чеках"],
                ["💾 Резервные копии", "📄 Логи"],
                ["ℹ️ Помощь"],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    async def show_main_menu(self):
        await self.send(
            "🛠 <b>Управление YooKassa → Мой Налог</b>\n\n"
            "Выберите нужный раздел кнопкой внизу.",
            reply_markup=self._main_keyboard(),
        )

    async def show_notifications(self):
        preferences = self._state().get("notification_preferences", {})
        errors = preferences.get("receipt_errors", True)
        await self.send(
            "🔔 <b>Уведомления</b>\n\n"
            f"Ошибки регистрации: <b>{self._enabled(errors)}</b>\n\n"
            "Отчёты об успешных чеках настраиваются в отдельном разделе.",
            reply_markup={
                "inline_keyboard": [
                    [{
                        "text": f"Ошибки регистрации: {'ВКЛ ✅' if errors else 'ВЫКЛ ⛔'}",
                        "callback_data": "notify:errors:toggle",
                    }],
                    [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
                ]
            },
        )

    async def show_receipt_reports(self):
        report = self._state().get("receipt_reports", {})
        enabled = report.get("enabled", True)
        chat_id = report.get("chat_id") or "не задан"
        thread_id = report.get("thread_id") or "основной чат"
        await self.send(
            "📣 <b>Отчёты о зарегистрированных чеках</b>\n\n"
            f"Отправка: <b>{self._enabled(enabled)}</b>\n"
            f"Чат: <code>{html.escape(str(chat_id))}</code>\n"
            f"Тема: <code>{html.escape(str(thread_id))}</code>\n\n"
            "Бот должен быть добавлен в выбранную группу и иметь право отправлять сообщения.",
            reply_markup={"inline_keyboard": [
                [{
                    "text": f"Отчёты: {'ВКЛ ✅' if enabled else 'ВЫКЛ ⛔'}",
                    "callback_data": "report:toggle",
                }],
                [{"text": "📍 Использовать этот чат и тему", "callback_data": "report:here"}],
                [{"text": "✏️ Ввести ID чата", "callback_data": "report:chat:input"}],
                [{"text": "✏️ Ввести ID темы", "callback_data": "report:thread:input"}],
                [{"text": "🧹 Без отдельной темы", "callback_data": "report:thread:clear"}],
                [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
            ]},
        )

    async def show_queue(self):
        state = self._state()
        rows = []
        for item in state.get("pending_payments", []):
            if isinstance(item, dict):
                rows.append([{"text": f"💳 {item.get('amount', '?')} ₽ · {item.get('status', '?')}",
                              "callback_data": f"queue:p:{item.get('payment_id')}"}])
        for item in state.get("pending_refunds", []):
            rows.append([{"text": f"↩️ {item.get('refund_amount', '?')} ₽ · {item.get('status', '?')}",
                          "callback_data": f"queue:r:{item.get('refund_id')}"}])
        if not rows:
            await self.send("📋 Очередь пуста.", reply_markup={"inline_keyboard": [
                [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}]
            ]})
            return
        rows = rows[:20]
        rows.append([{"text": "🔄 Обновить", "callback_data": "menu:queue"}])
        rows.append([{"text": "⬅️ Главное меню", "callback_data": "menu:main"}])
        await self.send("📋 <b>Очередь ФНС</b>\n\nВыберите операцию для подробностей.",
                        reply_markup={"inline_keyboard": rows})

    async def show_queue_item(self, kind, item_id):
        state = self._state()
        key, id_key = ("pending_payments", "payment_id") if kind == "p" else ("pending_refunds", "refund_id")
        item = next((x for x in state.get(key, []) if isinstance(x, dict) and x.get(id_key) == item_id), None)
        if not item:
            await self.send("Элемент уже обработан или удалён из очереди.")
            return
        amount = item.get("amount") if kind == "p" else item.get("refund_amount")
        status = item.get("status", "unknown")
        automatic_statuses = {"ready", "cancelled"}
        mode = "автоматический повтор" if status in automatic_statuses else "нужна ручная проверка"
        lines = [
            "💳 <b>Платёж</b>" if kind == "p" else "↩️ <b>Возврат</b>",
            f"ID: <code>{html.escape(str(item_id))}</code>",
            f"Сумма: <b>{html.escape(str(amount))} ₽</b>",
            f"Дата: <code>{html.escape(str(item.get('created_at', '—')))}</code>",
            f"Статус: <code>{html.escape(status)}</code>",
            f"Режим: {mode}",
            f"Попыток: {item.get('queue_attempts', item.get('attempts', 0))}",
            f"Последняя попытка: <code>{html.escape(str(item.get('last_attempt_at', '—')))}</code>",
            f"Ошибка: <code>{html.escape(str(item.get('error', '—'))[:700])}</code>",
        ]
        buttons = []
        retryable = kind == "p" or status in {
            "cancellation_unknown", "cancellation_rejected",
            "replacement_unknown", "replacement_rejected", "creating_replacement",
        }
        if retryable:
            if kind == "p":
                label = "🔁 Я проверил: чека нет — повторить"
            elif status.startswith("cancellation"):
                label = "🔁 Исходный чек активен — аннулировать"
            else:
                label = "🔁 Чека на остаток нет — создать"
            buttons.append([{"text": label, "callback_data": f"retry:ask:{kind}:{item_id}"}])
        buttons.extend([
            [{"text": "⬅️ К очереди", "callback_data": "menu:queue"}],
            [{"text": "🏠 Главное меню", "callback_data": "menu:main"}],
        ])
        await self.send("\n".join(lines), reply_markup={"inline_keyboard": buttons})

    async def retry_queue_item(self, kind, item_id):
        try:
            self.store.acquire_lock()
            state = self._state()
            key, id_key = ("pending_payments", "payment_id") if kind == "p" else ("pending_refunds", "refund_id")
            item = next((x for x in state.get(key, []) if isinstance(x, dict) and x.get(id_key) == item_id), None)
            if not item:
                await self.send("Элемент уже отсутствует в очереди.")
                return
            if kind == "p":
                if item.get("currency") != "RUB":
                    await self.send("Повтор запрещён: валюта платежа не поддерживается.")
                    return
                item["status"] = "ready"
                item["queue_attempts"] = 0
                item.pop("last_notified_error", None)
            elif item.get("status") in {"cancellation_unknown", "cancellation_rejected"}:
                item["status"] = "ready"
            elif item.get("status") in {"replacement_unknown", "replacement_rejected", "creating_replacement"}:
                item["status"] = "cancelled"
            else:
                await self.send("Из этого статуса безопасный ручной повтор запрещён.")
                return
            item.pop("error", None)
            self.store.save(state)
        except ConcurrentRunError:
            await self.send("Синхронизация сейчас выполняется. Повторите через минуту.")
            return
        finally:
            self.store.release_lock()
        await self.send("✅ Операция возвращена в очередь с безопасной фазы.")

    async def show_backups_menu(self):
        await self.send(
            "💾 <b>Резервные копии</b>\n\n"
            "Создание использует канал из <code>BACKUP_TARGET</code>. "
            "Удаление и восстановление доступны только вручную на сервере.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "➕ Создать backup", "callback_data": "backup:confirm"}],
                    [{"text": "📚 Список копий", "callback_data": "backup:list"}],
                    [{"text": "🔄 Обновить статус", "callback_data": "status:refresh"}],
                    [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
                ]
            },
        )

    async def show_logs_menu(self):
        await self.send(
            "📄 <b>Просмотр журналов</b>\n\nВыберите журнал и количество строк.",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Sync · 20", "callback_data": "logs:sync:20"},
                        {"text": "Sync · 50", "callback_data": "logs:sync:50"},
                    ],
                    [{"text": "Backup · 20", "callback_data": "logs:backup:20"}],
                    [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
                ]
            },
        )

    async def command_status(self):
        state = self._state()
        prefs = state.get("notification_preferences", {})
        report = state.get("receipt_reports", {})
        pending_payments = state.get("pending_payments", [])
        pending_refunds = state.get("pending_refunds", [])
        ready = sum(
            1 for item in pending_payments
            if isinstance(item, dict) and item.get("status") == "ready"
        )
        manual = len(pending_payments) - ready
        maximum = config.FNS_QUEUE_MAX_ATTEMPTS
        limit_text = str(maximum) if maximum else "без ограничений"
        backup_text = "отключены"
        backup_status_path = DATA_DIR / "backup_status.json"
        if config.BACKUP_TARGET:
            backup_text = f"включены, канал: {config.BACKUP_TARGET}"
            try:
                backup_status = json.loads(
                    backup_status_path.read_text(encoding="utf-8")
                )
                backup_text += f", статус: {backup_status.get('status', 'unknown')}"
            except (OSError, ValueError, json.JSONDecodeError):
                backup_text += ", успешных запусков ещё нет"
        await self.send(
            "📊 <b>Состояние сервиса</b>\n\n"
            f"Платежей в очереди: <b>{len(pending_payments)}</b>\n"
            f"— автоматический повтор: <b>{ready}</b>\n"
            f"— ручная проверка: <b>{manual}</b>\n"
            f"Возвратов в обработке: <b>{len(pending_refunds)}</b>\n"
            f"Лимит повторов: <b>{limit_text}</b>\n\n"
            f"Резервные копии: <b>{html.escape(backup_text)}</b>\n\n"
            "Отчёты об успешных чеках: "
            f"<b>{self._enabled(report.get('enabled', True))}</b>\n"
            "Ошибки регистрации: "
            f"<b>{self._enabled(prefs.get('receipt_errors', True))}</b>",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Обновить", "callback_data": "status:refresh"}],
                    [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
                ]
            },
        )

    async def command_logs(self, argument):
        parts = argument.split()
        source = "sync"
        if parts and parts[0].lower() in ("sync", "backup"):
            source = parts.pop(0).lower()
        try:
            count = max(1, min(100, int(parts[0] if parts else "20")))
        except ValueError:
            await self.send(
                "Использование: <code>/logs 20</code> или "
                "<code>/logs backup 20</code> (от 1 до 100 строк)"
            )
            return
        path = LOG_DIR / f"{source}.log"
        if not path.is_file():
            await self.send("Файл <code>logs/sync.log</code> ещё не создан.")
            return
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            lines = list(deque(stream, maxlen=count))
        text = "".join(lines)[-3500:]
        text = re.sub(r"live_[A-Za-z0-9_-]{12,}", "live_***", text)
        text = text.replace(self.token, "***")
        await self.send(
            f"📄 <b>Последние строки {source}.log</b>\n"
            f"<pre>{html.escape(text)}</pre>"
        )

    async def command_backups(self):
        backup_dir = DATA_DIR / "backups"
        files = sorted(backup_dir.glob("*.ynbackup"), reverse=True)[:10]
        if not files:
            await self.send("Локальных резервных копий пока нет.")
            return
        lines = ["🗄 <b>Последние резервные копии</b>", ""]
        for path in files:
            size = path.stat().st_size / 1024 / 1024
            lines.append(f"• <code>{html.escape(path.name)}</code> — {size:.1f} МБ")
        await self.send("\n".join(lines))

    async def command_backup(self):
        await self.send("⏳ Создаю и отправляю резервную копию…")
        try:
            path = await asyncio.to_thread(run_backup)
        except Exception as exc:
            await self.send(
                "❌ <b>Не удалось создать резервную копию</b>\n"
                f"<code>{html.escape(type(exc).__name__ + ': ' + str(exc)[:500])}</code>"
            )
            return
        await self.send(f"✅ Резервная копия создана: <code>{html.escape(path.name)}</code>")

    async def command_notifications(self, arguments):
        parts = arguments.lower().split()
        if not parts:
            await self.show_notifications()
            return
        if len(parts) != 2 or parts[0] != "errors" or parts[1] not in ("on", "off"):
            await self.send("Некорректная команда. Используйте <code>/notifications</code> для справки.")
            return
        key = "receipt_errors"
        value = parts[1] == "on"
        try:
            self.store.acquire_lock()
            state = self._state()
            state.setdefault("notification_preferences", {})[key] = value
            self.store.save(state)
        except ConcurrentRunError:
            await self.send("Синхронизация сейчас выполняется. Повторите команду через минуту.")
            return
        finally:
            self.store.release_lock()
        await self.show_notifications()

    async def handle_callback(self, callback):
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))
        callback_id = callback.get("id")
        if user_id != self.admin_user_id:
            logging.warning(
                "Отклонено неразрешённое нажатие Telegram: chat=%s user=%s",
                chat_id,
                user_id,
            )
            if callback_id:
                await self._api(
                    "answerCallbackQuery",
                    callback_query_id=callback_id,
                    text="Недостаточно прав",
                    show_alert=True,
                )
            return

        self._reply_chat_id = chat_id
        self._reply_thread_id = message.get("message_thread_id")

        if callback_id:
            await self._api("answerCallbackQuery", callback_query_id=callback_id)
        action = callback.get("data") or ""
        if action not in ("report:chat:input", "report:thread:input"):
            self.pending_input = None
            self.pending_input_deadline = None
        if action == "menu:main":
            await self.show_main_menu()
        elif action == "menu:notifications":
            await self.show_notifications()
        elif action == "menu:reports":
            await self.show_receipt_reports()
        elif action == "menu:queue":
            await self.show_queue()
        elif action == "menu:backups":
            await self.show_backups_menu()
        elif action == "menu:logs":
            await self.show_logs_menu()
        elif action == "status:refresh":
            await self.command_status()
        elif action == "backup:create":
            await self.command_backup()
        elif action == "backup:confirm":
            await self.send(
                "Создать новую резервную копию и отправить её настроенным каналом?",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "✅ Да, создать", "callback_data": "backup:create"},
                            {"text": "Отмена", "callback_data": "menu:backups"},
                        ]
                    ]
                },
            )
        elif action == "backup:list":
            await self.command_backups()
        elif action.startswith("logs:"):
            _, source, count = action.split(":", 2)
            await self.command_logs(f"{source} {count}")
        elif action.startswith("notify:"):
            _, kind, operation = action.split(":", 2)
            if operation != "toggle" or kind != "errors":
                await self.send("Неизвестное действие кнопки.")
                return
            preferences = self._state().get("notification_preferences", {})
            key = "receipt_errors"
            new_value = not preferences.get(key, True)
            await self.command_notifications(
                f"{kind} {'on' if new_value else 'off'}"
            )
        elif action == "report:toggle":
            if await self.update_receipt_report(toggle=True):
                await self.show_receipt_reports()
        elif action == "report:here":
            if await self.update_receipt_report(
                chat_id=chat_id, thread_id=message.get("message_thread_id")
            ):
                await self.show_receipt_reports()
        elif action == "report:chat:input":
            self.pending_input = "report_chat_id"
            self.pending_input_deadline = time.monotonic() + 300
            await self.send("Отправьте числовой ID группы, например <code>-1001234567890</code>.")
        elif action == "report:thread:input":
            self.pending_input = "report_thread_id"
            self.pending_input_deadline = time.monotonic() + 300
            await self.send("Отправьте числовой ID темы. Его можно получить кнопкой «Использовать этот чат и тему» внутри нужной темы.")
        elif action == "report:thread:clear":
            if await self.update_receipt_report(thread_id=None):
                await self.show_receipt_reports()
        elif action.startswith("queue:"):
            _, kind, item_id = action.split(":", 2)
            await self.show_queue_item(kind, item_id)
        elif action.startswith("retry:ask:"):
            _, _, kind, item_id = action.split(":", 3)
            state = self._state()
            key, id_key = ("pending_payments", "payment_id") if kind == "p" else ("pending_refunds", "refund_id")
            item = next((x for x in state.get(key, []) if isinstance(x, dict) and x.get(id_key) == item_id), {})
            status = item.get("status", "")
            if kind == "p":
                check_text = "Вы проверили «Мой Налог» и убедились, что чек платежа не был создан?"
                confirm_text = "✅ Да, чека нет"
            elif status.startswith("cancellation"):
                check_text = "Вы проверили «Мой Налог» и убедились, что исходный чек всё ещё активен?"
                confirm_text = "✅ Да, чек активен"
            else:
                check_text = "Вы проверили «Мой Налог» и убедились, что чек на остаток не был создан?"
                confirm_text = "✅ Да, чека нет"
            await self.send(
                "⚠️ <b>Подтвердите ручную сверку</b>\n\n"
                f"{check_text} "
                "Ошибочное подтверждение может привести к дубликату.",
                reply_markup={"inline_keyboard": [[
                    {"text": confirm_text, "callback_data": f"retry:confirm:{kind}:{item_id}"},
                    {"text": "Отмена", "callback_data": f"queue:{kind}:{item_id}"},
                ]]},
            )
        elif action.startswith("retry:confirm:"):
            _, _, kind, item_id = action.split(":", 3)
            await self.retry_queue_item(kind, item_id)
        else:
            await self.send("Эта кнопка устарела. Откройте главное меню заново.")

    async def handle(self, update):
        callback = update.get("callback_query")
        if callback:
            await self.handle_callback(callback)
            return
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))
        if user_id != self.admin_user_id:
            logging.warning(
                "Отклонена неразрешённая команда Telegram: chat=%s user=%s",
                chat_id,
                user_id,
            )
            return
        self._reply_chat_id = chat_id
        self._reply_thread_id = message.get("message_thread_id")
        text = (message.get("text") or "").strip()
        button_actions = {
            "📊 Статус": self.command_status,
            "📋 Очередь": self.show_queue,
            "🔔 Уведомления": self.show_notifications,
            "📣 Отчёты о чеках": self.show_receipt_reports,
            "💾 Резервные копии": self.show_backups_menu,
            "📄 Логи": self.show_logs_menu,
            "ℹ️ Помощь": self.show_main_menu,
        }
        deadline = getattr(self, "pending_input_deadline", None)
        if deadline and time.monotonic() >= deadline:
            self.pending_input = None
            self.pending_input_deadline = None
        if getattr(self, "pending_input", None):
            if text.startswith("/") or text in button_actions:
                self.pending_input = None
                self.pending_input_deadline = None
                if text.split("@", 1)[0].lower() == "/cancel":
                    await self.show_receipt_reports()
                    return
            else:
                try:
                    value = int(text)
                except ValueError:
                    await self.send(
                        "Нужен числовой ID. Для отмены нажмите /cancel, /start "
                        "или любую кнопку меню."
                    )
                    return
                key = "chat_id" if self.pending_input == "report_chat_id" else "thread_id"
                saved = await self.update_receipt_report(
                    **{key: str(value) if key == "chat_id" else value}
                )
                if not saved:
                    return
                self.pending_input = None
                self.pending_input_deadline = None
                await self.show_receipt_reports()
                return
        if text in button_actions:
            await button_actions[text]()
            return
        if not text.startswith("/"):
            await self.show_main_menu()
            return
        command, _, arguments = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        arguments = arguments.strip()
        if command in ("/start", "/help"):
            await self.show_main_menu()
        elif command == "/cancel":
            await self.show_receipt_reports()
        elif command == "/status":
            await self.command_status()
        elif command == "/logs":
            await self.command_logs(arguments)
        elif command == "/backup":
            await self.command_backup()
        elif command == "/backups":
            await self.command_backups()
        elif command == "/notifications":
            await self.command_notifications(arguments)
        else:
            await self.send("Неизвестная команда. Используйте <code>/help</code>.")

    async def run(self):
        await self.initialize_offset()
        await self._api(
            "setMyCommands",
            commands=[
                {"command": "status", "description": "Состояние сервиса"},
                {"command": "logs", "description": "Последние строки журнала"},
                {"command": "backup", "description": "Создать резервную копию"},
                {"command": "backups", "description": "Список резервных копий"},
                {"command": "notifications", "description": "Настроить уведомления"},
                {"command": "cancel", "description": "Отменить ввод значения"},
                {"command": "help", "description": "Список команд"},
            ],
        )
        logging.info("Telegram admin bot запущен для chat_id=%s", self.admin_chat_id)
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=self.offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
                write_status(DATA_DIR, "ok", filename="telegram_bot_status.json")
                for update in updates:
                    self.offset = update["update_id"] + 1
                    self._save_offset()
                    try:
                        await self.handle(update)
                    except Exception:
                        logging.exception("Ошибка обработки команды Telegram")
                        await self.send("Произошла внутренняя ошибка при выполнении команды.")
            except Exception as exc:
                safe = str(exc).replace(self.token, "***")[:300]
                logging.warning("Ошибка Telegram admin bot: %s", safe)
                write_status(
                    DATA_DIR,
                    "error",
                    filename="telegram_bot_status.json",
                    error=safe,
                )
                await asyncio.sleep(5)

    async def close(self):
        await self.client.aclose()


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(str(LOG_DIR))
    bot = TelegramAdminBot()
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
