import json
import logging
import os
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class StateStoreError(RuntimeError):
    pass


class ConcurrentRunError(StateStoreError):
    pass


class StateStore:
    LOCK_TTL = timedelta(hours=1)

    def __init__(self, db_path, legacy_json_path=None):
        self.db_path = Path(db_path)
        self.legacy_json_path = Path(legacy_json_path) if legacy_json_path else None
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        try:
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sync_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        data TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS run_lock (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        owner TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            if os.name != "nt":
                os.chmod(self.db_path, 0o600)
        except (OSError, sqlite3.Error) as e:
            raise StateStoreError(f"не удалось инициализировать SQLite state: {e}") from e

    def load(self):
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT data FROM sync_state WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as e:
            raise StateStoreError(f"не удалось прочитать SQLite state: {e}") from e

        if row:
            try:
                state = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError) as e:
                raise StateStoreError(f"повреждены данные в SQLite state: {e}") from e
            if not isinstance(state, dict):
                raise StateStoreError("SQLite state должен содержать JSON-объект")
            return state

        return self._migrate_legacy_json()

    def _migrate_legacy_json(self):
        if not self.legacy_json_path or not self.legacy_json_path.exists():
            return None

        try:
            with self.legacy_json_path.open("r", encoding="utf-8") as source:
                state = json.load(source)
        except (OSError, json.JSONDecodeError) as e:
            raise StateStoreError(
                f"не удалось импортировать {self.legacy_json_path}: {e}"
            ) from e

        if not isinstance(state, dict):
            raise StateStoreError(
                f"legacy state {self.legacy_json_path} должен содержать JSON-объект"
            )

        self.save(state)
        logging.info(
            f"✓ State импортирован из {self.legacy_json_path} в {self.db_path}. "
            "Исходный JSON оставлен как резервная копия."
        )
        return state

    def save(self, state):
        if not isinstance(state, dict):
            raise StateStoreError("state должен быть словарём")

        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        now = self._now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO sync_state (id, data, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        data = excluded.data,
                        updated_at = excluded.updated_at
                    """,
                    (payload, now),
                )
                connection.execute(
                    "UPDATE run_lock SET updated_at = ? WHERE id = 1 AND owner = ?",
                    (now, self.owner),
                )
        except sqlite3.Error as e:
            raise StateStoreError(f"не удалось сохранить SQLite state: {e}") from e

    def acquire_lock(self):
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT owner, updated_at FROM run_lock WHERE id = 1"
                ).fetchone()
                if row:
                    try:
                        updated_at = datetime.fromisoformat(row["updated_at"])
                    except ValueError as e:
                        raise StateStoreError(
                            "повреждена отметка времени блокировки SQLite state"
                        ) from e

                    if now_dt - updated_at <= self.LOCK_TTL:
                        raise ConcurrentRunError(
                            f"синхронизация уже выполняется ({row['owner']})"
                        )
                    logging.warning(
                        f"Удалена устаревшая блокировка синхронизации: {row['owner']}"
                    )
                    connection.execute("DELETE FROM run_lock WHERE id = 1")

                connection.execute(
                    "INSERT INTO run_lock (id, owner, updated_at) VALUES (1, ?, ?)",
                    (self.owner, now),
                )
        except ConcurrentRunError:
            raise
        except sqlite3.Error as e:
            raise StateStoreError(f"не удалось установить блокировку state: {e}") from e

    def release_lock(self):
        try:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM run_lock WHERE id = 1 AND owner = ?",
                    (self.owner,),
                )
        except sqlite3.Error as e:
            logging.error(f"Не удалось снять блокировку SQLite state: {e}")

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()
