#!/bin/bash
set -e

echo "=========================================="
echo "YooKassa to MyNalog Synchronizer"
echo "=========================================="
echo ""

TELEGRAM_STARTUP_NOTIFY=1 python /app/main.py

CRON_SCHEDULE=$(python -c "import config; print(config.CRON_SCHEDULE)")

echo ""
echo "Расписание: $CRON_SCHEDULE"
echo "Переключение на регулярное выполнение..."
echo "=========================================="

echo "$CRON_SCHEDULE cd /app && TELEGRAM_STARTUP_NOTIFY=0 /usr/local/bin/python /app/main.py >> /app/sync.log 2>&1" > /etc/cron.d/sync-cron
chmod 0644 /etc/cron.d/sync-cron
crontab /etc/cron.d/sync-cron

exec cron -f
