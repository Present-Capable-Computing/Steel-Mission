# Steel Mission

Steel Mission is an Agent Delivery Plane for software teams. It gives an AI-assisted delivery system a governed path from request to evidence: understand, plan, modify, build, test, inspect, repair, PR, deploy, and close.

## Editions

Steel Mission uses an open-core release model.

- Core: open source under Apache-2.0 for teams to download, inspect, run, modify, and deploy. Core includes self-managed SSO/OIDC, audit logging, SIEM/security-monitoring export, and the included synthetic starter company.
- Enterprise Edition: proprietary software and services for operational scale, including multi-organization fleet governance, managed deployment and upgrades, managed evidence retention, managed KMS/HSM operations, advanced separation-of-duties workflows, private-cloud operations, managed integrations, and support.

Copyright is held by Andrew Hermann, Switzerland. Contributions are welcome under the published contribution policy. Core trust controls are not license-gated; separately distributed Enterprise functionality may use a commercial entitlement.

See [LICENSE.md](LICENSE.md) for the plain-language licensing boundary and [LICENSE](LICENSE) for the Apache-2.0 core license text.

## What It Does

- Keeps model choice separate from organizational role. Delivery Coordinator can run on different model providers.
- Lets owners and admins configure users, capabilities, knowledge, runtime profiles, control policy, auth policy, and integrations.
- Forces executable agent and command-adapter actions through an authenticated private runner; the included production path uses an ephemeral hardened container.
- Blocks unsafe commands before execution.
- Requires external evidence signing when production policy is enabled.
- Creates signed, hash-chained mission evidence.
- Checks organizational knowledge for availability, freshness, ownership, provenance, expiration, conflicts, and context sufficiency before relying on it.
- Preserves provider-native capability requirements inside a common policy and evidence envelope.
- Returns work to existing SCM, issue, chat, CI, and provider workflows instead of requiring the control UI as the daily work surface.
- Maps proof packs to SOC 2, ISO 27001, and ISO 42001 evidence.
- Starts with Steel Mission's founder-led company, complete capability map, and active Durable Core project so the product is useful immediately.

## Starter Company

The `starter-company/` directory contains Steel Mission's launch data:

- `canon/`: knowledge domains and domain capability definitions.
- `knowledge/`: product, architecture, delivery, compliance, portfolio, and Durable Core notes.
- `portfolio.json`: the expandable project registry, beginning with PRJ-0001.
- `steel-mission-first-start-knowledge-v1.json`: the schema-validated, read-only knowledge import manifest for container first start.
- `Workspace Packs/_build/`: role and knowledge registries consumed by the app.

The starter company is not private data, and it is not your data. It ships with the
product so a fresh clone is usable immediately.

To run on your own organisation, point `STEEL_MISSION_ORG_DIR` at a directory
outside this tree:

```sh
STEEL_MISSION_ORG_DIR=/srv/acme-org bin/steel-mission serve
```

Do not run on your own organisation by writing your files into `starter-company/`.
That directory is distributed to every user: replacing its contents removes the
demo company they start from, and commits your roster, clients and decisions to a
repository published under an open-source licence. A test enforces this
(`tests/test_org_data_boundary.py`).

## Organization Skeleton

- Knowledge domains are organizational documents.
- Domain Capabilities are assignable organizational roles and workflows.
- Delivery Coordinator is responsible for mission state, evidence, approvals, and closure.

Delivery Coordinator is a role/capability. Binding it to Claude, OpenAI, Glimmer, or another model creates a model instance; it does not create a new role.

Snapshots are reproducibility artifacts, not an assertion that their contents are correct. Each mission records a knowledge-quality report and warns the model and operator when required sources are missing or expired, when ownership or freshness is unknown, or when authoritative sources conflict. Insufficient context must be disclosed rather than filled with a confident guess.

## Running Locally

```bash
python3 -m pip install -r requirements-dev.txt
bin/steel-mission doctor
bin/steel-mission serve
```

On first start, Steel Mission seeds its writable configuration outside the
checkout at `${XDG_STATE_HOME:-$HOME/.local/state}/steel-mission/config`. Later
starts reuse that copy, and new shipped configuration files are added without
overwriting files you have changed. Set `STEEL_MISSION_STATE_DIR` to choose a
different state root for this runtime configuration. An installation that
supplies its own configuration can instead set `STEEL_MISSION_CONFIG_DIR`; that
explicit directory is used as-is.

