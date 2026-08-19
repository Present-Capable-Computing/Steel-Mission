# Durable broker service and remote pull-runner — technical plan

Project [`PRJ-0001`](../plan/PRJ-0001.json). The governing rules for how this work
lands are in [`docs/workplan.md`](workplan.md); this document is the design.

## 1. Why

Steel-Mission has the domain logic and lacks the service underneath it. Every gap
below is verified in the current code, not inferred.

| Gap | Where | Consequence |
|---|---|---|
| State is one unlocked JSON document, loaded and written whole | `bin/present-lease-broker`, the state load and write helpers | Last-writer-wins. Two concurrent commands silently discard one another's work. |
| Execution is a synchronous subprocess call | private runner | A fixed cap, and output truncated silently partway through. A job that says nothing is indistinguishable from a job that said too much. |
| Requests carry host-local workspace paths | job request payload | A job can only run on the machine that created it. There is no remote execution to speak of. |
| Two orchestration paths | `steel-mission-chat/server.py` sequential daemon threads, versus the broker's dependency graph | The chat server only ever reads broker state. Nothing reconciles the two. |
| Restart marks running missions paused | chat server startup supervision | Work is stranded, and the field that records it reads like a diagnosis rather than a failure to resume. |
| Connector delivery has no retry, backoff or dead-letter path | connector action execution | A transient 429 loses the reply, and the exception is dropped. |
| Identity is a shared secret | worker authentication | Adequate for a local worker on the same host. Not adequate for a runner in someone else's infrastructure. |

## 2. Architecture

1. **New root package `steel_core/`**, following the import pattern of the existing
   root `adapters` package. All durable logic lives there. The executables and the
   chat server become thin consumers of it. (Decision D4 — the package is not named
   after the separate programme this code came from.)

2. **The database is the queue.** The broker command line, the daemon and the chat
   server are direct clients of one transactional store, sharing one command module
   and one state machine. There is no command-line-to-daemon remote call. The daemon
   notices work by polling at 250 ms; PostgreSQL `LISTEN`/`NOTIFY` is a later
   optimisation, not a design dependency. Two network surfaces exist: the
   runner-facing gRPC gateway on the daemon, and an authenticated HTTP operations
   API that replaces the current subprocess-per-request broker server.

3. **Document plus promoted columns.** The legacy per-task document moves into a
   `tasks` table as an opaque body with a promoted status. Everything that is
   concurrency-hot — workflows, jobs, leases, events, inbox, outbox, runners,
   evidence — gets real columns. A **JSON mirror** reconstructs the legacy state
   document after each mutation, on by default locally, so the existing suite stays
   green until the mirror is consciously retired.

4. **Fencing tokens.** Every lease grant increments a per-job fence token. Heartbeats
   and results carry it, and a stale fence is refused. This one mechanism is what
   makes both coordinator death and runner death safe; without it, a resurrected
   process can still write.

5. **JSON in a protobuf envelope.** gRPC messages carry canonical JSON validated
   against `schemas/canonical/*-v2.json`. The schema-first discipline is preserved
   and protobuf is framing only. Runner results are signed with Ed25519 using a
   runner-held key whose public half is pinned at enrolment. The existing shared-secret
   contract survives as the local compatibility adapter.

6. **Mission control becomes a runner** (P4). The chat server runs an embedded,
   in-process control-plane runner that claims `mission-node` jobs and executes the
   existing node bodies unchanged. Delivery steps become sandbox jobs under the v2
   specification. An approval gate becomes a first-class `WAITING_APPROVAL` job
   state, which removes the thread-exits-and-is-relaunched pattern entirely.

7. **Crash injection.** Environment-gated crash points through `steel_core/testhooks.py`,
   inert in production, plus SIGKILL on a sentinel file for whole-process death.
   Driven from subprocesses, matching how this suite already tests.

## 3. P1 — Durable broker service

**New modules**

- `steel_core/store/__init__.py` — the `Store` interface: transactions, job claim,
  event append, exclusive state for a task, and a factory that opens by URL.
- `steel_core/store/ddl.py` — portable table definitions and the dialect shim
  (identity columns, parameter style).
- `steel_core/store/sqlite_store.py` — write-ahead logging, immediate-mode write
  transactions, a busy timeout, and a durable commit.
