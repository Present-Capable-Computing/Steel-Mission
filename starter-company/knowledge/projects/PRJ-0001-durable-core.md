# PRJ-0001 — Durable Core

State: Active

Outcome: replace Steel Mission's fragile execution core with a durable transactional job service and a pull-based runner that executes in customer infrastructure. The finished core has one storage interface, a SQLite default, a PostgreSQL high-availability option, one orchestration path, durable connector ingress and egress, and signed attested runner results.

## Six epics

| Phase | Epic | Delivery focus |
| --- | --- | --- |
| P0 | Toolchain and CI baseline | Establish trustworthy build and test gates before core changes begin. |
| P1 | Durable broker service | Put state behind transactions, leases, fences, and restart-safe coordination. |
| P2 | Pull-runner agent and job protocol v2 | Let customer-owned runners claim and attest portable work over mutually authenticated transport. |
| P3 | Durable connector inbox and outbox | Make inbound deduplication, outbound retry, and dead-letter handling transactional. |
| P4 | Single orchestration path | Route mission control through the broker and resume work after restart. |
| P5 | Steel-Mission naming | Complete one-product naming without mixing renaming with behavior changes. |

## Work inventory

The GitHub working surface contains 58 issues: 6 epic issues and 52 child delivery issues. Every child issue states a requirement, acceptance evidence, an implementation surface, a milestone, and its epic. Declared dependencies may point to the same or an earlier milestone, never a later one.

## Sources

- Project record: `plan/PRJ-0001.json`
- Milestone records: `plan/MS-0001.json` through `plan/MS-0006.json`
- Full issue catalog: `tooling/github-plan.json`
- Binding workplan: `docs/workplan.md`
- Technical plan: `docs/durable-core-plan.md`

These files remain at their repository locations. The starter-company manifest registers them read-only; it does not copy or rewrite them.
