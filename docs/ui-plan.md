# PRJ-0000 — usable surface, honest admin writes

Project [`PRJ-0000`](../plan/PRJ-0000.json). The rules for how work lands are in
[`docs/workplan.md`](workplan.md); this document is the design.

## 1. Why

A person running a real installation could not work out why the console offered
three Delivery Coordinator entries when the organisation defines thirteen Domain
Capabilities. The three are **model backends for one capability**, and nothing in
the product says so.

That question was worth following, because the answer is not a missing label. It is
that the console teaches a wrong model of the system, and that several of the
writes behind it do not do what they report.

### The surface teaches the wrong model

`steel-mission-chat/index.html` is 3,914 lines: **2,580 of JavaScript, 1,217 of CSS,
117 of markup**. It is an application maintained without the tools an application
needs.

| Word | Means, in visible text |
|---|---|
| **Profile** | which model runs the coordinator · the settings dialog · a snapshot policy |
| **Role** | permission level · a model-binding record · a Domain Capability |
| **Capability** | a Domain Capability · a provider feature · a permission scope |

Consequences the user meets directly:

- The picker is labelled **"Profile"** with no hint, no description and no tooltip.
- Capability checkboxes read **`DC01 · DC01 · Counterweight`** — `fNumber` equals
  `roleKey`, so the key prints twice.
- A second renderer prints bare `DC13, DC01, …` from a **hardcoded array**.
- The only definition of a Domain Capability is **behind an owner/admin gate**, so
  the publishers and users who are assigned capabilities are never told what they
  have.
- The work-mode switch changes a placeholder, a button fill and four starter
  prompts. It has no visible group label and no explanation, and defaults to the
  non-obvious setting.
- **"My Capability Workspace" renders for nobody**: it clears itself for
  owner/admin, and publishers and users can never make its navigation section
  active.

### The writes are not what they claim

Each verified in source.

| Behaviour | Where |
|---|---|
| An organisation saved with **zero** capabilities is granted **all thirteen** | `server.py:666-667` |
| `POST {"users": []}` answers 200 and **recreates** four accounts with no identity subjects | `server.py:1187-1193` |
| `"status": "suspended"` becomes `"active"` — suspending a user re-enables them | `server.py:1181` |
| An unknown worktree mode silently becomes in-place, **mutating the source checkout**, behind a 202 | `server.py:4159` |
| A parse error falls back to a **different schema shape** granting the coordinator capability to synthetic identifiers | `server.py:1282-1291, 1356-1363` |
| An unknown runtime profile id is **fabricated** and recorded as valid | `server.py:367-368` |
| Capability authorization is skipped unless identity mode is provider-required — and the shipped mode is not | `server.py:325-341` |
| The same failure class answers 400 in one admin handler group and 403 in another | `server.py:9070` vs `9163` |

None of this is validated: `schemas/` holds thirteen schemas and **none for
`config/`**.

### The one-way door

`config/domain-capabilities.json` ships in a shape whose `publishers[]` arrays carry
real data. That array is **fallback-only** — read at `server.py:1405-1417` solely
when no active user of that role has any `assignedCapabilities`, and then granting
the capability to the role *collectively*, ignoring which user is named. The
assignment with actual effect is `users.json → assignedCapabilities`.

Worse, `save_domain_capability_registry` always writes the normalized payload, which
always contains `userAssignments`, and the loader prefers that branch. **The first
save permanently converts the file into the shape where `publishers[]` is never read
again**, and later hand-edits to it are silently ignored.

This is currently unreachable from a browser, because the UI never calls the
endpoint — **zero references**. Building the assignment interface without fixing the
loader first would *arm* it. That single fact sets the order of the whole project.

MS-0007 resolves this by making `users.json → assignedCapabilities` the only durable
assignment authority. The assignments endpoint remains a compatibility projection
that reads from and writes through the user registry; `config/domain-capabilities.json`
is no longer shipped or consulted.

## 2. Order

Correctness before appearance, because a surface rebuilt over writes that silently
do something else is a better-looking version of the same problem.

| | Milestone | |
|---|---|---|
| **U0** | [MS-0007](../plan/MS-0007.json) | Configuration integrity and the admin write path |
| **U1** | [MS-0008](../plan/MS-0008.json) | One vocabulary, in the page that ships today |
| **U2** | [MS-0009](../plan/MS-0009.json) | A build step, recorded and reversible |
| **U3** | [MS-0010](../plan/MS-0010.json) | The rebuilt console at parity, then default |
| **U4** | [MS-0011](../plan/MS-0011.json) | Legacy retirement and the standing rule |

