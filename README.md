# Closed-Claw

Closed-Claw is an Agentic Delivery Plain for software teams. It gives an AI-assisted delivery system a governed path from request to evidence: understand, plan, modify, build, test, inspect, repair, PR, deploy, and close.

Closed-Claw is part of the Present family of products. Present stands for capable computing. No other Present product assumptions are required to use this repository.

## Editions

Closed-Claw uses an open-core release model.

- Community Edition: source-available for teams to download, inspect, run, modify, and evaluate with the included synthetic starter company.
- Enterprise Edition: commercial features for production governance, including SSO/OIDC, KMS or external evidence signing, compliance evidence packs, SIEM export, enterprise approval routing, private-cloud deployment templates, and managed integrations.

Copyright is held by Andrew Hermann, Switzerland. Contributions are welcome under the published contribution policy. Commercial use of Enterprise Edition features requires a commercial license.

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
python3 closed-claw-chat/server.py --host 127.0.0.1 --port 8765 --profile dc13.local
```

Open:

```text
http://127.0.0.1:8765/
```

## Guarded Runner

Executable actions should use:

```bash
bin/present-control-plane session --actor admin@example.test --role admin
bin/present-control-plane exec --token "$TOKEN" --json '{"phase":"inspect","repositoryPath":".","command":"python3 -m py_compile closed-claw-chat/server.py"}'
```

Direct command execution paths are blocked by default when the control policy requires the guarded runner.

## External Signing

The default release configuration uses:

```bash
bin/present-evidence-signer --key-file ~/.present/control-plane/evidence-signing-key --signer-id present-external-signer sign
```

The signing key is created outside the repository. A customer KMS, Vault Transit service, or private signing service can replace this command without changing the evidence contract.

## n8n And Orchestration

Closed-Claw does not depend on n8n. n8n should be treated as a replaceable orchestration adapter that can request work, receive events, or coordinate external workflows. It is not the source of truth for policy, approval, evidence, or execution authority.

Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and a private job runner.

## Release Posture

This repository is prepared as a clean product distribution. The broader Present canon and internal development corpus are not included. The included company data is synthetic and exists only to make the first run understandable.
