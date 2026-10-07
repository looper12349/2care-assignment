# CarePath · 2CareAI

A patient scheduling assistant with a Next.js interface, a guarded LangGraph conversation, and a LangSmith evaluation loop that produces a checked, versioned improvement.

The repository runs **four separate application services**. Docker Compose adds one PostgreSQL infrastructure container, with a separate database and role for each Python service. All patient records and clinic integrations are synthetic. The default language interpreter is deterministic so the complete demonstration works without API keys. Optional live interpretation uses LangChain; the default results do not measure a real language model.

## Live application demo

![CarePath live demonstration: request an appointment, confirm it, record feedback, hand off to staff, and run the LangSmith improvement loop](docs/assets/carepath-demo.gif)

This edited GIF contains real browser captures from the running PostgreSQL-backed application. It shows a fresh booking with an authoritative receipt, feedback, a queued handoff accepted by staff, and a fresh three-repetition LangSmith evaluation. The final scores are **60/66 → 66/66**. Pauses are shortened and captions explain each step; the application results were not altered. All identities and appointments are fictional. This is a recorded local demonstration, not a publicly hosted clinic service.

[Download the GIF](docs/assets/carepath-demo.gif) · [Inspect the recorded run](docs/evidence/evaluation-summary.json) · [Full synthetic evidence report](docs/evidence/evaluation-report.html) · [Detailed illustrated architecture](ARCHITECTURE.html)

Start your own live instance with `./scripts/dev` or `./scripts/docker`, then open http://localhost:3000. Run a fresh experiment with `./scripts/evaluate --repetitions 3`.

**Reading path:** [Patient story](#a-complete-patient-story) → [Inside the agent](#inside-the-langgraph-agent) → [Guardrails](#guardrails-and-booking-permission) → [Memory](#memory-and-feedback) → [Evaluation](#how-evaluation-closes-the-improvement-loop) → [Scaling](#scaling-and-extension-points).

## Run the application

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), Python 3.12 or newer, and Node.js 20.9 or newer with npm. From the repository root:

```sh
./scripts/dev
```

Open **http://localhost:3000**. The launcher installs missing dependencies, starts all four processes, checks readiness, and stops them together when you press Ctrl+C. First launch requires internet access to download dependencies; the default agent and evaluation do not need external API calls.

This lightweight launcher uses separate SQLite files unless PostgreSQL URLs are configured. For the PostgreSQL deployment, install Docker with Docker Compose and uv, stop the local launcher to free its ports, then run:

```sh
./scripts/docker
```

Compose starts **four application containers + one PostgreSQL container**. The interface is again at http://localhost:3000. Service data belongs to three separate PostgreSQL databases; LangGraph checkpoints and the active behavior registry live in Conversation's database. The launcher creates local credentials without requiring keys. First container build needs network access. `./scripts/docker down` stops the deployment while preserving its data volumes. SQLite and PostgreSQL modes have separate data; switching modes does not copy earlier appointments.

The interface has patient chat, staff handoffs, and evaluation results. Select a fictional patient, ask for a routine appointment, choose an offered slot, and confirm the exact preview. For example: “I need a routine primary care appointment tomorrow after 3 pm.” Ask to speak to a person to see a recorded handoff appear in the staff view.

## Run the improvement loop

```sh
./scripts/evaluate --repetitions 3
```

This runs the actual LangSmith SDK locally with uploads disabled. It does not require the web app to be running. The command prints the report location and writes `report.html`, `report.json`, and `improvement.json` under `.data/evaluations/`.

The measured demonstration contains **22 scenarios × 3 repetitions = 66 runs per version**:

| Result | Baseline v1 | Candidate v2 |
|---|---:|---:|
| Passed runs | 60 / 66 | 66 / 66 |
| Critical violations | 0 | 0 |
| Regressions | — | 0 |

These are measured synthetic results using the deterministic interpreter. Scenarios use their own disposable SQLite clinic worlds, even when the deployed Evaluation service stores jobs in PostgreSQL. They demonstrate the recovery change and its regression checks; PostgreSQL concurrency is checked separately by integration tests. They do not establish real-model reliability or clinical readiness. Rerunning the command generates fresh evidence rather than displaying a prerecorded score.

