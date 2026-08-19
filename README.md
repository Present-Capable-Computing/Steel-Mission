# Steel Mission

Steel Mission is an Agent Delivery Plane for software teams. It gives an AI-assisted delivery system a governed path from request to evidence: understand, plan, modify, build, test, inspect, repair, PR, deploy, and close.

Steel Mission is part of the Present family of products. Present stands for capable computing. No other Present product assumptions are required to use this repository.

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
- Starts with a synthetic company, Northstar Forge, so the product is usable immediately.

## Starter Company

The `starter-company/` directory contains synthetic data:

- `canon/`: knowledge domains and domain capability definitions.
- `knowledge/`: starter product, architecture, delivery, and compliance notes.
- `Workspace Packs/_build/`: role and knowledge registries consumed by the app.

The starter company is not private data. Owners and admins can replace it with their own files, folders, repositories, users, missions, and profiles.

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

- Claude Code CLI for the `dc13.claude` profile;
- Codex CLI for the repair-agent path;
- Ollama and a local coding model for the `dc13.local` profile;
- GitHub CLI for release and PR flows.
- Docker for production-eligible private-runner isolation.

See [INSTALL.md](INSTALL.md) for the complete setup guide.

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

## Release Posture

This repository is prepared as a clean product distribution. The broader Present canon and internal development corpus are not included. The included company data is synthetic and exists only to make the first run understandable.
