# Steel Mission starter company

This directory is the launch dataset for the Steel Mission organization. It gives a new installation a real product mission, a founder-led operating model, complete capability coverage, and an active delivery portfolio without importing any external company corpus into the image.

## Identity and mission

Steel Mission is a software company building a governed software delivery plane for teams that use AI in production delivery.

**Mission:** make dependable, governed AI-assisted software delivery widely adoptable by placing a durable control plane between human intent and machine execution.

The starter goals are to:

1. deliver a durable transactional core that survives crashes, retries, and remote execution;
2. preserve human authority through explicit policy, approval, and evidence boundaries;
3. run in customer-owned infrastructure with replaceable models, runners, and connectors;
4. fit the repositories, issue trackers, chat systems, CI/CD, and security tools teams already use;
5. make the open core easy to evaluate and the enterprise operating model easy to trust; and
6. grow an evidence-led portfolio beyond the first project without weakening the core.

## Founder and team

The company is led by founder Andrew Hermann and a cross-functional team spanning architecture, product, design, security, intelligence, platform, governance, ecosystem, operations, synthesis, counterweight, creativity, and delivery coordination. Steel Mission owns its mission, decisions, product boundary, capability identities, and delivery records.

The existing plain three-ring mark is retained as the Steel Mission logo. No other external brand identity is carried into the launch set.

The roster and operating workflow are in `canon/KD03 Team Roster and Workflow.md`. Stable Steel Mission capability keys remain `DC01` through `DC13`.

## First project

`PRJ-0001`, **Durable Core**, is active. It contains 58 GitHub issues in total: 6 epic issues and 52 child delivery issues. The six epics are P0 through P5:

- Toolchain and CI baseline;
- Durable broker service;
- Pull-runner agent and job protocol v2;
- Durable connector inbox and outbox;
- Single orchestration path; and
- Steel-Mission naming.

The project record lives at `plan/PRJ-0001.json`; milestone records live at `plan/MS-0001.json` through `plan/MS-0006.json`; and the complete issue catalog lives at `tooling/github-plan.json`. `portfolio.json` binds those existing sources into the starter-company set without duplicating them. More projects can be appended to its `projects` array.

## Container first start

The canonical first-start input is `steel-mission-first-start-knowledge-v1.json`. The supplied paths assume the repository is mounted at `/workspace`, matching the included container workspace convention:

```text
/workspace/starter-company  read-only enterprise knowledge
/workspace/plan             read-only project records
/workspace/tooling          read-only issue catalog
```

The current application also ships the normalized equivalents in `config/organizations.json`, `config/users.json`, `config/domain-capabilities.json`, and `config/general-knowledge.json`, so the same company loads in the existing first-run path. The manifest is the future-safe, source-preserving contract described by `schemas/canonical/enterprise-knowledge-first-start-v1.json`.

Validate the set with:

```bash
python3 -m pytest tests/test_starter_company.py tests/test_plan_records.py
```
