## What this changes

<!-- Describe every commit in the branch, not only the last thing you worked on.
     Run `git log --oneline main..HEAD` and `git diff --stat main..HEAD` first:
     a reviewer decides how carefully to read from this description, so a title
     that names one part of a larger change gets the rest merged unread. -->

Closes #

## Evidence

<!-- What was run, and what it said. Paste the counts. -->

- [ ] `make test`; result:
- [ ] Tests added for the behaviour this changes, and watched to fail without the fix
- [ ] Docs or schemas updated if the contract moved

## Surfaces touched

- [ ] Authentication, authorization, or session handling
- [ ] A network-listening service, or its bind address
- [ ] Subprocess or container execution
- [ ] Authority-owned schemas (`schemas/canonical/`, `schemas/schema-registry.json`)
- [ ] None of the above

<!-- Anything ticked above wants a security review before merge, and a schema
     change may need ratification separately from this merge. -->

## Reversibility

<!-- How this is undone if it is wrong in production. If it cannot be undone
     cleanly, say so here rather than discovering it later. -->
