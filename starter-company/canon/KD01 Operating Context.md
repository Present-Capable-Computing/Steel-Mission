# KD01 — Steel Mission Operating Context

Company: Steel Mission

## Purpose

Steel Mission builds a governed software delivery plane for teams that use AI to plan, change, test, review, and ship software. It sits between human intent and machine execution so delivery can be durable, inspectable, restart-safe, and accountable.

## Mission

Make dependable, governed AI-assisted software delivery widely adoptable by placing a durable control plane between human intent and machine execution.

## Goals

1. Deliver a transactional core that survives concurrency, retries, process loss, and remote execution.
2. Preserve human authority with explicit policy, approvals, bounded permissions, and signed evidence.
3. Run in customer-owned infrastructure with replaceable models, runners, and connectors.
4. Fit existing repositories, issue trackers, chat, CI/CD, deployment, and security-monitoring workflows.
5. Make Steel Mission Core straightforward to evaluate, inspect, modify, and self-host.
6. Earn enterprise adoption through operability, evidence, integration quality, and support—not by withholding baseline security controls.
7. Expand through a deliberate project portfolio without allowing later ambitions to destabilize the durable core.

## Product principles

- People and organizational policy retain authority; models prepare and propose.
- A plan, schedule, or tool output is never proof that work is correct.
- Guarded execution must be enforceable before an action, not reconstructed afterward.
- Sources remain where their owners maintain them and are registered read-only.
- Durable state belongs in transactional systems, not process memory or unlocked files.
- Providers and integrations are replaceable adapters outside the root of trust.
- Uncertainty, missing context, degraded guarantees, and residual risk are stated plainly.
- Open-core trust controls must be sufficient for a serious enterprise pilot.

## Current portfolio

The first project is `PRJ-0001`, **Durable Core**. It has 58 GitHub issues in total: 6 epic issues and 52 child delivery issues. The complete working inventory is maintained in `tooling/github-plan.json`; project and milestone records under `plan/` govern its shape.

More projects are expected. Future work receives a stable `PRJ` identity only when its outcome, boundary, ownership, and evidence expectations are explicit.

## Boundaries

- The launch set contains only Steel Mission identities, knowledge, product doctrine, and delivery records.
- The Control Plane Protocol remains experimental until implementation and interoperability evidence justify a stable claim.
- Compliance mappings support customer assurance work; they are not certification or legal advice.
- Local development mode is an evaluation path, not a production-isolation claim.
