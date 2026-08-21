# Steel-Mission delivery workplan

**Status: binding.** This document governs how work lands in this repository, across
every project in it. It is not a suggestion set. Where it conflicts with habit, this
document wins; where it conflicts with a schema or a canonical contract, the
contract wins and this document is corrected.

It binds everyone who lands a commit here: employees, contractors, and agents
running under a person's account. An agent's commit is that person's commit.

**Active project: `PRJ-0001`.** `PRJ-0000` completed again on 2026-08-20 after
closing all four MS-0012 review findings. PRJ-0000-D2 therefore resumes PRJ-0001;
the interruption began and ended on the same date, so its already reset dates stay
true and do not move again. Each project's decisions live in its own record; §2
below carries PRJ-0001's, and PRJ-0000's are in `plan/PRJ-0000.json`. The rules in
§4, the definition of done in §5 and the enforcement in §6 apply to both.

| | Project | Technical plan | Milestones |
|---|---|---|---|
| Complete | [`PRJ-0000`](../plan/PRJ-0000.json): usable surface, honest admin writes | [`docs/ui-plan.md`](ui-plan.md) | [MS-0007](../plan/MS-0007.json) … [MS-0012](../plan/MS-0012.json) |
| **Active** | [`PRJ-0001`](../plan/PRJ-0001.json): durable broker and remote pull-runner | [`docs/durable-core-plan.md`](durable-core-plan.md) | [MS-0001](../plan/MS-0001.json) … [MS-0006](../plan/MS-0006.json) |

- How the plan layer maps onto GitHub: [`plan/README.md`](../plan/README.md)

---

## 1. What is being built, in one paragraph

Steel-Mission already has the domain logic: signed private-runner execution, a
distributed lease broker with a real dependency graph, mission lifecycle with
approvals and evidence, and signed idempotent ingress from GitHub, Slack and Jira.
What it does not have is a durable job service. State is one unlocked JSON document
written last-writer-wins. Execution is a synchronous subprocess call with a fixed
cap and silently truncated output. Job requests carry paths that only exist on the
machine that wrote them. There are two orchestration paths that never write to each
other. A restart marks running missions paused and never resumes them. Connector
delivery has no retry, no backoff and no dead-letter path. This project replaces
that core with a transactional store, a pull-based remote runner, transactional
connector ingress and egress, and one orchestration path.

The target pipeline:

```
signed webhook or API call
  -> durable inbox + workflow controller
  -> job queue with leases and fence tokens
  -> pull-based runner in customer infrastructure
  -> ephemeral container sandbox
  -> attested result, logs, artifacts
  -> evidence ledger + transactional connector outbox
```

## 2. Decisions that are already made: PRJ-0001

These are settled. Reopening one requires new evidence and a written decision record
that supersedes it, not a preference expressed in a review.

| Id | Decision |
|---|---|
| D1 | One storage interface. SQLite on the standard library is the default; PostgreSQL on psycopg is the high-availability option. Both pass the same parity suite. |
| D2 | gRPC bidirectional streaming is the pull-runner transport. Bodies are canonical JSON validated against the v2 schemas. Protobuf is framing only, and never becomes the schema of record. |
| D3 | Scope is a full core replacement, P1 through P4. Enterprise high availability, multi-tenancy and regional failover are later phases. The schema must not preclude them, which is why `tenant_id` exists and is unused. |
| D4 | The new root package is `steel_core/`. The originating plan asked for both a `present_core` package and a repository that names one product; those conflict, and this repository names Steel-Mission. |
| D5 | The database is the queue. The broker command line, the daemon and the chat server are direct clients of one store, sharing one command module and one state machine. There is no command-line-to-daemon remote call. |
| D6 | Two network surfaces exist and no more: the runner-facing gRPC gateway on the daemon, and an authenticated HTTP operations API. Anything else that wants to listen argues for itself first. |
| D7 | The founder console, the chat surface, codex as a coordinator provider and the four-stage mission pipeline fold into PRJ-0001 as milestones C0–C2, with the status feed persisted at P1 and the agent executor at P2. No separate project, and no durable second dispatch path: the C1 bench is disposable by declaration, and only its session status feed format, the draft of job protocol v2, survives into P1/P2. |
| D8 | The Okay happens at mission grant. A grant binds a plan, a machine-checkable definition of done, budgets and abort conditions; within those bounds the mission runs unattended through plan (Claude, Opus 5 at least), develop and commit (the local model), a bounded review loop (Codex), and final review, approval and merge (Claude, Opus 5 at least). When the plan proves unclean the mission escalates through the existing user-decision functionality and waits; it never widens its own authority, and a mission that cannot reach its definition of done stops and reports rather than redefining done. |
| D9 | One machine account per model worker, so commit authorship, review provenance and approval are real on GitHub rather than reconstructed from evidence packs. The acceptance account is a code owner for non-authority paths only; `schemas/canonical/` stays human-owned, and a mission touching it escalates instead of merging. Account creation is the maintainer's act. |

