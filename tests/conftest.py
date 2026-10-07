"""Disposable schemas for optional integration tests on a real PostgreSQL server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest


@pytest.fixture
def postgres_urls():
    config = os.getenv("CAREPATH_POSTGRES_TEST_CONFIG")
    if not config:
        pytest.skip("Run scripts/test_postgres.py with the isolated verification database.")
    import psycopg
    from psycopg import sql

    originals = json.loads(Path(config).read_text())
    schema = "test_" + uuid4().hex
    scoped = {}
    connections = []
    try:
        for domain, url in originals.items():
            try:
                connection = psycopg.connect(url, autocommit=True, connect_timeout=5)
            except psycopg.Error:
                raise RuntimeError(
                    "The isolated PostgreSQL verification database is unavailable."
                ) from None
            connections.append(connection)
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            parsed = urlsplit(url)
            params = dict(parse_qsl(parsed.query))
            params["options"] = f"-csearch_path={schema}"
            scoped[domain] = urlunsplit((*parsed[:3], urlencode(params), parsed.fragment))
        yield scoped
    finally:
        for connection in connections:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            connection.close()
