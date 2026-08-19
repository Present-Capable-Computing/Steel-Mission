# Contributing

Contributions are welcome under the published contribution policy.

## Start here

[`docs/workplan.md`](docs/workplan.md) is binding for everyone landing a commit in
this repository — employees, contractors, and agents running under a person's
account. An agent's commit is that person's commit. Read §4 (the rules), §5 (the
definition of done) and §6 (how those are enforced) before opening a pull request.

In short: one reason per pull request; the description covers every commit in the
branch, not the last thing you worked on; the full suite is green before you move on,
not before you open the pull request; and evidence means what a check said, not what
you expect it to say.

Work is organised as milestones and tasks. Pick up an
[open issue](https://github.com/Present-Capable-Computing/Steel-Mission/issues),
branch as `<milestone>/<short-slug>`, and open a draft pull request on the first
commit so the rest of us can see what is being worked on without asking.

## Boundaries

Before submitting a change, keep these boundaries intact:

- Do not add private company canon, personal data, secrets, credentials, or local machine paths.
- Keep starter company data synthetic.
- Keep knowledge domains separate from domain capabilities.
- Keep the Delivery Coordinator capability responsible for mission state, evidence, approvals, and closure.
- Keep executable actions behind the guarded runner.
- Keep Enterprise Edition features separated from Core behavior.
- Keep the licensing boundary clear: core repository contributions are Apache-2.0, while Enterprise Edition features remain proprietary and commercially licensed.