## 3. Milestone sequence: PRJ-0001

Milestones run in order. P5 is last because it collides with the file paths of every
other phase.

| Milestone | Phase | Outcome in one line | Budget (focused days) | Target |
|---|---|---|---|---|
| [MS-0001](../plan/MS-0001.json) | P0 | Dependency and runtime jumps resolved; CI compiles every entrypoint; test harness ready | 1.0 | 2026-08-27 |
| [MS-0013](../plan/MS-0013.json) | C0 | The Founder lands as Andrew Hermann, owner, seeded install-side, and sees true capability, server, authority and provider state | 3.8 | 2026-09-03 |
| [MS-0014](../plan/MS-0014.json) | C1 | Chat on the landing screen; granted missions run the four-stage pipeline on a disposable bench; progress is visible | 4.3 | 2026-09-17 |
| [MS-0015](../plan/MS-0015.json) | C2 | Codex is a coordinator provider and the registry's model choices govern the actual calls | 4.0 | 2026-09-24 |
| [MS-0002](../plan/MS-0002.json) | P1 | Durable transactional broker with fencing, sweeping, a daemon, and the session status feed persisted | 7.0 | 2026-10-15 |
| [MS-0003](../plan/MS-0003.json) | P2 | Remote pull-runner over mutual TLS returning signed, bound results; coding agents run as runner jobs and the bench retires | 10.8 | 2026-11-12 |
| [MS-0004](../plan/MS-0004.json) | P3 | Transactional connector inbox and outbox with backoff and a dead-letter queue | 3.0 | 2026-11-26 |
| [MS-0005](../plan/MS-0005.json) | P4 | One orchestration path; missions resume after restart | 3.0 | 2026-12-10 |
| [MS-0006](../plan/MS-0006.json) | P5 | The repository names one product | 2.0 | 2026-12-23 |

Total estimated effort is 33.3 days before contingency and approximately 41.6 days
with the declared 25 percent. It is estimated per category and divided by an
expected acceleration factor, with security-sensitive and novel integration work
held at the cautious end of its range and irreducible empirical time (kill tests,
image builds, live pipeline rehearsals, continuous integration waits) counted at
no acceleration at all. It is not a commitment, and a milestone being inside its
budget is not evidence that the work is correct. Re-defend the estimate at every
milestone boundary and print the delta rather than absorbing it.

Re-defended at the D7 rescope on 2026-08-20: the console, chat, provider and
pipeline work folds in, the status feed persists at P1 and the agent executor
lands at P2. Conventional effort grows 140 to 228 person-days and the focused
estimate 16.1 to 33.3 days, a printed delta of **+17.2 focused days**, with the
per-category derivation on the project record. Target dates from MS-0014 onward
assume pipeline-assisted throughput; that assumption is a hypothesis, and it is
re-examined at the MS-0014 boundary with the delta printed.

Target dates on the milestone records are targets. `AT_RISK` describes the schedule.
It never describes the work.

## 4. The rules that bind every change

### 4.1 Green at every step

The full suite passes before you move to the next step, not before you open the pull
request. A step that leaves the suite red is not a step; it is a branch you have not
finished. `make test` is the check. Paste the count into the pull request.

Steel-Mission currently has 227 tests in one file that drives the executables as
subprocesses and loads the chat server by path with module-scope monkeypatching.
That harness is fragile in a specific way: **a renamed module-level function does not
fail loudly, it silently stops being patched.** The rule is to wrap, never rename.
This matters most in P4 and it is the single most likely source of a green suite
that is testing nothing.

