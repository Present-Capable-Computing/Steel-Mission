# Codex Quickstart

Use this path when you want to evaluate the Codex-backed repair-agent path.

## Requirements

- Steel Mission repository cloned locally;
- Python 3.11 or newer;
- Git;
- Codex CLI installed;
- Codex CLI authenticated.

## Verify Codex

```bash
codex login status
bin/present-worker status
```

The status payload reports whether the Codex provider is installed and authenticated.

## Where Codex Fits

In Alpha v0.1, Codex is used by the worker's repair path. Delivery Coordinator remains DC13. Codex repairs occur inside the worker-controlled task/worktree flow and are validated before evidence is recorded.

## Run Validation

```bash
make release-check
```

## Notes

- Codex provider availability depends on the local Codex CLI session.
- Repair output is expected to be committed in the isolated task worktree by the worker path.
- Production execution still requires the guarded control-plane boundary.
