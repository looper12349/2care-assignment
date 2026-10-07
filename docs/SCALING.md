# Growing the four-service design

The application has four independently deployed services. Docker uses PostgreSQL with three separately owned databases, while the local launcher retains a single-host SQLite fallback. Shared records and coordination support multiple service instances. Production identity, clinic integration, operating limits, and load testing remain separate work.

| Area | Implemented | Next production work |
|---|---|---|
| Web | Next.js interface and thin API proxy | Deploy web replicas behind the normal ingress/load balancer |
| Identity | Signed synthetic sessions; server-side scope checks | Verified identity provider and clinic-approved patient/staff permissions |
| Conversation | PostgreSQL records/checkpoints and shared conversation locks; SQLite fallback | Add replicas behind ingress; measure throughput and provider limits |
| Scheduling | Owned PostgreSQL database, transactional capacity and operation receipts | Approved clinic integration and explicit recovery contracts |
| Evaluation | PostgreSQL job claims, database-clock leases, fenced results and shared report records | Add workers within budgets; consider a broker when traffic warrants it |
| Behavior activation | Audited shared registry and pinned conversation versions | Controlled audience rollout and production approval policy |

## Why these service boundaries stay useful

Conversation waits mainly on language-model calls. Scheduling protects short transactional writes. Evaluation performs longer background work. The web interface handles presentation. Each can gain capacity independently without changing the business rules.

The public Conversation API and agent remain together because they own the same conversation and usually change together. A separate general Platform API is useful later if several products, clients, or agent services need shared entry routing; adding it now would introduce another hop and split request context.

## Storage and coordination already implemented

Docker starts one PostgreSQL instance with three owned databases and separate credentials. Service roles cannot connect to another service's database. Conversation owns conversations, feedback, handoffs, checkpoints, and behavior activation. Scheduling owns capacity, appointments, and operation receipts. Evaluation owns jobs and experiment reports. Services communicate through APIs.

LangGraph's PostgreSQL checkpointer saves graph progress. It does not by itself prevent two replicas from processing the same thread simultaneously, and it does not make an external appointment write occur exactly once.

Each conversation uses a PostgreSQL advisory lock before reading saved state or duplicate receipts. The lock remains held until the updated state and response receipt are saved. Feedback uses the same lock to prevent lost updates. A dedicated connection pool handles waiting locks so waiting turns cannot consume a repository or checkpointer connection needed by the active turn. Different conversations can proceed independently. Repeated turn identifiers return the saved response.

Scheduling serializes identical operation references and locks the selected slot during its short write transaction. Capacity, the appointment, and the operation receipt commit together. An operation is bound to trusted identity and exact contents; reusing its reference with changed contents is rejected. An uncertain response is resolved by looking up that reference. A real clinic adapter must support safe outcome lookup or define a staff recovery process.

## Keep background jobs durable and bounded

The evaluation queue persists jobs, uses a 30-second claim lease with a 10-second heartbeat, and limits each process to one running job. PostgreSQL replicas claim different jobs with row locks and `SKIP LOCKED`. Shared admission caps outstanding jobs at ten; retries stop after three attempts. Heartbeats and completion lock the job before checking the database clock and owner token. An expired worker cannot publish, even before another worker reclaims its job. Attempt-specific folders keep file evidence separate; authoritative report JSON lives in PostgreSQL.

These PostgreSQL-backed claims can serve low-volume evaluation workloads. A separate broker becomes useful when measured job traffic, latency, or operating needs justify it. If a future transaction must both change a record and publish an event, add an outbox: save the event beside the record, then deliver it with retry. Consumers still need duplicate protection.

Normal chat need not wait in the same queue as evaluation. Keep evaluation on separate workers with separate budgets. Reconciliation of uncertain booking operations can become another durable background workload, with bounded checks and staff escalation.

## Control load before it becomes failure

Limit simultaneous model calls, request sizes, retries, and time budgets. Apply provider limits across replicas, not independently on every process. Measure queue age, model latency, conversation errors, booking conflicts, unknown outcomes, and handoff delivery failures. Scale the service that actually reaches its limit.

Protect Scheduling from retry storms. Backoff does not replace checking whether a write already succeeded. Use request deadlines and explicit outcome types; never turn a timeout into an invented “booking failed” response.

## Add clinics through explicit ownership

Carry verified clinic/patient scope through every API call, record, operation, and checkpoint lookup. Separate clinic credentials and policy. Keep evaluation credentials unable to mutate real clinic appointments. Include cross-clinic isolation and authorization tests before a second tenant.

Clinic-approved emergency instructions, escalation ownership, response promises, data retention, and external-provider contracts must be settled before real patient data is introduced. Model interpretation is never the source of patient identity or booking permission.

## Preserve the improvement loop while scaling

The shared registry records the active behavior and activation audit. Each new conversation reads it and pins its version; existing conversations keep their version. Activation recomputes safety, evidence completeness, original and held-out performance, and per-case regression gates. In production, add designated-owner rollout controls, a small initial audience, and a rollback procedure.

Expand beyond the current deterministic demonstration with live-model evaluations, more independent patient phrasing, larger held-out sets, and calibrated human review of wording. The current communication score is a limited deterministic check; it cannot establish empathy, comprehension, or clinical correctness.

## Verified evidence and limits

All 51 tests passed with PostgreSQL configured. Six backend integration tests check independent instances, duplicate confirmations, slot races, crash recovery, checkpoints, shared activation, feedback ordering, and role isolation. Nine worker tests check claims, leases, admission, retry limits, concurrent schema startup, and database access.

Both Docker images built. Real requests through Next.js verified booking, feedback, staff handoffs, the LangSmith 60/66 → 66/66 improvement, and version activation. Restarting all three Python containers preserved records and active behavior. Fresh database initialization was checked separately. Local evidence is in `.data/evaluations/postgres-verification/`.

These checks prove the tested safety and persistence properties. They do not measure sustained throughput, real-model reliability, or real clinic behavior. Load testing, migration release management, backups, deployment secret management, retention, and service-wide model budgets remain necessary before production use.

## Official references

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence): checkpoints and stores.
- [Next.js backend-for-frontend guidance](https://nextjs.org/docs/app/guides/backend-for-frontend): the web layer can provide a thin server-side proxy.
- [Microsoft microservices guidance](https://learn.microsoft.com/en-us/azure/architecture/guide/architecture-styles/microservices): independent ownership/scaling and the costs of additional service boundaries.
- [Microsoft queue-based load leveling](https://learn.microsoft.com/en-us/azure/architecture/patterns/queue-based-load-leveling): separating background demand from processing capacity.
