import json
import logging
import os
import socket
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_migrations import MIGRATIONS


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
            database_existed = (
                self.db_path.exists() and self.db_path.stat().st_size > 0
            )
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                self._run_migrations(connection, database_existed)
            if os.name != "nt":
                os.chmod(self.db_path, 0o600)
        except StateStoreError:
            raise
        except (OSError, sqlite3.Error) as e:
            raise StateStoreError(f"не удалось инициализировать SQLite state: {e}") from e

    def _run_migrations(self, connection, database_existed):
        known_versions = [migration.version for migration in MIGRATIONS]
        if known_versions != list(range(1, len(MIGRATIONS) + 1)):
            raise StateStoreError(
                "реестр миграций SQLite должен содержать последовательные версии с 1"
            )

        applied = self._applied_migration_versions(connection)
        known = set(known_versions)
        unknown = applied - known
        if unknown:
            versions = ", ".join(str(version) for version in sorted(unknown))
            raise StateStoreError(
                f"версия базы новее приложения: неизвестные миграции {versions}"
            )
        if applied and applied != set(range(1, max(applied) + 1)):
            raise StateStoreError("в истории миграций SQLite нарушена последовательность")

        pending = [
            migration for migration in MIGRATIONS if migration.version not in applied
        ]
        if not pending:
            return

        backup_path = None
        if database_existed:
            backup_path = self._create_pre_migration_backup(connection, applied, pending)

        try:
            connection.execute("BEGIN IMMEDIATE")
            # Версия перечитывается после получения write lock: другой процесс мог
            # успеть применить миграции, пока текущий ожидал SQLite.
            applied = self._applied_migration_versions(connection)
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                migration.apply(connection)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, migration.name, self._now()),
                )
            connection.commit()
        except Exception as e:
            connection.rollback()
            suffix = f" Резервная копия: {backup_path}." if backup_path else ""
            raise StateStoreError(f"не удалось обновить схему SQLite: {e}.{suffix}") from e

        versions = ", ".join(str(migration.version) for migration in pending)
        logging.info(f"Применены миграции SQLite: {versions}")

    @staticmethod
    def _applied_migration_versions(connection):
        exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()
        if not exists:
            return set()
        return {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }

    def _create_pre_migration_backup(self, connection, applied, pending):
        old_version = max(applied, default=0)
        new_version = max(migration.version for migration in pending)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.db_path.with_name(
            f"{self.db_path.name}.pre-migration-v{old_version}-to-v{new_version}-{timestamp}.bak"
        )
        try:
            with closing(sqlite3.connect(backup_path)) as destination:
                connection.backup(destination)
            if os.name != "nt":
                os.chmod(backup_path, 0o600)
        except (OSError, sqlite3.Error) as e:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateStoreError(
                f"не удалось создать резервную копию перед миграцией SQLite: {e}"
            ) from e
        logging.info(f"Перед миграцией SQLite создана копия: {backup_path}")
        return backup_path

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
