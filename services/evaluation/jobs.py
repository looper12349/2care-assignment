"""Durable evaluation jobs, with cross-worker claims and fenced leases."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from uuid import uuid4

from clinic_agent.database import connect_database

MAX_ATTEMPTS = 3
LEASE_SECONDS = 30
MAX_QUEUED_JOBS = 10


class JobStore:
    def __init__(self, path: str | Path, *, database_url: str | None = None):
        self.db = connect_database(database_url or str(path))
        self.dialect = self.db.dialect
        self.lock = threading.RLock()
        epoch_type = "DOUBLE PRECISION" if self.dialect == "postgres" else "REAL"
        with self.db.advisory_lock("jobs:schema-migrations"):
            self.db.executescript(f"""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until {epoch_type}, report_json TEXT, error TEXT,
                    created_at {epoch_type} NOT NULL, updated_at {epoch_type} NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status, created_at);
            """)

    def _now(self, now: float | None = None) -> float:
        if self.dialect == "postgres":
            # A replica's machine clock cannot extend or prematurely expire a
            # lease. Explicit test clocks are intentionally SQLite-only.
            row = self.db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now").fetchone()
            return float(row["now"])
        return time.time() if now is None else now

    def _begin(self) -> None:
        self.db.execute("BEGIN" if self.dialect == "postgres" else "BEGIN IMMEDIATE")

    def enqueue(self, payload: dict) -> dict:
        job_id = str(uuid4())
        with self.lock, self.db.advisory_lock("jobs:enqueue"):
            self._begin()
            try:
                now = self._now()
                queued = self.db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('pending','running')"
                ).fetchone()[0]
                if queued >= MAX_QUEUED_JOBS:
                    raise ValueError("The evaluation queue is full. Wait for an existing job.")
                self.db.execute(
                    "INSERT INTO jobs(job_id,payload_json,status,created_at,updated_at) VALUES(?,?,'pending',?,?)",
                    (job_id, json.dumps(payload), now, now),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Evaluation job not found.")
        return self.public(row)

    def latest(self) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.public(row) if row else None

    @staticmethod
    def public(row) -> dict:
        return {
            "job_id": row["job_id"],
            "status": row["status"],
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "report": json.loads(row["report_json"]) if row["report_json"] else None,
            "error": row["error"],
        }

    def claim(self, now: float | None = None) -> dict | None:
        with self.lock:
            self._begin()
            try:
                current = self._now(now)
                locking = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""
                exhausted = self.db.execute(
                    "SELECT job_id FROM jobs WHERE status='running' AND lease_until<=? AND attempts>=?"
                    + locking,
                    (current, MAX_ATTEMPTS),
                ).fetchall()
                for row in exhausted:
                    self.db.execute(
                        "UPDATE jobs SET status='failed', error='Worker restart retry limit reached after expired leases.', updated_at=?,lease_until=NULL WHERE job_id=?",
                        (current, row["job_id"]),
                    )
                row = self.db.execute(
                    "SELECT * FROM jobs WHERE (status='pending' OR (status='running' AND lease_until<=?)) AND attempts<? ORDER BY created_at LIMIT 1"
                    + locking,
                    (current, MAX_ATTEMPTS),
                ).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return None
                token = str(uuid4())
                self.db.execute(
                    "UPDATE jobs SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=?,error=NULL WHERE job_id=?",
                    (token, current + LEASE_SECONDS, current, row["job_id"]),
                )
                self.db.execute("COMMIT")
                return {
                    "job_id": row["job_id"],
                    "payload": json.loads(row["payload_json"]),
                    "lease_token": token,
                    "attempt": row["attempts"] + 1,
                }
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _lock_job(self, job_id: str) -> None:
        # PostgreSQL obtains the row lock before we sample the lease clock. This
        # is stricter than an UPDATE whose predicate was evaluated before a
        # long wait for another transaction's row lock.
        if self.dialect == "postgres":
            self.db.execute(
                "SELECT job_id FROM jobs WHERE job_id=? FOR UPDATE", (job_id,)
            ).fetchone()

    def heartbeat(self, job_id: str, token: str, now: float | None = None) -> bool:
        with self.lock:
            self._begin()
            try:
                self._lock_job(job_id)
                current = self._now(now)
                cursor = self.db.execute(
                    "UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=? AND status='running' AND lease_token=? AND lease_until>?",
                    (current + LEASE_SECONDS, current, job_id, token, current),
                )
                self.db.execute("COMMIT")
                return cursor.rowcount == 1
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def finish(
        self,
        job_id: str,
        token: str,
        report: dict | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> bool:
        with self.lock:
            self._begin()
            try:
                self._lock_job(job_id)
                current = self._now(now)
                cursor = self.db.execute(
                    "UPDATE jobs SET status=?,report_json=?,error=?,lease_until=NULL,updated_at=? WHERE job_id=? AND status='running' AND lease_token=? AND lease_until>?",
                    (
                        "failed" if error else "completed",
                        json.dumps(report) if report is not None else None,
                        error,
                        current,
                        job_id,
                        token,
                        current,
                    ),
                )
                self.db.execute("COMMIT")
                return cursor.rowcount == 1
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self.lock:
            self.db.close()
