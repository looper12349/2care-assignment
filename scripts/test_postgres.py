"""Run PostgreSQL integration tests against the isolated verification stack."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    from clinic_agent.cli import local_environment

    environment = local_environment()
    from urllib.parse import quote

    urls = {}
    for domain in ("conversation", "scheduling", "evaluation"):
        password = quote(environment[domain.upper() + "_DB_PASSWORD"], safe="")
        urls[domain] = (
            f"postgresql://carepath_{domain}:{password}@127.0.0.1:55432/carepath_{domain}"
        )
    config = ROOT / ".data" / "postgres-test-env.json"
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(urls, stream)
    environment["CAREPATH_POSTGRES_TEST_CONFIG"] = str(config)
    for domain, url in urls.items():
        environment["PG_TEST_" + domain.upper() + "_URL"] = url
    files = sorted(
        str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("test_postgres*.py")
    )
    if not files:
        raise SystemExit("PostgreSQL integration tests have not been created.")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", *files], cwd=ROOT, env=environment
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
