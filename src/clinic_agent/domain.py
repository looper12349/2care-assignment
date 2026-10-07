"""Scheduling rules. No model or orchestration framework is trusted to waive them."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from clinic_agent.models import BookingProposal, Constraints, Principal, Slot

DEMO_TENANT = "demo-clinic"
DEMO_PATIENTS = {"patient-maya", "patient-arjun"}
PROVIDERS = {"dr-rao": "Dr. Rao", "dr-patel": "Dr. Patel"}


def validate_principal(principal: Principal) -> None:
    if principal.tenant_id != DEMO_TENANT:
        raise PermissionError("This clinic account is not authorized.")
    if principal.role == "patient" and principal.patient_id not in DEMO_PATIENTS:
        raise PermissionError("This patient account is not authorized.")


def check_owner(principal: Principal, state: dict) -> None:
    validate_principal(principal)
    if (
        principal.role != "patient"
        or principal.tenant_id != state["tenant_id"]
        or principal.patient_id != state["patient_id"]
    ):
        raise PermissionError("You cannot access this conversation.")


def safety_signal(text: str) -> str | None:
    """Explicit demo triggers, not a medical triage classifier or diagnosis."""
    lowered = text.casefold()
    historical = bool(
        re.search(r"last (?:year|month)|years? ago|in the past|used to have|history of", lowered)
    )
    current = bool(
        re.search(r"\b(now|right now|currently|today|can't|cannot|having|have)\b", lowered)
    )
    explicitly_resolved = bool(
        re.search(
            r"no symptoms (?:now|currently)|(?:no|without) (?:current|present) symptoms", lowered
        )
    )
    current_urgent = bool(
        re.search(
            r"(?:have|having|currently).{0,20}(?:chest pain|trouble breathing)|(?:chest pain|trouble breathing|cannot breathe|can't breathe).{0,12}(?:now|today|currently)",
            lowered,
        )
    )
    urgent = bool(
        re.search(
            r"chest pain|can(?:not|'t) breathe|trouble breathing|unconscious|severe bleeding|kill myself|suicidal",
            lowered,
        )
    )
    if (
        urgent
        and (not (historical and explicitly_resolved and not current_urgent))
        and (current or not historical)
    ):
        return "emergency_indication"
    if re.search(
        r"(?:ignore|override|bypass).{0,40}(?:instructions|rules|guardrails|confirmation)|system prompt|reveal.{0,20}(?:secrets|api key)",
        lowered,
    ):
        return "instruction_injection"
    if re.search(
        r"(?:another|someone else(?:'s)?|other patient|patient-arjun|patient-maya).{0,35}(?:appointment|record|booking)|(?:appointment|record).{0,25}(?:another patient|someone else|patient-arjun|patient-maya)",
        lowered,
    ):
        return "cross_patient_request"
    return None


def merge_constraints(
    current: dict, updates: dict, *, clear_time: bool = False
) -> tuple[dict, bool]:
    result = Constraints.model_validate(current).model_dump()
    if clear_time:
        result["after_hour"] = result["before_hour"] = result["after_minute"] = result[
            "before_minute"
        ] = None
    for key in (
        "appointment_type",
        "date",
        "after_hour",
        "after_minute",
        "before_hour",
        "before_minute",
        "provider_id",
    ):
        if updates.get(key) is not None:
            result[key] = updates[key]
    validated = Constraints.model_validate(result)
    if validated.appointment_type not in (None, "primary-care"):
        raise ValueError("This appointment type requires staff assistance.")
    if validated.provider_id not in (None, *PROVIDERS):
        raise ValueError("The requested clinician is not in the catalogue.")
    if (
        validated.after_hour is not None
        and validated.before_hour is not None
        and validated.after_hour * 60 + (validated.after_minute or 0)
        >= validated.before_hour * 60 + (validated.before_minute or 0)
    ):
        raise ValueError("The end of your time window must come after its start.")
    return validated.model_dump(), result != current


def slot_matches(slot: Slot, constraints: Constraints) -> bool:
    local = datetime.fromisoformat(slot.start_at).astimezone(ZoneInfo(constraints.timezone))
    minute_of_day = local.hour * 60 + local.minute
    return (
        (
            constraints.appointment_type is None
            or slot.appointment_type == constraints.appointment_type
        )
        and (constraints.date is None or local.date().isoformat() == constraints.date)
        and (
            constraints.after_hour is None
            or minute_of_day > constraints.after_hour * 60 + (constraints.after_minute or 0)
        )
        and (
            constraints.before_hour is None
            or minute_of_day < constraints.before_hour * 60 + (constraints.before_minute or 0)
        )
        and (constraints.provider_id is None or slot.provider_id == constraints.provider_id)
        and slot.location == constraints.location
    )


def validate_confirmation(
    state: dict, principal: Principal, now: datetime, proposal_id: str | None = None
) -> BookingProposal:
    check_owner(principal, state)
    if state["status"] != "awaiting_confirmation" or not state.get("proposal"):
        raise ValueError("Please select an appointment before confirming.")
    proposal = BookingProposal.model_validate(state["proposal"])
    if proposal_id is not None and proposal_id != proposal.proposal_id:
        raise ValueError(
            "That confirmation is out of date. Please use the current appointment details."
        )
    if (
        proposal.patient_id != principal.patient_id
        or proposal.request_revision != state["request_revision"]
    ):
        raise ValueError("Your request changed. Please select and confirm an updated appointment.")
    if datetime.fromisoformat(proposal.expires_at) <= now:
        raise ValueError("These appointment details have expired. Please search again.")
    if not slot_matches(proposal.slot, Constraints.model_validate(state["constraints"])):
        raise ValueError("These details do not match your latest requirements.")
    return proposal


def operation_digest(principal: Principal, proposal: BookingProposal) -> str:
    payload = {
        "tenant": principal.tenant_id,
        "patient": principal.patient_id,
        "proposal": proposal.model_dump(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def describe_slot(slot: Slot) -> str:
    dt = datetime.fromisoformat(slot.start_at).astimezone(ZoneInfo(slot.timezone))
    return f"{dt.strftime('%A, %d %B %Y at %I:%M %p').replace(' 0', ' ')} ({slot.timezone}) with {slot.provider_name} at {slot.location}"
