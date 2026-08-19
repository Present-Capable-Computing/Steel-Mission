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

Production-isolated execution additionally requires Docker or a compatible implementation of the versioned private-runner request/result contract.

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

## Private Runner

Local evaluation uses `development-local` mode so setup remains small. That mode is not a production isolation claim. Build and verify the included execution image before production use:

```bash
make private-runner-image
PRESENT_PRIVATE_RUNNER_MODE=docker bin/present-private-runner status
```

Change `executionBoundary.privateRunnerMode` in `config/control-plane-policy.json` from `development-local` to `container`. Keep the default `PRESENT_PRIVATE_RUNNER_NETWORK=none` for offline phases. If GitHub or another provider must be reached, attach only a reviewed, egress-controlled Docker network. Add only required credential names to `executionBoundary.allowedEnvironment`; their values are passed through the runner environment and are not placed in Docker argv.

The included image supports this repository's Python checks plus Git and GitHub CLI. For Node, Java, Go, infrastructure CLIs, or other repository-specific tools, derive a pinned image from `Dockerfile.private-runner` and select it with `PRESENT_PRIVATE_RUNNER_IMAGE`.

## GitHub, Slack, And Jira

Set the scoped token and signing-secret variables documented in `.env.example`, enable the connector in Settings, and configure provider delivery to the matching endpoint:

- `/api/integrations/github/webhook`
- `/api/integrations/slack/events`
- `/api/integrations/jira/webhook`

Set `STEEL_MISSION_PUBLIC_URL` so returned workflow messages contain a usable investigation link. Jira must be delivered through a webhook gateway that computes `X-Steel-Mission-Signature: sha256=<HMAC-SHA256(raw-body)>` with `JIRA_WEBHOOK_SECRET`.

## Runtime Data

Steel Mission writes runtime output into ignored local folders such as `logs/`, `missions/`, `tasks/`, `jobs/`, `worktrees/`, and `test-results/`. These folders are not release artifacts.

Use environment variables from `.env.example` when you want to redirect runtime data outside the repository.

## Production Notes

Before production use:

- configure customer identity through OIDC/JWKS;
- configure SIEM/security-monitoring export and evidence retention;
- replace local signing with customer KMS, Vault Transit, HSM, or equivalent external signing;
- configure repository, issue tracker, chat, CI/CD, and SIEM connectors;
- run the included hardened private-runner image inside customer infrastructure or a private cloud environment;
- require approvals for high-risk changes and production deployments.

## Core Trust Controls And Commercial Operations

The downloadable Core build includes self-managed OIDC/JWKS identity, audit logging, SIEM/security-monitoring export, connector configuration, and customer-controlled external evidence signing. None of those baseline controls requires an Enterprise entitlement.

Enterprise offerings cover the operational layer around Core: managed deployment and upgrades, multi-organization fleet governance, centralized policy operations, managed retention and integrations, private-cloud operations, and support.
