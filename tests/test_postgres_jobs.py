"""Real PostgreSQL worker isolation and lease fencing (optional integration suite).

Set PG_TEST_EVALUATION_URL or provide the private local test environment file.
Each test uses its own schema; no application jobs are deleted.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from services.evaluation.jobs import MAX_ATTEMPTS, MAX_QUEUED_JOBS, JobStore

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> dict:
    path = ROOT / ".data/postgres-test-env.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    for service in ("evaluation", "conversation", "scheduling"):
        if service in settings:
            settings[f"PG_TEST_{service.upper()}_URL"] = settings[service]
    return {
        **settings,
        **{key: value for key, value in os.environ.items() if key.startswith("PG_TEST_")},
    }


@pytest.fixture
def isolated_database():
    settings = _settings()
    dsn = settings.get("PG_TEST_EVALUATION_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration requires PG_TEST_EVALUATION_URL.")
    schema = f"evaluation_test_{uuid4().hex}"
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    except psycopg.Error:
        pytest.fail(
            "PostgreSQL test setup failed. Verify the private test database and role configuration.",
            pytrace=False,
        )
    parsed = urlsplit(dsn)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "options"]
    query.append(("options", f"-c search_path={schema}"))
    scoped = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query, quote_via=quote),
            parsed.fragment,
        )
    )
    stores = []

    def store():
        instance = JobStore(":memory:", database_url=scoped)
        stores.append(instance)
        return instance

    # Initialize the table once before parallel worker instances open it.
    store()
    try:
        yield store, settings, scoped
    finally:
        for instance in stores:
            instance.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_independent_workers_cannot_claim_the_same_job(isolated_database):
    factory, _, _ = isolated_database
    first, second = factory(), factory()
    job = first.enqueue({"repetitions": 1, "online": False})
    start = threading.Barrier(2)

    def claim(worker):
        start.wait()
        return worker.claim(now=0)  # PostgreSQL deliberately ignores replica time.

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [first, second]))
    claims = [result for result in results if result]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["job_id"] == job["job_id"]
    row = second.db.execute(
        "SELECT lease_until FROM jobs WHERE job_id=?", (job["job_id"],)
    ).fetchone()
    assert row["lease_until"] > 1_000_000_000
    assert second.get(job["job_id"])["attempts"] == 1
    assert not second.finish(job["job_id"], "wrong-token", report={"bad": True})
    assert first.finish(job["job_id"], claim["lease_token"], report={"evidence": "shared database"})
    assert second.get(job["job_id"])["report"] == {"evidence": "shared database"}


def test_skip_locked_claim_does_not_wait_for_another_worker(isolated_database):
    factory, _, _ = isolated_database
    holder, worker = factory(), factory()
    locked = holder.enqueue({"case": "locked"})
    available = holder.enqueue({"case": "available"})
    holder.db.execute("BEGIN")
    try:
        holder.db.execute("SELECT * FROM jobs WHERE job_id=? FOR UPDATE", (locked["job_id"],))
        started = time.monotonic()
        claimed = worker.claim()
        assert time.monotonic() - started < 2
        assert claimed["job_id"] == available["job_id"]
    finally:
        holder.db.execute("COMMIT")
    assert holder.claim()["job_id"] == locked["job_id"]


def test_expired_owner_cannot_renew_or_finish_even_before_reclaim(isolated_database):
    factory, _, _ = isolated_database
    stale, replacement = factory(), factory()
    job = stale.enqueue({"case": "restart"})
    first = stale.claim()
    # Fault injection changes only this test's lease, using the DB server clock.
    stale.db.execute(
        "UPDATE jobs SET lease_until=EXTRACT(EPOCH FROM clock_timestamp())-1 WHERE job_id=?",
        (job["job_id"],),
    )
    assert not stale.heartbeat(job["job_id"], first["lease_token"], now=0)
    assert not stale.finish(job["job_id"], first["lease_token"], report={"stale": True}, now=0)
    second = replacement.claim()
    assert second["attempt"] == 2
    assert second["lease_token"] != first["lease_token"]
    assert not stale.finish(job["job_id"], first["lease_token"], report={"stale": True})
    assert replacement.heartbeat(job["job_id"], second["lease_token"])
    assert replacement.finish(job["job_id"], second["lease_token"], report={"current": True})
    assert stale.get(job["job_id"])["report"] == {"current": True}


@pytest.mark.parametrize("operation", ["heartbeat", "finish"])
def test_lease_expiring_during_row_lock_wait_is_rejected(isolated_database, operation):
    factory, _, _ = isolated_database
    holder, worker = factory(), factory()
    job = holder.enqueue({"case": "blocked-fence"})
    claimed = holder.claim()
    holder.db.execute("BEGIN")
    holder.db.execute("SELECT * FROM jobs WHERE job_id=? FOR UPDATE", (job["job_id"],))
    holder.db.execute(
        "UPDATE jobs SET lease_until=EXTRACT(EPOCH FROM clock_timestamp())+0.2 WHERE job_id=?",
        (job["job_id"],),
    )
    entered = threading.Event()
    original_execute = worker.db.execute

    def observed_execute(statement, parameters=None):
        if "FOR UPDATE" in statement:
            entered.set()
        return original_execute(statement, parameters)

    worker.db.execute = observed_execute
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(getattr(worker, operation), job["job_id"], claimed["lease_token"])
        try:
            assert entered.wait(timeout=2)
            holder.db.execute("SELECT pg_sleep(0.3)")
        finally:
            holder.db.execute("COMMIT")
        assert future.result(timeout=2) is False


def test_parallel_admission_never_exceeds_queue_capacity(isolated_database):
    factory, _, _ = isolated_database
    workers = [factory() for _ in range(20)]
    start = threading.Barrier(len(workers))

    def enqueue(worker):
        start.wait()
        try:
            return worker.enqueue({"case": "queue-capacity"})
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        results = list(executor.map(enqueue, workers))
    assert sum(result is not None for result in results) == MAX_QUEUED_JOBS
    assert workers[0].db.execute("SELECT count(*) FROM jobs").fetchone()[0] == MAX_QUEUED_JOBS


def test_dead_workers_reach_retry_cap_and_release_queue_capacity(isolated_database):
    factory, _, _ = isolated_database
    worker = factory()
    job = worker.enqueue({"case": "dead-worker"})
    tokens = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claimed = worker.claim()
        assert claimed["attempt"] == attempt
        tokens.append(claimed["lease_token"])
        worker.db.execute(
            "UPDATE jobs SET lease_until=EXTRACT(EPOCH FROM clock_timestamp())-1 WHERE job_id=?",
            (job["job_id"],),
        )
    assert len(set(tokens)) == MAX_ATTEMPTS
    assert worker.claim() is None
    assert worker.get(job["job_id"])["status"] == "failed"
    assert worker.get(job["job_id"])["attempts"] == MAX_ATTEMPTS


def test_evaluation_role_cannot_connect_to_other_service_databases(isolated_database):
    _, settings, own_dsn = isolated_database
    own = urlsplit(own_dsn)
    targets = [settings.get("PG_TEST_CONVERSATION_URL"), settings.get("PG_TEST_SCHEDULING_URL")]
    targets = [target for target in targets if target]
    if not targets:
        pytest.skip("Role-isolation integration requires other service test URLs.")
    for target in targets:
        database = urlsplit(target).path
        unauthorized = urlunsplit((own.scheme, own.netloc, database, "", ""))
        with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
            psycopg.connect(unauthorized, connect_timeout=2)


def test_independent_workers_initialize_a_fresh_schema_concurrently(postgres_urls):
    start = threading.Barrier(2)

    def open_store():
        start.wait()
        return JobStore(":memory:", database_url=postgres_urls["evaluation"])

    stores = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(open_store) for _ in range(2)]
            stores.extend(future.result(timeout=10) for future in futures)
        assert len(stores) == 2
        job = stores[0].enqueue({"case": "concurrent-first-start"})
        assert stores[1].get(job["job_id"])["status"] == "pending"
        assert stores[1].claim()["job_id"] == job["job_id"]
    finally:
        for store in stores:
            store.close()
