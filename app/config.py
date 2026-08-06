import os
import time
from datetime import date, datetime, time as datetime_time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv

load_dotenv()

TZ = os.getenv("TZ", "Europe/Moscow")
if TZ:
    os.environ["TZ"] = TZ
    if hasattr(time, "tzset"):
        time.tzset()

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_API_KEY = os.getenv("YOOKASSA_API_KEY")
MOY_NALOG_LOGIN = os.getenv("MOY_NALOG_LOGIN")
MOY_NALOG_PASSWORD = os.getenv("MOY_NALOG_PASSWORD")
MOY_NALOG_AUTH_METHOD = os.getenv("MOY_NALOG_AUTH_METHOD", "password").lower()
MOY_NALOG_REFRESH_TOKEN = os.getenv("MOY_NALOG_REFRESH_TOKEN")

YOOKASSA_NALOG_PROXY = os.getenv("YOOKASSA_NALOG_PROXY")
if YOOKASSA_NALOG_PROXY:
    os.environ["HTTPS_PROXY"] = YOOKASSA_NALOG_PROXY
    os.environ["HTTP_PROXY"] = YOOKASSA_NALOG_PROXY

DEVICE_ID = os.getenv("DEVICE_ID")
SYNC_START_DATE = os.getenv("SYNC_START_DATE")
INCOME_DESCRIPTION_TEMPLATE = os.getenv("INCOME_DESCRIPTION_TEMPLATE", "Платеж #{description}")
CRON_SCHEDULE = os.getenv("CRON_SCHEDULE", "0 */4 * * *")
FNS_RETRY_SCHEDULE = os.getenv("FNS_RETRY_SCHEDULE", "*/5 * * * *")
FNS_RETRY_DELAY_SECONDS = float(os.getenv("FNS_RETRY_DELAY_SECONDS", "3"))
FNS_QUEUE_MAX_ATTEMPTS = int(os.getenv("FNS_QUEUE_MAX_ATTEMPTS", "0"))
STATE_RETENTION_DAYS = int(os.getenv("STATE_RETENTION_DAYS", "1095"))
HEALTH_MAX_AGE_HOURS = float(os.getenv("HEALTH_MAX_AGE_HOURS", "25"))

