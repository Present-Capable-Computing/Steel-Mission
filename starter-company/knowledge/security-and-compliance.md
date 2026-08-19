# Steel Mission Security and Compliance Notes

Human and organizational authority must be enforced before consequential execution. Identity, role, organization membership, assigned capabilities, policy, and separation of duties are server-owned inputs; a model or connector cannot grant itself broader authority.

Remote work uses mutually authenticated transport, portable pinned inputs, digest-pinned execution images, just-in-time secret references, fence tokens, and signed results. Mission evidence is signed, hash-chained, exportable, and distinct from operational status. Customer-controlled KMS, Vault Transit, HSM, or an equivalent signing service can replace the local signer.

Steel Mission maps evidence to SOC 2, ISO 27001, and ISO 42001 review needs, but mappings are not certifications. Degraded connector guarantees, local-development execution, stale knowledge, missing owners, and unverified claims remain visible rather than being promoted into assurance.
