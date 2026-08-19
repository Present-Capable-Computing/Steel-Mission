# Plan records

This directory holds the project and milestone records for delivery work. They are
schema-validated documents, not notes: `plan/PRJ-*.json` validates against
`schemas/canonical/project-v1.json` and `plan/MS-*.json` against
`schemas/canonical/milestone-v1.json`.

They live here rather than under `tasks/` because `tasks/` is ignored by git. A
plan record that does not commit is a plan record that goes stale without anyone
noticing.

## Neither record acquires authority

A project organises work. A milestone schedules it. Neither says anything about
whether the work is correct.

`AT_RISK` is a statement about the schedule, never about correctness. `COMPLETE`
means every task in the milestone is closed or explicitly dropped — it does not
mean anything passed verification. A test asserts that no milestone state reads as
a verification outcome, because a state that could imply completion would silently
upgrade every task scheduled against it.

Verification outcomes live where they already live: in task contracts, verification
records and the evidence chain.

## How these map onto GitHub

GitHub is the working surface. These records are the source of truth for the shape
of the work. Where they disagree, the record in this directory wins and GitHub is
corrected.

| Record | GitHub object | How they are linked |
|---|---|---|
| `PRJ-0001` | Projects v2 board | The board description carries `PRJ-0001`; the board URL is written back into `metadata.githubProject` |
| `MS-000N` | Repository milestone | The milestone description carries `MS-000N`; the milestone number is written back into `metadata.githubMilestone` |
| Task | Issue | The issue body carries the milestone id it is scheduled against; a `DEV-NNNNNN` id appears only once the control plane mints one |

Two mismatches are deliberate and must not be "fixed":

- **A GitHub milestone has two states, open and closed.** The five-state model —
  `PLANNED`, `ACTIVE`, `AT_RISK`, `COMPLETE`, `ABANDONED` — lives in the `MS` record.
  The GitHub milestone is a projection of it, not a competing status.
- **Issues do not carry `DEV-NNNNNN` ids at creation.** Those ids are minted when
  work enters the control plane pipeline, which refuses a requirement no human has
  written. `taskIds` on a milestone record fills in as tasks are minted; an empty
  `taskIds` means no control-plane task exists yet, never that the milestone is
  unplanned.

## Keeping GitHub in step

`tooling/github-plan.json` is the manifest of labels, milestones and issues.
`tooling/gh-plan-sync.py` applies it. The sync is idempotent: it searches by title
before it creates anything, so re-running it after an edit updates rather than
duplicates.

```sh
python3 tooling/gh-plan-sync.py --dry-run   # print what would change
python3 tooling/gh-plan-sync.py             # apply
```

The Projects v2 board needs a token scope the repository scope does not include.
`tooling/gh-project-bootstrap.sh` creates the board and reports what it needs if the
scope is missing.
