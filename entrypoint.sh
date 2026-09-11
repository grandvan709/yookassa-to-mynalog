#!/bin/bash
set -Eeuo pipefail

APP_USER=app
DATA_DIR=${DATA_DIR:-/app/data}
LOG_DIR=${LOG_DIR:-/app/logs}

if [ "$(id -u)" -ne 0 ]; then
    echo "entrypoint должен запускаться от root, чтобы настроить bind-mount и cron" >&2
    exit 1
fi

mkdir -p "$DATA_DIR" "$LOG_DIR"

# Однократная миграция SQLite из прежнего каталога logs/. Файлы-источники
# намеренно сохраняются как резервная копия.
if [ ! -e "$DATA_DIR/sync_state.db" ] && [ -f "$LOG_DIR/sync_state.db" ]; then
    echo "→ Перенос существующей базы из logs/ в data/"
    for suffix in "" "-wal" "-shm"; do
        if [ -f "$LOG_DIR/sync_state.db$suffix" ]; then
            cp -p "$LOG_DIR/sync_state.db$suffix" "$DATA_DIR/sync_state.db$suffix"
        fi
    done
fi

chown -R "$APP_USER:$APP_USER" "$DATA_DIR" "$LOG_DIR"
chmod 0750 "$DATA_DIR" "$LOG_DIR"

CRON_SCHEDULE=$(runuser -u "$APP_USER" -- python -c "import config; print(config.CRON_SCHEDULE)")
FNS_RETRY_SCHEDULE=$(runuser -u "$APP_USER" -- python -c "import config; print(config.FNS_RETRY_SCHEDULE)")
APP_TZ=$(runuser -u "$APP_USER" -- python -c "import config; print(config.TZ or '')")
BACKUP_TARGET=$(runuser -u "$APP_USER" -- python -c "import config; print(config.BACKUP_TARGET)")
BACKUP_SCHEDULE=$(runuser -u "$APP_USER" -- python -c "import config; print(config.BACKUP_SCHEDULE)")
TELEGRAM_ADMIN_BOT_ENABLED=$(runuser -u "$APP_USER" -- python -c "import config; print('1' if config.TELEGRAM_ADMIN_BOT_ENABLED and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID and config.TELEGRAM_ADMIN_USER_ID else '0')")

python - "$CRON_SCHEDULE" "$FNS_RETRY_SCHEDULE" "$BACKUP_SCHEDULE" "$APP_TZ" <<'PY'
import re
import sys

sync_schedule, retry_schedule, backup_schedule, timezone = sys.argv[1:]
for name, schedule in (
    ("CRON_SCHEDULE", sync_schedule),
    ("FNS_RETRY_SCHEDULE", retry_schedule),
    ("BACKUP_SCHEDULE", backup_schedule),
):
    parts = schedule.split()
    if len(parts) != 5 or any(
        not re.fullmatch(r"[A-Za-z0-9*/?,#L-]+", part) for part in parts
    ):
        raise SystemExit(f"Некорректный {name}: {schedule!r}")
if "\n" in timezone or "\r" in timezone or timezone.startswith("/") or ".." in timezone:
    raise SystemExit(f"Некорректный TZ: {timezone!r}")
PY

if [ -n "$APP_TZ" ]; then
    if [ ! -f "/usr/share/zoneinfo/$APP_TZ" ]; then
        echo "Неизвестный часовой пояс TZ: $APP_TZ" >&2
        exit 1
    fi
    ln -snf "/usr/share/zoneinfo/$APP_TZ" /etc/localtime
    printf '%s\n' "$APP_TZ" > /etc/timezone
fi

# Cron не обязан наследовать окружение контейнера. Сохраняем его явно в
# доступном только пользователю приложения файле внутри контейнера.
python - <<'PY'
import os
import shlex

with open("/app/runtime.env", "w", encoding="utf-8") as stream:
    for key, value in sorted(os.environ.items()):
        if key.isidentifier():
            stream.write(f"export {key}={shlex.quote(value)}\n")
PY
chown "$APP_USER:$APP_USER" /app/runtime.env
chmod 0600 /app/runtime.env

runuser -u "$APP_USER" -- env STARTUP_NOTIFY=1 python /app/main.py
if [ -n "$BACKUP_TARGET" ]; then
    echo "→ Создаю и отправляю первоначальную резервную копию через $BACKUP_TARGET."
    runuser -u "$APP_USER" -- python /app/backup.py run
fi

echo ""
echo "→ Первая синхронизация завершена. Переключение на работу по расписанию."

if [ "$TELEGRAM_ADMIN_BOT_ENABLED" = "1" ]; then
    echo "→ Запускаю Telegram-бота управления."
    (
        while true; do
            runuser -u "$APP_USER" -- python /app/telegram_admin_bot.py
            echo "Telegram-бот завершился; повторный запуск через 5 секунд." >&2
            sleep 5
        done
    ) &
fi

{
    printf 'SHELL=/bin/sh\n'
    printf 'PATH=/usr/local/bin:/usr/bin:/bin\n'
    printf 'TZ=%s\n' "$APP_TZ"
    printf '%s %s . /app/runtime.env && cd /app && STARTUP_NOTIFY=0 /usr/local/bin/python /app/main.py >/dev/null 2>&1\n' \
        "$CRON_SCHEDULE" "$APP_USER"
    printf '%s %s . /app/runtime.env && cd /app && STARTUP_NOTIFY=0 /usr/local/bin/python /app/main.py --retry-fns-only >/dev/null 2>&1\n' \
        "$FNS_RETRY_SCHEDULE" "$APP_USER"
    if [ -n "$BACKUP_TARGET" ]; then
        printf '%s %s . /app/runtime.env && cd /app && /usr/local/bin/python /app/backup.py run >/dev/null 2>&1\n' \
            "$BACKUP_SCHEDULE" "$APP_USER"
    fi
} > /etc/cron.d/sync-cron
chmod 0644 /etc/cron.d/sync-cron

exec cron -f
