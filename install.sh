#!/bin/bash
set -Eeuo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/yookassa-to-mynalog}
RAW_URL=https://raw.githubusercontent.com/grandvan709/yookassa-to-mynalog/refs/heads/master

say() {
    printf '\n\033[1m%s\033[0m\n' "$1"
}

download() {
    local url=$1 target=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 -o "$target" "$url"
    else
        wget -q --timeout=20 --tries=3 -O "$target" "$url"
    fi
}

if [ "$(uname -s)" != "Linux" ]; then
    echo "Скрипт предназначен для Linux. На Windows и macOS используйте Docker Desktop." >&2
    exit 1
fi

SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "Нужны права root: запустите от root или установите sudo." >&2
        exit 1
    fi
    SUDO=(sudo)
fi

say "1/5 Проверяю Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "Docker с Compose уже установлен."
else
    echo "Docker не найден, устанавливаю с get.docker.com."
    if ! command -v curl >/dev/null 2>&1; then
        "${SUDO[@]}" apt-get update && "${SUDO[@]}" apt-get install -y curl
    fi
    curl -fsSL https://get.docker.com | "${SUDO[@]}" sh
    if command -v systemctl >/dev/null 2>&1; then
        "${SUDO[@]}" systemctl enable --now docker
    fi
fi

if ! "${SUDO[@]}" docker compose version >/dev/null 2>&1; then
    echo "Docker Compose недоступен. Установите плагин docker-compose-plugin и повторите запуск." >&2
    exit 1
fi

say "2/5 Создаю каталоги"
"${SUDO[@]}" mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
"${SUDO[@]}" chmod 0750 "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
echo "$INSTALL_DIR/{data,logs}"

if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    "${SUDO[@]}" apt-get update && "${SUDO[@]}" apt-get install -y curl
fi

say "3/5 Скачиваю docker-compose.yml"
TEMP_COMPOSE=$(mktemp)
trap 'rm -f "$TEMP_COMPOSE"' EXIT
if ! download "$RAW_URL/docker-compose.yml" "$TEMP_COMPOSE"; then
    echo "Не удалось скачать docker-compose.yml с GitHub. Проверьте доступ в интернет и повторите." >&2
    exit 1
fi
if [ ! -s "$TEMP_COMPOSE" ] || ! grep -q "yookassa-to-mynalog" "$TEMP_COMPOSE"; then
    echo "Скачанный docker-compose.yml повреждён. Проверьте доступ к GitHub и повторите." >&2
    exit 1
fi
"${SUDO[@]}" cp "$TEMP_COMPOSE" "$INSTALL_DIR/docker-compose.yml"
"${SUDO[@]}" chmod 0644 "$INSTALL_DIR/docker-compose.yml"
echo "Файл обновлён до последней версии из репозитория."

say "4/5 Готовлю .env"
ENV_CREATED=0
if "${SUDO[@]}" test -f "$INSTALL_DIR/.env"; then
    echo "Файл .env уже существует, оставляю без изменений."
    echo "Новые переменные смотрите в $RAW_URL/.env.example"
else
    TEMP_ENV=$(mktemp)
    trap 'rm -f "$TEMP_COMPOSE" "$TEMP_ENV"' EXIT
    if ! download "$RAW_URL/.env.example" "$TEMP_ENV"; then
        echo "Не удалось скачать .env.example с GitHub. Проверьте доступ в интернет и повторите." >&2
        exit 1
    fi
    if [ ! -s "$TEMP_ENV" ] || ! grep -q "YOOKASSA_SHOP_ID" "$TEMP_ENV"; then
        echo "Скачанный .env.example повреждён. Проверьте доступ к GitHub и повторите." >&2
        exit 1
    fi
    "${SUDO[@]}" cp "$TEMP_ENV" "$INSTALL_DIR/.env"
    "${SUDO[@]}" chmod 0600 "$INSTALL_DIR/.env"
    ENV_CREATED=1
    echo "Создан из .env.example."
fi

say "5/5 Установка завершена"
cat <<INFO

Осталось два шага.

1. Заполните реквизиты ЮKassa и «Мой Налог»:

   sudo nano $INSTALL_DIR/.env

2. Запустите сервис и посмотрите логи:

   cd $INSTALL_DIR && sudo docker compose up -d && sudo docker compose logs -f -t

Обновление на новую версию:

   cd $INSTALL_DIR && sudo docker compose pull && sudo docker compose up -d

INFO

if [ "$ENV_CREATED" -eq 1 ]; then
    echo "Обязательные переменные: YOOKASSA_SHOP_ID, YOOKASSA_API_KEY и данные"
    echo "для входа в «Мой Налог» (логин с паролем либо refresh token)."
    echo
fi

echo "Чтобы не писать sudo перед каждой командой docker, добавьте себя в группу docker:"
echo "  sudo usermod -aG docker \$USER"
echo "и перезайдите на сервер. Учтите, что эта группа даёт root-уровень доступа."
