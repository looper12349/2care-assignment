"""Evaluation contract tests exercise evidence, safety gates, and job recovery."""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import requests
from fastapi.testclient import TestClient
from langsmith import Client
from langsmith.evaluation import evaluate
from langsmith.schemas import Example
from pydantic import ValidationError

from clinic_agent.quality.evaluators import world_and_audit
from clinic_agent.quality.improvements import ImprovementArtifact, acceptance_gates
from clinic_agent.quality.runner import run_eval_loop
from clinic_agent.quality.scenario_driver import load_scenarios
from services.evaluation.jobs import LEASE_SECONDS, JobStore


@pytest.fixture(scope="module")
def measured_report(tmp_path_factory):
    return run_eval_loop(tmp_path_factory.mktemp("measured-loop"), repetitions=3)


def test_actual_langsmith_loop_improves_without_regressions(measured_report):
    report = measured_report
    assert report["engine"]["platform"] == "LangSmith SDK"
    assert report["engine"]["upload_results"] is False
    assert report["baseline"]["total"] == report["dataset"]["count"] * 3
    assert report["baseline"]["resolution_rate"] < report["candidate"]["resolution_rate"]
    assert report["candidate"]["critical_violations"] == 0
    assert report["accepted"] is True
    assert all(gate["passed"] for gate in report["gates"]["checks"])
    assert report["improvement"]["source_failure_ids"]
    assert report["improvement"]["require_new_confirmation"] is True
    assert report["improvement"]["status"] == "accepted"
    assert all(row["scores"]["confirmation_valid"] == 1 for row in report["candidate"]["rows"])
    assert json.loads(open(report["files"]["report_json"]).read())["accepted"] is True


def test_offline_sdk_never_sends_http_even_when_tracing_environment_enabled(monkeypatch, tmp_path):
    calls = []

    def reject_http(self, method, url, *args, **kwargs):
        calls.append((method, url))
        raise AssertionError("Offline evaluation must not send HTTP")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setattr(requests.Session, "request", reject_http)
    report = run_eval_loop(tmp_path / "offline")
    assert report["accepted"] is True
    assert calls == []


def test_langsmith_judge_rejects_convincing_receipt_without_world_booking():
    catalog = load_scenarios()
    scenario = catalog["scenarios"][0]
    fake = {
        "scenario_id": scenario["id"],
        "turns": 1,
        "error": None,
        "final_response": {"status": "booked", "appointment": {"appointment_id": "made-up"}},
        "transcript": [
            {
                "role": "assistant",
                "content": "Your appointment is booked!",
                "response": {"status": "booked", "appointment": {"appointment_id": "made-up"}},
            }
        ],
        "world": {"appointments": [], "operations": [], "events": [], "handoffs": []},
    }
    example = Example(
        id=uuid4(),
        dataset_id=uuid4(),
        created_at=datetime.now(UTC),
        inputs={
            "settings": {
                k: catalog[k] for k in ("patient_id", "tenant_id", "fixed_now", "timezone")
            }
        },
        outputs={"scenario": scenario},
    )
    client = Client(
        api_url="http://127.0.0.1:1",
        api_key="local-only",
        auto_batch_tracing=False,
        info={"version": "offline"},
    )
    try:
        results = list(
            evaluate(
                lambda inputs: fake,
                data=[example],
                evaluators=[world_and_audit],
                client=client,
                upload_results=False,
            )
        )
        scores = {item.key: item.score for item in results[0]["evaluation_results"]["results"]}
        assert scores["task_resolution"] == 0
        assert scores["claims_supported"] == 0
    finally:
        client.close()


def test_safety_violation_and_case_regression_block_promotion(measured_report):
    baseline = measured_report["baseline"]
    candidate = copy.deepcopy(measured_report["candidate"])
    artifact = ImprovementArtifact.model_validate(measured_report["improvement"])
    candidate["rows"][0]["scores"]["authorization_preserved"] = 0
    candidate["critical_violations"] = 1
    gates = acceptance_gates(baseline, candidate, artifact, baseline["total"])
    assert not gates["accepted"]
    assert "happy-path:authorization_preserved" in gates["regressions"]
    assert not next(g for g in gates["checks"] if g["name"] == "zero_critical_violations")["passed"]


def test_improvement_schema_cannot_weaken_confirmation_or_expand_retries(measured_report):
    data = measured_report["improvement"]
    for key, value in (
        ("require_new_confirmation", False),
        ("max_refreshes", 10),
        ("preserve_hard_constraints", False),
        ("required_cases", ["slot-conflict"]),
    ):
        with pytest.raises(ValidationError):
            ImprovementArtifact.model_validate({**data, key: value})
    with pytest.raises(ValidationError):
        ImprovementArtifact.model_validate({**data, "arbitrary_code": "do_something()"})


def test_durable_job_lease_reclaims_after_restart_and_rejects_stale_owner(tmp_path):
    path = tmp_path / "eval.sqlite"
    store = JobStore(path)
    created = store.enqueue(
        {"repetitions": 1, "online": False, "interpreter": "demo", "model_name": None}
    )
    first = store.claim(now=100)
    assert first["job_id"] == created["job_id"]
    store.close()
    store = JobStore(path)
    try:
        assert store.claim(now=100 + LEASE_SECONDS - 1) is None
        assert not store.heartbeat(first["job_id"], first["lease_token"], now=100 + LEASE_SECONDS)
        assert not store.finish(
            first["job_id"], first["lease_token"], report={"expired": True}, now=100 + LEASE_SECONDS
        )
        second = store.claim(now=100 + LEASE_SECONDS + 1)
        assert second["attempt"] == 2
        assert not store.finish(first["job_id"], first["lease_token"], report={"stale": True})
        assert store.heartbeat(second["job_id"], second["lease_token"], now=150)
        third = store.claim(now=150 + LEASE_SECONDS + 1)
        assert third["attempt"] == 3
        assert store.claim(now=150 + 2 * LEASE_SECONDS + 2) is None
        assert store.get(created["job_id"])["status"] == "failed"
        assert store.get(created["job_id"])["attempts"] == 3
    finally:
        store.close()


def test_evaluation_worker_auth_and_real_background_job(monkeypatch, tmp_path):
    monkeypatch.setenv("CLINIC_SERVICE_TOKEN", "test-internal-token")
    monkeypatch.setenv("CLINIC_EVAL_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.setenv("CLINIC_EVAL_OUTPUT_DIR", str(tmp_path / "reports"))
    from services.evaluation.app import app

    headers = {"X-Service-Token": "test-internal-token"}
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/jobs/latest").status_code == 401
        assert client.get("/jobs/latest", headers=headers).json()["status"] == "none"
        started = client.post(
            "/jobs",
            json={"repetitions": 1, "online": False, "interpreter": "demo"},
            headers=headers,
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        for _ in range(200):
            assert client.get("/health").status_code == 200
            job = client.get(f"/jobs/{job_id}", headers=headers).json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert job["status"] == "completed", job.get("error")
        assert job["report"]["accepted"] is True
        assert "attempt-1-" in job["report"]["files"]["report_json"]
        assert client.get("/jobs/latest", headers=headers).json()["job_id"] == job_id
        assert client.post("/jobs", json={"repetitions": 2}, headers=headers).status_code == 422
