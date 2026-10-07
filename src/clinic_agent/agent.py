"""The conversation service: a guarded LangGraph workflow, not an open-ended agent."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from clinic_agent.database import connect_database, is_postgres
from clinic_agent.domain import (
    check_owner,
    describe_slot,
    merge_constraints,
    operation_digest,
    safety_signal,
    validate_confirmation,
    validate_principal,
)
from clinic_agent.interpretation import Interpreter, explicit_confirmation
from clinic_agent.models import (
    BookingProposal,
    Constraints,
    ConversationResponse,
    Interpretation,
    Principal,
    Slot,
)
from clinic_agent.storage import ClinicStore, ConversationStore


class WorkState(TypedDict, total=False):
    conversation: dict[str, Any]
    principal: dict[str, Any]
    turn: dict[str, Any]
    interpretation: dict[str, Any]
    skip: bool
    changed: bool


class SchedulingService:
    def __init__(
        self,
        db_path: str = "data/scheduling.sqlite3",
        checkpoint_path: str | None = None,
        model_name: str | None = None,
        behavior_version: str = "v1",
        fixed_now: datetime | None = None,
        fault_plan: dict | None = None,
        scheduler: Any | None = None,
    ):
        self.fixed_now = fixed_now
        self.behavior_version = behavior_version
        if behavior_version not in {"v1", "v2"}:
            raise ValueError("Unknown behavior version.")
        self.interpreter = Interpreter(model_name)
        if is_postgres(db_path) and scheduler is None:
            raise ValueError(
                "A PostgreSQL conversation service needs a separate scheduling gateway."
            )
        self.scheduler = scheduler or ClinicStore(db_path, self.now(), fault_plan)
        self.owns_scheduler = scheduler is None
        conversation_path = (
            db_path
            if scheduler is not None
            else (":memory:" if db_path == ":memory:" else f"{db_path}.conversations.sqlite3")
        )
        self.store = ConversationStore(conversation_path, self.now(), fault_plan)
        self.store.initialize_behavior(behavior_version, self.now())
        self._locks: dict[str, threading.RLock] = {}
        self._locks_lock = threading.Lock()
        checkpoint_path = checkpoint_path or (
            db_path
            if is_postgres(db_path)
            else ":memory:"
            if db_path == ":memory:"
            else f"{conversation_path}.checkpoints.sqlite3"
        )
        if is_postgres(checkpoint_path):
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row

            self._checkpoint_connection = psycopg.connect(
                checkpoint_path, autocommit=True, row_factory=dict_row
            )
            checkpointer = PostgresSaver(self._checkpoint_connection)
            migration_guard = connect_database(checkpoint_path)
            try:
                with migration_guard.advisory_lock("clinic:checkpoint-migrations"):
                    checkpointer.setup()
            finally:
                migration_guard.close()
        else:
            if is_postgres(db_path):
                raise ValueError("PostgreSQL conversations require PostgreSQL checkpoints.")
            if checkpoint_path != ":memory:":
                Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
            checkpointer = SqliteSaver(self._checkpoint_connection)
        graph = StateGraph(WorkState)
        graph.add_node("guard", self._guard)
        graph.add_node("interpret", self._interpret)
        graph.add_node("merge_requirements", self._merge)
        graph.add_node("permitted_action", self._act)
        graph.add_node("persist_response", self._persist)
        graph.add_edge(START, "guard")
        graph.add_conditional_edges(
            "guard", lambda value: "persist_response" if value.get("skip") else "interpret"
        )
        graph.add_edge("interpret", "merge_requirements")
        graph.add_edge("merge_requirements", "permitted_action")
        graph.add_edge("permitted_action", "persist_response")
        graph.add_edge("persist_response", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    def now(self) -> datetime:
        return self.fixed_now or datetime.now(UTC)

    def _lock(self, conversation_id: str) -> threading.RLock:
        with self._locks_lock:
            return self._locks.setdefault(conversation_id, threading.RLock())

    @contextmanager
    def _conversation_lock(self, conversation_id: str, tenant_id: str):
        # The PG lock uses its own session; neither the store connection nor the
        # checkpointer's connection is held while waiting for another replica.
        with (
            self._lock(conversation_id),
            self.store.connection.advisory_lock(f"conversation:{tenant_id}:{conversation_id}"),
        ):
            yield

    def current_behavior_version(self) -> str:
        self.behavior_version = self.store.active_behavior()
        return self.behavior_version

    def set_active_behavior(
        self, version: str, job_id: str, artifact_id: str, principal: Principal | None = None
    ) -> None:
        self.store.set_active_behavior(version, job_id, artifact_id, self.now(), principal)
        self.behavior_version = version

    def create_conversation(self, principal: Principal) -> ConversationResponse:
        validate_principal(principal)
        if principal.role != "patient":
            raise PermissionError("Use a patient account for scheduling.")
        state = {
            "conversation_id": uuid4().hex,
            "tenant_id": principal.tenant_id,
            "patient_id": principal.patient_id,
            "status": "collecting",
            "constraints": Constraints().model_dump(),
            "request_revision": 0,
            "behavior_version": self.current_behavior_version(),
            "mode": self.interpreter.mode,
            "options": [],
            "proposal": None,
            "appointment": None,
            "handoff": None,
            "operation_id": None,
            "refresh_count": 0,
            "clarification_count": 0,
            "turn_count": 0,
            "messages": [],
            "events": [],
            "provenance": {},
            "reply": "Hi! I can help you book a routine primary care appointment at Main clinic. Which day would you like? This demonstration uses synthetic patients and appointments.",
            "outstanding_question": "appointment_date",
        }
        self._event(state, "conversation_created", mode=self.interpreter.mode)
        self.store.put_conversation(state)
        return self._response(state)

    @traceable(name="scheduling.turn", run_type="chain")
    def message(
        self,
        conversation_id: str,
        turn_id: str,
        text: str,
        principal: Principal,
        action: str | None = None,
        slot_id: str | None = None,
        proposal_id: str | None = None,
    ) -> ConversationResponse:
        if action not in {None, "select_slot", "confirm", "decline"}:
            raise ValueError("Unsupported action.")
        if not turn_id or len(turn_id) > 128 or len(text) > 4000:
            raise ValueError("A bounded message and turn ID are required.")
        request = {"text": text, "action": action, "slot_id": slot_id, "proposal_id": proposal_id}
        with self._conversation_lock(conversation_id, principal.tenant_id):
            state = self.store.get_conversation(conversation_id)
            check_owner(principal, state)
            cached = self.store.cached_turn(conversation_id, turn_id, request)
            if cached:
                return ConversationResponse.model_validate(cached)
            work = self.graph.invoke(
                {
                    "conversation": state,
                    "principal": principal.model_dump(),
                    "turn": {**request, "turn_id": turn_id},
                    "skip": False,
                    "changed": False,
                    "interpretation": Interpretation().model_dump(),
                },
                config={
                    "configurable": {"thread_id": f"{principal.tenant_id}:{conversation_id}"},
                    "recursion_limit": 15,
                },
                durability="sync",
            )
            response = self._response(work["conversation"])
            self.store.save_turn(conversation_id, turn_id, request, response.model_dump())
            return response

    def get_conversation(self, conversation_id: str, principal: Principal) -> ConversationResponse:
        state = self.store.get_conversation(conversation_id)
        check_owner(principal, state)
        if state.get("handoff"):
            with self.store.lock:
                row = self.store.connection.execute(
                    "SELECT * FROM handoffs WHERE ticket_id=?", (state["handoff"]["ticket_id"],)
                ).fetchone()
                if row:
                    state["handoff"] = dict(row)
        return self._response(state)

    def _event(self, state: dict, kind: str, **data: Any) -> None:
        event = self.store.event(kind, state, self.now(), **data)
        state["events"].append(event)

    def _guard(self, work: WorkState) -> dict:
        state, turn = work["conversation"], work["turn"]
        principal = Principal.model_validate(work["principal"])
        check_owner(principal, state)
        state["turn_count"] += 1
        state["messages"].append(
            {"role": "user", "content": turn["text"], "turn_id": turn["turn_id"]}
        )
        signal = safety_signal(turn["text"])
        if signal:
            self._event(state, "guardrail_triggered", reason=signal)
            if signal == "emergency_indication":
                state.update(
                    status="emergency",
                    options=[],
                    proposal=None,
                    outstanding_question=None,
                    reply="If you may be in immediate danger, contact your local emergency number now or go to the nearest emergency department. Do not wait for an appointment or this chat. I have not contacted emergency services. This demo cannot assess or diagnose symptoms.",
                )
            else:
                state.update(
                    status="blocked",
                    reply="I can only help with your own appointment and must keep booking confirmation and access checks in place. Please describe your own routine scheduling request.",
                    outstanding_question="own_scheduling_request",
                )
            return {"conversation": state, "skip": True}
        if state.get("operation_id") and state["status"] == "unknown_outcome":
            self._reconcile(state, principal)
            return {"conversation": state, "skip": True}
        if state["turn_count"] > 20:
            self._handoff(
                state,
                principal,
                "turn_limit",
                "administrative",
                "The conversation needs staff assistance after its configured turn limit.",
            )
            return {"conversation": state, "skip": True}
        return {"conversation": state, "skip": False}

    def _interpret(self, work: WorkState) -> dict:
        state = work["conversation"]
        try:
            parsed = self.interpreter.interpret(work["turn"]["text"], self.now(), state)
        except Exception as exc:
            self._event(state, "model_unavailable", error_type=type(exc).__name__)
            parsed = Interpretation(intent="human")
        return {"interpretation": parsed.model_dump()}

    def _merge(self, work: WorkState) -> dict:
        state = work["conversation"]
        parsed = Interpretation.model_validate(work["interpretation"])
        if state["status"] in {"booked", "withdrawn", "escalated", "handoff_failed", "emergency"}:
            return {"changed": False}
        if parsed.ambiguous_date or parsed.unsupported_context or parsed.unsupported_service:
            # Keep incompatible input from being combined with an old valid proposal.
            state.update(options=[], proposal=None, status="collecting")
            return {"conversation": state, "changed": False}
        try:
            updates, changed = merge_constraints(
                state["constraints"], parsed.model_dump(), clear_time=parsed.clear_time_preference
            )
        except ValueError as exc:
            state.update(
                options=[],
                proposal=None,
                status="collecting",
                reply=str(exc),
                outstanding_question="requirements",
            )
            return {"conversation": state, "changed": False, "skip": True}
        if changed:
            state["constraints"] = updates
            state["request_revision"] += 1
            state.update(options=[], proposal=None, status="collecting", clarification_count=0)
            for key, value in parsed.model_dump().items():
                if key in updates and value is not None:
                    state["provenance"][key] = work["turn"]["turn_id"]
            self._event(
                state, "constraints_changed", constraints=updates, turn_id=work["turn"]["turn_id"]
            )
        return {"conversation": state, "changed": changed}

    def _act(self, work: WorkState) -> dict:
        state, turn = work["conversation"], work["turn"]
        principal = Principal.model_validate(work["principal"])
        parsed = Interpretation.model_validate(work["interpretation"])
        if work.get("skip"):
            return {"conversation": state}
        if parsed.intent == "human":
            self._handoff(
                state,
                principal,
                "human_requested",
                "administrative",
                "The patient requested a receptionist.",
            )
        elif parsed.intent == "clinical":
            self._handoff(
                state,
                principal,
                "clinical_judgment",
                "clinical",
                "The patient asked for medical judgment outside routine appointment scheduling.",
            )
        elif state["status"] in {"booked", "escalated", "handoff_failed", "withdrawn", "emergency"}:
            state["reply"] = (
                "This conversation has finished. Start a new conversation for another request. "
                + state["reply"]
            )
        elif parsed.unsupported_service:
            self._handoff(
                state,
                principal,
                "unsupported_service",
                "administrative",
                "This demo supports new routine primary care appointments; the requested service needs staff assistance.",
            )
        elif parsed.unsupported_context:
            self._clarify(
                state,
                principal,
                "This demo offers appointments at Main clinic in Asia/Kolkata only. Would you like that location and timezone, or help from a receptionist?",
                "location_timezone",
            )
        elif parsed.ambiguous_date:
            self._clarify(
                state,
                principal,
                "Please give an unambiguous date such as 2026-10-09 or Friday, and a time with AM/PM. I will not book until those details are clear.",
                "date_time",
            )
        elif turn["action"] == "decline" or parsed.intent == "decline":
            state.update(
                status="withdrawn",
                options=[],
                proposal=None,
                outstanding_question=None,
                reply="Understood. I have not booked an appointment. You can start a new conversation whenever you are ready.",
            )
            self._event(state, "consent_withdrawn", turn_id=turn["turn_id"])
        elif work.get("changed"):
            self._continue_request(state, principal)
        elif parsed.changes_with_confirmation:
            state.update(proposal=None, options=[], status="collecting")
            self._clarify(
                state,
                principal,
                "Your message includes a change, so I have not booked. Please tell me the complete new requirement.",
                "requirements",
            )
        elif turn["action"] == "select_slot" or parsed.intent == "select":
            selected_id = turn["slot_id"] or parsed.selection_slot_id
            if (
                selected_id is None
                and parsed.selection_index
                and parsed.selection_index <= len(state["options"])
            ):
                selected_id = state["options"][parsed.selection_index - 1]["slot_id"]
            self._select(state, principal, selected_id)
        elif turn["action"] == "confirm" or parsed.intent == "confirm":
            if turn["action"] != "confirm" and not explicit_confirmation(turn["text"]):
                self._clarify(
                    state,
                    principal,
                    "Please confirm the exact appointment shown, or tell me what should change.",
                    "confirmation",
                )
            else:
                self._book(state, principal, turn)
        elif parsed.intent == "request" or state["status"] == "blocked":
            self._continue_request(state, principal)
        elif state["status"] == "awaiting_confirmation":
            self._clarify(
                state,
                principal,
                "Please confirm the exact appointment shown, or tell me what should change.",
                "confirmation",
            )
        elif state["options"]:
            self._clarify(
                state,
                principal,
                "Which appointment would you like? Choose an option first; I will then ask you to confirm its exact details.",
                "slot_selection",
            )
        else:
            self._continue_request(state, principal)
        return {"conversation": state}

    def _continue_request(self, state: dict, principal: Principal) -> None:
        if not state["constraints"]["appointment_type"]:
            self._clarify(
                state,
                principal,
                "Is this a new routine primary care appointment? Please also tell me the day you prefer.",
                "appointment_type",
            )
        elif not state["constraints"]["date"]:
            self._clarify(
                state,
                principal,
                "Which day would you like your primary care appointment? You can say tomorrow, Friday, or an exact date.",
                "appointment_date",
            )
        else:
            self._search(state, principal)

    def _search(self, state: dict, principal: Principal, recovery: bool = False) -> None:
        for attempt in range(2):
            try:
                slots = self.scheduler.search(
                    principal, Constraints.model_validate(state["constraints"]), state, self.now()
                )
                break
            except ConnectionError:
                self._event(state, "search_retry", attempt=attempt + 1)
        else:
            self._handoff(
                state,
                principal,
                "read_outage",
                "administrative",
                "Availability could not be loaded after two attempts.",
            )
            return
        state.update(options=[slot.model_dump() for slot in slots], proposal=None)
        self._event(
            state,
            "availability_searched",
            result_count=len(slots),
            constraints=state["constraints"],
        )
        if not slots:
            state.update(
                status="collecting",
                outstanding_question="relax_constraints_or_human",
                reply="No appointments match your current requirements. Would you like to change the date or time yourself, or ask a receptionist for help? I have not changed your requirements.",
            )
            return
        state.update(
            status="offering", clarification_count=0, outstanding_question="slot_selection"
        )
        prefix = (
            "That appointment is no longer available. I refreshed once using your same requirements. Please choose a replacement; it needs a new confirmation. "
            if recovery
            else "Here are appointments matching your requirements. Choose one, and I will ask for confirmation before booking. "
        )
        state["reply"] = prefix + " ".join(
            f"Option {index}: {describe_slot(slot)}." for index, slot in enumerate(slots, 1)
        )
        self._event(state, "options_presented", slot_ids=[slot.slot_id for slot in slots])

    def _select(self, state: dict, principal: Principal, slot_id: str | None) -> None:
        match = next((item for item in state["options"] if item["slot_id"] == slot_id), None)
        if match is None:
            self._clarify(
                state,
                principal,
                "That option is not part of the current results. Please choose one of the current appointment options.",
                "slot_selection",
            )
            return
        proposal = BookingProposal(
            proposal_id=f"proposal-{uuid4().hex}",
            request_revision=state["request_revision"],
            patient_id=principal.patient_id,
            slot=Slot.model_validate(match),
            expires_at=(self.now() + timedelta(minutes=10)).isoformat(),
        )
        state.update(
            status="awaiting_confirmation",
            proposal=proposal.model_dump(),
            outstanding_question="confirmation",
            clarification_count=0,
            reply=f"Please confirm: a routine primary care appointment on {describe_slot(proposal.slot)}. Shall I book this exact appointment for you?",
        )
        self._event(
            state,
            "proposal_presented",
            proposal_id=proposal.proposal_id,
            slot_id=slot_id,
            expires_at=proposal.expires_at,
        )

    def _book(self, state: dict, principal: Principal, turn: dict) -> None:
        try:
            proposal = validate_confirmation(state, principal, self.now(), turn.get("proposal_id"))
        except ValueError as exc:
            self._event(
                state, "confirmation_rejected", reason=str(exc), proposal_id=turn.get("proposal_id")
            )
            state["reply"] = str(exc)
            if (
                state.get("proposal")
                and datetime.fromisoformat(state["proposal"]["expires_at"]) <= self.now()
            ):
                state.update(proposal=None, options=[], status="collecting")
                self._search(state, principal)
                state["reply"] = (
                    "Those appointment details expired; no booking was made. " + state["reply"]
                )
            return
        self._event(
            state,
            "confirmation_validated",
            proposal_id=proposal.proposal_id,
            slot_id=proposal.slot.slot_id,
            turn_id=turn["turn_id"],
        )
        operation_id = f"op-{proposal.proposal_id.removeprefix('proposal-')}"
        state.update(operation_id=operation_id, status="unknown_outcome", outstanding_question=None)
        self._event(
            state,
            "booking_dispatch_prepared",
            proposal_id=proposal.proposal_id,
            operation_id=operation_id,
            slot_id=proposal.slot.slot_id,
        )
        # This durable record exists before the request can mutate the scheduler.
        self.store.put_conversation(state)
        try:
            result = self.scheduler.book(
                principal,
                proposal,
                operation_id,
                operation_digest(principal, proposal),
                state,
                self.now(),
            )
        except ConnectionError:
            result = {"status": "OUTCOME_UNKNOWN", "operation_id": operation_id}
        if result["status"] == "OUTCOME_UNKNOWN":
            self._event(
                state,
                "booking_outcome_unknown",
                operation_id=operation_id,
                proposal_id=proposal.proposal_id,
            )
            self._reconcile(state, principal)
        else:
            self._apply_booking_result(state, principal, result)

    def _reconcile(self, state: dict, principal: Principal) -> None:
        try:
            result = self.scheduler.lookup(principal, state["operation_id"], state, self.now())
        except ConnectionError:
            result = {"status": "OUTCOME_UNKNOWN"}
        if result["status"] == "OUTCOME_UNKNOWN":
            self._handoff(
                state,
                principal,
                "unknown_booking_outcome",
                "operations",
                "The original booking attempt has an unresolved outcome. Do not create a replacement booking until it is reconciled.",
            )
            # Keep the unresolved operation blocking every future mutation.
            state["status"] = "unknown_outcome"
            state["reply"] = (
                "The booking result is still uncertain. I have not tried another booking. "
                + state["reply"]
            )
        else:
            self._event(
                state,
                "booking_reconciled",
                operation_id=state["operation_id"],
                result=result["status"],
            )
            self._apply_booking_result(state, principal, result)

    def _apply_booking_result(self, state: dict, principal: Principal, result: dict) -> None:
        if result["status"] == "BOOKED":
            state.update(
                status="booked",
                appointment=result["appointment"],
                options=[],
                outstanding_question=None,
            )
            slot = BookingProposal.model_validate(state["proposal"]).slot
            state["reply"] = (
                f"Your appointment is booked: {describe_slot(slot)}. Confirmation: {state['appointment']['appointment_id']}."
            )
            self._event(
                state,
                "booking_receipt_verified",
                operation_id=state["operation_id"],
                proposal_id=state["proposal"]["proposal_id"],
                slot_id=slot.slot_id,
                appointment_id=state["appointment"]["appointment_id"],
            )
        elif result["status"] == "SLOT_UNAVAILABLE":
            self._event(
                state,
                "slot_conflict_detected",
                operation_id=state["operation_id"],
                proposal_id=state["proposal"]["proposal_id"],
                slot_id=state["proposal"]["slot"]["slot_id"],
            )
            state.update(operation_id=None, proposal=None, options=[])
            if state["behavior_version"] == "v2" and state["refresh_count"] < 1:
                state["refresh_count"] += 1
                self._search(state, principal, recovery=True)
            else:
                self._handoff(
                    state,
                    principal,
                    "slot_unavailable",
                    "administrative",
                    "The selected slot became unavailable; no replacement was booked.",
                )
        else:
            state["status"] = "unknown_outcome"
            self._handoff(
                state,
                principal,
                "unexpected_scheduler_result",
                "operations",
                "The scheduler returned an unexpected outcome.",
            )

    def _clarify(self, state: dict, principal: Principal, reply: str, question: str) -> None:
        state["clarification_count"] += 1
        if state["clarification_count"] > 3:
            self._handoff(
                state,
                principal,
                "clarification_limit",
                "administrative",
                "The scheduling details remain unclear after repeated clarification.",
            )
        else:
            state.update(reply=reply, outstanding_question=question)
            self._event(state, "clarification_requested", question=question)

    def _handoff(
        self, state: dict, principal: Principal, reason: str, destination: str, summary: str
    ) -> None:
        if state.get("handoff"):
            state.update(
                status="escalated",
                reply=f"Your existing request {state['handoff']['ticket_id']} is queued for {state['handoff']['destination']} staff. This does not mean a staff member is connected yet.",
            )
            return
        ticket = self.store.handoff(principal, state, reason, destination, summary, self.now())
        state.update(options=[], outstanding_question=None)
        if reason != "unknown_booking_outcome":
            state["proposal"] = None
        if ticket:
            state.update(
                status="escalated",
                handoff=ticket.model_dump(),
                reply=f"{summary} Your request is queued for {destination} staff. Ticket: {ticket.ticket_id}. A staff member has not joined this chat yet.",
            )
        else:
            state.update(
                status="handoff_failed",
                handoff=None,
                reply="I could not queue a staff request. Please use your clinic's published reception contact details directly. I have not connected you to a staff member.",
            )
            self._event(state, "handoff_failed", reason=reason, destination=destination)

    def _persist(self, work: WorkState) -> dict:
        state = work["conversation"]
        state["messages"].append(
            {"role": "assistant", "content": state["reply"], "turn_id": work["turn"]["turn_id"]}
        )
        self.store.put_conversation(state)
        return {"conversation": state}

    @staticmethod
    def _response(state: dict) -> ConversationResponse:
        fields = {key: state[key] for key in ConversationResponse.model_fields if key in state}
        return ConversationResponse.model_validate(fields)

    def list_handoffs(self, principal: Principal):
        return self.store.list_handoffs(principal)

    def update_handoff(self, ticket_id: str, status: str, principal: Principal):
        return self.store.update_handoff(ticket_id, status, principal, self.now())

    def record_feedback(
        self, conversation_id: str, principal: Principal, rating: int | None, comment: str
    ) -> dict:
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError("Rating must be between 1 and 5.")
        if len(comment) > 2000:
            raise ValueError("Feedback is too long.")
        feedback_id = uuid4().hex
        with self._conversation_lock(conversation_id, principal.tenant_id):
            state = self.store.get_conversation(conversation_id)
            check_owner(principal, state)
            with self.store.lock:
                self.store.connection.execute(
                    "INSERT INTO feedback VALUES(?,?,?,?,?,?)",
                    (
                        feedback_id,
                        conversation_id,
                        principal.patient_id,
                        rating,
                        comment,
                        self.now().isoformat(),
                    ),
                )
            self._event(state, "feedback_recorded", feedback_id=feedback_id, rating=rating)
            self.store.put_conversation(state)
        return {"feedback_id": feedback_id, "status": "recorded", "behavior_changed": False}

    def inspect_world(self) -> dict:
        conversation = self.store.inspect_world()
        scheduling = (
            self.scheduler.inspect_world() if hasattr(self.scheduler, "inspect_world") else {}
        )
        result = {**scheduling, **conversation}
        for key in ("slots", "appointments", "operations"):
            result[key] = scheduling.get(key, [])
        events = conversation["events"] + scheduling.get("events", [])
        events.sort(key=lambda event: event.get("data", {}).get("ordering_ns", 0))
        for sequence, event in enumerate(events, 1):
            event["source_seq"] = event["seq"]
            event["seq"] = sequence
        result["events"] = events
        result["faults_consumed"] = {
            **scheduling.get("faults_consumed", {}),
            **conversation.get("faults_consumed", {}),
        }
        return result

    def close(self) -> None:
        self.store.close()
        self._checkpoint_connection.close()
        if self.owns_scheduler:
            self.scheduler.close()
