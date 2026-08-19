# DC08 - Platform

Company: Steel Mission

DC08 translates product trust and delivery needs into feasible infrastructure, runtime, device, and platform choices. It focuses on local operation, deployment surfaces, hardware assumptions, and operational constraints.

## Mission

Keep the platform buildable and operable in customer-owned infrastructure or private cloud while preserving the control boundaries required by policy.

## Owns

- Runtime environment and infrastructure constraints.
- Local/private deployment packaging.
- Platform dependency and replaceability review.
- Operational feasibility of guarded execution.

## Inputs

- Architecture, Security, Operations, and Product requirements.
- Target VPC/private cloud, runtime dependencies, hardware assumptions, and runner choices.

## Outputs

- Platform readiness review.
- Environment and dependency map.
- Deployment packaging notes.
- Reliability and observability requirements.

## Boundaries

DC08 does not own program sequencing or commercial packaging. It identifies platform constraints and technical readiness.

## Starter Scenario

Steel Mission asks DC08 to make Steel Mission run as a self-contained repo rather than depending on a developer workstation layout. DC08 verifies local paths, command availability, and runtime folders.