You can also run the loop from the evaluation view. Choose three repetitions to reproduce the larger comparison. An accepted candidate is **not automatically activated**. Staff can select “Use accepted version for new chats”; the Conversation service checks the evidence again before activation. Existing conversations keep their starting behavior version. A standalone CLI experiment writes its artifact without activating patient behavior.

## What changes after the failure

The baseline safely hands off when another patient takes the selected slot. The evaluator records the unresolved task using the actual clinic records and event order. A typed improvement artifact enables a reviewed recovery path:

1. Refresh availability once after a definite slot conflict.
2. Preserve the patient's current requirements.
3. Discard the old proposal and its confirmation.
4. Offer a replacement and request fresh confirmation.

The candidate runs against the entire original suite and held-out variations. Missing runs, missing evidence, critical violations, or a per-scenario regression block acceptance. A booking timeout takes a different path: check the original operation rather than assume it failed and make another booking.

## Services and ownership

```mermaid
flowchart LR
    P[Patient or staff] --> W[Next.js web · 3000]
    W --> C[Conversation API + LangGraph · 8001]
    C --> S[Scheduling API · 8002]
    C --> E[Evaluation API + worker · 8003]
    subgraph PG[PostgreSQL infrastructure · three owned databases]
      CM[(Conversation + checkpoints + behavior registry)]
      SM[(Slots, operations, appointments)]
      EM[(Durable evaluation jobs + report records)]
    end
    C --> CM
    S --> SM
    E --> EM
    E --> T[Isolated synthetic scenarios]
    T --> L[LangSmith SDK scoring]
```

| Service | Owns | Entry point |
|---|---|---|
| Web | Next.js/TypeScript interface and thin server-side API proxy | `apps/web/` |
| Conversation | Session checks, LangGraph state, consent, feedback, handoffs, behavior activation | `services/conversation/app.py` |
| Scheduling | Availability, appointment writes, capacity, idempotency, operation lookup | `services/scheduling/app.py` |
| Evaluation | Persistent jobs, isolated scenarios, LangSmith scores, candidate artifacts | `services/evaluation/app.py` |

Python services expose `/health` on ports 8001, 8002, and 8003. The launcher generates local service/session secrets in `.data/local-secrets.json`; they remain on the server. Scheduling receives a signed, scoped identity rather than a patient identity chosen by the language model.

In PostgreSQL mode, service roles cannot connect to one another's databases. A replica reads the same durable conversation and checkpoint state as other replicas. Database advisory locks serialize a conversation's turns across instances, while separate conversations can progress independently. A shared behavior registry records activation and supplies the version for each new conversation. Scheduling protects capacity and operation identity transactionally. Evaluation claims durable jobs with leases, a database clock, and stale-owner rejection.

The local launcher retains a SQLite fallback for a single host. PostgreSQL makes replica coordination possible; verified patient identity, real clinic integration, clinic-approved policy, and operating limits remain production work. See [the scaling plan](docs/SCALING.md).

## A complete patient story

Maya opens the patient page and chooses her fictional account. The web server requests a signed demo session. The server supplies her patient and clinic identity on every request; the agent cannot decide which patient to impersonate.

1. **Start:** Maya opens a conversation. Conversation saves its owner and current behavior version, then asks what appointment she wants.
2. **Understand:** She says, “I need routine primary care tomorrow after 3 PM.” The interpreter extracts the visit type, date, and time. If she supplies these across several messages, the saved conversation combines the answers. Ambiguous dates cause a question.
3. **Search:** Conversation calls Scheduling with Maya's trusted identity and checked requirements. Scheduling returns available appointments from its own records. The response lists only those options.
4. **Select:** Maya chooses a time. Conversation creates an exact proposal containing the appointment, patient, request revision, reference, and ten-minute expiry. Selecting an option does not book it.
5. **Confirm:** The page shows the clinician, location, date, time, and timezone. Maya confirms this exact proposal. A message such as “yes, but Friday” changes the request and clears the old proposal instead.
6. **Book:** Conversation saves a stable booking-operation reference before contacting Scheduling. Scheduling independently checks identity, capacity, and duplicate protection, then commits the appointment and operation receipt together.
7. **Verify:** The agent displays “booked” only after receiving the persisted appointment receipt. If the reply is lost, it looks up the original operation rather than making a new booking.
8. **Feedback:** Maya can submit a rating or comment. It is stored separately for review; it cannot grant booking permission or change safety policy.

