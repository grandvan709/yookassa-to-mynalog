#!/bin/bash
set -Eeuo pipefail

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
START_SERVICE=1
ADD_DOCKER_GROUP=0

usage() {
    cat <<'EOF'
Использование: ./install.sh [--no-start] [--add-docker-group]

  --no-start          только подготовить Docker, .env, data/ и logs/
  --add-docker-group  разрешить текущему пользователю запускать Docker без sudo
EOF
}

for arg in "$@"; do
    case "$arg" in
        --no-start) START_SERVICE=0 ;;
        --add-docker-group) ADD_DOCKER_GROUP=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Неизвестный параметр: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$(uname -s)" != "Linux" ]; then
    echo "Автоустановка предназначена для Linux. На Windows/macOS установите Docker Desktop." >&2
    exit 1
fi

SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || {
        echo "Для установки Docker нужен root или sudo." >&2
        exit 1
    }
    SUDO=(sudo)
fi

install_docker() {
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) ;;
        *)
            echo "Автоустановка Docker поддерживает Debian и Ubuntu: обнаружено ${ID:-unknown}." >&2
            echo "Установите Docker Engine вручную и повторите ./install.sh." >&2
            exit 1
            ;;
    esac

    local repo_os="$ID"
    local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    [ -n "$codename" ] || { echo "Не удалось определить codename дистрибутива." >&2; exit 1; }

    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" apt-get install -y ca-certificates curl
    "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
    "${SUDO[@]}" curl -fsSL "https://download.docker.com/linux/$repo_os/gpg" -o /etc/apt/keyrings/docker.asc
    "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$repo_os $codename stable" |
        "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null
    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    "${SUDO[@]}" systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "→ Docker Engine с Compose не найден; устанавливаю его из официального apt-репозитория."
    install_docker
fi

if [ "$ADD_DOCKER_GROUP" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
    getent group docker >/dev/null 2>&1 || "${SUDO[@]}" groupadd docker
    "${SUDO[@]}" usermod -aG docker "$USER"
    echo "→ Пользователь $USER добавлен в группу docker. Важно: эта группа даёт root-уровень доступа."
    echo "  Перезайдите в систему, чтобы запускать docker без sudo."
fi

cd "$PROJECT_DIR"
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 0600 .env
    echo "→ Создан .env. Заполните обязательные реквизиты и повторите ./install.sh."
    exit 2
fi

append_if_missing() {
    local key=$1 value=$2
    grep -q "^${key}=" .env || printf "\n%s='%s'\n" "$key" "$value" >> .env
}

HOST_UID=${SUDO_UID:-$(id -u)}
HOST_GID=${SUDO_GID:-$(id -g)}
if [ "$HOST_UID" -eq 0 ] || [ "$HOST_GID" -eq 0 ]; then
    HOST_UID=1000
    HOST_GID=1000
fi
append_if_missing APP_UID "$HOST_UID"
append_if_missing APP_GID "$HOST_GID"

mkdir -p data logs
chmod 0750 data logs

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
    DOCKER=("${SUDO[@]}" docker)
fi
if ! "${DOCKER[@]}" info >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
    "${SUDO[@]}" systemctl enable --now docker
fi
"${DOCKER[@]}" info >/dev/null
"${DOCKER[@]}" compose version >/dev/null

if [ "$START_SERVICE" -eq 1 ]; then
    echo "→ Собираю локальный образ и запускаю сервис."
    "${DOCKER[@]}" compose up -d --build
    "${DOCKER[@]}" compose ps
else
    echo "→ Подготовка завершена. Запуск: docker compose up -d --build"
fi
