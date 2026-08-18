# Conformance fixtures

Copied verbatim from the Mini's `schemas/fixtures/`, the declared schema
authority (`SCHEMA_AUTHORITY = present-control`). Do not edit them here: a
fixture that disagrees with the worker is evidence of drift to reconcile with
the Mini, never a file to adjust locally until it passes.

Only fixtures whose schema this worker vendors are copied. The Mini's
`trust-bundle-v1` and `admission-context-v1` fixtures are omitted because
nothing worker-side validates those contracts.

`tests/test_worker.py::test_worker_validator_agrees_with_the_schema_authority`
asserts every valid fixture is accepted and every invalid one rejected.
