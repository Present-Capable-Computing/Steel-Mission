# Steel Mission Control Plane Protocol

Status: experimental v0.1.

Steel Mission includes a protocol-shaped contract for governed agentic delivery. It should be described as the Steel Mission Control Plane Protocol, experimental v0.1, until the contract has a stable specification, conformance tests, and at least one independent implementation.

## Purpose

The protocol defines how an agent delivery system requests work, resolves context, checks policy, obtains approval, executes guarded actions, records evidence, and closes delivery missions.

## Current Contract Surfaces

- runtime profile registry;
- model role registry;
- snapshot policy;
- mission control record;
- mission audit event;
- mission evidence record;
- guarded control-plane execution request;
- authenticated private-runner request and attested result;
- normalized GitHub, Slack, and Jira workflow connector event;
- worker status and doctor payloads;
- workflow admission and result records;
- proof bundle and SIEM JSONL export.

## Lifecycle

The delivery lifecycle is:

1. understand;
2. plan;
3. modify;
4. build;
5. test;
6. inspect;
7. repair;
8. PR;
9. deploy;
10. close.

## Guarantees In Alpha v0.1

- Jobs resolve a bounded snapshot before model execution.
- Runtime profiles bind providers without changing organizational capabilities.
- Guarded execution checks policy before action.
- Executable delivery/provider/command-adapter actions cross the private-runner boundary; request and result integrity are verified on both sides.
- Signed workflow ingress is replay/idempotency protected and retains origin/thread context for bidirectional updates.
- Unsafe actions can be blocked before execution.
- Approval gates can stop sensitive phases.
- Mission evidence is content-hashed and hash-chained.
- External signing can be required by policy.

## Not Yet A Stable Standard

Do not call this a stable public standard yet. The correct public wording for Alpha v0.1 is:

> Steel Mission is an Agent Delivery Plane with an experimental v0.1 control-plane protocol.

The protocol can become a public standard later when the schemas, conformance tests, compatibility rules, and extension points are versioned independently from the product.