Removing the runtime `config` directory resets the starter configuration from
the product on the next start. The files under this repository's `config/`
remain immutable shipped defaults.

Or use the Python entrypoint directly:

```bash
python3 steel-mission-chat/server.py --host 127.0.0.1 --port 8765 --profile dc13.local
```

Open:

```text
http://127.0.0.1:8765/
```

## Requirements

Minimum core:

- macOS or Linux;
- Python 3.11 or newer;
- Git;
- a modern browser.

Optional provider tools:

- Claude Code CLI for planning and acceptance assessment;
- Codex CLI for the read-only reviewer path;
- Ollama and `qwen2.5-coder:14b` for the local coding-model path;
- GitHub CLI for release and PR flows.
- Docker for production-eligible private-runner isolation.

See [INSTALL.md](INSTALL.md) for the complete setup guide.

## Starter Company Docker Launch

The `starter-company/` directory contains synthetic data for Northstar Forge, and the application image ships it. PRJ-0001
Durable Core, its six epics, and all 58 issues. The launch contract connects
to host Ollama through Docker Desktop and mounts the existing Claude and Codex
CLI credentials read-only; credentials are never copied into the image.

```bash
docker compose build steel-mission
docker compose up -d steel-mission
docker compose ps
```

Open `http://127.0.0.1:8765/`. The governed delivery bindings are:

- planner: Claude Code;
- coder: local `qwen2.5-coder:14b` through Ollama;
- reviewer: Codex CLI in read-only mode;
- acceptance assessment: Claude Code;
- final PASS authority: deterministic verification.

The launch expects `~/.claude/steel-mission-worker-token` from `claude setup-token`,
`~/.codex/auth.json` from `codex login`, and a host Ollama service on port
`11434` with `qwen2.5-coder:14b` installed.

## Quickstarts

- [Local starter](docs/quickstart-local.md)
- [Claude Code](docs/quickstart-claude-code.md)
- [Codex](docs/quickstart-codex.md)

## Guarded Runner

Executable actions should use:

```bash
bin/present-control-plane session --actor admin@example.test --role admin
bin/present-control-plane exec --token "$TOKEN" --json '{"phase":"inspect","repositoryPath":".","command":"python3 -m py_compile steel-mission-chat/server.py"}'
```

Direct command execution paths are blocked by default when the control policy requires the guarded runner.

The guarded control plane signs a bounded request to `bin/present-private-runner` and verifies the runner's signed result. For a production-eligible boundary, build the included image and select container mode:

```bash
make private-runner-image
PRESENT_PRIVATE_RUNNER_MODE=docker bin/present-private-runner status
```

Set `executionBoundary.privateRunnerMode` to `container` in `config/control-plane-policy.json`. The container runs as a non-root host-mapped user with a read-only root filesystem, dropped capabilities, no-new-privileges, bounded CPU/memory/processes, a tmpfs scratch directory, and only the mission workspace mounted. Network access defaults to `none`; use a reviewed Docker network when a PR/provider phase needs outbound access. `development-local` mode is explicitly non-production and exists for local evaluation and tests.

The alpha image contains Python, the release-test dependencies, Git, and GitHub CLI. Derive a customer image and set `PRESENT_PRIVATE_RUNNER_IMAGE` when a repository needs another toolchain. HTTPS GitHub pushes can use the allowlisted `GITHUB_TOKEN`/`GH_TOKEN`; token values remain in the process environment rather than Docker argv or image files.

## Identity Boundary

Local evaluation uses `identityBoundary.mode: development-local`. In that mode only loopback requests may use the role/actor development headers, and locally issued CLI sessions remain available.

Before exposing the service, set `identityBoundary.mode` to `oidc-required` in `config/auth-policy.json` and configure the OIDC issuer, audience, JWKS source, authorization endpoint, token endpoint, client ID, redirect URI, and scopes. Set the client secret only through the environment named by `oidc.clientSecretEnv` (default `PRESENT_OIDC_CLIENT_SECRET`). The browser uses Authorization Code with PKCE, state, and nonce, then receives a short-lived HttpOnly session cookie with CSRF protection. CLI callers exchange an ID token with:

```bash
bin/present-control-plane session --oidc-token "$OIDC_ID_TOKEN"
```

