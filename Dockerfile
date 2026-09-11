FROM python:3.13-slim-bookworm

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 && \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources && \
    apt-get -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 update && \
    apt-get install -y --no-install-recommends cron tzdata util-linux && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid "$APP_GID" app && \
    useradd --uid "$APP_UID" --gid "$APP_GID" --home-dir /app --no-create-home --shell /usr/sbin/nologin app && \
    mkdir -p /app/data /app/logs && \
    chown app:app /app/data /app/logs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app/ .
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