### 4.2 One reason per pull request

A pull request does one thing. A refactor that also fixes a bug produces a
regression nobody can attribute. The two riskiest changes in this project (the
container hardening extraction in P2 and the naming migration in P5) ship as pure
refactors with characterization tests and no behaviour change whatsoever.

### 4.3 The pull request describes the branch

Run `git log --oneline main..HEAD` and `git diff --stat main..HEAD` before you write
the description, and describe all of it. A reviewer decides how carefully to read
from what you wrote; a title naming one part of a larger change gets the rest merged
unread. The template asks for this because it is the most common way a real change
enters this repository unreviewed.

### 4.4 Evidence, not intention

"Should work" is not a result. Name the check and paste what it said. A test you did
not watch fail without your fix is a test you have not verified tests anything.

### 4.5 Surfaces that need a second look

The pull request template lists them: authentication and authorization, anything
that binds a network port, subprocess or container execution, and the authority-owned
schemas under `schemas/canonical/`. Ticking one of those boxes means the change gets
a security review before it merges. A schema change may additionally need
ratification separately from the merge that carries it.

### 4.6 Reversibility is stated before the merge, not discovered after

Every pull request says how the change is undone. If it cannot be undone cleanly
a migration that drops a column, a schema identifier that has already been published
then say that in the pull request. That is a decision to take deliberately, and it is
much cheaper to take it before the merge.

### 4.7 Schemas move first

This repository is schema-first. A new message, record or contract gets its schema
and its registry entry before the code that produces it, not after. The v2 job
specification and result carry the JSON that a protobuf message frames; the schema
is what validates, and the proto is what transports.

### 4.8 Secrets are references

A job specification carries `secretRefs` (a name and where to look), never a value.
Resolution happens just in time in the runner, into the environment of the sandboxed
process. There is a test that greps the process arguments, the store dump, the logs
and the artifacts for a sentinel. If you add a path that could carry a secret, add it
to that test.

### 4.9 Never hold a transaction across the world

The exclusive state transaction serializes writers. It must never enclose a worker
invocation, a network call, or a heartbeat renewal: that converts a correctness fix
into a throughput collapse. Heartbeats write to the leases table directly.

### 4.10 A migration keeps the old path green

The legacy JSON state document is mirrored after every mutation for as long as tests
assert on it. Retiring the mirror is its own decision, taken once, deliberately,
not a side effect of a convenient refactor.

### 4.11 The front-end dependency budget is three

The browser build has exactly three direct front-end packages: esbuild, Preact and
TypeScript. They stay pinned to an exact version in `package.json` and the committed
lockfile. Adding or replacing a direct package requires a written decision that
re-defends the budget; it is not smuggled into a version bump. Version bumps arrive
as their own reviewable pull requests.

Continuous integration installs only from the lockfile, runs
`npm audit --audit-level=high`, type-checks and tests the typed source, then proves
the committed self-contained page is byte-identical to a clean rebuild. Node remains
a build dependency only and does not enter the runtime image.

## 5. Definition of done

A task is done when all of the following are true. Any one of them missing means it
is in progress, whatever the board says.

1. The requirement was written by a person as an outcome, before the work started.
2. The full suite passes, and the new tests were watched to fail without the change.
3. Acceptance evidence named in the issue exists and is linked.
4. Documentation and schemas moved with the contract, in the same pull request.
5. The pull request describes every commit in the branch and states how it is undone.
6. Code owner review passed on every touched owned path.
7. Continuous integration is green on the full interpreter matrix.

A milestone is done when every task in it is closed or explicitly dropped with a
written reason, and the completion evidence named on the milestone record exists.
`COMPLETE` means the work is closed. It does not mean anything passed verification.

**Work executed inside a granted mission** (D8, D9) meets the same seven conditions
with two substitutions, both bounded by the grant. The requirement in item 1 is
fixed at grant time: the planner may draft it, but the grant is a person's act and
the mission cannot amend its own requirement. The review in item 6 is performed by
the acceptance role (Claude, Opus 5 at least) through its machine account for
non-authority paths; `schemas/canonical/` and this document's binding sections stay
human-owned, and a mission touching them escalates through the existing
user-decision functionality and waits. The definition of done in a grant is
machine-checkable (acceptance criteria as tests the CI scaffolding runs, not
prose), and a mission that cannot reach it stops and reports rather than
redefining done.

