"""Account-scoped SQLite history with a commit after each completed request."""

import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any
from urllib.parse import quote

from .contracts import METRICS, SCHEMA_VERSION, is_empty, make_record, parse_dates


# Preserve the schema-v1 metadata identifier used by existing histories.
class HistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self._connection: sqlite3.Connection | None = None
        created = False
        created_identity = None
        if not self.path.parent.exists():
            self.path.parent.mkdir(mode=0o700, parents=True)
            self.path.parent.chmod(0o700)
        if os.path.lexists(self.path):
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("History path must be a regular SQLite file.")
            with self.path.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    raise ValueError("History path contains an unrelated file.")
        else:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                info = os.fstat(descriptor)
                created_identity = (info.st_dev, info.st_ino)
            finally:
                os.close(descriptor)
            created = True
        try:
            uri = f"file:{quote(str(self.path), safe='/')}?mode=rw"
            self._connection = sqlite3.connect(uri, timeout=5, uri=True)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if created:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) != created_identity or not stat.S_ISREG(info.st_mode):
                    raise ValueError("History path changed while opening it.")
                self._initialize()
            else:
                self._validate_schema()
        except BaseException:
            self.close()
            if created:
                try:
                    info = self.path.lstat()
                    if (info.st_dev, info.st_ino) == created_identity:
                        self.path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute(
                "CREATE TABLE garmin_agent_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._connection.execute(
                "INSERT INTO garmin_agent_meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )
            self._connection.execute("""
                CREATE TABLE records (
                    account_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('available', 'empty', 'error')),
                    data_json TEXT,
                    error_json TEXT,
                    fetched_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL CHECK (attempts > 0),
                    PRIMARY KEY (account_id, metric, date)
                )
            """)
            self._connection.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    def _validate_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > int(SCHEMA_VERSION):
            raise ValueError("History uses a newer unsupported schema version.")
        tables = {
            row[0] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not {"garmin_agent_meta", "records"}.issubset(tables):
            raise ValueError("History path contains an unrelated SQLite database.")
        row = self._connection.execute(
            "SELECT value FROM garmin_agent_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row[0] != SCHEMA_VERSION or version != int(SCHEMA_VERSION):
            raise ValueError("History schema version is unsupported.")
        expected = {
            "account_id", "metric", "date", "status", "data_json", "error_json", "fetched_at", "attempts",
        }
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(records)")}
        if columns != expected:
            raise ValueError("History schema is invalid.")

    @staticmethod
    def _validate_key(account_id: str, metric: str, date: str) -> None:
        if not isinstance(account_id, str) or not account_id or "\x00" in account_id:
            raise ValueError("Account ID must be a nonempty string.")
        if not isinstance(metric, str) or metric not in METRICS:
            raise ValueError("Unknown health metric.")
        parse_dates(date, date)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return make_record(
            row["date"], row["metric"], row["status"],
            data=json.loads(row["data_json"]) if row["data_json"] is not None else None,
            error=json.loads(row["error_json"]) if row["error_json"] is not None else None,
            fetched_at=row["fetched_at"], attempts=row["attempts"],
        )

    def get(self, account_id: str, metric: str, date: str) -> dict[str, Any]:
        self._validate_key(account_id, metric, date)
        row = self._connection.execute(
            "SELECT * FROM records WHERE account_id = ? AND metric = ? AND date = ?",
            (account_id, metric, date),
        ).fetchone()
        return self._decode(row) if row is not None else make_record(date, metric, "missing")

    def save(
        self, account_id: str, metric: str, date: str, data: Any = None, error: Any = None,
    ) -> dict[str, Any]:
        self._validate_key(account_id, metric, date)
        status = "error" if error is not None else "empty" if is_empty(data) else "available"
        record = make_record(date, metric, status, data=data, error=error, attempts=1)
        data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        error_json = (
            json.dumps(record["error"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if record["error"] is not None else None
        )
        with self._connection:
            self._connection.execute("""
                INSERT INTO records (account_id, metric, date, status, data_json, error_json, fetched_at, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (account_id, metric, date) DO UPDATE SET
                    status = excluded.status,
                    data_json = excluded.data_json,
                    error_json = excluded.error_json,
                    fetched_at = excluded.fetched_at,
                    attempts = records.attempts + 1
            """, (account_id, metric, date, status, data_json, error_json, record["fetched_at"]))
            saved = self.get(account_id, metric, date)
        return saved

    def accounts(self) -> list[str]:
        return [row[0] for row in self._connection.execute("SELECT DISTINCT account_id FROM records ORDER BY account_id")]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
