"""Verify a running four-service installation through real HTTP requests."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--evaluation", action="store_true", help="Also verify LangSmith jobs and gated activation"
    )
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=30) as client:

        def call(method, path, *, headers=None, body=None, expected=200):
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code == expected, (
                method,
                path,
                response.status_code,
                response.text,
            )
            return response.json()

        def login(patient="patient-maya", role="patient"):
            result = call("POST", "/api/sessions", body={"patient_key": patient, "role": role})
            return {"Authorization": "Bearer " + result["token"]}

        maya, arjun, staff = login(), login("patient-arjun"), login(role="staff")
        created = call("POST", "/api/conversations", headers=maya, expected=201)
        path = "/api/conversations/" + created["conversation_id"]
        call("GET", path, headers=arjun, expected=403)

        def message(text, **extra):
            return call(
                "POST",
                path + "/messages",
                headers=maya,
                body={"text": text, "turn_id": uuid4().hex, **extra},
            )

        offered = message("I need routine primary care tomorrow after 3 pm.")
        assert offered["options"], offered
        selected = message(
            "Choose this appointment.",
            action="select_slot",
            slot_id=offered["options"][0]["slot_id"],
        )
        assert selected["proposal"] and selected["status"] == "awaiting_confirmation", selected
        consent = {
            "text": "Yes.",
            "turn_id": uuid4().hex,
            "action": "confirm",
            "proposal_id": selected["proposal"]["proposal_id"],
        }
        booked = call("POST", path + "/messages", headers=maya, body=consent)
        replay = call("POST", path + "/messages", headers=maya, body=consent)
        assert booked["status"] == "booked" and booked["appointment"] == replay["appointment"], (
            booked
        )
        call(
            "POST",
            path + "/feedback",
            headers=maya,
            body={"rating": 5, "comment": "Synthetic HTTP smoke verification."},
            expected=201,
        )
        handoff_conversation = call("POST", "/api/conversations", headers=maya, expected=201)
        escalated = call(
            "POST",
            f"/api/conversations/{handoff_conversation['conversation_id']}/messages",
            headers=maya,
            body={"turn_id": uuid4().hex, "text": "Please get a human receptionist."},
        )
        ticket = escalated["handoff"]["ticket_id"]
        call("GET", "/api/handoffs", headers=maya, expected=403)
        call("PATCH", "/api/handoffs/" + ticket, headers=staff, body={"status": "accepted"})
        call("PATCH", "/api/handoffs/" + ticket, headers=staff, body={"status": "resolved"})
        latest = call("GET", "/api/evaluations/latest", headers=staff)
        if args.evaluation:
            started = call(
                "POST",
                "/api/evaluations/run",
                headers=staff,
                body={"repetitions": 3, "online": False},
                expected=202,
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                latest = call("GET", "/api/evaluations/jobs/" + started["job_id"], headers=staff)
                if latest["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.5)
            assert latest["status"] == "completed", latest.get("error", "Evaluation timed out.")
            report = latest["report"]
            assert report["gates"]["accepted"] and report["candidate"]["critical_violations"] == 0
            assert report["baseline"]["passed"] == 60 and report["candidate"]["passed"] == 66
            call(
                "POST",
                "/api/behavior/activate",
                headers=staff,
                body={"version": "v2", "job_id": started["job_id"]},
            )
            fresh = call("POST", "/api/conversations", headers=maya, expected=201)
            assert fresh["behavior_version"] == "v2"
            assert (
                call("GET", path, headers=maya)["behavior_version"] == created["behavior_version"]
            )
            print(
                "LangSmith job passed: 60/66 -> 66/66; gated activation and pinned conversations verified."
            )
        output = Path(".data") / "http-smoke.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "booking": booked,
                    "handoff_ticket": ticket,
                    "evaluation_job": latest.get("job_id"),
                    "checks_passed": True,
                },
                indent=2,
            )
        )
        print(
            "Real HTTP smoke passed: booking, replay, patient isolation, feedback, staff handoff."
        )
        print(f"Synthetic receipt: {output}")


if __name__ == "__main__":
    main()