## 6. How this is enforced

Prose does not bind anyone. These do:

| Mechanism | What it enforces |
|---|---|
| Branch protection on `main` | No direct pushes; pull request required |
| Required status checks | The suite must be green on every interpreter in the matrix before merge |
| Required code owner review | `CODEOWNERS` marks the trust boundaries and the schema surface; an owner reviews before those merge |
| Required conversation resolution | A raised concern is answered, not scrolled past |
| Pull request template | Branch-wide description, evidence, surfaces touched, reversibility |
| Issue templates | A task cannot enter with an empty requirement, matching the control plane's own refusal |
| Dependabot | Action, pip and container bumps arrive as reviewable pull requests, because a workflow runs with repository credentials |
| `make release-check` | Whitespace, compilation of every entrypoint, style, full suite: the same gate locally and in CI |
| Machine accounts, one per model worker | Author and approver are different GitHub identities on mission pull requests, so review provenance is real; the acceptance account owns non-authority paths only, and `schemas/canonical/` stays human-owned |

If you find a way to land a change that skips one of these, that is a defect in the
setup. Report it rather than using it.

One caveat, stated because a rule everyone quietly bypasses is worse than no rule.
While there is a single code owner, that person cannot approve their own pull
request, so their own changes can only land through the repository admin bypass,
which is deliberately left available, or the repository would be unmergeable. The
review requirement therefore binds contributors immediately and the sole maintainer
only once there is a second reviewer. Every other gate in the table above applies to
everyone, including the maintainer: the status checks, the conversation resolution
and the templates do not have a bypass in normal use.

## 7. Working agreement

- **Branches** are named `<milestone>/<short-slug>`, for example `ms-0002/store-interface`.
- **One issue per branch.** The pull request closes it by number.
- **Draft early.** A draft pull request opened on the first commit is how the rest of
  us see what is being worked on without asking.
- **A finished pull request is queued, not watched.** The moment the work is
  finished (never before, and never while it is still a draft) arm auto-merge
  (`gh pr merge <n> --auto --merge`); it lands by itself once the checks and any
  required review pass, which means it lands without you looking at it again.
  Queuing is never a way of not waiting for a review you expect to be told
  something in. Having queued, report completion without
  waiting for the landing, and report the true state: queued with checks pending
  or green, never "merged" before the merge exists. The issue closes on the merge
  itself, by number. The head branch is deleted on merge; the commits are on
  `main`, so nothing is lost.
- **Ask in the issue, not in a private message.** A decision taken where nobody can
  find it later gets retaken.
- **Blocked is a status, not a failure.** Say so in the issue the day it happens.
  Silent blockage is the most expensive state in the project.
- **Dropped work is recorded.** A task closed without doing it gets one written line
  saying why. Milestone completion depends on being able to read that line.
- **Missions claim by assignment.** A granted mission assigns its issue and comments
  its session id before the first commit, so two workers never hold one issue and
  the board shows who has what. Mission branches follow the same
  `<milestone>/<short-slug>` convention as everyone else's.
- **Reachability before work.** Before the first commit, a session verifies every
  acceptance precondition that is someone else's act: accounts, credentials,
  infrastructure, another issue's outcome. One unmet precondition means the
  session posts the blocker on the issue and ends. Finding a blocker and
  continuing to build is the failure mode this rule exists to prevent; the
  blocker comment is the deliverable, not a footnote to five more hours of work.
- **Session ceilings end the session.** A session carries a commit ceiling and a
  wall-clock ceiling proportionate to the issue's budget, enforced by the
  harness rather than by the agent's judgment. Hitting a ceiling stops the work
  and posts the state: what is done, what is not, what changed the estimate.
  Twenty commits on a task budgeted in hours is not persistence, it is the
  signal that the plan was wrong.
- **An issue names its paths.** Each agent-workable issue carries `allowedPaths`
  in the manifest, and the session checks every commit's files against it. An
  edit outside the wall is an escalation before the commit, never a discovery in
  review. Trust boundaries (`steel-mission-chat/`, `bin/`, `adapters/`,
  `schemas/canonical/`) enter a session's wall only when the issue says so.