- `steel_core/store/postgres_store.py` — psycopg 3; claim by row lock with skip-locked.
- `steel_core/statemachine.py` — one authoritative transition table. Job states:
  `QUEUED`, `AVAILABLE`, `LEASED`, `RUNNING`, `WAITING_APPROVAL`, `RETRY_WAIT`,
  `SUCCEEDED`, `FAILED`, `CANCELLED`, `DEAD`. Workflow states: `ADMITTED`, `RUNNING`,
  `PAUSED_APPROVAL`, `SUCCEEDED`, `FAILED`, `CANCELLED`. A single entry point applies
  a transition and appends the event row in the same transaction, so an event and the
  state it describes cannot disagree. A compatibility map covers the legacy status set.
- `steel_core/commands.py` — the transactional command functions lifted out of the
  broker's command handlers, shared by the command line, the daemon and the chat server.
- `steel_core/controller.py` — the drive loop lifted out of the distributed run
  command: admission, topological ready batches, dispatch, collect, retry.
  Exponential backoff is added behind configuration whose default reproduces today's
  single retry, so the existing tests keep passing.
- `steel_core/leases.py` — broker-authoritative grant, heartbeat, expiry and reclaim,
  with fence tokens and a sweeper. This replaces expiry noticed lazily on the next read.
- `steel_core/compat.py` — import and export between the legacy state document and
  the store, the JSON mirror, and automatic migration on first open. It reuses the
  existing state export and import-validate seam rather than inventing a second one.
- `steel_core/testhooks.py`, `bin/steel-brokerd` (controller loop, sweepers, a
  controller-singleton leader lease, the HTTP operations API, graceful drain on
  SIGTERM), `steel_core/api_http.py` (bearer token read from a 0600 file, matching
  the existing key conventions).
- Schemas: `store-archive-v2`, `lease-record-v2`, `broker-daemon-status-v1`, with
  registry entries, plus new event types for job claimed, lease expired, lease
  reclaimed, workflow resumed after restart, controller elected and controller drained.

**Tables.** ISO-8601 timestamps as text and JSON as text, following the repository's
existing convention: `schema_migrations`, `kv_meta`, `tasks`, `workflows` (with a
unique idempotency key and a `tenant_id` defaulted to `local` — unused now, so that
multi-tenancy is not a breaking change later), `jobs` (unique on workflow, node and
attempt; indexed on status and availability; carrying retry policy and deadline),
`leases` (fence token, indexed on expiry), `runners`, `events` (identity primary key,
unique event id, validated against the event schema on insert), `recovery_ledger`,
`operator_audit`, `artifacts` and `job_artifacts`.

**Refactor order.** The storage swap happens **first**, underneath the existing
handlers: every read-modify-write is wrapped in an exclusive state transaction, which
converts last-writer-wins into serialized transactions with no handler rewritten.
Heartbeat renewals bypass that transaction and write the leases table directly, or
throughput collapses. The broker server becomes a thin authenticated proxy with its
response shapes preserved; its tests gain a token header, and that is the only test
change in P1. Worker-held lease files continue to be written; the broker-side row
becomes the authority.

**Sequence, green at each step.** Store and state machine with unit tests → broker
state through the store behind an environment flag, mirror on, auto-migrating, full
suite green → transitions routed through the single entry point → leases table,
fencing and sweeper → controller extraction, with the distributed run command becoming
enqueue-then-drive-synchronously and producing identical output → the daemon, plus the
coordinator-kill test → PostgreSQL backend, its pytest marker, and a CI service job.

## 4. P2 — Pull-runner agent and job protocol v2

**Schemas.** `job-spec-v2` carries job, workflow, mission, task and node identity, a
phase, and a **source** — a pinned commit, a bundle digest, or none — which is what
replaces the host-local workspace path. It pins the image by digest, names a toolchain
profile, carries the command as an argument vector with named environment variables,
lists input artifacts and expected outputs, carries `secretRefs` as references only,
declares an egress profile, resources, an idempotency key, a retry policy, a deadline
and its attestation requirements. `job-result-v2` carries the fence token, the runner
id, the image digest actually used as reported by the container runtime, the input
hashes, the outputs, a log digest, standard output and error tails **with explicit
truncation markers**, and an Ed25519 signature with a key id. `runner-identity-v1`
covers enrolment.

