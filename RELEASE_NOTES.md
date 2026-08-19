# Steel Mission Alpha v0.1 Release Notes

Steel Mission Alpha v0.1 is the first public release of the Steel Mission Agent Delivery Plane.

## Release Summary

This release provides a runnable local control surface for governed AI-assisted software delivery. It includes a work view, role-aware settings, synthetic starter organization, knowledge and capability management, runtime profiles, model bindings, mission control, guarded execution, signed evidence, compliance mappings, and starter integration contracts.

## Installation

```bash
git clone https://github.com/Present-Capable-Computing/Steel-Mission.git
cd Steel-Mission
python3 -m pip install -r requirements-dev.txt
bin/steel-mission doctor
bin/steel-mission serve
```

Open `http://127.0.0.1:8765/`.

## Model Paths

Claude Code:

- install and authenticate Claude Code;
- use runtime profile `dc13.claude`;
- Delivery Coordinator remains DC13 and binds to Claude as one provider instance.

Codex:

- install and authenticate Codex CLI;
- Codex is used by the worker repair path;
- run `bin/present-worker status` to confirm readiness.

Local:

- install Ollama;
- pull or configure `qwen2.5-coder:14b`;
- use runtime profile `dc13.local`;
- 16 GB RAM is the practical minimum for local 14B model work, with 24-32 GB recommended.

## Included In Alpha v0.1

- Apache-2.0 core license and explicit proprietary Enterprise Edition boundary.
- Steel Mission product naming and Agent Delivery Plane terminology.
- Synthetic Northstar Forge starter organization.
- KD01-KD03 organizational knowledge domains.
- DC01-DC13 Domain Capabilities.
- DC13 Delivery Coordinator runtime profile and model role binding.
- Admin/owner organization and knowledge management.
- Publisher/user assignment model.
- Per-job snapshot policy.
- Mission control lifecycle: understand, plan, modify, build, test, inspect, repair, PR, deploy, close.
- Guarded control-plane execution boundary.
- Pre-execution blocking for unsafe actions.
- Signed and hash-chained mission evidence.
- Core local evidence signing plus customer-controlled KMS, Vault Transit, HSM, or equivalent external signing.
- Compliance evidence mappings for SOC 2, ISO 27001, and ISO 42001.
- Connector contracts for GitHub/GitHub Actions, GitLab, Jira, Linear, Slack, CI/CD, SIEM, and replaceable orchestration adapters.
- Core OIDC/JWKS identity, audit logging, SIEM/security-monitoring export, and customer-controlled external signing.
- Knowledge-quality reports for source availability, freshness, ownership, provenance, expiration, conflicts, and context sufficiency.
- Provider-native capability declarations and runtime requirements that prevent silent lowest-common-denominator fallback.
- Existing-tools-first connector contract for bidirectional workflow embedding.
- Optional n8n adapter posture; n8n is not a product dependency.
- CI workflow for Python 3.11 and 3.12.

## Known Limitations

- The release is a public alpha, not a production support commitment.
- Enterprise Edition focuses on operational scale: multi-organization fleet governance, managed operation and upgrades, advanced governance, managed integrations, and support.
- Container deployment is not included yet.
- The local UI is functional, but not yet packaged as a desktop or hosted distribution.
- Provider setup is manual.
- The control-plane protocol is experimental and may change before v1.0.
- Connectors are represented by contracts and registries; managed production connectors are Enterprise scope.
- Compliance mappings are support material, not an auditor certification.

## Validation

The release branch was validated with:

```bash
python3 -m pytest
```

Expected result for Alpha v0.1: 214 tests pass.