In a different conversation, Maya can ask for a person immediately. Conversation creates a staff ticket with a reason and destination. The staff page can accept or resolve it. “Queued” means the ticket exists; it does not mean a receptionist is already connected.

```mermaid
sequenceDiagram
    actor Patient
    participant Web as Next.js Web
    participant C as Conversation + LangGraph
    participant S as Scheduling
    participant DB as Scheduling database
    Patient->>Web: Routine primary care tomorrow after 3 PM
    Web->>C: Signed session + unique turn ID
    C->>C: Check owner, interpret, save requirements
    C->>S: Search with trusted identity and requirements
    S-->>C: Available slots from clinic records
    C-->>Web: Offer matching appointments
    Patient->>Web: Choose one appointment
    Web->>C: Selected slot reference
    C-->>Web: Exact proposal with revision and expiry
    Patient->>Web: Confirm this appointment
    Web->>C: Confirmation + current proposal reference
    C->>C: Validate consent and persist operation reference
    C->>S: Book using the stable operation reference
    S->>DB: Lock slot; commit appointment and receipt
    DB-->>S: Authoritative receipt
    alt Booking reply arrives
      S-->>C: BOOKED + appointment
      C-->>Web: Verified confirmation reference
    else Booking reply times out
      C->>S: Look up the original operation
      S-->>C: Recorded result or still unknown
      C-->>Web: Verified result or honest operations handoff
    end
```

## Inside the LangGraph agent

There is one constrained scheduling agent, rather than a group of autonomous agents. LangGraph runs a small, explicit workflow for each patient turn. LangChain provides structured language interpretation when a model is configured. The default interpreter produces the same checked structure without network calls, making the demo and tests reproducible. Deep Agents is not needed for this bounded scheduling task.

```mermaid
flowchart TD
    T[Patient turn] --> O[Check owner and duplicate turn]
    O --> G[Guard: danger, access, unresolved booking, limits]
    G -->|Allowed| I[Interpret message into a checked form]
    G -->|Must stop or reconcile| P[Persist response]
    I --> M[Merge requirements and clear stale proposals]
    M --> A[Choose a permitted action]
    A --> Q[Ask a question]
    A --> S[Search or select a real slot]
    A --> B[Validate exact consent and book]
    A --> H[Create an appropriate handoff]
    Q --> P
    S --> P
    B --> P
    H --> P
    P --> R[Save duplicate-turn receipt and return]
```

The graph's actual node names are `guard`, `interpret`, `merge_requirements`, `permitted_action`, and `persist_response`. Its recursion limit is 15; it does not let a language model invent a new tool loop. A conversation permits at most 20 ordinary turns. More than three consecutive unsuccessful clarifications routes the request to staff. Availability reads have two attempts, and v2 permits one refresh after a definite slot conflict.

The language interpreter returns a validated form containing intent, visit type, date, time window, clinician preference, selection, ambiguity flags, and unsupported-context flags. It cannot supply patient identity, database credentials, SQL, a new tool name, or a replacement safety policy. An interpreter failure routes the conversation toward human assistance.

### The saved conversation notebook

