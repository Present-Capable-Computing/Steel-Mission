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

## Organization Data

An installation runs on its own organization by pointing `STEEL_MISSION_ORG_DIR` at
a directory outside the product tree. Everything under it follows the same shape as
the shipped `starter-company/`: `canon/`, `knowledge/`, and the registries under
`canon/Workspace Packs/_build/`.

```sh
STEEL_MISSION_ORG_DIR=/srv/acme-org bin/steel-mission serve
```

Config and runtime profiles refer to `${ORG_DIR}`, so one variable redirects all of
them. `PRESENT_CANON_DIR` predates this and still takes precedence where it is set.

In a container, mount the directory rather than building it into the image:

```yaml
services:
  steel-mission:
    environment:
      STEEL_MISSION_ORG_DIR: /data/org
      STEEL_MISSION_CONFIG_DIR: /data/config
    volumes:
      - ./acme-org:/data/org:ro
      - ./acme-config:/data/config:ro
```

Both variables are needed, and the second is the one that is easy to miss.
`STEEL_MISSION_ORG_DIR` redirects the organization's documents: canon, knowledge,
workspace packs. `STEEL_MISSION_CONFIG_DIR` redirects the configuration that says
who the organization is: `organizations.json`, `users.json` and the registries
beside them. Capability assignments live only in `users.json`. Set only the first and
the application serves your canon under the shipped company's identity; the
documents are yours and `activeOrganization` is still Northstar Forge, which reads
as working and is not.

Authenticated clients read presentation labels from `GET /api/vocabulary`. Its
`terms` table defines the product vocabulary and wire-name mapping, while
`capabilities` is derived from the organization's knowledge registry and contains
each `capabilityKey` with its `displayName`. The response is governed by
`schemas/canonical/ui-vocabulary-v1.json`; pages must not maintain a second
capability-label list.

Start from a copy of the shipped `config/` and change the registries you own.

In your own config, paths use `${ORG_DIR}`. A path written as
`${WORKER_DIR}/starter-company/...` resolves into the image's shipped company and
silently reads the wrong organization's documents; the knowledge-quality check
reports those as missing sources once the file is not there at all.

Keep whatever your knowledge registry references inside the mounted directory. The
image deliberately excludes the plan layer, so a config entry pointing at
`${WORKER_DIR}/plan/...` resolves to nothing in a container.

A published image that carries an organization's data distributes that data to
everyone who pulls it. The image ships the synthetic starter company; installations
mount over it.

Never make an installation work by writing your files into `starter-company/`. It is
product data, distributed to every user, and it is under version control in a public
repository. `tests/test_org_data_boundary.py` fails if the shipped company is
overwritten.

## Signing In To A Container

Development identity is accepted only from a loopback address. A published
container port never presents one: Docker forwards the connection, so the server
sees the container network gateway. Every browser reaching a containerised server
is therefore refused on its first API call, whatever the host-side binding was.

Issue a session where the server runs, and paste it into the sign-in page:

```sh
docker exec -i <container> bin/present-control-plane session \
  --actor <user-id> --role admin
```

Open `/auth/login`, paste the `accessToken`, and the browser is signed in. The
token is the credential and is verified on arrival exactly as a bearer token is,
so the page grants nothing on its own; issuing one requires access to the
container. Sign-ins and failures are recorded in the auth audit.

This is a development path. In `oidc-required` mode the page does not exist and
`/auth/login` is the provider redirect.

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