**Protocol.** `steel_core/grpc/proto/runner_v2.proto`, with generated code **checked
in** under `steel_core/grpc/gen/`; `make proto` regenerates it and `make proto-check`
gates drift in continuous integration. The gateway exposes one bidirectional session
stream plus artifact upload and download chunk streams. Runner to broker: hello with
labels, protocol version and result-signing public key; claim; heartbeat with lease and
fence; job started; log chunk; result with signature; cancel acknowledgement. Broker to
runner: hello acknowledgement, job offer with lease, fence and specification, heartbeat
acknowledgement carrying either an extension or a stale-fence refusal, cancel, result
acknowledgement, drain. **Streams hold no durable truth.** Everything lives in the
database, a hello re-associates a runner after a reconnect, and leases outlive stream
death until their time to live expires.

**Code.** `steel_core/grpc/service.py` maps a peer certificate to a runner row and
performs claim, heartbeat and result inside store transactions.
`steel_core/sandbox.py` is the container hardening logic **extracted** from the private
runner executable into an importable module; the executable becomes a thin command-line
wrapper over it, with its schema and all of its tests unchanged, protected by a
characterization test that compares the container arguments produced before and after.
`bin/steel-runner-agent` is the dial-out agent: claim loop, source materialization by
pinned commit or bundle digest, just-in-time secret resolution into the process
environment, sandbox execution, chunked artifact streaming in place of base64 over
standard input and output, Ed25519 signing, and enrolment with a broker-side approval
step. The daemon gains the gRPC listener behind a new configuration block, and a
development certificate-authority helper built on the dependency already present.

**Wiring.** The dormant transport-kind seam finally branches. The remote runner kind
routes node invocation to a job-table enqueue and a wait on the result; the local
subprocess kind stays the default, so the whole existing suite keeps exercising the
unchanged path. The remote artifact store stub becomes real.

**Sequence.** Schemas → proto, code generation and the drift gate → sandbox extraction
as its own pull request → runner registry and gateway service, flag off → agent and
enrolment → the transport branch and an end-to-end test running the daemon and two
agent processes over loopback mutual TLS.

## 5. P3 — Durable connector inbox and outbox

**Tables.** `inbox` with a unique deduplication key, the source, the external event
id, whether the signature verified, the payload, a status and the mission it started.
`outbox` with a unique deduplication key, the connector, the action, the payload, a
status, the attempt count, the next attempt time, any retry hint, the last error and a
dead-letter reason, indexed on status and next attempt. Modules `steel_core/inbox.py`
and `steel_core/outbox.py`; the delivery worker does exponential backoff with jitter,
honours a retry hint on 429 and 503, and caps attempts into the dead-letter queue,
with a command-line surface to list and requeue it.

**Chat server changes.** Workflow ingress replaces the receipt-file deduplication and
the lock released before the work starts with a transactional insert that does nothing
on conflict; the mission starts only for the transaction that inserted and claimed the
row, and the received-to-consumed transition commits with the mission creation.
Signature verification is unchanged. Tests that asserted on receipt files are
consciously rewritten to assert on inbox rows. Native connector posts become outbox
enqueues in the same transaction as the evidence write; the delivery worker runs in the
chat server locally, or in the daemon where a leader lease prevents duplicates. The
bare exception that dropped failures disappears.

**Effective exactly-once reply.** The deduplication key is mission, node, connector and
action. A row goes pending to in-flight before the post. An idempotency marker is
embedded where the connector supports one — a hidden comment in a GitHub body, Slack
message metadata, a Jira entity property. On restart, in-flight rows are verified by
querying for the marker before anything is re-sent. GitHub ships first. Where a marker
cannot be read back, the behaviour degrades to a retry with a visible duplicate
annotation, and the documentation says so rather than claiming a guarantee.

**Sequence.** Tables and modules unwired → ingress swap with its test updates → outbox
swap one call site at a time, GitHub then Slack then Jira, each with a fake-endpoint
test → crash-point and dead-letter tests.

## 6. P4 — One orchestration path

- `steel_core/mission_bridge.py` translates a mission into a workflow: the node list
  becomes a sequential chain, preserving today's semantics. Parallel execution is
  possible under the new model and is not turned on here. Job kinds are `mission-node`
  for the existing control-plane node kinds, and `sandbox-job` for delivery steps under
  the v2 specification; local development keeps using the local adapter path, so the v1
  private runner still works end to end.