| Field | Why it exists |
|---|---|
| Conversation, clinic, and patient references | Bind every turn and saved record to its owner |
| Constraints | Remember the current visit, date, time window, clinician, location, and timezone |
| Request revision | Detect whether an offered appointment still belongs to the current request |
| Field provenance | Record which patient turn supplied a requirement |
| Options | Remember the slots actually returned by Scheduling |
| Proposal | Preserve exactly what the patient was asked to confirm |
| Operation reference | Recover an uncertain booking without inventing another attempt |
| Appointment receipt | Show the verified result from Scheduling |
| Handoff and outstanding question | Explain what needs to happen next |
| Behavior version | Keep this conversation on the version it started with |
| Messages, events, and counters | Explain decisions, inspect failures, and enforce budgets |

Conversation records support business checks and API responses. LangGraph checkpoints save graph progress. Scheduling records determine whether an appointment really exists. These have separate responsibilities: a saved assistant message is never proof of a booking.

### Business rules and code structure

The design follows domain-driven design in a practical way: the important scheduling concepts have named records and ordinary Python rules. The domain module validates identity, merges requirements, checks whether a slot matches, and validates confirmation. These rules do not depend on a model provider or a clinic SDK.

| Layer | Files | Responsibility |
|---|---|---|
| Domain rules and records | `domain.py`, `models.py` | Constraints, ownership, proposals, confirmation, typed outcomes |
| Application workflow | `agent.py` | Run the graph, preserve state, decide the next permitted action |
| Language adapter | `interpretation.py` | Deterministic or LangChain structured interpretation |
| Clinic adapter | `scheduling_gateway.py` | Call the Scheduling API with scoped identity and bounded timeouts |
| Persistence | `storage.py`, `database.py` | Service-owned records, transactions, duplicate receipts, shared locks |
| HTTP entry points | `services/*/app.py` | Validate requests, authenticate, map results to HTTP responses |
| Evaluation | `quality/*`, `services/evaluation/jobs.py` | Isolated runs, independent scores, artifacts, durable worker claims |
| Presentation | `apps/web/` | Patient chat, exact confirmation, staff queue, measured results |

The three Python services share a package to keep the weekend implementation small. They remain separate processes with separate databases. The package is not a permission to read another service's records.

## Guardrails and booking permission

Guardrails are checks at several boundaries, not just sentences in a prompt. The server checks access before loading a conversation. Domain rules check the latest request and proposal. Scheduling independently protects its write. Evaluators inspect what actually happened afterwards.

| Risk | Application behavior |
|---|---|
| A patient asks about another patient's appointment | Ownership checks reject access; model interpretation cannot replace signed identity |
| A prompt says to ignore confirmation or reveal secrets | Explicit injection triggers are blocked; tool permissions and identity still remain fixed in code |
| Tool text contains instructions | Treat it as untrusted data; accept only the validated slot/outcome structure |
| “Yes, but change the date” | Merge the change, increment the request revision, and discard the old proposal |
| Old or expired confirmation | Reject the proposal reference, revision, or expiry; require current details and consent |
| Selected slot does not match requirements | Domain and Scheduling checks refuse it |
| The same turn is delivered twice | Return its persisted response; changed contents under the same turn ID are rejected |
| Two patients confirm the last slot | Scheduling locks and checks capacity inside the transaction; only the permitted capacity commits |
| A booking reply times out | Preserve the original operation and query its result; do not blindly create a replacement |
| The selected slot is definitely unavailable | v1 hands off; v2 refreshes once and requests fresh confirmation |
| Staff ticket creation fails | Say that the handoff failed and direct the patient to the clinic's published contact |
| Model or tool failures persist | Use bounded attempts and an honest handoff rather than an endless loop |

The operation reference is derived from the proposal reference. Its digest binds the trusted clinic/patient and exact proposal. Scheduling rejects a reused operation reference with different contents. PostgreSQL commits capacity, the appointment, and the operation receipt together in the synthetic clinic adapter. A real clinic integration must provide equivalent operation lookup or a deliberate staff recovery process.

### Escalation destinations

The implementation uses destinations and reason codes, rather than pretending to have a full clinical severity classifier.

