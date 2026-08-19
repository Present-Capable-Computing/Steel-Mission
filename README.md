# Steel Mission

Steel Mission is an Agent Delivery Plane for software teams. It gives an AI-assisted delivery system a governed path from request to evidence: understand, plan, modify, build, test, inspect, repair, PR, deploy, and close.

Steel Mission is part of the Present family of products. Present stands for capable computing. No other Present product assumptions are required to use this repository.

## Editions

Steel Mission uses an open-core release model.

- Core: open source under Apache-2.0 for teams to download, inspect, run, modify, and evaluate with the included synthetic starter company.
- Enterprise Edition: closed-source, proprietary, commercially licensed features and services for production governance, including SSO/OIDC, KMS or external evidence signing, managed evidence retention, SIEM export, enterprise approval routing, private-cloud deployment templates, and managed integrations.

Copyright is held by Andrew Hermann, Switzerland. Contributions are welcome under the published contribution policy. Enterprise Edition functionality is gated behind a commercial license key or equivalent entitlement check in the official distribution.

See [LICENSE.md](LICENSE.md) for the plain-language licensing boundary and [LICENSE](LICENSE) for the Apache-2.0 core license text.

## What It Does

- Keeps model choice separate from organizational role. Delivery Coordinator can run on different model providers.
- Lets owners and admins configure users, capabilities, knowledge, runtime profiles, control policy, auth policy, and integrations.
- Forces executable agent actions through the guarded runner.
- Blocks unsafe commands before execution.
- Requires external evidence signing when production policy is enabled.
- Creates signed, hash-chained mission evidence.
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

## External Signing

Core uses local HMAC signing for downloadable evaluation. Customer-held signing custody is Enterprise-only and is locked unless a valid Enterprise entitlement is active.

The Enterprise signing adapter can use:

```bash
bin/present-evidence-signer --key-file ~/.present/control-plane/evidence-signing-key --signer-id present-external-signer sign
```

The signing key is created outside the repository. A customer KMS, Vault Transit service, HSM, or private signing service can replace this command without changing the evidence contract.

## Enterprise Entitlement

The official runtime keeps the following features locked in Core:

- OIDC/JWKS customer identity configuration;
- customer KMS, Vault Transit, HSM, or equivalent external evidence signing;
- SIEM/security-monitoring connectors and exports.

For licensed Enterprise environments, configure:

```bash
STEEL_MISSION_EDITION=enterprise
STEEL_MISSION_LICENSE_KEY=...
STEEL_MISSION_LICENSE_KEY_SHA256=...
```

The hash is the SHA-256 digest of the configured license key. The key value is never returned by the API.

## n8n And Orchestration

Steel Mission does not depend on n8n. n8n should be treated as a replaceable orchestration adapter that can request work, receive events, or coordinate external workflows. It is not the source of truth for policy, approval, evidence, or execution authority.

Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and a private job runner.

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