- The chat server runs an embedded control-plane runner with an in-process claim loop
  and a `mission-control` label, executing the existing mission node bodies unchanged.
- Approval gates become the `WAITING_APPROVAL` job state. Approving or resuming a
  mission signals the job through the shared command module. Separation of duties is
  retained at the point of signalling, and the approval evidence is written before any
  successor is dispatched, in one transaction.
- The orchestrator launcher submits through the mission bridge, and the per-mission
  daemon threads are deleted at the end of the phase. Startup supervision is replaced:
  the sweeper reclaims dead mission-node leases, missions resume automatically, and the
  field that recorded orphaned-at-startup is removed along with the tests that assert it.
- Evidence is mirrored into an `evidence` table — digest, chain hashes, signature — so
  that an archive export captures workflow, approvals and evidence in one restorable
  artifact, wrapping the database's own backup mechanism. Mission documents and the
  integrity chain stay authoritative and are referenced by digest.
- A rollout flag selects the legacy or broker orchestrator. It defaults to legacy while
  tests migrate, flips mid-phase, and the legacy code is deleted at the end of the
  phase. It is not left as a permanent fork.
- **Test churn concentrates here.** Enumerate it first by grepping for the
  orphaned-at-startup field, the orchestrator launcher and the supervision entrypoint.
  The chat server is loaded by path with module-scope monkeypatching: never rename a
  patched module-level function, wrap it.

## 7. P5 — Naming

The repository names one product. Executable names, environment variable prefixes,
schema identifier namespaces and prose are migrated to Steel-Mission. No behaviour
changes: a rename that also fixes something is a rename whose regression cannot be
attributed. Each executable keeps a working compatibility name for one release, and
removing each shim is its own recorded decision. The package name is settled up front
by decision D4 so that P1 never creates a package that P5 has to rename.

## 8. Tests, dependencies and tooling

New test files cover the store, the state machine, the broker daemon, the runner
protocol, the runner agent, the sandbox extraction, the inbox and outbox, mission and
broker integration, and the acceptance criteria. A root `pytest.ini` adds markers for
postgres, docker and slow. A `tests/conftest.py` provides a temporary store, process
managers for the daemon and agents, a fake connector server and a crash-point helper.
The existing single test file grows no further.

Runtime dependencies gain gRPC and protobuf, and the cryptography dependency moves
from development to runtime. PostgreSQL support is an optional requirements file, so
the default SQLite path stays on the standard library. Development requirements pin
the code generator to the gRPC minor version. A development compose file provides
PostgreSQL. Make targets cover proto generation, the drift check, the PostgreSQL test
run and the daemon.

Continuous integration installs runtime and development requirements, runs the drift
check, runs a PostgreSQL job against a service container, and extends the compile step
to the daemon and the agent — which also closes an existing gap, since the private
runner and the lease broker are compiled by `make release-check` but not by CI.

Documentation moves in lockstep, per the repository's convention: the README, the
architecture and operations documents, the security policy (remote scheduling moves out
of what is not yet included), and the protocol document gains the v2 surfaces.

## 9. Estimate

Estimated per category and divided by an expected acceleration factor, with
security-sensitive and novel-integration work held at the cautious end of its range
and irreducible empirical time counted at no acceleration at all.

| Category | Conventional days | Acceleration | Focused days |
|---|---|---|---|
| Architecture exploration and synthesis — store interface, state machine, protocol, mission bridge | 20 | 15× | 1.3 |
| Boilerplate and routine implementation — table definitions, backends, wiring, scaffolding | 40 | 25× | 1.6 |
| Schema, tests and documentation | 30 | 30× | 1.0 |
| Security-sensitive — mutual TLS and the certificate authority, Ed25519, fencing, secret references, sandbox extraction | 25 | 6× | 4.2 |
| Novel integration debugging — bidirectional stream reconnect, SQLite and PostgreSQL portability, crash tests, harness churn | 20 | 4× | 5.0 |
| Debugging known patterns — backoff and outbox | 5 | 10× | 0.5 |
| Irreducible empirical time — kill-test wall clock, image builds, CI, compose | — | 1× | 2.5 |
| **Focused total** | **140** | | **16.1** |
| **With 25 percent contingency, declared, on the focused total** | | | **≈20** |

Re-defend this at every milestone boundary and print the delta. Contingency applies to
the focused total and is declared, never silently absorbed.