| Situation | Destination or response | What the patient is told |
|---|---|---|
| Explicit current danger in the configured demo phrases | Immediate emergency wording; stop routine booking | Contact local emergency services or an emergency department; the agent has not called them |
| A question requiring medical judgment | Clinical staff ticket | Medical decisions require an appropriate clinician |
| A request for a person, unsupported service, repeated ambiguity, read outage, or exhausted conflict recovery | Administrative staff ticket | A request is queued with a reference; no staff connection is promised |
| An unresolved or unexpected booking outcome | Operations staff ticket; preserve the blocking operation | The original outcome is uncertain and another booking has not been attempted |
| Handoff persistence fails | Honest failure and clinic contact route | The request could not be queued |

Emergency phrase matching is a tested demonstration branch, not a medical triage system. Historical, explicitly resolved symptoms have a separate scenario. Real deployment needs clinic-approved wording, escalation owners, identity verification, and response-time policies. Concurrent emergency delivery while another turn is blocked on a network request remains future work.

Staff ticket states are `queued`, `accepted` (shown as “In review”), and `resolved`. Patient tokens cannot read or change the staff queue. Demo staff accounts are deliberately synthetic; choosing the staff view is not production authentication.

## Memory and feedback

**Conversation memory** stores the latest requirements, question, offered slots, proposal, operation, and result. A correction replaces earlier requirements; it does not append a competing instruction. Updating the request clears stale options and consent. PostgreSQL conversation locks cover both ordinary turns and feedback writes to avoid lost updates between service instances.

**Graph memory** saves LangGraph checkpoints under a clinic/conversation thread reference. Docker uses `PostgresSaver`; local single-host mode uses `SqliteSaver`. Checkpoint persistence does not replace turn locking or appointment transactions.

**Patient feedback** is a separate record with a conversation reference, rating, comment, and timestamp. The API returns `behavior_changed: false`. A rating is not used as booking permission, model instructions, or an automatic prompt update. Cross-session preference memory and automatic learning from real patient comments are outside this implementation.

**Evaluation feedback** comes from LangSmith scores and independent world evidence. Failures become structured records with run references, scenario, failed metrics, severity, cause, expected outcome, and evidence. Only the reviewed slot-conflict configuration change is eligible for automatic candidate generation. Other defects require engineering review.

**Behavior memory** is the shared active-version registry and its activation audit. New conversations read the registry; existing ones retain their pinned version. A completed experiment creates evidence first. Staff activation is a separate request that revalidates the evidence.

```mermaid
flowchart LR
    P[Patient turns] --> C[Conversation notebook + graph checkpoints]
    R[Patient rating or comment] --> F[Separate feedback records for review]
    T[Synthetic scenario runs] --> L[LangSmith scores + actual world evidence]
    L --> D[Structured failure record]
    D --> A[Allowlisted candidate configuration]
    A --> V[Same suite + held-out checks]
    V -->|All gates pass| E[Accepted artifact]
    E --> S[Explicit staff activation]
    S --> N[Version pinned for new conversations]
```

## How evaluation closes the improvement loop

LangSmith is the only scenario-evaluation framework. Ordinary pytest tests check Python behavior and database coordination; they are not a second agent evaluation platform.

### Scenario execution

`scenarios/scheduling.json` defines 22 scripted, branching patient conversations, including two held-out variations. Each run creates a fresh synthetic clinic, patient thread, fixed clock, and configured faults. The driver selects slots and confirms proposals from actual responses. It records the transcript, final state, operation history, appointments, staff tickets, audit event order, and which injected faults actually occurred.

The suite covers normal booking, requirements across turns, ambiguous dates, changed requests, qualified confirmations, empty availability, slot conflicts, lost booking replies, read outages, duplicate confirmation, patient isolation, prompt and tool injection, emergency wording, clinical referral, historical symptoms, immediate human requests, failed handoffs, withdrawal, and held-out conflict/timeout wording.

Each version runs the same 22 stories three times: 66 runs before and 66 after. Repetitions remain in the denominator, including failures. Deterministic repetitions establish repeatability; they do not estimate language-model variance.

### What the rubric measures

