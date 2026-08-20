# Working in this repository

This is **Steel Mission, the product** — public, Apache-2.0, and what every user
installs. It is not anyone's installation.

An installation's own data — its canon, roster, registries — lives outside this
tree and is mounted at run time. Never copy it in. See §*The one thing never to do*.

## Run it in development

No container, no build, no sign-in:

```sh
make local
```

Then open **http://127.0.0.1:8765/**.

That is the whole loop. The server is standard-library Python. Edit
`steel-mission-chat/server.py` directly; for console changes edit `steel-mission-ui/`
and run `npm run ui:build`, then restart and reload.

**Why development mode needs no token.** Development identity is accepted only from
a loopback address. Running directly on `127.0.0.1` *is* loopback, so the console's
own role and actor selectors are enough and you are signed in immediately. Inside a
container it is not: Docker forwards the connection and the server sees the bridge
gateway, which is why a containerised run needs a session from `/auth/login`. If you
find yourself pasting tokens during development, you are running the container when
you wanted `make local`.

The shipped demo organisation is **Northstar Forge**. That is correct and expected —
it is the synthetic company every user receives.

```sh
make test           # the full suite, ~2 minutes
make release-check  # whitespace, compile, suite — the same gate as CI
```

## Before you write anything

Read [`docs/workplan.md`](docs/workplan.md). It is binding, and it covers what a
pull request must describe, what counts as evidence, and what "done" means.

The three rules that matter most here:

- **Green at every step**, not before you open the pull request.
- **One reason per pull request.** A refactor that also fixes a bug produces a
  regression nobody can attribute.
- **Wrap, never rename.** The suite loads `server.py` by path and monkeypatches
  module-level names. A renamed function does not fail loudly — it silently stops
  being patched, and the suite stays green while testing nothing.

## The active project is PRJ-0001

| | |
|---|---|
| Project record | [`plan/PRJ-0001.json`](plan/PRJ-0001.json) |
| Design | [`docs/durable-core-plan.md`](docs/durable-core-plan.md) |
| Milestone | [`plan/MS-0001.json`](plan/MS-0001.json) |
| Issues | label `prj:PRJ-0001` |
| Board | https://github.com/orgs/Present-Capable-Computing/projects/1 |

`PRJ-0000` completed again on 2026-08-20 after all four MS-0012 review findings
closed. `PRJ-0001` is active again under the still-binding PRJ-0000-D2 ordering
decision. Its scope, estimate and already reset target dates remain unchanged
because the interruption began and ended on the same date.

### Picking up an issue

```sh
gh issue list --label prj:PRJ-0001 --state open
gh issue view <n>
git checkout -b <milestone>/<short-slug>      # e.g. ms-0001/checkout-v7
```

Each issue states a **requirement** as an outcome and the **acceptance evidence**
that settles it. Neither is decoration: write the test that proves the acceptance
evidence, watch it fail first, then make it pass. An assertion you never saw fail is
an assertion you have not verified tests anything.

## The one thing never to do

**Never make an installation work by changing this repository, and never copy an
organisation's data into it.**

`starter-company/` and `config/` ship to every user. Writing a real organisation's
canon, roster or registries into them removes the demo company everyone starts from
*and* publishes that organisation's people and decisions under an open-source
licence.

This is not hypothetical — it happened once and was caught before it was committed.
Two tests fail if it happens again:
[`tests/test_org_data_boundary.py`](tests/test_org_data_boundary.py).

An installation redirects its own data instead:

```sh
STEEL_MISSION_ORG_DIR=/srv/acme-org STEEL_MISSION_CONFIG_DIR=/srv/acme-config \
  bin/steel-mission serve
```

Both are needed. Setting only the first serves the installation's documents under
the shipped company's identity, which reads as working and is not.

## Landing a change

The pull request template asks for every commit in the branch, the evidence, the
surfaces touched, and how the change is undone. Fill it in — a reviewer decides how
carefully to read from what you wrote.

Tick **authentication**, **a network-listening service**, **subprocess or container
execution**, or **authority-owned schemas** where they apply; those get a security
review before merge.

`main` requires both interpreter checks green and every review conversation
resolved.

When the pull request is finished, arm auto-merge — `gh pr merge <n> --auto
--merge` — and report the queued state rather than waiting for the landing.
Report it truthfully: queued is queued, not merged; the issue closes when the
merge happens.

## Where things are

| | |
|---|---|
| `steel-mission-chat/server.py` | the server: routing, auth, config registries, missions |
| `steel-mission-ui/`, `steel-mission-chat/app.html` | typed console source and its committed self-contained build |
| `config/` | product configuration: organisations, users, capabilities, policy |
| `starter-company/` | the shipped synthetic organisation, Northstar Forge |
| `adapters/`, `bin/` | provider adapters and the command-line tools |
| `schemas/canonical/` | authority-owned contracts — changing one may need ratification |
| `plan/`, `tooling/` | project records and the GitHub sync; excluded from the image |
