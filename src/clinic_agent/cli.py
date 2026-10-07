"""One-command local orchestration and reproducible synthetic experiments."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def local_environment() -> dict[str, str]:
    load_dotenv(ROOT / ".env", override=False)
    environment = dict(os.environ)
    data = Path(environment.get("CLINIC_AGENT_DATA_DIR", str(ROOT / ".data"))).resolve()
    data.mkdir(parents=True, exist_ok=True)
    secrets_file = data / "local-secrets.json"
    if not secrets_file.exists():
        descriptor = os.open(secrets_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                {
                    "CLINIC_SERVICE_TOKEN": secrets.token_urlsafe(48),
                    "CLINIC_SESSION_SECRET": secrets.token_urlsafe(48),
                },
                stream,
            )
    configured = json.loads(secrets_file.read_text())
    changed = False
    for key in (
        "POSTGRES_PASSWORD",
        "CONVERSATION_DB_PASSWORD",
        "SCHEDULING_DB_PASSWORD",
        "EVALUATION_DB_PASSWORD",
    ):
        if key not in configured:
            configured[key] = secrets.token_urlsafe(36)
            changed = True
    if changed:
        temporary = secrets_file.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(configured, stream)
        temporary.replace(secrets_file)
    for key, value in configured.items():
        if not environment.get(key):
            environment[key] = value
    environment["CLINIC_AGENT_DATA_DIR"] = str(data)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT), environment.get("PYTHONPATH", "")]
    )
    # Keep each service's state in its own file. Evaluation scenarios create
    # separate disposable clinics and cannot reach the live scheduler gateway.
    environment.setdefault("CLINIC_EVAL_DB", str(data / "evaluation.sqlite3"))
    environment.setdefault("CLINIC_EVAL_OUTPUT_DIR", str(data / "evaluations"))
    environment.setdefault("SCHEDULING_URL", "http://127.0.0.1:8002")
    environment.setdefault("EVALUATION_URL", "http://127.0.0.1:8003")
    environment.setdefault("CONVERSATION_URL", "http://127.0.0.1:8001")
    environment.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    return environment


def serve(*, production_web: bool = False) -> int:
    environment = local_environment()
    data = Path(environment["CLINIC_AGENT_DATA_DIR"])
    logs = data / "logs"
    logs.mkdir(exist_ok=True)
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("Install Node.js and npm to start the Next.js frontend.")
    web = ROOT / "apps" / "web"
    if not (web / "node_modules" / "next").exists():
        subprocess.run([npm, "ci"], cwd=web, check=True)
    if production_web and not (web / ".next" / "BUILD_ID").exists():
        subprocess.run([npm, "run", "build"], cwd=web, check=True, env=environment)
    processes: list[subprocess.Popen] = []
    streams = []
    specs = [
        (
            "scheduling",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "services.scheduling.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8002",
            ],
            ROOT,
        ),
        (
            "evaluation",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "services.evaluation.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8003",
            ],
            ROOT,
        ),
        (
            "conversation",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "services.conversation.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
            ],
            ROOT,
        ),
        (
            "web",
            [
                npm,
                "run",
                "start" if production_web else "dev",
                "--",
                "--hostname",
                "127.0.0.1",
                "--port",
                "3000",
            ],
            web,
        ),
    ]
    try:
        for name, command, directory in specs:
            stream = (logs / f"{name}.log").open("a")
            streams.append(stream)
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=directory,
                    env=environment,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=2) as client:
            while time.monotonic() < deadline:
                for (name, _, _), process in zip(specs, processes, strict=True):
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited. Inspect {logs / (name + '.log')}.")
                try:
                    checks = [
                        client.get(f"http://127.0.0.1:{port}/health").status_code == 200
                        for port in (8001, 8002, 8003)
                    ]
                    checks.append(client.get("http://127.0.0.1:3000/api/config").status_code == 200)
                    if all(checks):
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError(f"Startup timed out. Inspect service logs in {logs}.")
        print("CarePath is running: http://localhost:3000", flush=True)
        print("Four independent services · synthetic clinic · LangSmith evaluation", flush=True)
        print(f"Logs: {logs}\nPress Ctrl+C to stop all services.", flush=True)
        while True:
            for (name, _, _), process in zip(specs, processes, strict=True):
                if process.poll() is not None:
                    raise RuntimeError(f"{name} stopped. Inspect its service log.")
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        for stream in streams:
            stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="CarePath local four-service scheduling agent")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Start all four services")
    server.add_argument(
        "--production-web", action="store_true", help="Build and serve optimized Next.js"
    )
    evaluation = commands.add_parser("eval-loop", help="Run the LangSmith before/after experiment")
    evaluation.add_argument("--repetitions", type=int, choices=(1, 3), default=3)
    evaluation.add_argument("--output", type=Path)
    evaluation.add_argument(
        "--online", action="store_true", help="Explicitly upload synthetic experiments to LangSmith"
    )
    evaluation.add_argument(
        "--live", action="store_true", help="Evaluate the configured real LangChain model"
    )
    args = parser.parse_args()
    if args.command == "serve":
        return serve(production_web=args.production_web)
    load_dotenv(ROOT / ".env", override=False)
    from clinic_agent.quality.runner import run_eval_loop

    output = args.output or ROOT / ".data" / "evaluations" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    report = run_eval_loop(
        output,
        repetitions=args.repetitions,
        online=args.online,
        interpreter="live" if args.live else "demo",
        model_name=os.getenv("AGENT_MODEL") if args.live else None,
    )
    print(f"Baseline: {report['baseline']['passed']}/{report['baseline']['total']}")
    print(f"Candidate: {report['candidate']['passed']}/{report['candidate']['total']}")
    print(f"Accepted: {report['gates']['accepted']}")
    print(f"Report: {output / 'report.html'}")
    return 0 if report["gates"]["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
