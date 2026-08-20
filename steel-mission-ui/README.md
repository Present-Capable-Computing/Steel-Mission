# Steel Mission UI build

The source in this directory builds one committed file: `steel-mission-chat/app.html`.
Edit the TypeScript or CSS and run `make ui-build`; never hand-edit the generated page.

## Determinism boundary

The required `UI build is reproducible` check proves byte-for-byte reproduction in a
clean Ubuntu GitHub runner with Node 24.14.0 and the exact packages and integrity
hashes in `package-lock.json`. It installs with `npm ci`, audits at high severity,
type-checks, runs the front-end unit tests, and compares an in-memory rebuild with
the committed page. The job also edits the committed page deliberately, observes
the comparison fail, restores it, and observes the comparison pass.

The build has no timestamp, absolute source path, environment-derived value,
minification, source map, or remote asset. This is the claimed reproducibility
scope; output from another Node major, package-manager version, or uncommitted
source tree is not treated as equivalent merely because it looks the same.
