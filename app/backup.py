import argparse
import hashlib
import io
import json
import logging
import os
import smtplib
import sqlite3
import ssl
import tempfile
import zipfile
from datetime import datetime, timezone
from contextlib import closing
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from health_state import write_status
from version import __version__


MAGIC = b"YNBACKUP1"
SALT_SIZE = 16
NONCE_SIZE = 12


def _bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _derive_key(password, salt):
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(
        password.encode("utf-8")
    )


def encrypt_bytes(plaintext, password):
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(_derive_key(password, salt)).encrypt(
        nonce, plaintext, MAGIC
    )
    return MAGIC + salt + nonce + ciphertext


def decrypt_bytes(payload, password):
    minimum = len(MAGIC) + SALT_SIZE + NONCE_SIZE + 16
    if len(payload) < minimum or not payload.startswith(MAGIC):
        raise ValueError("неизвестный или повреждённый формат backup")
    offset = len(MAGIC)
    salt = payload[offset:offset + SALT_SIZE]
    offset += SALT_SIZE
    nonce = payload[offset:offset + NONCE_SIZE]
    ciphertext = payload[offset + NONCE_SIZE:]
    return AESGCM(_derive_key(password, salt)).decrypt(
        nonce, ciphertext, MAGIC
    )


def _sqlite_snapshot(source_path, target_path):
    source_uri = f"file:{Path(source_path).resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)
            result = target.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite snapshot повреждён: {result}")


def create_backup(data_dir, log_dir, password, include_logs=True):
    if len(password) < 12:
        raise ValueError("BACKUP_PASSWORD должен содержать не менее 12 символов")

    data_dir = Path(data_dir)
    log_dir = Path(log_dir)
    source_db = data_dir / "sync_state.db"
    if not source_db.is_file():
        raise FileNotFoundError(f"база не найдена: {source_db}")

    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = backup_dir / f"yookassa-mynalog-{timestamp}.ynbackup"

    with tempfile.TemporaryDirectory(prefix="yn-backup-") as temp_name:
        temp_dir = Path(temp_name)
        snapshot = temp_dir / "sync_state.db"
        archive = temp_dir / "backup.zip"
        _sqlite_snapshot(source_db, snapshot)

        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
            "database": "sync_state.db",
            "logs_included": bool(include_logs),
        }
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as bundle:
            bundle.write(snapshot, "data/sync_state.db")
            if include_logs and log_dir.is_dir():
                for log_file in sorted(log_dir.glob("*.log*")):
                    if log_file.is_file():
                        try:
                            bundle.write(log_file, f"logs/{log_file.name}")
                        except FileNotFoundError:
                            # Файл мог ротироваться между glob и чтением.
                            continue
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        encrypted = encrypt_bytes(archive.read_bytes(), password)
        temporary = final_path.with_suffix(final_path.suffix + ".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, final_path)

    if os.name != "nt":
        os.chmod(final_path, 0o600)
    return final_path


def restore_backup(backup_path, output_dir, password):
    backup_path = Path(backup_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("каталог восстановления должен быть пустым")
    output_dir.mkdir(parents=True, exist_ok=True)
    plaintext = decrypt_bytes(backup_path.read_bytes(), password)

    with zipfile.ZipFile(io.BytesIO(plaintext)) as bundle:
        for member in bundle.infolist():
            destination = (output_dir / member.filename).resolve()
            if output_dir.resolve() not in destination.parents:
                raise ValueError("опасный путь внутри архива")
        bundle.extractall(output_dir)

    db_path = output_dir / "data" / "sync_state.db"
    with closing(
        sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    ) as db:
        result = db.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"восстановленная SQLite повреждена: {result}")
    return output_dir


def _send_telegram(path, caption):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": caption}
    thread_id = os.getenv("TELEGRAM_THREAD_ID")
    if thread_id:
        data["message_thread_id"] = thread_id
    with path.open("rb") as stream:
        with httpx.Client(
            timeout=120, proxy=os.getenv("TELEGRAM_PROXY"), trust_env=False
        ) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data=data,
                files={"document": (path.name, stream, "application/octet-stream")},
            )
    if response.status_code != 200:
        safe_body = response.text.replace(token, "***")[:500]
        raise RuntimeError(f"Telegram API {response.status_code}: {safe_body}")


def _send_email(path, caption):
    message = EmailMessage()
    message["Subject"] = f"Backup YooKassa → Мой Налог — {path.name}"
    from_email = os.getenv("SMTP_FROM_EMAIL") or os.environ["SMTP_USER"]
    from_name = os.getenv("SMTP_FROM_NAME")
    message["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    message["To"] = os.environ["SMTP_TO_EMAIL"]
    message.set_content(caption)
    message.add_attachment(
        path.read_bytes(),
        maintype="application",
        subtype="octet-stream",
        filename=path.name,
    )

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    password = os.environ["SMTP_PASSWORD"]
    if _bool_env("SMTP_USE_TLS", True):
        with smtplib.SMTP(host, port, timeout=120) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(os.environ["SMTP_USER"], password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(
            host, port, timeout=120, context=ssl.create_default_context()
        ) as server:
            server.login(os.environ["SMTP_USER"], password)
            server.send_message(message)


def _prune(backup_dir, keep):
    backups = sorted(Path(backup_dir).glob("*.ynbackup"), reverse=True)
    for old_backup in backups[max(1, keep):]:
        old_backup.unlink()


def _setup_logging(log_dir):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        Path(log_dir) / "backup.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    if root.handlers:
        handler.close()
        return
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def run_backup():
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    target = os.getenv("BACKUP_TARGET", "").strip().lower()
    password = os.getenv("BACKUP_PASSWORD", "")
    max_bytes = int(float(os.getenv("BACKUP_MAX_MB", "45")) * 1024 * 1024)
    _setup_logging(log_dir)

    if target not in ("telegram", "email"):
        raise ValueError("BACKUP_TARGET должен быть telegram или email")

    try:
        path = create_backup(
            data_dir,
            log_dir,
            password,
            include_logs=_bool_env("BACKUP_INCLUDE_LOGS", True),
        )
        _prune(path.parent, int(os.getenv("BACKUP_RETENTION_COUNT", "7")))
        if path.stat().st_size > max_bytes:
            raise ValueError(
                f"backup {path.stat().st_size / 1024 / 1024:.1f} МБ превышает "
                f"BACKUP_MAX_MB"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        caption = (
            f"Зашифрованный backup YooKassa → Мой Налог\n"
            f"SHA-256: {digest}\n"
            "Пароль храните отдельно от сообщения."
        )
        if target == "telegram":
            _send_telegram(path, caption)
        else:
            _send_email(path, caption)
        write_status(
            data_dir,
            "ok",
            filename="backup_status.json",
            target=target,
            filename_sent=path.name,
            sha256=digest,
        )
        logging.info("Backup %s отправлен через %s", path.name, target)
        return path
    except Exception as exc:
        write_status(
            data_dir,
            "failed",
            filename="backup_status.json",
            target=target,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        logging.exception("Backup завершился ошибкой")
        raise


def main():
    parser = argparse.ArgumentParser(description="Зашифрованные резервные копии")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="создать и отправить backup")
    restore = subparsers.add_parser("restore", help="расшифровать backup в новый каталог")
    restore.add_argument("archive")
    restore.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.command == "run":
        run_backup()
    else:
        password = os.getenv("BACKUP_PASSWORD", "")
        restore_backup(args.archive, args.output, password)
        print(f"Backup восстановлен в {args.output}; активная база не изменена.")


if __name__ == "__main__":
    main()