Within U0 the order also matters: schemas land first; then the loader accepts both
registry shapes so the one-way door closes *before* anything can call the writer;
then fail-closed parsing; then the user cross-check; then the refusals; then
validation on write; then authorization; then identity precedence.

## 3. The build step

**TypeScript + Preact, bundled by esbuild, emitted as one self-contained
`index.html` that is committed.**

Three exact-pinned direct dependencies. Vite would pull rollup and postcss and
several hundred transitive packages onto a product whose selling point is a
supply chain one person can review.

**The committed-single-file shape is the design, not a convenience:**

- `INDEX = APP_DIR / "index.html"` keeps working untouched, and routing stays the
  closed allow-list it is today. **No static-asset route is added** — that would be
  the product's first file-serving handler and a new surface on the trust boundary.
- No container build stage, so the platform-specific-package problem the image
  already works around is not reopened. `tests/test_org_data_boundary.py` fails the
  build on any Dockerfile line naming a CPU architecture.
- The Python suite and the container need no Node.

**Two existing tests constrain the bundler flags.** The suite matches a **bare**
`<script>` element — a module-typed script matches nothing — and runs a syntax check
on the extracted script as an ordinary script file, where a top-level `import` is a
syntax error. **The bundle must be emitted in immediately-invoked format.** Verify
against real output as the first commit of U2.

Minification stays off through the migration so the committed diff is reviewable.

**Risk treatment:** exact pins, lockfile integrity, `npm ci` only, a high-severity
audit as a required check, a Dependabot block, `CODEOWNERS` over the package files,
and a rebuild-and-diff check — a file a clean checkout reproduces byte-for-byte
cannot be a hand-edited bundle.

**Reversibility conditions.** If any stops holding, the decision reopens:

1. The served page is one self-contained file needing no build and no network fetch.
2. The server knows only a file path — no static route, no MIME map.
3. The image gains no Node runtime dependency.
4. The superseded page stays behind a server flag for one release.
5. Deleting the UI source directory and reverting one commit yields a working product.

## 4. The vocabulary

| Concept | Label | Wire name — **never renamed** |
|---|---|---|
| DC01–DC13 | Domain Capability | `roleKey`, `capabilityKey`, `assignedCapabilities` |
| KD01–KD03 | Knowledge Domain | `knowledgeDomainKeys` |
| owner/admin/publisher/user | Access level | `role`, `operatorRole` |
| Which model runs the coordinator | Coordinator model | `runtimeProfile`, `STEEL_MISSION_RUNTIME_PROFILE` |
| Frozen knowledge for a job | Snapshot policy | `snapshotProfile` |
| Normal / Domain Capabilities | Work mode | `workMode` |

"Delivery Coordinator (DC13)" on first mention, "Delivery Coordinator" after —
never a bare key in prose.

This is a **presentation** vocabulary. Every wire name stays: the runtime-profile
tests pin the resolution path, and the harness loads the server by path with
module-scope monkeypatching, so a renamed function stops being patched *without
failing*.

Enforced in three layers rather than by discipline: labels come from a server
endpoint instead of string literals, killing the hardcoded array and the duplicated
key at source; a lexicon test refuses the banned collocations; and a TypeScript
union makes an unknown label a compile error.

## 5. Migration

The existing suite contains **231 assertions** matching implementation strings in
the served page — element identifiers, endpoint paths, even function names. No
bundler output survives them, minified or not.

They are retired safely, not deleted under pressure:

1. **U1** writes a behavioural contract test that is green against the page that
   ships today, with every assertion watched to fail first.
2. **U2** lands the build with the default unchanged, so all 231 stay green.
3. **U3** reaches parity, then flips the default and deletes the 231 in the same
   pull request — permissible only because the contract test has been green for two
   milestones.
4. **U4** removes the superseded page as its own decision.

The router's entry function keeps its name and gains a selector; the legacy and
application renderers are new names. The head-request handler takes its length from
the file on disk and will be wrong the moment the served page is not that file, so
it is corrected in the same pull request as the selector — that is the shape of
defect that leaves a green suite and a broken client.

## 6. Estimate

| Milestone | Focused days |
|---|---|
| U0 configuration integrity | 2.0 |
| U1 vocabulary | 1.5 |
| U2 build step | 2.0 |
| U3 rebuild and flip | 5.0 |
| U4 retirement | 0.5 |
| **Total** | **11.0** |
| **With 25 percent contingency, declared** | **13.75** |

Estimated per category and divided by an expected acceleration factor, with
security-sensitive and novel-integration work held at the cautious end and
irreducible empirical time counted at no acceleration. Re-defend at every milestone
boundary and print the delta rather than absorbing it.
