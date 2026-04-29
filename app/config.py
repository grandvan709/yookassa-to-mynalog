import os
import time
from dotenv import load_dotenv

load_dotenv()

TZ = os.getenv("TZ")
if TZ:
    os.environ["TZ"] = TZ
    time.tzset()

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_API_KEY = os.getenv("YOOKASSA_API_KEY")
MOY_NALOG_LOGIN = os.getenv("MOY_NALOG_LOGIN")
MOY_NALOG_PASSWORD = os.getenv("MOY_NALOG_PASSWORD")

DEVICE_ID = os.getenv("DEVICE_ID")
SYNC_START_DATE = os.getenv("SYNC_START_DATE")
INCOME_DESCRIPTION_TEMPLATE = os.getenv("INCOME_DESCRIPTION_TEMPLATE", "Платеж #{description}")
CRON_SCHEDULE = os.getenv("CRON_SCHEDULE", "0 */4 * * *")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.getenv("TELEGRAM_THREAD_ID")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME")
SMTP_TO_EMAIL = os.getenv("SMTP_TO_EMAIL")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Синхронизация чеков в налоговой")


def validate_config():
    required_vars = [
        ("YOOKASSA_SHOP_ID", YOOKASSA_SHOP_ID),
        ("YOOKASSA_API_KEY", YOOKASSA_API_KEY),
        ("MOY_NALOG_LOGIN", MOY_NALOG_LOGIN),
        ("MOY_NALOG_PASSWORD", MOY_NALOG_PASSWORD),
    ]

    missing = [var for var, val in required_vars if not val]
    if missing:
        raise ValueError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}")

    if TELEGRAM_BOT_TOKEN and not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN задан, но TELEGRAM_CHAT_ID отсутствует.")
    if TELEGRAM_CHAT_ID and not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_CHAT_ID задан, но TELEGRAM_BOT_TOKEN отсутствует.")

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

    return True
