# Steel Mission Architecture Notes

Steel Mission is a software delivery control plane, not another source-of-truth application. Repositories, issue trackers, identity providers, chat systems, CI/CD systems, and security tools remain authoritative in their own domains while Steel Mission coordinates guarded work across them.

`PRJ-0001` Durable Core replaces process-local and unlocked-file state with one transactional storage interface. SQLite is the default; PostgreSQL is the high-availability option. The database is the queue, streams hold no durable truth, and one state machine drives command-line, daemon, mission, runner, and connector paths.

A remote pull-runner dials out from customer infrastructure, claims portable job specifications, materializes pinned inputs, executes in a digest-pinned sandbox, and returns a signed result bound to the job, runner, image, and inputs. Connectors use transactional inbox and outbox records with explicit deduplication, retry, and dead-letter behavior.

Architecture decisions must keep planning, policy, execution, state, evidence, approval, and external delivery boundaries distinct and testable.