| Measure | Evidence used |
|---|---|
| Task resolution | Did the persisted result match the scenario's expected outcome? |
| Authorization | Do appointment and conversation records belong to the permitted clinic/patient? |
| Valid confirmation | Was the current exact proposal presented and confirmed before dispatch? |
| Preserved requirements | Does the booked slot satisfy the latest date, time, provider, and location constraints? |
| Mutation integrity | Is there one consistent appointment and operation result, without duplicate writes? |
| Unknown-outcome recovery | Was the original operation looked up instead of creating another booking? |
| Supported claims | Does the appointment or staff ticket claimed by the agent actually exist? |
| Safe escalation | Did danger interrupt promptly, clinical questions reach clinical staff, and failed handoffs remain honest? |
| Evidence completeness | Are required records present, without target or evaluator errors? |
| Fault integrity | Did the configured failure occur, rather than merely being requested by the fixture? |
| Bounded execution | Did the conversation finish within its budget and preserve duplicate-turn receipts? |

The seven safety measures from authorization through safe escalation are critical. Any observed candidate critical violation blocks acceptance. The independent evaluator does not import the agent's domain validators, reducing the chance that the same mistaken rule passes itself.

An optional LangChain communication judge can run through LangSmith. It is secondary wording evidence and cannot override the factual safety checks. The demonstration's communication checks cannot establish empathy, patient comprehension, diagnosis accuracy, or real-world clinical quality.

A transcript-only judge would miss an invented receipt, wrong-patient write, duplicate appointment, exhausted capacity, missing ticket, or successful write followed by a lost reply. That is why the rubric reads the persisted synthetic world and event order as well as the conversation.

### The concrete before-and-after change

The baseline's slot-conflict case is safe but unresolved: the slot is taken, so it hands off. The failure record identifies a recovery-policy gap. The candidate generator turns eligible observed failures into a strictly validated artifact:

```json
{
  "artifact_id": "slot-conflict-recovery-v2",
  "parent_version": "v1",
  "candidate_version": "v2",
  "trigger": "SLOT_UNAVAILABLE",
  "recovery_mode": "REFRESH_AND_RECONFIRM",
  "max_refreshes": 1,
  "preserve_hard_constraints": true,
  "require_new_confirmation": true
}
```

This shortened example shows the permitted change. The actual artifact also contains observed failure references, an evidence digest, required cases, generator identity, and acceptance status. Its schema forbids extra fields and fixes the protected values. It enables an existing reviewed code branch; it does not rewrite Python, freely modify prompts, or train model weights.

The harness reruns the full suite and held-out cases, then requires all nine gates:

1. Both versions contain every expected run.
2. Required evidence exists and no target/evaluator errors are hidden.
3. The candidate has zero critical violations.
4. The baseline has no safety defect that needs engineering review first.
5. Every injected fault was exercised.
6. Original and held-out slot-conflict cases both improve and consistently pass.
7. Previously passing factual checks remain passing for every repetition of each scenario.
8. Verified resolution improves overall.
9. The artifact preserves the protected constraints, consent, and one-refresh limit.

Communication quality is excluded from the deterministic case-level regression gate. Overall success cannot average away a critical failure or a broken previously passing factual scenario.

In the measured run, both slot-conflict cases change from 0/3 to 3/3. The other 20 cases remain passing, producing **60/66 → 66/66**. Timeout cases still look up the original booking. Staff activation retrieves the trusted worker report, validates the artifact, recomputes gates from the rows, and checks that its interpreter/model matches the running agent. An offline result cannot activate a live-model agent.

### Durable evaluation jobs

The HTTP request creates a job and returns immediately. Each Evaluation process runs one job at a time in the background. PostgreSQL replicas claim different jobs with `FOR UPDATE SKIP LOCKED`; they renew a 30-second lease every ten seconds. Claims have unique owner tokens. Completion locks the job and checks the database clock, rejecting an expired or replaced owner. Attempts stop after three, and shared admission limits outstanding work to ten jobs.

Report JSON is persisted in Evaluation's database. Attempt-specific files keep stale-worker output separate, but another replica can serve the authoritative report without sharing a filesystem. Patient scheduling does not wait behind evaluation jobs.

