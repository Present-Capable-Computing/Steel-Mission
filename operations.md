# Closed-Claw Operations

## Local Run

```bash
python3 closed-claw-chat/server.py --host 127.0.0.1 --port 8765 --profile dc13.local
```

Open `http://127.0.0.1:8765/`.

## First-Run Setup

Closed-Claw starts with the synthetic Northstar Forge organization:

- starter users;
- knowledge domains;
- domain capabilities;
- Delivery Coordinator;
- local runtime profile;
- guarded runner policy;
- external signer policy.

Owners and admins can replace starter data from Settings.

## Production Controls

Before production use:

- configure customer identity through OIDC/JWKS;
- replace the default external signer with customer KMS, Vault Transit, or an equivalent signing service;
- configure repository, issue-tracking, chat, CI/CD, and SIEM connectors;
- run agents in a private worker, container, or customer VPC;
- require approvals for high-risk phases and production deployments.

## Guarded Execution

Use `bin/present-control-plane` for executable delivery work. Direct command paths are blocked by policy.

## Adapter Roadmap

n8n is optional. Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and a private job runner.
