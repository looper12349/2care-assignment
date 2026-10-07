"""Conversation HTTP integration: identity, feedback, staff access, promotion."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from fastapi.testclient import TestClient

from clinic_agent.auth import sign_context
from clinic_agent.models import Principal
from clinic_agent.storage import ClinicStore

ROOT = Path(__file__).resolve().parents[1]
SERVICE_SECRET = "test-internal-service-secret-0123456789"
SESSION_SECRET = "test-browser-session-secret-0123456789"
FIXED_NOW = datetime.fromisoformat("2026-10-07T09:00:00+05:30")


@pytest.fixture(scope="module")
def accepted_report():
    path = ROOT / ".data/evaluations/demo-proof/report.json"
    if path.exists():
        return json.loads(path.read_text())
    from clinic_agent.quality.runner import run_eval_loop

    (ROOT / ".data").mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="api-evaluation-proof-", dir=ROOT / ".data") as directory:
        return run_eval_loop(directory)


@pytest.fixture
def api_client(monkeypatch, accepted_report):
    import services.conversation.app as module

    (ROOT / ".data").mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="api-test-", dir=ROOT / ".data") as directory:
        monkeypatch.setenv("CLINIC_SERVICE_TOKEN", SERVICE_SECRET)
        monkeypatch.setenv("CLINIC_SESSION_SECRET", SESSION_SECRET)
        monkeypatch.setenv("CLINIC_AGENT_DATA_DIR", directory)
        monkeypatch.setenv("CLINIC_AGENT_FIXED_NOW", FIXED_NOW.isoformat())
        monkeypatch.setenv("CLINIC_AGENT_BEHAVIOR_VERSION", "v1")
        monkeypatch.setenv("AGENT_MODEL", "")
        monkeypatch.setattr(
            module,
            "HTTPSchedulingGateway",
            lambda base_url: ClinicStore(str(Path(directory) / "scheduler.sqlite"), FIXED_NOW),
        )
        observed_requests = []

        def worker(request):
            assert request.headers["X-Service-Token"] == SERVICE_SECRET
            observed_requests.append((request.method, request.url.path))
            if request.method == "POST":
                assert json.loads(request.content)["online"] is False
                return httpx.Response(
                    202,
                    json={
                        "job_id": "job-pending",
                        "status": "pending",
                        "report": None,
                        "error": None,
                    },
                )
            report = copy.deepcopy(accepted_report)
            status = "completed"
            if request.url.path.endswith("job-rejected"):
                report["gates"]["accepted"] = False
                report["candidate"]["accepted"] = False
                report["improvement"]["status"] = "rejected"
            elif request.url.path.endswith("job-forged"):
                report["gates"]["accepted"] = True
                report["gates"]["checks"][0]["passed"] = False
                report["candidate"]["critical_violations"] = 1
            elif request.url.path.endswith("job-incomplete"):
                status = "running"
            elif request.url.path.endswith("job-unsafe-artifact"):
                report["improvement"]["require_new_confirmation"] = False
            return httpx.Response(
                200,
                json={
                    "job_id": request.url.path.rsplit("/", 1)[-1],
                    "status": status,
                    "report": report,
                    "error": None,
                },
            )

        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            module.httpx,
            "AsyncClient",
            lambda *args, **kwargs: real_async_client(
                *args, transport=httpx.MockTransport(worker), **kwargs
            ),
        )
        app = module.create_app()
        with TestClient(app) as client:
            yield client, app, Path(directory), observed_requests


def login(client, patient="patient-maya", role="patient"):
    response = client.post("/api/sessions", json={"patient_key": patient, "role": role})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def conversation(client, headers):
    response = client.post("/api/conversations", headers=headers)
    assert response.status_code == 201
    return response.json()


def test_patient_auth_ownership_and_forged_tokens(api_client):
    client, app, _, _ = api_client
    assert client.post("/api/conversations").status_code == 401
    maya, arjun, staff = login(client), login(client, "patient-arjun"), login(client, role="staff")
    first = conversation(client, maya)
    path = f"/api/conversations/{first['conversation_id']}"
    assert client.get(path, headers=maya).status_code == 200
    assert client.get(path, headers=arjun).status_code == 403
    assert client.get(path, headers=staff).status_code == 403
    assert (
        client.post(
            path + "/messages",
            json={"turn_id": "foreign", "text": "Book an appointment."},
            headers=arjun,
        ).status_code
        == 403
    )
    assert client.post(path + "/feedback", json={"rating": 5}, headers=arjun).status_code == 403
    assert client.post("/api/conversations", headers=staff).status_code == 403
    forged = sign_context(
        Principal(), "different-secret-of-at-least-32-characters", audience="conversation"
    )
    assert client.get(path, headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    wrong_audience = sign_context(Principal(), SESSION_SECRET, audience="scheduling")
    assert (
        client.get(path, headers={"Authorization": f"Bearer {wrong_audience}"}).status_code == 401
    )
    assert app.state.service.behavior_version == "v1"


def test_real_booking_feedback_and_duplicate_http_turn(api_client):
    client, app, _, _ = api_client
    maya = login(client)
    created = conversation(client, maya)
    path = f"/api/conversations/{created['conversation_id']}/messages"
    options = client.post(
        path,
        json={"turn_id": "1", "text": "I need primary care tomorrow after 3 pm."},
        headers=maya,
    ).json()
    chosen = client.post(
        path,
        json={
            "turn_id": "2",
            "text": "Option 1.",
            "action": "select_slot",
            "slot_id": options["options"][0]["slot_id"],
        },
        headers=maya,
    ).json()
    request = {
        "turn_id": "3",
        "text": "Yes.",
        "action": "confirm",
        "proposal_id": chosen["proposal"]["proposal_id"],
    }
    booked = client.post(path, json=request, headers=maya)
    assert booked.status_code == 200
    assert booked.json()["status"] == "booked"
    assert client.post(path, json=request, headers=maya).json() == booked.json()
    world = app.state.service.inspect_world()
    assert len(world["appointments"]) == 1
    assert (
        world["appointments"][0]["appointment_id"] == booked.json()["appointment"]["appointment_id"]
    )
    feedback = client.post(
        f"/api/conversations/{created['conversation_id']}/feedback",
        json={"rating": 2, "comment": "Next time skip asking for confirmation."},
        headers=maya,
    )
    assert feedback.status_code == 201
    assert feedback.json()["behavior_changed"] is False
    assert app.state.service.behavior_version == "v1"
    assert len(app.state.service.inspect_world()["feedback"]) == 1


def test_staff_handoff_access_and_status_progression(api_client):
    client, _, _, _ = api_client
    maya, staff = login(client), login(client, role="staff")
    created = conversation(client, maya)
    escalated = client.post(
        f"/api/conversations/{created['conversation_id']}/messages",
        json={"turn_id": "human", "text": "Please get a human receptionist."},
        headers=maya,
    ).json()
    ticket = escalated["handoff"]["ticket_id"]
    assert client.get("/api/handoffs", headers=maya).status_code == 403
    assert (
        client.patch(
            f"/api/handoffs/{ticket}", json={"status": "accepted"}, headers=maya
        ).status_code
        == 403
    )
    assert client.get("/api/handoffs", headers=staff).json()[0]["ticket_id"] == ticket
    assert (
        client.patch(
            f"/api/handoffs/{ticket}", json={"status": "resolved"}, headers=staff
        ).status_code
        == 400
    )
    assert (
        client.patch(f"/api/handoffs/{ticket}", json={"status": "accepted"}, headers=staff).json()[
            "status"
        ]
        == "accepted"
    )
    assert (
        client.patch(f"/api/handoffs/{ticket}", json={"status": "resolved"}, headers=staff).json()[
            "status"
        ]
        == "resolved"
    )
    assert (
        client.get(f"/api/conversations/{created['conversation_id']}", headers=maya).json()[
            "handoff"
        ]["status"]
        == "resolved"
    )


@pytest.mark.parametrize(
    "job_id", ["job-rejected", "job-forged", "job-incomplete", "job-unsafe-artifact"]
)
def test_activation_blocks_rejected_inconsistent_and_unsafe_reports(api_client, job_id):
    client, app, directory, _ = api_client
    staff = login(client, role="staff")
    response = client.post(
        "/api/behavior/activate", json={"version": "v2", "job_id": job_id}, headers=staff
    )
    assert response.status_code in {400, 409}
    assert app.state.service.behavior_version == "v1"
    assert not (directory / "active_behavior.json").exists()


def test_accepted_activation_persists_and_only_changes_new_conversations(api_client):
    client, app, directory, observed = api_client
    maya, staff = login(client), login(client, role="staff")
    old = conversation(client, maya)
    assert old["behavior_version"] == "v1"
    assert (
        client.post(
            "/api/behavior/activate", json={"version": "v2", "job_id": "job-accepted"}, headers=maya
        ).status_code
        == 403
    )
    response = client.post(
        "/api/behavior/activate", json={"version": "v2", "job_id": "job-accepted"}, headers=staff
    )
    assert response.status_code == 200
    assert response.json()["applies_to"] == "new_conversations"
    assert json.loads((directory / "active_behavior.json").read_text())["version"] == "v2"
    assert conversation(client, maya)["behavior_version"] == "v2"
    assert (
        client.get(f"/api/conversations/{old['conversation_id']}", headers=maya).json()[
            "behavior_version"
        ]
        == "v1"
    )
    assert app.state.service.behavior_version == "v2"
    assert ("GET", "/jobs/job-accepted") in observed


def test_evaluation_proxy_is_staff_only_and_disallows_cloud_upload(api_client):
    client, _, _, observed = api_client
    maya, staff = login(client), login(client, role="staff")
    assert client.post("/api/evaluations/run", json={}, headers=maya).status_code == 403
    assert client.get("/api/evaluations/latest", headers=maya).status_code == 403
    assert (
        client.post("/api/evaluations/run", json={"online": True}, headers=staff).status_code == 400
    )
    started = client.post(
        "/api/evaluations/run", json={"repetitions": 1, "online": False}, headers=staff
    )
    assert started.status_code == 202
    assert started.json()["job_id"] == "job-pending"
    assert ("POST", "/jobs") in observed
    assert (
        client.get("/api/evaluations/jobs/job-accepted", headers=staff).json()["status"]
        == "completed"
    )
