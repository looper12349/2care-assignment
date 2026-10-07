"""Small database boundary for the application's fixed, parameter-bound SQL.

SQLite keeps the offline demo self-contained. PostgreSQL supplies shared durable
state and database locks for multiple independently running service instances.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def is_postgres(value: str) -> bool:
    return value.startswith(("postgresql://", "postgres://"))


class DatabaseRow(Mapping[str, Any]):
    def __init__(self, values: Mapping[str, Any]):
        self._values = dict(values)

    def __getitem__(self, key: str | int) -> Any:
        return tuple(self._values.values())[key] if isinstance(key, int) else self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class DatabaseCursor:
    def __init__(self, cursor: Any = None):
        self.raw = cursor

    def fetchone(self) -> DatabaseRow | None:
        result = self.raw.fetchone() if self.raw is not None else None
        return DatabaseRow(result) if result is not None else None

    def fetchall(self) -> list[DatabaseRow]:
        return [DatabaseRow(row) for row in self.raw.fetchall()] if self.raw is not None else []

    def __iter__(self) -> Iterator[DatabaseRow]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount if self.raw is not None else 0

    @property
    def lastrowid(self) -> int | None:
        return getattr(self.raw, "lastrowid", None)


class DatabaseConnection:
    def __init__(self, value: str):
        self.dialect = "postgres" if is_postgres(value) else "sqlite"
        self.dsn = value if self.dialect == "postgres" else None
        self._lock_pool = None
        if self.dialect == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            self.raw = psycopg.connect(value, autocommit=True, row_factory=dict_row)
            # Waiting lock holders use a different pool from repository and
            # checkpoint connections. They cannot consume a connection required
            # by the thread currently holding the lock.
            self._lock_pool = ConnectionPool(
                value,
                min_size=0,
                max_size=16,
                timeout=30,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
        else:
            if value != ":memory:":
                Path(value).parent.mkdir(parents=True, exist_ok=True)
            self.raw = sqlite3.connect(value, check_same_thread=False, isolation_level=None)
            self.raw.row_factory = sqlite3.Row

    def execute(self, sql: str, parameters: Sequence[Any] | None = None) -> DatabaseCursor:
        if self.dialect == "postgres":
            statement = sql.strip().rstrip(";")
            if statement in {"PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON"}:
                return DatabaseCursor()
            if statement == "BEGIN IMMEDIATE":
                sql = "BEGIN"
            # All callers supply fixed application SQL; only DB-API parameter
            # markers change. Identifiers and values are never interpolated here.
            if parameters is not None:
                sql = sql.replace("?", "%s")
        cursor = (
            self.raw.execute(sql, parameters) if parameters is not None else self.raw.execute(sql)
        )
        return DatabaseCursor(cursor)

    def executescript(self, schema: str) -> None:
        for statement in schema.split(";"):
            if statement.strip():
                self.execute(statement)

    @contextmanager
    def advisory_lock(self, key: str):
        if self._lock_pool is None:
            yield
            return
        lock_key = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        with self._lock_pool.connection() as connection:
            connection.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            try:
                yield
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))

    def close(self) -> None:
        self.raw.close()
        if self._lock_pool is not None:
            self._lock_pool.close()


def connect_database(value: str) -> DatabaseConnection:
    return DatabaseConnection(value)
