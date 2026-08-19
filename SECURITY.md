# Security Policy

Steel Mission is a public alpha. Do not connect it to production secrets, production repositories, or regulated customer data without a reviewed deployment plan.

## Supported Version

| Version | Status |
| --- | --- |
| v0.1.x-alpha | Public alpha |

## Reporting Security Issues

For now, report security issues through a private GitHub security advisory on the Steel Mission repository when available. If private advisories are not enabled for your account, open a minimal public issue that says a security report is available and avoid posting exploit details, secrets, logs, customer data, or credentials.

## Security Design

Steel Mission is designed around these boundaries:

- executable actions enter through the guarded control plane;
- the control plane authenticates private-runner requests and verifies signed results;
- the production runner uses an ephemeral non-root container without a runtime socket, with a read-only root filesystem, dropped capabilities, resource limits, a workspace-only bind mount, and default-deny network access;
- policy is evaluated before execution;
- unsafe commands can be blocked before they run;
- high-risk phases can require approval;
- evidence is content-hashed and hash-chained;
- production policy can require an external signer or customer KMS-equivalent signer;
- evidence writing fails closed when required signing is unavailable.
- GitHub, Slack, and Jira ingress fails closed without a valid signature; Slack also enforces a five-minute replay window and all workflow deliveries are idempotent.

## Operational Guidance

Before production use:

- run inside customer infrastructure or a private cloud environment;
- build `Dockerfile.private-runner`, set the policy runner mode to `container`, and confirm `bin/present-private-runner status` reports `productionEligible: true`;
- configure OIDC/JWKS or an equivalent identity boundary;
- use a customer-controlled external signer;
- keep secrets out of repository files and starter data;
- configure SIEM export and evidence retention;
- use least-privilege GitHub, Slack, and Jira tokens, rotate webhook secrets, and place Jira behind a signing gateway that adds the documented HMAC header;
- require approvals for production changes and sensitive operations;
- run release validation before deploying changes.

## Not Yet Included In Core Alpha

- hosted vulnerability disclosure process;
- managed identity lifecycle and federation operations;
- managed KMS/HSM integrations;
- multi-organization fleet policy and upgrade operations;
- remote private-worker scheduling and turnkey cluster network policy;
- formal third-party security audit;
- certified SOC 2, ISO 27001, or ISO 42001 evidence pack.
