import asyncio
import html
import json
import logging
import os
import re
import tempfile
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
            "chat_id": self.admin_chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
        }
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id
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

    @staticmethod
    def _enabled(value):
        return "включены ✅" if value else "выключены ⛔"

    @staticmethod
    def _main_keyboard():
        return {
            "keyboard": [
                ["📊 Статус", "🔔 Уведомления"],
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
        success = preferences.get("receipt_success", True)
        errors = preferences.get("receipt_errors", True)
        await self.send(
            "🔔 <b>Уведомления</b>\n\n"
            f"Успешные чеки: <b>{self._enabled(success)}</b>\n"
            f"Ошибки регистрации: <b>{self._enabled(errors)}</b>\n\n"
            "Нажмите кнопку, чтобы изменить настройку.",
            reply_markup={
                "inline_keyboard": [
                    [{
                        "text": f"Успешные чеки: {'ВКЛ ✅' if success else 'ВЫКЛ ⛔'}",
                        "callback_data": "notify:receipts:toggle",
                    }],
                    [{
                        "text": f"Ошибки регистрации: {'ВКЛ ✅' if errors else 'ВЫКЛ ⛔'}",
                        "callback_data": "notify:errors:toggle",
                    }],
                    [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
                ]
            },
        )

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
            "Успешные чеки: "
            f"<b>{self._enabled(prefs.get('receipt_success', True))}</b>\n"
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
        if len(parts) != 2 or parts[0] not in ("receipts", "errors") or parts[1] not in ("on", "off"):
            await self.send("Некорректная команда. Используйте <code>/notifications</code> для справки.")
            return
        key = "receipt_success" if parts[0] == "receipts" else "receipt_errors"
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
        if chat_id != self.admin_chat_id or user_id != self.admin_user_id:
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

        if callback_id:
            await self._api("answerCallbackQuery", callback_query_id=callback_id)
        action = callback.get("data") or ""
        if action == "menu:main":
            await self.show_main_menu()
        elif action == "menu:notifications":
            await self.show_notifications()
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
            if operation != "toggle" or kind not in ("receipts", "errors"):
                await self.send("Неизвестное действие кнопки.")
                return
            preferences = self._state().get("notification_preferences", {})
            key = "receipt_success" if kind == "receipts" else "receipt_errors"
            new_value = not preferences.get(key, True)
            await self.command_notifications(
                f"{kind} {'on' if new_value else 'off'}"
            )
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
        if chat_id != self.admin_chat_id or user_id != self.admin_user_id:
            logging.warning(
                "Отклонена неразрешённая команда Telegram: chat=%s user=%s",
                chat_id,
                user_id,
            )
            return
        text = (message.get("text") or "").strip()
        button_actions = {
            "📊 Статус": self.command_status,
            "🔔 Уведомления": self.show_notifications,
            "💾 Резервные копии": self.show_backups_menu,
            "📄 Логи": self.show_logs_menu,
            "ℹ️ Помощь": self.show_main_menu,
        }
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
