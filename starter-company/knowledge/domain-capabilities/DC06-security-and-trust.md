# DC06 - Security and Trust

Company: Northstar Forge

DC06 protects authority, system integrity, privacy, recoverability, and verifiable trust. It asks what asset is protected, how authority could broaden, and how keys, permissions, approvals, and evidence can be revoked or verified.

## Mission

Classify risk, design pre-execution controls, evaluate trust boundaries, and require evidence for security claims.

## Owns

- Threat model and misuse paths.
- Authority, consent, and permission boundaries.
- Evidence signing and tamper-evident records.
- Revocation, recovery, and assurance posture.

## Inputs

- Mission risk class, action type, integration scope, secrets, model provider, and deployment target.
- Governance obligations and architecture boundaries.

## Outputs

- Security and trust review.
- Pre-execution policy recommendations.
- Evidence and signing requirements.
- Residual-risk statement and specialist-review trigger.

## Boundaries

DC06 is not a ritual veto. Lower-assurance prototypes may proceed when the risk is explicit, bounded, and reversible.

## Starter Scenario

Northstar Forge uses DC06 before enabling guarded runner commands that can modify repositories. DC06 requires policy checks before execution, signed evidence after execution, and human approval for high-risk actions.
