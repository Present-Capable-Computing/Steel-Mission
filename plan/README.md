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
means every task in the milestone is closed or explicitly dropped; it does not
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
| Epic → task | Sub-issue | A task is a real sub-issue of its epic, so the epic shows its children's state and a progress count |
| `dependsOn` | Blocked-by dependency | A declared dependency is a real GitHub blocker, visible on the issue rather than only in the manifest |

Three things GitHub owns once they are set, and the manifest deliberately does not
restate: the epic's list of tasks, a task's blockers, and a task's parent. Each was
originally written into the issue body as prose, which made it a second copy that
nothing kept true. The body now carries only what GitHub has no field for: the
requirement, the acceptance evidence and the surface.

Two mismatches are deliberate and must not be "fixed":

- **A GitHub milestone has two states, open and closed.** The five-state model
  (`PLANNED`, `ACTIVE`, `AT_RISK`, `COMPLETE`, `ABANDONED`) lives in the `MS` record.
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
python3 tooling/gh-plan-sync.py --dry-run       # print what would change
python3 tooling/gh-plan-sync.py                 # labels, milestones, issues,
                                                # sub-issues, dependencies
python3 tooling/gh-project-fields.py            # board fields, item values, views
tooling/gh-project-bootstrap.sh                 # all of the above, from nothing
```

The board's `Phase`, `Area` and `Kind` values are **derived from each issue's
labels**, not declared a second time in the manifest. A phase stated in two places
is a phase that will eventually disagree with itself. An item whose labels do not
map is left unset and reported, never guessed, which is how the two acceptance
criteria missing an `area:` label were found.

A view's name, layout, filter and visible columns are set from the manifest.
Grouping is not: the API exposes the first four and not the fifth, so grouping a
table by `Phase` is a click in the interface. Views that are not in the manifest are
reported and left alone rather than deleted: a view someone built for themselves is
not drift.

The board needs the `project` token scope, which the `repo` scope does not include.
`tooling/gh-project-bootstrap.sh` checks for it first and prints exactly what to run
if it is missing, rather than failing halfway through with a half-built board.
