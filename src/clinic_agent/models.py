"""Validated boundaries shared by the API, workflow, and evaluation runner."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Principal(StrictModel):
    tenant_id: str = "demo-clinic"
    patient_id: str = "patient-maya"
    role: Literal["patient", "staff"] = "patient"


class Constraints(StrictModel):
    appointment_type: str | None = None
    date: str | None = None
    after_hour: int | None = Field(default=None, ge=0, le=23)
    after_minute: int | None = Field(default=None, ge=0, le=59)
    before_hour: int | None = Field(default=None, ge=0, le=23)
    before_minute: int | None = Field(default=None, ge=0, le=59)
    provider_id: str | None = None
    timezone: str = "Asia/Kolkata"
    location: str = "Main clinic"


class Interpretation(StrictModel):
    intent: Literal["request", "select", "confirm", "decline", "human", "clinical", "unknown"] = (
        "unknown"
    )
    appointment_type: str | None = None
    date: str | None = None
    after_hour: int | None = Field(default=None, ge=0, le=23)
    after_minute: int | None = Field(default=None, ge=0, le=59)
    before_hour: int | None = Field(default=None, ge=0, le=23)
    before_minute: int | None = Field(default=None, ge=0, le=59)
    provider_id: str | None = None
    selection_index: int | None = Field(default=None, ge=1, le=5)
    selection_slot_id: str | None = None
    ambiguous_date: bool = False
    unsupported_service: bool = False
    unsupported_context: bool = False
    clear_time_preference: bool = False
    changes_with_confirmation: bool = False


class Slot(StrictModel):
    slot_id: str
    start_at: str
    end_at: str
    provider_id: str
    provider_name: str
    appointment_type: str = "primary-care"
    location: str = "Main clinic"
    timezone: str = "Asia/Kolkata"


class BookingProposal(StrictModel):
    proposal_id: str
    request_revision: int
    patient_id: str
    slot: Slot
    expires_at: str


class Appointment(StrictModel):
    appointment_id: str
    operation_id: str
    tenant_id: str
    patient_id: str
    slot_id: str
    start_at: str
    provider_id: str
    provider_name: str
    appointment_type: str
    location: str
    created_at: str


class Handoff(StrictModel):
    ticket_id: str
    conversation_id: str
    tenant_id: str
    patient_id: str
    reason: str
    destination: Literal["administrative", "clinical", "operations"]
    status: Literal["queued", "accepted", "resolved"] = "queued"
    summary: str
    created_at: str


class ConversationResponse(StrictModel):
    conversation_id: str
    status: str
    reply: str
    options: list[Slot] = Field(default_factory=list)
    proposal: BookingProposal | None = None
    appointment: Appointment | None = None
    handoff: Handoff | None = None
    request_revision: int = 0
    behavior_version: str
    mode: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    outstanding_question: str | None = None


class Feedback(StrictModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str = Field(default="", max_length=2000)
    created_at: datetime