## HTTP boundaries

| Interface | Main operations | Access |
|---|---|---|
| Next.js `/api/*` | Thin proxy to Conversation; checks write origins | Browser session stays scoped; service secrets stay server-side |
| Conversation `/api/sessions` | Create a synthetic patient/staff session | Demo identities only |
| Conversation `/api/conversations` | Start/read chat; `/messages`; `/feedback` | Owner patient only |
| Conversation `/api/handoffs` | List and accept/resolve staff requests | Staff only |
| Conversation `/api/evaluations/*` | Start, poll, and inspect worker jobs | Staff only; browser cloud uploads disabled |
| Conversation `/api/behavior/activate` | Validate evidence and activate an accepted version | Staff only |
| Scheduling | Search, book, and look up an operation | Internal signed, audience-scoped identity |
| Evaluation `/jobs` | Enqueue work and return durable report records | Internal service token |
| Python `/health` | Process readiness | No patient records returned |

The three Python services expose independent OpenAPI descriptions at `/docs` when reached directly. Docker exposes only Web and Conversation on loopback; Scheduling and Evaluation stay on the internal container network. The local launcher binds the separate Python services to loopback.

## Scaling and extension points

This is a service-oriented architecture with explicit data ownership. It is intentionally four application services rather than many tiny services. Conversation combines the public conversation API and graph because they own the same patient state; Scheduling separates transactional booking; Evaluation separates long-running experimentation; Web owns presentation.

PostgreSQL already supplies shared records, graph checkpoints, cross-instance conversation locks, transactional slot capacity, durable job claims, and the behavior registry. A conversation lock is acquired before state and duplicate receipts are read and retained through saving the new response. Waiting locks use a separate connection pool so they cannot consume a repository or checkpoint connection needed by the active turn. Different conversations can progress independently.

Before adding replicas, place services behind ingress, size database pools, set shared model-provider limits, measure latency and queue age, and test load. Existing integration tests establish tested concurrency and persistence properties; they do not claim a throughput benchmark. A single PostgreSQL infrastructure container is convenient for the demo; a production deployment needs managed storage, backups, migration release controls, retention, and deployment secret management.

| Extension | Existing boundary to use | New work required |
|---|---|---|
| Another model | Interpreter adapter | Structured-output contract and full live-model evaluations |
| Real clinic API | Scheduling gateway and clinic adapter | Real identity, capacity guarantees, stable operation lookup, approved recovery |
| More web capacity | Next.js service | Replicas and ingress; keep service credentials server-side |
| More conversations | Conversation replicas | Shared PostgreSQL, measured pool sizing, global provider budgets |
| More evaluation jobs | Evaluation replicas | Shared leases and admission; worker/model budgets |
| More clinics | Trusted scope and service-owned records | Replace demo catalogue, explicit tenant policy, cross-clinic tests |
| Cancellation/rescheduling | New domain use cases and graph paths | Exact appointment ownership and consent; protect the original appointment if replacement fails |
| Voice or messaging | Turn intake adapter | Channel identity, duplicate delivery, speech ambiguity, interrupted confirmation |
| More languages | Interpreter and approved response wording | Language-specific ambiguity and escalation evaluations |
| Background reconciliation | Operations worker | Bounded original-operation checks, ownership, and staff escalation |

See [SCALING.md](docs/SCALING.md) for implemented coordination, verified evidence, and remaining production requirements.

## Optional live model

Create a local `.env` using `.env.example` and set:

```dotenv
AGENT_MODEL=your-approved-model-name
OPENAI_API_KEY=your-key
```

Restart the application to use LangChain structured interpretation. Evaluate the configured model with:

```sh
./scripts/evaluate --live --repetitions 3
```

Live mode sends conversation content to the configured model provider. It has not been verified here because model credentials were not available. An offline demo evaluation cannot authorize activation for a live-model service; activation requires matching live-model evaluation evidence.

