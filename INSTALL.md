# Install Steel Mission Alpha v0.1

Steel Mission is an Agent Delivery Plane for governed AI-assisted software delivery. The core repository is open source under Apache-2.0 and runs locally with synthetic starter data.

## Fast Start

```bash
git clone https://github.com/Present-Capable-Computing/Steel-Mission.git
cd Steel-Mission
python3 -m pip install -r requirements-dev.txt
bin/steel-mission doctor
bin/steel-mission serve
```

Open `http://127.0.0.1:8765/`.

The first run starts with the synthetic Northstar Forge organization. Owners and admins can replace it from Settings.

## Requirements

Minimum for the core:

- macOS or Linux;
- Python 3.11 or newer;
- Git;
- a modern browser;
- `pytest` for validation.

Recommended for development:

- GitHub CLI `gh`;
- `curl` and `jq`;
- 4 CPU cores;
- 8 GB RAM for core-only operation;
- 16 GB RAM minimum for local model mode;
- 24-32 GB RAM recommended for local 14B model work.

Optional providers:

- Claude Code CLI for the Claude-backed Delivery Coordinator profile;
- Codex CLI for the repair-agent path;
- Ollama for the local Glimmer profile;
- `qwen2.5-coder:14b` or another configured local coding model.

## Local Starter

Use the local profile when you want to evaluate Steel Mission without depending on a cloud model.

```bash
make install-dev
make doctor
make local
```

If Ollama is installed and the model is available, start Glimmer first:

```bash
make glimmer-start
make glimmer-status
make local
```

If the local model is not ready, the app still opens and shows readiness status.

## Claude Code Profile

Install and authenticate Claude Code, then run:

```bash
claude auth status
STEEL_MISSION_RUNTIME_PROFILE=dc13.claude bin/steel-mission serve
```

The Claude profile binds the Delivery Coordinator role to the Claude provider. It does not create a new organizational role.

## Codex Profile

Codex is currently used by the worker as the repair-agent path.

```bash
codex login status
bin/present-worker status
```

The Codex adapter is available to the worker when the Codex CLI is installed and authenticated.

## Validate The Release

```bash
make release-check
```

This runs whitespace checks, compiles Python entrypoints, and runs the test suite.

## Runtime Data

Steel Mission writes runtime output into ignored local folders such as `logs/`, `missions/`, `tasks/`, `jobs/`, `worktrees/`, and `test-results/`. These folders are not release artifacts.

Use environment variables from `.env.example` when you want to redirect runtime data outside the repository.

## Production Notes

Before production use:

- configure customer identity through OIDC/JWKS;
- replace local signing with customer KMS, Vault Transit, HSM, or equivalent external signing;
- configure repository, issue tracker, chat, CI/CD, and SIEM connectors;
- run inside customer infrastructure, a private worker, container, or private cloud environment;
- require approvals for high-risk changes and production deployments.
