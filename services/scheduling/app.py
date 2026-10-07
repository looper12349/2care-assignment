"""Synthetic clinic service. It alone owns slot capacity and booking operations."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import Field

from clinic_agent.auth import verify_context
from clinic_agent.domain import operation_digest, slot_matches, validate_principal
from clinic_agent.models import BookingProposal, Constraints, Principal, StrictModel
from clinic_agent.settings import Settings
from clinic_agent.storage import ClinicStore

settings = Settings.from_env()


def now() -> datetime:
    return settings.fixed_now or datetime.now(UTC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_storage()
    app.state.store = ClinicStore(
        os.getenv("SCHEDULING_DATABASE_URL") or str(settings.data_dir / "scheduling.sqlite3"), now()
    )
    yield
    app.state.store.close()


app = FastAPI(title="Clinic scheduling service", version="0.1.0", lifespan=lifespan)


def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(403, "Signed service identity is required.")
    try:
        result = verify_context(authorization.removeprefix("Bearer "), audience="scheduling")
        validate_principal(result)
        return result
    except (ValueError, PermissionError) as exc:
        raise HTTPException(403, "Invalid service identity.") from exc


class StateRef(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    tenant_id: str
    patient_id: str
    request_revision: int = Field(ge=0)
    constraints: Constraints
    status: str


class BookBody(StrictModel):
    proposal: BookingProposal
    operation_id: str = Field(min_length=1, max_length=128)
    digest: str = Field(min_length=64, max_length=64)
    state: StateRef


class LookupBody(StrictModel):
    state: StateRef


def scoped_state(state: StateRef, identity: Principal) -> dict:
    if (
        identity.role != "patient"
        or state.tenant_id != identity.tenant_id
        or state.patient_id != identity.patient_id
    ):
        raise HTTPException(403, "Patient scope mismatch.")
    return state.model_dump()


@app.get("/health")
def health():
    return {"status": "ok", "service": "scheduling", "synthetic_data": True}


@app.get("/internal/slots")
def slots(
    identity: Annotated[Principal, Depends(principal)],
    constraints: str = Query(max_length=2000),
    conversation_id: str = Query(min_length=1, max_length=128),
    request_revision: int = Query(ge=0),
):
    if identity.role != "patient":
        raise HTTPException(403, "A scoped patient identity is required.")
    try:
        filters = Constraints.model_validate(json.loads(constraints))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid availability filters.") from exc
    if (
        filters.timezone != "Asia/Kolkata"
        or filters.location != "Main clinic"
        or filters.appointment_type != "primary-care"
    ):
        raise HTTPException(400, "Unsupported clinic filters.")
    state = {
        "conversation_id": conversation_id,
        "request_revision": request_revision,
        **identity.model_dump(),
    }
    results = app.state.store.search(identity, filters, state, now())
    return {"slots": [slot.model_dump() for slot in results]}


@app.post("/internal/book")
def book(body: BookBody, identity: Annotated[Principal, Depends(principal)]):
    state = scoped_state(body.state, identity)
    proposal = body.proposal
    if (
        proposal.patient_id != identity.patient_id
        or proposal.request_revision != body.state.request_revision
    ):
        raise HTTPException(403, "Proposal scope mismatch.")
    # The store checks an existing operation before freshness/capacity, so exact
    # idempotent replay still returns its receipt after the proposal expires.
    if not slot_matches(proposal.slot, body.state.constraints):
        raise HTTPException(400, "Incompatible proposal.")
    if (
        body.digest != operation_digest(identity, proposal)
        or body.operation_id != f"op-{proposal.proposal_id.removeprefix('proposal-')}"
    ):
        raise HTTPException(400, "Operation does not match this proposal.")
    return app.state.store.book(identity, proposal, body.operation_id, body.digest, state, now())


@app.post("/internal/operations/{operation_id}/lookup")
def lookup(operation_id: str, body: LookupBody, identity: Annotated[Principal, Depends(principal)]):
    state = scoped_state(body.state, identity)
    return app.state.store.lookup(identity, operation_id, state, now())


@app.get("/internal/world")
def world(identity: Annotated[Principal, Depends(principal)]):
    if identity.role != "staff":
        raise HTTPException(403, "Staff access is required.")
    return app.state.store.inspect_world()