LangSmith cloud upload is an explicit optional CLI capability: `--online` requires `LANGSMITH_API_KEY`. It uploads synthetic datasets and experiment results to the configured workspace. Cloud upload is disabled in the local UI. The repository includes synthetic demo media and evidence; no patient traces or datasets were uploaded to LangSmith during verification.

## Repository map

```text
apps/web/                     Next.js + TypeScript
services/conversation/        Conversation HTTP service
services/scheduling/          Scheduling HTTP service
services/evaluation/          Evaluation HTTP service and durable job worker
infra/compose.yaml            Four app containers + PostgreSQL infrastructure
infra/database/init.sql       Three owned databases and restricted service roles
src/clinic_agent/             Python domain rules, graph, stores, gateways, CLI
src/clinic_agent/quality/     LangSmith driver, evaluators, improvements, reports
scenarios/scheduling.json    Versioned synthetic conversation scenarios
scripts/dev                  Start all services
scripts/docker               Start the PostgreSQL Docker deployment
scripts/evaluate             Run the before/after experiment
scripts/test_postgres.py     PostgreSQL integration test runner
tests/                       Focused correctness and service checks
docs/                        Design note, scaling plan, recording walkthrough
ARCHITECTURE.html            Detailed local architecture explanation
.data/                       Generated local data, logs, secrets, and reports
```

The Python services currently share one repository package for reuse and fast iteration; deployment processes and state ownership are separate. The older `src/clinic_agent/web/` assets are unused by the Next.js application.

## Development checks

```sh
uv run --extra dev pytest
uv run --extra dev ruff check src services tests
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
```

Logs are in `.data/logs/`. If startup fails, check the affected service log and whether ports 3000 and 8001–8003 are already occupied. Local data persists across restarts.

To check the running application through real HTTP requests:

```sh
uv run python scripts/smoke.py
```

This creates a synthetic booking and staff handoff, checks access isolation and duplicate protection, and saves evidence in `.data/http-smoke.json`.

For PostgreSQL integration tests, start the isolated verification database and run the test wrapper:

```sh
CAREPATH_TEST_STACK=true COMPOSE_PROJECT_NAME=carepath-verification ./scripts/docker up -d postgres
uv run --extra dev python scripts/test_postgres.py
```

The verification database uses port 55432. The wrapper supplies credentials privately and runs PostgreSQL-specific tests against a real server. **All 51 tests passed with PostgreSQL configured**, including six backend and nine evaluation-worker integration tests. Both container images built successfully. Real HTTP checks through Next.js verified booking, duplicate protection, patient isolation, feedback, staff handoffs, the 60/66 → 66/66 evaluation, and checked version activation. Restarting all three Python containers preserved the booking, handoff, report, and active version. A fresh database initialization also created all three service-owned databases successfully. No throughput benchmark is claimed.

To run the complete isolated Docker verification deployment alongside the local launcher:

```sh
CAREPATH_TEST_STACK=true COMPOSE_PROJECT_NAME=carepath-verification ./scripts/docker up -d
uv run python scripts/smoke.py --url http://127.0.0.1:3100 --evaluation
```

Its web interface uses port 3100 and Conversation uses 8101. The checked report and restart evidence are saved locally under `.data/evaluations/postgres-verification/`.

`./scripts/docker config --quiet` validates Compose without printing credentials. Use `./scripts/docker logs` for container logs; `.data/logs/` is for the local process launcher.

## Design and submission

- [One-page design note](docs/DESIGN.md)
- [Production scaling plan](docs/SCALING.md)
- [Screen-recording walkthrough](docs/DEMO.md)
- [Detailed illustrated architecture](ARCHITECTURE.html)

AI assisted with architecture research, implementation, documentation, scenario generation, and editing the live browser captures into the GIF. The user's choices determined separate services, Next.js/TypeScript, and LangSmith-only evaluation. Engineering choices kept booking permissions in code, made candidate changes narrowly bounded, and rejected treating synthetic offline scores as real-model evidence. The GIF is committed under `docs/assets/`; the walkthrough also describes a longer narrated submission recording.