- **Reviews converge or escalate.** The review loop is bounded, and each round
  may only shrink or correct what exists. A review that demands new
  architecture, a new mechanism, or a new dependency has found a design
  question, and design questions belong to the Person: the loop ends and the
  session escalates. Where thinness is load-bearing, the acceptance states a
  line ceiling, and crossing it is evidence the design is wrong, not a reason
  to keep going.
- **Acceptance runs where the code runs.** Every acceptance criterion is
  executable on the host the deliverable targets, and platform-specific
  mechanisms name their platform in the issue. A protection that no test on the
  target host can exercise is decoration, and a skipped test on shipped code is
  a red flag, not a pass.

## 8. Acceptance criteria for the project

These seven tests are the project. They are written as automated tests with the
failures injected, not as a checklist someone confirms by hand.

| Criterion | Milestone | How the failure is injected |
|---|---|---|
| Coordinator dies mid-job; work resumes with no duplicate side effect | P1, again for missions in P4 | SIGKILL at a crash point; side-effect counter file reads exactly 1; exactly one recovery ledger entry |
| Runner dies; the lease is reclaimed and the stale result is refused | P2 | SIGKILL agent A with a short lease; agent B gets fence + 1; agent A's replayed result is rejected |
| A duplicate webhook produces one mission | P3 | Five concurrent signed posts; unique deduplication key; one mission directory |
| Connector retries produce exactly one reply | P3 | Fake endpoint answers 429 with a hint twice then 200; crash variant kills after the post |
| No secret appears in arguments, logs, store or artifacts | P2 | Container shim captures arguments; sentinel grepped across the store dump, logs and artifacts |
| A result is bound to job, runner, image and inputs | P2 | Tampered image digest, then tampered input hashes, then an unenrolled key; each rejected |
| Backup and restore preserve integrity | P4 | Archive round-tripped mid-approval; hash chain verifies; approval completes; no duplicate delivery |

Crash points are environment-gated through a test hook module and are inert in
production. Whole-process death is a SIGKILL on a sentinel file, driven from a
subprocess, matching how this suite already tests.

## 9. Risks that are live now

1. **The container hardening extraction.** Several hundred lines of isolation logic
   move from an executable into an importable module. A regression there weakens a
   security boundary without failing a test. Mitigated by an old-versus-new argument
   characterization test, and by shipping it alone.
2. **Serialization throughput.** See §4.9. The failure mode is a system that is
   correct and unusably slow, which is harder to notice in a test than a wrong answer.
3. **Two drivers on one workflow.** The synchronous command-line drive and the daemon
   can both decide to advance the same workflow. Prevented by a driver-owner column
   and a controller leader lease, and tested by running both at once on purpose.
4. **The load-by-path test harness.** See §4.1. Concentrated in P4.
5. **Exactly-once is connector-specific.** GitHub supports a queryable marker. Where a
   connector does not, the behaviour degrades to a retry with a visible duplicate
   annotation, and the operator documentation says so. Do not describe the result as
   exactly-once delivery in prose that a customer reads.
6. **The runtime jumps in P0.** The container base image and the continuous
   integration interpreter matrix are different runtimes that fail independently.
   A green matrix says nothing about the container.
7. **The local coder is the weakest worker.** A 14-billion-parameter local model
   drafting changes produces review burden faster than it removes work if it is
   handed anything large. Mitigated by assignment discipline (smallest mechanical
   issues first) and by the Codex review loop standing between its commits and
   the acceptance review.
8. **Pipeline authority creep.** An unattended mission that quietly widens its own
   scope is the failure mode D8 exists to prevent. Mitigated by grant-time budgets
   and abort conditions, escalation through the existing decision functionality,
   the bench refusing security-review-labelled issues, and `schemas/canonical/`
   staying human-owned.

## 10. Changing this document

This workplan is versioned with the repository and changes by pull request like
anything else. A change to §2, §4, §5 or §6 needs code owner review, because those
sections are what other people are relying on. Add the reason to the pull request:
a rule whose reason is not written down is a rule that gets dropped the first time it
is inconvenient.
