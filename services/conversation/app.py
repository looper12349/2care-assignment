"""Public conversation API. Scheduling and evaluation remain separate services."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from clinic_agent.agent import SchedulingService
from clinic_agent.auth import sign_context, verify_context
from clinic_agent.models import Principal, StrictModel
from clinic_agent.scheduling_gateway import HTTPSchedulingGateway
from clinic_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
PATIENTS = [
    {"id": "patient-maya", "name": "Maya Shah"},
    {"id": "patient-arjun", "name": "Arjun Mehta"},
]


def create_app() -> FastAPI:
    settings = Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if (
            len(os.getenv("CLINIC_SERVICE_TOKEN", "")) < 32
            or len(os.getenv("CLINIC_SESSION_SECRET", "")) < 32
        ):
            raise RuntimeError("Run `clinic-agent serve` or configure both local service secrets.")
        settings.ensure_storage()
        # Patient conversations never upload by default, even when a shell has
        # tracing enabled from another project. Synthetic online eval is opt-in.
        if os.getenv("CLINIC_ALLOW_TRACE_UPLOAD") != "true":
            os.environ["LANGSMITH_TRACING"] = "false"
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
        active_file = settings.data_dir / "active_behavior.json"
        version = settings.behavior_version
        database_url = os.getenv("CONVERSATION_DATABASE_URL")
        if not database_url and active_file.exists():
            version = json.loads(active_file.read_text())["version"]
        gateway = HTTPSchedulingGateway(os.getenv("SCHEDULING_URL", "http://127.0.0.1:8002"))
        app.state.service = SchedulingService(
            db_path=database_url or str(settings.data_dir / "conversations.sqlite3"),
            checkpoint_path=os.getenv("CHECKPOINT_DATABASE_URL")
            or database_url
            or str(settings.data_dir / "checkpoints.sqlite3"),
            scheduler=gateway,
            model_name=settings.model_name,
            behavior_version=version,
            fixed_now=settings.fixed_now,
        )
        app.state.evaluation = httpx.AsyncClient(
            base_url=os.getenv("EVALUATION_URL", "http://127.0.0.1:8003"),
            timeout=10,
            headers={"X-Service-Token": os.environ["CLINIC_SERVICE_TOKEN"]},
        )
        app.state.active_file = active_file
        app.state.database_url = database_url
        yield
        await app.state.evaluation.aclose()
        app.state.service.close()
        gateway.close()

    api = FastAPI(title="CarePath conversation service", version="0.1.0", lifespan=lifespan)

    @api.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": "You cannot access this resource."})

    @api.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": "Resource not found."})

    @api.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def identity(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Sign in to a synthetic demo account.")
        try:
            return verify_context(
                authorization[7:], os.environ["CLINIC_SESSION_SECRET"], audience="conversation"
            )
        except ValueError as exc:
            raise HTTPException(401, "Your demo session expired. Please sign in again.") from exc

    def staff(principal: Annotated[Principal, Depends(identity)]) -> Principal:
        if principal.role != "staff":
            raise HTTPException(403, "Staff access is required.")
        return principal

    def public_response(response, principal: Principal) -> dict:
        result = response.model_dump(mode="json")
        # Ownership was checked by the service before exposing conversation history.
        state = api.state.service.store.get_conversation(response.conversation_id)
        result["messages"] = state.get("messages", [])
        if not result["messages"]:
            result["messages"] = [{"role": "assistant", "content": result["reply"]}]
        return result

    async def eval_request(method: str, path: str, **kwargs) -> dict:
        try:
            response = await api.state.evaluation.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise HTTPException(
                503, "The evaluation worker is unavailable. Try again shortly."
            ) from exc
        if response.is_error:
            detail = response.json().get("detail", "Evaluation request failed.")
            raise HTTPException(response.status_code, detail)
        return response.json()

    @api.get("/health")
    def health():
        return {"status": "ok", "service": "conversation", "synthetic_data": True}

    @api.get("/api/config")
    def config():
        service = api.state.service
        return {
            "clinic_name": "CarePath · Main clinic",
            "mode": service.interpreter.mode,
            "patients": PATIENTS,
            "active_behavior_version": service.current_behavior_version(),
            "timezone": "Asia/Kolkata",
            "langsmith_enabled": False,
            "synthetic_data": True,
            "evaluation_engine": "LangSmith SDK",
            "evaluation_interpreter": "live" if settings.model_name else "demo",
            "evaluation_model_name": settings.model_name,
        }

    class SessionBody(StrictModel):
        patient_key: Literal["patient-maya", "patient-arjun"] = "patient-maya"
        role: Literal["patient", "staff"] = "patient"

    @api.post("/api/sessions")
    def session(body: SessionBody):
        principal = Principal(
            patient_id=body.patient_key if body.role == "patient" else "staff-demo", role=body.role
        )
        return {
            "token": sign_context(
                principal,
                os.environ["CLINIC_SESSION_SECRET"],
                audience="conversation",
                expires_in=8 * 3600,
            ),
            "identity": principal.model_dump(),
            "authentication": "synthetic-demo-only",
        }

    @api.post("/api/conversations", status_code=201)
    def create_conversation(principal: Annotated[Principal, Depends(identity)]):
        return public_response(api.state.service.create_conversation(principal), principal)

    @api.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, principal: Annotated[Principal, Depends(identity)]):
        return public_response(
            api.state.service.get_conversation(conversation_id, principal), principal
        )

    class MessageBody(StrictModel):
        turn_id: str = Field(min_length=1, max_length=128)
        text: str = Field(min_length=1, max_length=4000)
        action: Literal["select_slot", "confirm", "decline"] | None = None
        slot_id: str | None = Field(default=None, max_length=128)
        proposal_id: str | None = Field(default=None, max_length=128)

    @api.post("/api/conversations/{conversation_id}/messages")
    def message(
        conversation_id: str, body: MessageBody, principal: Annotated[Principal, Depends(identity)]
    ):
        response = api.state.service.message(
            conversation_id=conversation_id, principal=principal, **body.model_dump()
        )
        return public_response(response, principal)

    class FeedbackBody(StrictModel):
        rating: int = Field(ge=1, le=5)
        comment: str = Field(default="", max_length=2000)

    @api.post("/api/conversations/{conversation_id}/feedback", status_code=201)
    def feedback(
        conversation_id: str, body: FeedbackBody, principal: Annotated[Principal, Depends(identity)]
    ):
        return api.state.service.record_feedback(
            conversation_id, principal, body.rating, body.comment
        )

    @api.get("/api/handoffs")
    def handoffs(principal: Annotated[Principal, Depends(staff)]):
        return api.state.service.list_handoffs(principal)

    class HandoffBody(StrictModel):
        status: Literal["accepted", "resolved"]

    @api.patch("/api/handoffs/{ticket_id}")
    def update_handoff(
        ticket_id: str, body: HandoffBody, principal: Annotated[Principal, Depends(staff)]
    ):
        return api.state.service.update_handoff(ticket_id, body.status, principal)

    class EvaluationBody(StrictModel):
        repetitions: Literal[1, 3] = 1
        online: bool = False
        interpreter: Literal["demo", "live"] = "demo"
        model_name: str | None = Field(default=None, max_length=120)

    @api.post("/api/evaluations/run", status_code=202)
    async def run_evaluation(body: EvaluationBody, principal: Annotated[Principal, Depends(staff)]):
        if body.online:
            raise HTTPException(
                400,
                "Cloud uploads are disabled in the local UI. Use the explicit CLI option for synthetic online experiments.",
            )
        return await eval_request("POST", "/jobs", json=body.model_dump())

    @api.get("/api/evaluations/latest")
    async def latest_evaluation(principal: Annotated[Principal, Depends(staff)]):
        return await eval_request("GET", "/jobs/latest")

    @api.get("/api/evaluations/jobs/{job_id}")
    async def evaluation_job(job_id: str, principal: Annotated[Principal, Depends(staff)]):
        return await eval_request("GET", f"/jobs/{job_id}")

    class ActivationBody(StrictModel):
        version: Literal["v2"]
        job_id: str = Field(min_length=1, max_length=128)

    @api.post("/api/behavior/activate")
    async def activate(body: ActivationBody, principal: Annotated[Principal, Depends(staff)]):
        from clinic_agent.quality.improvements import ImprovementArtifact, acceptance_gates

        job = await eval_request("GET", f"/jobs/{body.job_id}")
        report = job.get("report") or {}
        checks = report.get("gates", {}).get("checks", [])
        if (
            job.get("status") != "completed"
            or not report.get("gates", {}).get("accepted")
            or not checks
            or not all(check.get("passed") is True for check in checks)
            or report.get("candidate", {}).get("critical_violations") != 0
            or not report.get("candidate", {}).get("accepted")
        ):
            raise HTTPException(
                409, "Only a completed experiment passing every gate can be activated."
            )
        artifact = ImprovementArtifact.model_validate(report["improvement"])
        if artifact.status != "accepted" or artifact.candidate_version != body.version:
            raise HTTPException(409, "The accepted artifact does not match this version.")
        if settings.model_name and (
            report.get("engine", {}).get("actor") != "langchain-model"
            or report.get("engine", {}).get("model_name") != settings.model_name
        ):
            raise HTTPException(
                409,
                "Run live-model evaluation before activating this change for a live-model agent.",
            )
        expected_runs = report["dataset"]["count"] * report["dataset"]["repetitions"]
        verified = acceptance_gates(
            report["baseline"], report["candidate"], artifact, expected_runs
        )
        if not verified["accepted"]:
            raise HTTPException(
                409, "Rechecking the evaluation evidence failed the activation gates."
            )
        api.state.service.set_active_behavior(
            body.version, body.job_id, artifact.artifact_id, principal
        )
        if not api.state.database_url:
            active_file = api.state.active_file
            temporary = active_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": body.version,
                        "job_id": body.job_id,
                        "artifact_id": artifact.artifact_id,
                    }
                )
            )
            temporary.replace(active_file)
        return {"version": body.version, "status": "active", "applies_to": "new_conversations"}

    @api.get("/architecture", include_in_schema=False)
    def architecture():
        return FileResponse(ROOT / "ARCHITECTURE.html", media_type="text/html")

    return api


app = create_app()
