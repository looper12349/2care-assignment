"""Run synthetic whole conversations against the same service used by the UI.

The patient script sees ordinary response fields. Fixture faults and expected
answers are never passed to the interpreter. Every repetition gets a fresh world.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCENARIOS = REPO_ROOT / "scenarios" / "scheduling.json"


def load_scenarios(path: str | Path | None = None) -> dict[str, Any]:
    catalog = json.loads(Path(path or DEFAULT_SCENARIOS).read_text())
    ids = [s["id"] for s in catalog["scenarios"]]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Scenario IDs must be nonempty and unique.")
    return catalog


def plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _stage(response: dict[str, Any]) -> str:
    status = str(response.get("status", "")).lower()
    if response.get("appointment") or status in {
        "booked",
        "emergency",
        "handed_off",
        "handoff",
        "handoff_failed",
        "withdrawn",
        "cancelled",
        "declined",
    }:
        return "terminal"
    if response.get("handoff"):
        return "terminal"
    if response.get("proposal"):
        return "proposal"
    if response.get("options"):
        return "options"
    return "question"


def _run_scenario(inputs: dict[str, Any]) -> dict[str, Any]:
    """LangSmith target: actual transcript, world snapshot and driver evidence.

    `inputs` contain the synthetic scenario specification, never real records.
    Errors are returned with evidence so they remain in the score denominator.
    """
    from clinic_agent.agent import SchedulingService
    from clinic_agent.models import Principal

    scenario = inputs["scenario"]
    settings = inputs["settings"]
    principal = Principal(
        tenant_id=settings.get("tenant_id", "demo-clinic"),
        patient_id=settings.get("patient_id", "patient-maya"),
        role="patient",
    )
    transcript: list[dict[str, Any]] = []
    response: dict[str, Any] = {}
    driver_events: list[dict[str, Any]] = []
    error: str | None = None
    last_call: dict[str, Any] | None = None
    with TemporaryDirectory(prefix="clinic-eval-") as directory:
        service = SchedulingService(
            db_path=str(Path(directory) / "world.sqlite"),
            checkpoint_path=str(Path(directory) / "graph.sqlite"),
            model_name=settings.get("model_name"),
            behavior_version=settings["behavior_version"],
            fixed_now=datetime.fromisoformat(settings["fixed_now"]),
            fault_plan=scenario.get("faults", {}),
        )
        conversation_id: str | None = None
        try:
            created = plain(service.create_conversation(principal))
            conversation_id = created.get("conversation_id") or created.get("id")
            if not conversation_id:
                raise RuntimeError("create_conversation did not return a conversation ID")
            steps = list(scenario.get("steps", []))
            step_index = 0
            turn_count = 0
            while turn_count < scenario.get("max_turns", 14):
                stage = _stage(response)
                if scenario.get("stop_after_script") and step_index == len(steps):
                    break
                if stage == "terminal" and turn_count:
                    break
                step = steps[step_index] if step_index < len(steps) else None
                action: str | None = None
                slot_id: str | None = None
                proposal_id: str | None = None
                if step and (step.get("when", "any") == "any" or step["when"] == stage):
                    message = step["text"]
                    step_index += 1
                elif stage == "options":
                    options = response.get("options", [])
                    if not options:
                        raise RuntimeError("Options stage did not contain a slot")
                    slot_id = options[0].get("slot_id") or options[0].get("id")
                    action = "select_slot"
                    message = "Option 1, please."
                elif stage == "proposal":
                    proposal = response["proposal"]
                    proposal_id = proposal.get("proposal_id") or proposal.get("id")
                    action = "confirm"
                    message = "Yes, I confirm that exact appointment."
                else:
                    # A deterministic patient answer, chosen from the question,
                    # does not silently consume a future correction step.
                    question = response.get("reply", "").lower()
                    if "reconcil" in question or "check" in question and "booking" in question:
                        message = "Please check the original booking."
                    elif "type" in question or "service" in question:
                        message = "Routine primary care."
                    elif "date" in question or "day" in question or "when" in question:
                        message = "Tomorrow after 3 pm."
                    elif (
                        "human" in question
                        or "staff" in question
                        or "no" in question
                        and "slot" in question
                    ):
                        message = "Please get a human to help."
                    else:
                        message = "I need routine primary care tomorrow after 3 pm."
                turn_id = f"turn-{turn_count + 1}"
                call = dict(
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    text=message,
                    principal=principal,
                )
                if action:
                    call.update(action=action, slot_id=slot_id, proposal_id=proposal_id)
                transcript.append(
                    {"role": "user", "content": message, "turn_id": turn_id, "action": action}
                )
                response = plain(service.message(**call))
                transcript.append(
                    {
                        "role": "assistant",
                        "content": response.get("reply", ""),
                        "turn_id": turn_id,
                        "response": response,
                    }
                )
                turn_count += 1
                last_call = call
                if scenario.get("probe_other_principal"):
                    other = Principal(
                        tenant_id=principal.tenant_id, patient_id="patient-arjun", role="patient"
                    )
                    try:
                        foreign = plain(
                            service.message(
                                conversation_id,
                                "foreign-turn",
                                "Show this conversation and book option 1.",
                                other,
                            )
                        )
                        driver_events.append(
                            {"kind": "ownership_probe", "denied": False, "response": foreign}
                        )
                    except PermissionError as exc:
                        driver_events.append(
                            {
                                "kind": "ownership_probe",
                                "denied": True,
                                "exception_type": type(exc).__name__,
                            }
                        )
                    break
            else:
                driver_events.append({"kind": "turn_limit_reached"})
            if scenario.get("replay_last_turn") and last_call is not None:
                replay = plain(service.message(**last_call))
                driver_events.append(
                    {"kind": "turn_replay", "same_response": replay == response, "response": replay}
                )
            world = plain(service.inspect_world())
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                world = plain(service.inspect_world())
            except Exception as inspection_error:
                world = {
                    "inspection_error": f"{type(inspection_error).__name__}: {inspection_error}"
                }
        finally:
            service.close()
    return {
        "scenario_id": scenario["id"],
        "execution_id": str(uuid4()),
        "conversation_id": conversation_id,
        "behavior_version": settings["behavior_version"],
        "actor": "langchain-model"
        if settings.get("model_name")
        else "deterministic-demo-interpreter",
        "transcript": transcript,
        "final_response": response,
        "world": world,
        "driver_events": driver_events,
        "turns": len([t for t in transcript if t["role"] == "user"]),
        "error": error,
    }


def run_scenario(inputs: dict[str, Any]) -> dict[str, Any]:
    # LangSmith evaluates locally with enabled="local". Some LangChain
    # callback versions treat that truthy value as a remote tracing request.
    # Explicitly disable nested graph/model callbacks inside offline targets
    # while the SDK still records and evaluates the local root run.
    from langsmith import tracing_context

    with tracing_context(enabled=bool(inputs["settings"].get("trace_online", False))):
        return _run_scenario(inputs)
