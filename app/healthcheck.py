import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


def fail(message):
    print(f"UNHEALTHY: {message}")
    return 1


def _cron_is_running():
    proc = Path("/proc")
    if not proc.exists():
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() == "cron":
                return True
        except (OSError, UnicodeError):
            continue
    return False


def _parse_timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main():
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    max_age = timedelta(hours=float(os.getenv("HEALTH_MAX_AGE_HOURS", "25")))

    for directory in (data_dir, log_dir):
        if not directory.is_dir() or not os.access(directory, os.R_OK | os.W_OK):
            return fail(f"нет доступа к {directory}")

    if not _cron_is_running():
        return fail("процесс cron не найден")

    status_path = data_dir / "health.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        updated_at = _parse_timestamp(status["updated_at"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return fail(f"некорректный health marker: {exc}")

    if status.get("status") != "ok":
        return fail(
            f"последняя синхронизация: {status.get('status', 'unknown')}; "
            f"pending payments={status.get('pending_payments', '?')}, "
            f"refunds={status.get('pending_refunds', '?')}"
        )
    if datetime.now(timezone.utc) - updated_at > max_age:
        return fail(f"последняя синхронизация старше {max_age}")

    db_path = data_dir / "sync_state.db"
    try:
        with closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        ) as db:
            result = db.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        return fail(f"SQLite недоступна: {exc}")
    if result != "ok":
        return fail(f"SQLite quick_check: {result}")

    if os.getenv("BACKUP_TARGET", "").strip():
        backup_status_path = data_dir / "backup_status.json"
        backup_max_age = timedelta(
            hours=float(os.getenv("BACKUP_HEALTH_MAX_AGE_HOURS", "48"))
        )
        try:
            backup_status = json.loads(backup_status_path.read_text(encoding="utf-8"))
            backup_updated = _parse_timestamp(backup_status["updated_at"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            return fail(f"некорректный backup marker: {exc}")
        if backup_status.get("status") != "ok":
            return fail(
                f"последний backup: {backup_status.get('status', 'unknown')}; "
                f"{backup_status.get('error', 'без деталей')}"
            )
        if datetime.now(timezone.utc) - backup_updated > backup_max_age:
            return fail(f"последний backup старше {backup_max_age}")

    admin_bot_enabled = os.getenv(
        "TELEGRAM_ADMIN_BOT_ENABLED", "false"
    ).lower() in ("1", "true", "yes", "on")
    if (
        admin_bot_enabled
        and os.getenv("TELEGRAM_BOT_TOKEN")
        and os.getenv("TELEGRAM_CHAT_ID")
        and os.getenv("TELEGRAM_ADMIN_USER_ID")
    ):
        bot_status_path = data_dir / "telegram_bot_status.json"
        bot_max_age = timedelta(
            minutes=float(
                os.getenv("TELEGRAM_BOT_HEALTH_MAX_AGE_MINUTES", "5")
            )
        )
        try:
            bot_status = json.loads(bot_status_path.read_text(encoding="utf-8"))
            bot_updated = _parse_timestamp(bot_status["updated_at"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            return fail(f"некорректный статус Telegram-бота: {exc}")
        if bot_status.get("status") != "ok":
            return fail(
                "Telegram-бот: "
                f"{bot_status.get('status', 'unknown')}; "
                f"{bot_status.get('error', 'без деталей')}"
            )
        if datetime.now(timezone.utc) - bot_updated > bot_max_age:
            return fail(f"Telegram-бот не отвечает дольше {bot_max_age}")

    print("OK: включённые компоненты, синхронизация и SQLite в норме")
    return 0


if __name__ == "__main__":
    sys.exit(main())