BACKUP_TARGET = os.getenv("BACKUP_TARGET", "").strip().lower()
BACKUP_SCHEDULE = os.getenv("BACKUP_SCHEDULE", "0 3 * * *")
BACKUP_PASSWORD = os.getenv("BACKUP_PASSWORD", "")
BACKUP_RETENTION_COUNT = int(os.getenv("BACKUP_RETENTION_COUNT", "7"))
BACKUP_MAX_MB = float(os.getenv("BACKUP_MAX_MB", "45"))
BACKUP_HEALTH_MAX_AGE_HOURS = float(
    os.getenv("BACKUP_HEALTH_MAX_AGE_HOURS", "48")
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY")
TELEGRAM_ADMIN_USER_ID = os.getenv("TELEGRAM_ADMIN_USER_ID")
TELEGRAM_ADMIN_BOT_ENABLED = os.getenv(
    "TELEGRAM_ADMIN_BOT_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
TELEGRAM_BOT_HEALTH_MAX_AGE_MINUTES = float(
    os.getenv("TELEGRAM_BOT_HEALTH_MAX_AGE_MINUTES", "5")
)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME")
SMTP_TO_EMAIL = os.getenv("SMTP_TO_EMAIL")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Синхронизация чеков в налоговой")


def parse_sync_start(value):
    """Преобразовать дату/время начала синхронизации в точный UTC timestamp."""
    if value is None or not str(value).strip():
        return None

    raw = str(value).strip()
    try:
        if len(raw) == 10:
            parsed = datetime.combine(
                date.fromisoformat(raw),
                datetime_time.min,
            )
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "SYNC_START_DATE должен быть датой YYYY-MM-DD или временем ISO 8601, "
            "например 2026-08-06T15:42:30+03:00."
        ) from exc

    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(TZ))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Неизвестный часовой пояс TZ: {TZ}") from exc

    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_config():
    parse_sync_start(SYNC_START_DATE)
    if FNS_RETRY_DELAY_SECONDS < 0:
        raise ValueError("FNS_RETRY_DELAY_SECONDS не может быть отрицательным.")
    if FNS_QUEUE_MAX_ATTEMPTS < 0:
        raise ValueError("FNS_QUEUE_MAX_ATTEMPTS не может быть отрицательным.")
    if TELEGRAM_BOT_HEALTH_MAX_AGE_MINUTES <= 0:
        raise ValueError(
            "TELEGRAM_BOT_HEALTH_MAX_AGE_MINUTES должен быть положительным."
        )
    if STATE_RETENTION_DAYS < 1:
        raise ValueError("STATE_RETENTION_DAYS должен быть положительным числом.")
    if HEALTH_MAX_AGE_HOURS <= 0:
        raise ValueError("HEALTH_MAX_AGE_HOURS должен быть положительным числом.")
    if BACKUP_TARGET not in ("", "telegram", "email"):
        raise ValueError("BACKUP_TARGET должен быть пустым, telegram или email.")
    if BACKUP_RETENTION_COUNT < 1 or BACKUP_MAX_MB <= 0:
        raise ValueError("Параметры хранения и размера backup должны быть положительными.")
    if BACKUP_TARGET and len(BACKUP_PASSWORD) < 12:
        raise ValueError("Для backup задайте BACKUP_PASSWORD длиной не менее 12 символов.")

    if MOY_NALOG_AUTH_METHOD not in ("password", "refresh"):
        raise ValueError(f"MOY_NALOG_AUTH_METHOD имеет недопустимое значение: '{MOY_NALOG_AUTH_METHOD}' (допустимо: password, refresh)")

    required_vars = [
        ("YOOKASSA_SHOP_ID", YOOKASSA_SHOP_ID),
        ("YOOKASSA_API_KEY", YOOKASSA_API_KEY),
    ]

    if MOY_NALOG_AUTH_METHOD == "password":
        required_vars.append(("MOY_NALOG_LOGIN", MOY_NALOG_LOGIN))
        required_vars.append(("MOY_NALOG_PASSWORD", MOY_NALOG_PASSWORD))
    else:
        required_vars.append(("MOY_NALOG_REFRESH_TOKEN", MOY_NALOG_REFRESH_TOKEN))

    missing = [var for var, val in required_vars if not val]
    if missing:
        raise ValueError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}")

    if MOY_NALOG_AUTH_METHOD == "refresh" and not DEVICE_ID and not MOY_NALOG_LOGIN:
        raise ValueError("В режиме refresh необходимо задать DEVICE_ID или MOY_NALOG_LOGIN (источник deviceId).")

    if TELEGRAM_BOT_TOKEN and not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN задан, но TELEGRAM_CHAT_ID отсутствует.")
    if TELEGRAM_CHAT_ID and not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_CHAT_ID задан, но TELEGRAM_BOT_TOKEN отсутствует.")
    if TELEGRAM_ADMIN_BOT_ENABLED and not (
        TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and TELEGRAM_ADMIN_USER_ID
    ):
        raise ValueError(
            "Для TELEGRAM_ADMIN_BOT_ENABLED=true задайте TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_CHAT_ID и TELEGRAM_ADMIN_USER_ID."
        )
    if BACKUP_TARGET == "telegram" and not (
        TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    ):
        raise ValueError("Для BACKUP_TARGET=telegram настройте Telegram bot token и chat ID.")

    smtp_partial = [
        ("SMTP_HOST", SMTP_HOST),
        ("SMTP_USER", SMTP_USER),
        ("SMTP_PASSWORD", SMTP_PASSWORD),
        ("SMTP_TO_EMAIL", SMTP_TO_EMAIL),
    ]
    smtp_set = [name for name, val in smtp_partial if val]
    smtp_missing = [name for name, val in smtp_partial if not val]
    if smtp_set and smtp_missing:
        raise ValueError(f"Email-уведомления настроены частично. Отсутствуют: {', '.join(smtp_missing)}")
    if BACKUP_TARGET == "email" and smtp_missing:
        raise ValueError(f"Для BACKUP_TARGET=email отсутствуют: {', '.join(smtp_missing)}")

    return True
