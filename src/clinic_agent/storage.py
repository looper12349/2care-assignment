"""Persistent synthetic clinic and operation journal, with controlled test faults."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from clinic_agent.database import DatabaseRow, connect_database
from clinic_agent.domain import PROVIDERS, operation_digest, slot_matches, validate_principal
from clinic_agent.models import Appointment, BookingProposal, Constraints, Handoff, Principal, Slot


class ClinicStore:
    def __init__(
        self,
        path: str,
        now: datetime,
        fault_plan: dict[str, Any] | None = None,
        *,
        scheduling: bool = True,
    ):
        self.connection = connect_database(path)
        self.lock = threading.RLock()
        self.faults = dict(fault_plan or {})
        self.consumed_faults: dict[str, int] = {}
        self.scheduling = scheduling
        schema = """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS slots (
                slot_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, start_at TEXT NOT NULL,
                end_at TEXT NOT NULL, provider_id TEXT NOT NULL, provider_name TEXT NOT NULL,
                appointment_type TEXT NOT NULL, location TEXT NOT NULL, timezone TEXT NOT NULL,
                available INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, patient_id TEXT NOT NULL,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                turn_id TEXT NOT NULL, request_json TEXT NOT NULL, response_json TEXT NOT NULL,
                PRIMARY KEY(conversation_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, patient_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
                request_revision INTEGER NOT NULL, slot_id TEXT NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL, appointment_id TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS appointments (
                appointment_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
                tenant_id TEXT NOT NULL, patient_id TEXT NOT NULL, slot_id TEXT NOT NULL UNIQUE REFERENCES slots(slot_id),
                start_at TEXT NOT NULL, provider_id TEXT NOT NULL, provider_name TEXT NOT NULL,
                appointment_type TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handoffs (
                ticket_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                patient_id TEXT NOT NULL, reason TEXT NOT NULL, destination TEXT NOT NULL,
                status TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                conversation_id TEXT, tenant_id TEXT, patient_id TEXT,
                request_revision INTEGER, proposal_id TEXT, operation_id TEXT, slot_id TEXT,
                created_at TEXT NOT NULL, data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                patient_id TEXT NOT NULL, rating INTEGER, comment TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS behavior_registry (
                tenant_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                job_id TEXT, artifact_id TEXT, updated_at TEXT NOT NULL
            );
        """
        if self.connection.dialect == "postgres":
            schema = schema.replace(
                "seq INTEGER PRIMARY KEY AUTOINCREMENT", "seq BIGSERIAL PRIMARY KEY"
            )
        allowed = (
            {"slots", "operations", "appointments", "events"}
            if scheduling
            else {"conversations", "turns", "handoffs", "feedback", "events", "behavior_registry"}
        )
        self._tables = allowed
        with self.connection.advisory_lock(
            f"clinic-schema:{'scheduling' if scheduling else 'conversation'}"
        ):
            for statement in schema.split(";"):
                table = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
                if statement.strip() and (not table or table.group(1) in allowed):
                    self.connection.execute(statement)
        if scheduling:
            self._seed(now)

    def _seed(self, now: datetime) -> None:
        with self.connection.advisory_lock("clinic:seed:scheduling"), self.lock:
            if self.connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0]:
                return
            local = now.astimezone(ZoneInfo("Asia/Kolkata"))
            for offset in range(1, 15):
                day = local.date() + timedelta(days=offset)
                if day.weekday() >= 5:
                    continue
                for provider_id, name in PROVIDERS.items():
                    for hour, minute in ((9, 0), (9, 30), (11, 0), (16, 0), (16, 30)):
                        start = datetime.combine(
                            day, datetime.min.time(), tzinfo=ZoneInfo("Asia/Kolkata")
                        ).replace(hour=hour, minute=minute)
                        slot_id = f"slot-{day.isoformat()}-{hour:02d}{minute:02d}-{provider_id}"
                        self.connection.execute(
                            "INSERT INTO slots VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(slot_id) DO NOTHING",
                            (
                                slot_id,
                                "demo-clinic",
                                start.isoformat(),
                                (start + timedelta(minutes=30)).isoformat(),
                                provider_id,
                                name,
                                "primary-care",
                                "Main clinic",
                                "Asia/Kolkata",
                            ),
                        )

    def event(self, kind: str, state: dict, now: datetime, **data: Any) -> dict:
        data["ordering_ns"] = time.time_ns()
        fields = {key: data.pop(key, None) for key in ("proposal_id", "operation_id", "slot_id")}
        record = {
            "kind": kind,
            "conversation_id": state.get("conversation_id"),
            "tenant_id": state.get("tenant_id"),
            "patient_id": state.get("patient_id"),
            "request_revision": state.get("request_revision", 0),
            **fields,
            "created_at": now.isoformat(),
            "data": data,
        }
        with self.lock:
            query = "INSERT INTO events(kind,conversation_id,tenant_id,patient_id,request_revision,proposal_id,operation_id,slot_id,created_at,data_json) VALUES(?,?,?,?,?,?,?,?,?,?)"
            if self.connection.dialect == "postgres":
                query += " RETURNING seq"
            cursor = self.connection.execute(
                query,
                (
                    kind,
                    record["conversation_id"],
                    record["tenant_id"],
                    record["patient_id"],
                    record["request_revision"],
                    fields["proposal_id"],
                    fields["operation_id"],
                    fields["slot_id"],
                    record["created_at"],
                    json.dumps(data),
                ),
            )
            record["seq"] = (
                cursor.fetchone()[0] if self.connection.dialect == "postgres" else cursor.lastrowid
            )
        return record

    def _consume(self, name: str, state: dict, now: datetime) -> bool:
        value = self.faults.get(name, False)
        if not value:
            return False
        if isinstance(value, bool):
            self.faults[name] = False
        elif isinstance(value, int):
            self.faults[name] = value - 1
        else:
            self.faults[name] = False
        self.consumed_faults[name] = self.consumed_faults.get(name, 0) + 1
        self.event("fault_injected", state, now, fault=name)
        return True

    def put_conversation(self, state: dict) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json",
                (
                    state["conversation_id"],
                    state["tenant_id"],
                    state["patient_id"],
                    json.dumps(state),
                ),
            )

    def get_conversation(self, conversation_id: str) -> dict:
        with self.lock:
            row = self.connection.execute(
                "SELECT state_json FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Conversation not found.")
        return json.loads(row[0])

    def cached_turn(self, conversation_id: str, turn_id: str, request: dict) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT request_json,response_json FROM turns WHERE conversation_id=? AND turn_id=?",
                (conversation_id, turn_id),
            ).fetchone()
        if not row:
            return None
        if json.loads(row[0]) != request:
            raise ValueError("A turn ID cannot be reused for different input.")
        return json.loads(row[1])

    def save_turn(self, conversation_id: str, turn_id: str, request: dict, response: dict) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO turns VALUES(?,?,?,?)",
                (conversation_id, turn_id, json.dumps(request), json.dumps(response)),
            )

    def search(
        self, principal: Principal, constraints: Constraints, state: dict, now: datetime
    ) -> list[Slot]:
        validate_principal(principal)
        if self._consume("read_failures", state, now):
            raise ConnectionError("Synthetic clinic read outage.")
        if self.faults.get("no_slots"):
            self._consume("no_slots", state, now)
            return []
        if self._consume("tool_injection", state, now):
            self.event(
                "untrusted_tool_text_rejected",
                state,
                now,
                text="Synthetic instruction payload excluded from typed slot facts.",
            )
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM slots WHERE tenant_id=? AND available=1 ORDER BY start_at,provider_id",
                (principal.tenant_id,),
            ).fetchall()
        slots = [self._slot(row) for row in rows]
        return [
            slot
            for slot in slots
            if datetime.fromisoformat(slot.start_at) > now and slot_matches(slot, constraints)
        ][:5]

    @staticmethod
    def _slot(row: DatabaseRow) -> Slot:
        return Slot.model_validate({key: row[key] for key in Slot.model_fields})

    def book(
        self,
        principal: Principal,
        proposal: BookingProposal,
        operation_id: str,
        digest: str,
        state: dict,
        now: datetime,
    ) -> dict:
        validate_principal(principal)
        if principal.role != "patient" or proposal.patient_id != principal.patient_id:
            raise PermissionError("Booking not authorized.")
        with (
            self.connection.advisory_lock(
                f"booking-operation:{principal.tenant_id}:{operation_id}"
            ),
            self.lock,
        ):
            existing = self.connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                if (
                    existing["digest"] != digest
                    or existing["patient_id"] != principal.patient_id
                    or existing["tenant_id"] != principal.tenant_id
                ):
                    raise PermissionError("Operation key belongs to a different request.")
                return self.lookup(principal, operation_id, state, now)
            if (
                digest != operation_digest(principal, proposal)
                or operation_id != f"op-{proposal.proposal_id.removeprefix('proposal-')}"
            ):
                raise ValueError("The operation does not match the proposal.")
            if datetime.fromisoformat(proposal.expires_at) <= now:
                raise ValueError("These appointment details have expired.")
            if proposal.request_revision != state.get("request_revision") or not slot_matches(
                proposal.slot, Constraints.model_validate(state.get("constraints", {}))
            ):
                raise ValueError("These appointment details do not match the current requirements.")
            self.connection.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    state["conversation_id"],
                    principal.tenant_id,
                    principal.patient_id,
                    proposal.proposal_id,
                    proposal.request_revision,
                    proposal.slot.slot_id,
                    digest,
                    "pending",
                    None,
                    now.isoformat(),
                ),
            )
            self.event(
                "operation_created",
                state,
                now,
                operation_id=operation_id,
                proposal_id=proposal.proposal_id,
                slot_id=proposal.slot.slot_id,
            )
            if self._consume("slot_conflict", state, now):
                self.connection.execute(
                    "UPDATE slots SET available=0 WHERE slot_id=?", (proposal.slot.slot_id,)
                )
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                slot_query = "SELECT * FROM slots WHERE slot_id=? AND tenant_id=? AND available=1"
                if self.connection.dialect == "postgres":
                    slot_query += " FOR UPDATE"
                row = self.connection.execute(
                    slot_query,
                    (proposal.slot.slot_id, principal.tenant_id),
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        "UPDATE operations SET status='conflict' WHERE operation_id=?",
                        (operation_id,),
                    )
                    self.connection.execute("COMMIT")
                    return {"status": "SLOT_UNAVAILABLE"}
                authoritative = self._slot(row)
                if authoritative.model_dump() != proposal.slot.model_dump():
                    self.connection.execute(
                        "UPDATE operations SET status='conflict' WHERE operation_id=?",
                        (operation_id,),
                    )
                    self.connection.execute("COMMIT")
                    return {"status": "SLOT_UNAVAILABLE"}
                appointment = Appointment(
                    appointment_id=f"apt-{uuid4().hex[:12]}",
                    operation_id=operation_id,
                    tenant_id=principal.tenant_id,
                    patient_id=principal.patient_id,
                    slot_id=authoritative.slot_id,
                    start_at=authoritative.start_at,
                    provider_id=authoritative.provider_id,
                    provider_name=authoritative.provider_name,
                    appointment_type=authoritative.appointment_type,
                    location=authoritative.location,
                    created_at=now.isoformat(),
                )
                values = appointment.model_dump()
                self.connection.execute(
                    "INSERT INTO appointments VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(values.values())
                )
                self.connection.execute(
                    "UPDATE slots SET available=0 WHERE slot_id=?", (authoritative.slot_id,)
                )
                self.connection.execute(
                    "UPDATE operations SET status='booked',appointment_id=? WHERE operation_id=?",
                    (appointment.appointment_id, operation_id),
                )
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            self.event(
                "booking_committed",
                state,
                now,
                operation_id=operation_id,
                proposal_id=proposal.proposal_id,
                slot_id=proposal.slot.slot_id,
                appointment_id=appointment.appointment_id,
            )
            if self._consume("booking_timeout", state, now):
                return {"status": "OUTCOME_UNKNOWN", "operation_id": operation_id}
            return {"status": "BOOKED", "appointment": appointment.model_dump()}

    def lookup(self, principal: Principal, operation_id: str, state: dict, now: datetime) -> dict:
        validate_principal(principal)
        self.event("operation_lookup", state, now, operation_id=operation_id)
        if self._consume("lookup_failures", state, now):
            return {"status": "OUTCOME_UNKNOWN", "operation_id": operation_id}
        with self.lock:
            op = self.connection.execute(
                "SELECT * FROM operations WHERE operation_id=? AND tenant_id=? AND patient_id=?",
                (operation_id, principal.tenant_id, principal.patient_id),
            ).fetchone()
            if not op:
                return {"status": "OUTCOME_UNKNOWN", "operation_id": operation_id}
            if op["status"] == "booked":
                row = self.connection.execute(
                    "SELECT * FROM appointments WHERE appointment_id=?", (op["appointment_id"],)
                ).fetchone()
                return {"status": "BOOKED", "appointment": dict(row)}
            if op["status"] == "conflict":
                return {"status": "SLOT_UNAVAILABLE"}
        return {"status": "OUTCOME_UNKNOWN", "operation_id": operation_id}

    def handoff(
        self,
        principal: Principal,
        state: dict,
        reason: str,
        destination: str,
        summary: str,
        now: datetime,
    ) -> Handoff | None:
        validate_principal(principal)
        if self._consume("handoff_failure", state, now):
            return None
        ticket = Handoff(
            ticket_id=f"handoff-{uuid4().hex[:12]}",
            conversation_id=state["conversation_id"],
            tenant_id=principal.tenant_id,
            patient_id=principal.patient_id,
            reason=reason,
            destination=destination,
            summary=summary[:1000],
            created_at=now.isoformat(),
        )
        with self.lock:
            self.connection.execute(
                "INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,?,?)",
                tuple(ticket.model_dump().values()),
            )
        self.event(
            "handoff_created",
            state,
            now,
            ticket_id=ticket.ticket_id,
            reason=reason,
            destination=destination,
            status="queued",
        )
        return ticket

    def list_handoffs(self, principal: Principal) -> list[Handoff]:
        validate_principal(principal)
        if principal.role != "staff":
            raise PermissionError("Staff access is required.")
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM handoffs WHERE tenant_id=? ORDER BY created_at DESC",
                (principal.tenant_id,),
            ).fetchall()
        return [Handoff.model_validate(dict(row)) for row in rows]

    def update_handoff(
        self, ticket_id: str, status: str, principal: Principal, now: datetime
    ) -> Handoff:
        validate_principal(principal)
        if principal.role != "staff":
            raise PermissionError("Staff access is required.")
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM handoffs WHERE ticket_id=? AND tenant_id=?",
                (ticket_id, principal.tenant_id),
            ).fetchone()
            if not row:
                raise KeyError("Handoff not found.")
            if status == row["status"]:
                return Handoff.model_validate(dict(row))
            allowed = {"queued": "accepted", "accepted": "resolved"}
            if allowed.get(row["status"]) != status:
                raise ValueError("Handoffs move from queued to accepted to resolved.")
            self.connection.execute(
                "UPDATE handoffs SET status=? WHERE ticket_id=?", (status, ticket_id)
            )
            result = dict(row)
            result["status"] = status
            self.event(
                "handoff_status_updated",
                result,
                now,
                ticket_id=ticket_id,
                status=status,
                staff_id=principal.patient_id,
            )
        return Handoff.model_validate(result)

    def inspect_world(self) -> dict:
        with self.lock:
            result = {
                table: [
                    dict(row)
                    for row in self.connection.execute(f"SELECT * FROM {table}").fetchall()
                ]
                if table in self._tables
                else []
                for table in (
                    "slots",
                    "appointments",
                    "operations",
                    "handoffs",
                    "conversations",
                    "feedback",
                )
            }
            result["events"] = []
            for row in self.connection.execute("SELECT * FROM events ORDER BY seq").fetchall():
                item = dict(row)
                item["data"] = json.loads(item.pop("data_json"))
                result["events"].append(item)
        result["faults_consumed"] = dict(self.consumed_faults)
        return result

    def close(self) -> None:
        self.connection.close()


class ConversationStore(ClinicStore):
    """Only conversation, handoff, feedback, and audit tables; no clinic data."""

    def __init__(self, path: str, now: datetime, fault_plan: dict[str, Any] | None = None):
        super().__init__(path, now, fault_plan, scheduling=False)

    def initialize_behavior(self, version: str, now: datetime) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO behavior_registry VALUES(?,?,?,?,?) ON CONFLICT(tenant_id) DO NOTHING",
                ("demo-clinic", version, None, None, now.isoformat()),
            )

    def active_behavior(self, tenant_id: str = "demo-clinic") -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT version FROM behavior_registry WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("The behavior registry is not initialized.")
        return row[0]

    def set_active_behavior(
        self,
        version: str,
        job_id: str,
        artifact_id: str,
        now: datetime,
        principal: Principal | None = None,
    ) -> None:
        if version not in {"v1", "v2"}:
            raise ValueError("Unknown behavior version.")
        tenant = principal.tenant_id if principal is not None else "demo-clinic"
        if principal is not None:
            validate_principal(principal)
            if principal.role != "staff":
                raise PermissionError("Staff access is required to activate behavior.")
        with self.connection.advisory_lock(f"behavior-registry:{tenant}"), self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT INTO behavior_registry VALUES(?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET version=excluded.version,job_id=excluded.job_id,artifact_id=excluded.artifact_id,updated_at=excluded.updated_at",
                    (tenant, version, job_id, artifact_id, now.isoformat()),
                )
                self.event(
                    "behavior_activated",
                    {
                        "tenant_id": tenant,
                        "patient_id": principal.patient_id
                        if principal is not None
                        else "internal-service",
                    },
                    now,
                    version=version,
                    job_id=job_id,
                    artifact_id=artifact_id,
                )
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
