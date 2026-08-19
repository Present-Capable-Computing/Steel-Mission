# Steel Mission Product Overview

Steel Mission is an Agent Delivery Plane for governed software delivery. It gives teams a working system for model-independent agent delivery, customer-controlled execution, pre-execution blocking, signed evidence, risk-based approvals, and compliance-oriented proof packs.

## Product Shape

- Work view: chat and mission progress.
- Settings: role-aware administration for users, knowledge, missions, runtime profiles, model binding, control policy, auth policy, integrations, and audit.
- Knowledge Domains: KD01-KD03 starter organizational documents.
- Domain Capabilities: DC01-DC13 starter domain capabilities.
- Delivery Coordinator: the DC13 capability that coordinates mission state, evidence, approvals, and closure.
- Guarded runner: the only production execution boundary for commands and provider actions.
- Evidence signer: external signer or customer KMS-equivalent command.

## Starter Company

Northstar Forge is synthetic demonstration data. It exists so a team can launch the product, see a complete company skeleton, run missions, inspect evidence, and then replace the starter material with its own users, knowledge, profiles, missions, and policies.

## Open-Core Boundary

Steel Mission Core is open source under Apache-2.0. Teams can download, inspect, run, modify, and evaluate the core with the included synthetic starter company.

Steel Mission Enterprise Edition is closed-source, proprietary, commercially licensed functionality gated behind a license key or equivalent entitlement check. Enterprise features include production SSO/OIDC, customer KMS custody, compliance evidence packs, SIEM export, enterprise approval routing, private-cloud templates, and managed integrations.

## Orchestration

Steel Mission does not depend on n8n. n8n is a replaceable adapter. Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and a private job runner.