OIDC claims prove identity but never grant a Steel Mission role. `config/users.json` maps the verified issuer/subject or email to a server-owned role, capabilities, and `organizationIds`; disabled or unknown identities fail closed. The same registry can map `externalIdentities.github`, `.slack`, and `.jira` identifiers (or a connector `serviceUserId`) so signed workflow ingress preserves the registered actor. Mission reads and actions are scoped to organization membership and actor assignment, and `authorization.preventSelfApproval` enforces separation of duties.

Auth sessions use a signing key independent from evidence and private-runner keys, have bounded `iat`/`nbf`/`exp` claims, and can be revoked through `POST /api/auth/logout`. Authentication events and revocations are written to dedicated audit ledgers under the mission root.

## Native Workflow Adapters

GitHub, Slack, and Jira can start an investigation from signed webhook or command events and receive mission status, approval requests, control decisions, evidence links, and completion in the original issue or thread. Configure the corresponding token and signing-secret variables from `.env.example`, then point the provider at:

- GitHub: `/api/integrations/github/webhook`;
- Slack: `/api/integrations/slack/events`;
- Jira or a Jira webhook-signing gateway: `/api/integrations/jira/webhook`.

GitHub accepts an explicit `/steel-mission …` comment or a `steel-mission` label. Slack accepts the slash command or an app mention. Jira accepts the explicit command or label. Duplicate deliveries are idempotent. Jira ingress requires the gateway/provider to attach `X-Steel-Mission-Signature: sha256=<HMAC>` because Jira Cloud webhooks do not provide the same shared-secret signature contract as GitHub and Slack.

## Evidence Signing

Core uses local HMAC signing by default and can require a customer-controlled KMS, Vault Transit service, HSM, or private signing service for evidence custody.

The external signing adapter can use:

```bash
bin/present-evidence-signer --key-file ~/.present/control-plane/evidence-signing-key --signer-id present-external-signer sign
```

The signing key is created outside the repository. A customer KMS, Vault Transit service, HSM, or private signing service can replace this command without changing the evidence contract.

## Open-Core Boundary

Core includes the controls teams need to admit Steel Mission into an enterprise pilot:

- self-managed SSO/OIDC through OIDC issuer and JWKS configuration;
- audit logging and SIEM/security-monitoring JSONL export;
- self-managed native GitHub/Slack/Jira, command, webhook, and outbox connectors;
- customer-controlled external evidence signing.

Enterprise monetizes operational scale rather than access to baseline security: managed deployment and upgrades, multi-organization fleet governance, managed retention and integrations, advanced governance workflows, private-cloud operations, and support. Core trust controls do not require a license key.

## n8n And Orchestration

Steel Mission does not depend on n8n. n8n should be treated as a replaceable orchestration adapter that can request work, receive events, or coordinate external workflows. It is not the source of truth for policy, approval, evidence, or execution authority.

Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and remote private-runner scheduling. The local/container private-runner contract and native GitHub, Slack, and Jira workflow paths are included now.

The intended interaction model is existing-tools-first: requests originate in repositories, issue trackers, chat, CI, IDEs, or provider-native tools, and Steel Mission returns status, approval requests, control decisions, and evidence links to the originating workflow. The built-in UI is primarily for configuration, investigation, and fallback.

## Protocol Status

Steel Mission includes the Steel Mission Control Plane Protocol, experimental v0.1. It is a protocol candidate embedded in the product, with versioned schemas for runtime profiles, snapshot policy, mission control, audit, evidence, and guarded execution.

Do not describe it as a stable public standard yet. See [docs/steel-mission-control-plane-protocol.md](docs/steel-mission-control-plane-protocol.md).

## Release And Security

- [Release notes](RELEASE_NOTES.md)
- [Security policy](SECURITY.md)
- [Architecture](architecture.md)
- [Operations](operations.md)

## Working On Steel Mission

Delivery work is organised as one project with milestones and tasks, not as loose
branches. Read the workplan before your first pull request: it is binding, and it
states what "done" means here.

- [Workplan](docs/workplan.md) — binding rules, milestone sequence, definition of done, how it is enforced
- [Durable core plan](docs/durable-core-plan.md) — the technical design for the current project
- [Plan records](plan/) — the project and milestone records, and how they map onto GitHub
- [Contributing](CONTRIBUTING.md) — set up, test and land a change
- [Milestones](https://github.com/Present-Capable-Computing/Steel-Mission/milestones) · [Issues](https://github.com/Present-Capable-Computing/Steel-Mission/issues)

## Release Posture

This repository is prepared as a clean Steel Mission product distribution. The included company data defines Steel Mission's own launch organization and contains no external company corpus.
