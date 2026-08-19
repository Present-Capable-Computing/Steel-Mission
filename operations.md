# Steel Mission Operations

## Local Run

```bash
python3 steel-mission-chat/server.py --host 127.0.0.1 --port 8765 --profile dc13.local
```

Open `http://127.0.0.1:8765/`.

## First-Run Setup

Steel Mission starts with its founder-led launch organization:

- founder and cross-functional team users;
- knowledge domains;
- domain capabilities;
- PRJ-0001 Durable Core with 6 epic issues and 52 child issues;
- Delivery Coordinator;
- local runtime profile;
- guarded runner policy;
- external signer policy.

Owners and admins can revise or extend the launch data from Settings.

## Knowledge Hygiene

Give each durable source an accountable owner plus review or expiration metadata. Treat an `insufficient` knowledge-quality result as an operational blocker for claims about organizational policy or intent: repair the source, resolve conflicts, or ask the owner. Do not solve quality warnings by copying the same documents into a second Steel Mission-only documentation system; bind the authoritative repositories and systems that teams already maintain.

## Production Controls

Before production use:

- configure customer identity through OIDC/JWKS;
- replace the default external signer with customer KMS, Vault Transit, or an equivalent signing service;
- configure repository, issue-tracking, chat, CI/CD, and SIEM connectors;
- run agents in a private worker, container, or customer VPC;
- require approvals for high-risk phases and production deployments.

## Guarded Execution

Use `bin/present-control-plane` for executable delivery work. Direct command paths are blocked by policy.

For production isolation, build the included image with `make private-runner-image`, set `executionBoundary.privateRunnerMode` to `container`, and require `bin/present-private-runner status` to report `productionEligible: true`. Local mode is only for development. Docker egress defaults to disabled; provision an egress-controlled network only for phases that need a provider API. Review the environment allowlist whenever credentials change.

## Native Workflow Operations

GitHub, Slack, and Jira connectors expose the webhook paths documented in `INSTALL.md`. Monitor `_workflow-ingress` receipts under the mission root for accepted, ignored, duplicate, or failed delivery state. Use scoped bot/app tokens; rotate signing secrets; retain webhook delivery IDs; and set `STEEL_MISSION_PUBLIC_URL` for investigation deep links. Jira requires a signing gateway that attaches the Steel Mission HMAC header.

## Adapter Roadmap

n8n is optional. Native GitHub, Slack, and Jira plus the local/container private runner are included. Upcoming orchestration adapters include Temporal, GitHub Actions, GitLab, and remote private-runner scheduling.

Adapters should support bidirectional workflow embedding. Preserve the source event and thread identity on ingress, and publish approvals, status, decisions, evidence, and completion back to that same surface. Use the built-in control UI for setup, investigation, and recovery.
