# Evaluation service

Run this internal service with a single Uvicorn worker on localhost port 8003:

```sh
uv run uvicorn services.evaluation.app:app --host 127.0.0.1 --port 8003
```

Set `CLINIC_SERVICE_TOKEN` to the same secret as the conversation service.
Non-health requests require `X-Service-Token`; this token stays on the server.
Jobs and reports default to `.data/eval.db` and `.data/evaluations/<job-id>/attempt-<number>-<lease>/`.
Each attempt has its own files, so an expired worker cannot overwrite a newer
worker's evidence.
Use `CLINIC_EVAL_DB` and `CLINIC_EVAL_OUTPUT_DIR` to change them. Set
`EVALUATION_DATABASE_URL` to use PostgreSQL instead; Docker configures it by
default. The service owns its database and role. Completed report JSON is stored
in the database, so another replica can serve it without sharing report files.

`POST /jobs` accepts `repetitions` (1 or 3), `interpreter` (`demo` or `live`),
`model_name` for live mode, and `online` (false by default). It returns a job ID.
Poll `GET /jobs/{id}` or use `GET /jobs/latest` for the most recent job.

The worker runs the real LangSmith SDK, using `upload_results=False` by default.
Offline target execution explicitly disables nested remote tracing, including
when tracing environment variables are set. The entire dataset is synthetic.
Live mode requires an explicit model name and model credentials. Remote
LangSmith dataset/experiment upload requires `online: true` and LangSmith
credentials; local evaluation does not require them.

Each example is a whole branching conversation in its own fresh scheduling
world. Expected outcomes are separate reference outputs. Scoring checks actual
persisted appointments, operations, handoffs, and event order, rather than
trusting the transcript alone. The output records every repetition and error.

A safe baseline slot-conflict failure generates a typed configuration artifact.
It can only enable one availability refresh followed by fresh patient consent.
The original suite and held-out variants run again. Critical violations, missing
evidence, unexercised faults, or per-case regressions reject the candidate.
Acceptance writes an artifact. The conversation service owns explicit activation
for new conversations and keeps existing conversations pinned to their version.

Both storage modes preserve jobs across restarts. A claim has a 30-second lease,
renewed every 10 seconds, and at most three attempts. Each process executes one
evaluation job at a time. PostgreSQL replicas claim different jobs using locked
rows with `SKIP LOCKED`. Lease decisions use the database clock; an expired or
replaced owner cannot publish a result. Admission is serialized across replicas
and caps outstanding work at ten. SQLite remains a single-host fallback.
Nine real PostgreSQL tests exercise claims, locked-row skipping, stale-owner
rejection, retry limits, concurrent admission and startup, and database isolation.

Demo mode uses a deterministic language interpreter. The measurements verify
the configured behavior and recovery loop, not real-model reliability or
clinical triage. Every report states these limits.
