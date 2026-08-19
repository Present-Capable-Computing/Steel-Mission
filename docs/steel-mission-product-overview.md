# Steel Mission Product Overview

Steel Mission is an Agent Delivery Plane for governed software delivery. It gives teams a working system for model-independent agent delivery, customer-controlled execution, pre-execution blocking, signed evidence, risk-based approvals, and compliance-oriented proof packs.

## Product Shape

- Work view: chat and mission progress.
- Settings: role-aware administration for users, knowledge, missions, runtime profiles, model binding, control policy, auth policy, integrations, and audit.
- Knowledge Domains: KD01-KD03 starter organizational documents.
- Domain Capabilities: DC01-DC13 starter domain capabilities.
- Delivery Coordinator: the DC13 capability that coordinates mission state, evidence, approvals, and closure.
- Guarded runner: the only production execution boundary for commands and provider actions.
- Evidence signer: Core local signing, with optional customer-controlled external signer or KMS-equivalent custody.
- Knowledge quality: freshness, ownership, provenance, expiration, conflict, and sufficiency signals attached to every mission.
- Provider capability contracts: a common governance envelope without suppressing provider-native strengths.
- Workflow embedding: existing SCM, issue, chat, CI, IDE, and provider surfaces remain the primary places users work.

## Starter Company

Northstar Forge is synthetic demonstration data. It exists so a team can launch the product, see a complete company skeleton, run missions, inspect evidence, and then replace the starter material with its own users, knowledge, profiles, missions, and policies.

## Open-Core Boundary

Steel Mission Core is open source under Apache-2.0. Teams can download, inspect, run, modify, and evaluate the core with the included synthetic starter company.

Steel Mission Enterprise Edition is proprietary, commercially licensed software and service capability for operational scale. Enterprise scope includes multi-organization fleet governance, managed deployment and upgrades, managed KMS/HSM operations, advanced governance and compliance workflows, managed retention and integrations, private-cloud operations, and support.

Core includes the enterprise trust surface needed for adoption and pilots: self-managed OIDC/JWKS customer identity, audit logging, SIEM/security-monitoring export, self-managed connectors, and customer-controlled external evidence signing. These controls are not license-gated.

## Orchestration

Steel Mission does not depend on n8n. n8n is a replaceable adapter. Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and a private job runner.

Steel Mission should not become a second documentation job or a replacement workspace. It binds authoritative sources maintained elsewhere, warns when context is stale or insufficient, and returns governed work to the originating tool. Its durable differentiation is cross-provider policy, evidence, organizational context, and execution governance while native adapters preserve provider-specific capabilities.
