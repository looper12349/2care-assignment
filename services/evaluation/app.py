"""Internal evaluation service. Run with one Uvicorn worker on localhost:8003."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from clinic_agent.quality.runner import run_eval_loop
from services.evaluation.jobs import JobStore

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repetitions: Literal[1, 3] = 1
    online: bool = False
    interpreter: Literal["demo", "live"] = "demo"
    model_name: str | None = Field(default=None, max_length=120)


def authorize(x_service_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("CLINIC_SERVICE_TOKEN", "")
    if not expected or not x_service_token or not secrets.compare_digest(expected, x_service_token):
        raise HTTPException(status_code=401, detail="Service authentication required.")


async def _heartbeat(store: JobStore, claimed: dict) -> None:
    while True:
        await asyncio.sleep(10)
        if not await asyncio.to_thread(store.heartbeat, claimed["job_id"], claimed["lease_token"]):
            return


async def _work(app: FastAPI) -> None:
    store = app.state.jobs
    output_root = app.state.output_root
    while True:
        claimed = await asyncio.to_thread(store.claim)
        if claimed is None:
            await asyncio.sleep(0.5)
            continue
        heartbeat = asyncio.create_task(_heartbeat(store, claimed))
        try:
            report = await asyncio.to_thread(
                run_eval_loop,
                output_root
                / claimed["job_id"]
                / f"attempt-{claimed['attempt']}-{claimed['lease_token']}",
                **claimed["payload"],
            )
            await asyncio.to_thread(
                store.finish, claimed["job_id"], claimed["lease_token"], report=report
            )
        except asyncio.CancelledError:
            # The lease is deliberately left running. A restarted worker
            # reclaims it after expiry rather than silently losing the job.
            raise
        except Exception as exc:
            # Avoid reflecting credentials/provider response bodies into the UI.
            message = (
                "Evaluation configuration is invalid."
                if isinstance(exc, ValueError)
                else f"Evaluation failed ({type(exc).__name__}). Check the local service configuration."
            )
            await asyncio.to_thread(
                store.finish, claimed["job_id"], claimed["lease_token"], error=message
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("CLINIC_SERVICE_TOKEN"):
        raise RuntimeError("CLINIC_SERVICE_TOKEN must be configured for internal services.")
    app.state.jobs = JobStore(
        os.getenv("CLINIC_EVAL_DB", str(ROOT / ".data" / "eval.db")),
        database_url=os.getenv("EVALUATION_DATABASE_URL") or None,
    )
    app.state.output_root = Path(
        os.getenv("CLINIC_EVAL_OUTPUT_DIR", str(ROOT / ".data" / "evaluations"))
    ).resolve()
    app.state.worker = asyncio.create_task(_work(app))
    yield
    app.state.worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state.worker
    app.state.jobs.close()


app = FastAPI(title="Clinic evaluation worker", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "evaluation",
        "engine": "LangSmith SDK",
        "workers": 1,
        "storage": app.state.jobs.dialect,
        "uploads_default": False,
    }


@app.post("/jobs", dependencies=[Depends(authorize)], status_code=202)
def create_job(body: EvaluationRequest, request: Request):
    if body.interpreter == "live" and not (body.model_name and os.getenv("OPENAI_API_KEY")):
        raise HTTPException(
            status_code=400,
            detail="Live evaluation requires a model name and configured model credentials.",
        )
    if body.online and not (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")):
        raise HTTPException(
            status_code=400, detail="Online evaluation requires configured LangSmith credentials."
        )
    try:
        return request.app.state.jobs.enqueue(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


# Register the literal path before the dynamic job ID route.
@app.get("/jobs/latest", dependencies=[Depends(authorize)])
def latest_job(request: Request):
    job = request.app.state.jobs.latest()
    return job or {"job_id": None, "status": "none", "report": None, "error": None}


@app.get("/jobs/{job_id}", dependencies=[Depends(authorize)])
def get_job(job_id: str, request: Request):
    try:
        return request.app.state.jobs.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Evaluation job not found.") from exc
