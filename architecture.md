# Steel Mission Architecture

Steel Mission is an Agent Delivery Plane. It keeps policy, approval, evidence, and execution authority inside the customer-controlled runtime boundary.

## Core Components

- Work view: simple user-facing chat and mission surface.
- Settings overlay: role-aware configuration for owners, admins, publishers, and users.
- Runtime profiles: model/provider binding without changing organizational roles.
- Snapshot policy: per-job source boundary for repositories, folders, documents, tasks, logs, and mission evidence.
- Knowledge manager: durable organizational sources plus task-local chat uploads.
- Knowledge quality: automated availability, freshness, ownership, provenance, expiration, conflict, and sufficiency assessment attached to missions.
- Mission control: long-running missions with pause, resume, approval, evidence, and closure.
- Guarded runner: CLI/API boundary for executable actions.
- Control policy: pre-execution blocking, approval requirements, compliance mappings, and direct-command enforcement.
- Auth policy: signed sessions, OIDC/JWKS support, and external signing policy.
- Evidence signer: external process boundary for signed, hash-chained mission records.
- Integration registry: model providers, SCM, issue tracking, chat, CI/CD, and SIEM outputs.

## Model Independence

Delivery Coordinator is the role. Claude, OpenAI, Glimmer, local models, and future providers are bindings for an instance of that role. Provider choice does not create a new role or authority boundary.

The abstraction boundary covers role binding, bounded context, policy, execution authority, and evidence. It does not flatten provider execution into a lowest-common-denominator feature set. Models advertise native capabilities, runtime profiles can require them, and resolution fails when no candidate satisfies the requirement. Provider adapters remain free to use native context, tools, reasoning controls, streaming, and execution features inside the governance envelope.

## Knowledge Quality Boundary

Snapshots provide reproducibility but are not automatically trustworthy. Source entries can declare an owner, review time, expiration, maximum age, authority, and provenance. Before a mission relies on organizational context, Steel Mission checks missing and stale required sources, unowned or freshness-unknown sources, and conflicting logical sources. The report is stored with the mission and injected into model context; insufficient context must be surfaced to an owner rather than inferred.

## Customer Boundary

Steel Mission is designed to run inside customer infrastructure or a private cloud environment. Policies, auth configuration, knowledge, evidence, and connector configuration are customer-owned.

## Execution Boundary

Executable agent actions must enter through the guarded runner:

- `bin/present-control-plane`
- `/api/control-plane/execute`

The runner verifies a signed session, evaluates pre-execution policy, blocks unsafe commands, checks approval requirements, executes only allowed actions, and records signed evidence.

Direct command execution is blocked by policy for delivery missions unless the action is explicitly inside the guarded control-plane boundary.

## Evidence Boundary

Mission evidence is:

- content-hashed;
- signed by an external signer or customer KMS-equivalent command;
- linked into an integrity chain;
- retained with audit records and proof packs.

If production policy requires external signing and the signer is unavailable, evidence writing fails closed.

## Orchestration Adapters

Steel Mission does not depend on n8n. Orchestration systems may request work or receive events, but they are not the source of truth for policy, approval, evidence, or execution authority.

Upcoming adapters include Temporal, GitHub Actions, GitLab, and a private job runner.

## Workflow Embedding

The primary experience belongs in the tools where work already starts: SCM, issues, chat, CI, IDEs, and provider-native environments. Connector contracts preserve originating identity and thread context, then return status, approval requests, control decisions, completion, evidence links, and an optional investigation deep link. The Steel Mission control surface is for administration, cross-workflow investigation, and fallback—not a mandatory replacement workspace.
