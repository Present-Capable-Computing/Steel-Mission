"""Small stdlib validator for the canonical worker contract schemas.

It implements the JSON Schema subset used by the Mini's v1 status,
verification, and task-contract documents. The Mini remains the final schema
authority and validates every returned artifact independently.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / name).read_text())


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(expected, True)


def _errors_for(instance: Any, schema: dict[str, Any], root: dict[str, Any]) -> list[str]:
    """Whether a subschema accepts the instance, without reporting.

    Combinators need to ask "does this match?" recursively, using the same
    rules as everything else. Answering that with a bespoke matcher per
    keyword is how gaps accumulate: an `if` was once evaluated by a weak
    required/const check, an unconditional `allOf` was skipped because the
    loop only looked for `if`, and `not` was understood only as a scalar
    const exclusion. There is one evaluator now, and every combinator uses it.
    """
    probe: list[str] = []
    _validate(instance, schema, "$", probe, root)
    return probe


def _resolve(schema: dict[str, Any], root: dict[str, Any], path: str,
             errors: list[str]) -> dict[str, Any] | None:
    """Follow a local `$ref` to the definition it names.

    Without this the validator silently accepted whatever a `$ref` pointed at:
    task-contract-v2 declares `build` as `{"$ref": "#/$defs/buildCommands"}`,
    so the entire build command set -- which runs with an 86400s timeout --
    went unchecked, and a shell string in place of an argv array passed.
    Only in-document pointers are supported; a remote or unresolvable ref is
    reported rather than skipped, because silently validating nothing is how
    this defect stayed invisible.
    """
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    if not ref.startswith("#/"):
        errors.append(f"{path}: non-local $ref {ref!r} cannot be resolved")
        return None
    target: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or token not in target:
            errors.append(f"{path}: $ref {ref!r} does not resolve")
            return None
        target = target[token]
    if not isinstance(target, dict):
        errors.append(f"{path}: $ref {ref!r} does not name a schema")
        return None
    return {**{k: v for k, v in schema.items() if k != "$ref"}, **target}


def _validate(instance: Any, schema: dict[str, Any], path: str, errors: list[str],
              root: dict[str, Any] | None = None) -> None:
    root = schema if root is None else root
    schema = _resolve(schema, root, path, errors)
    if schema is None:
        return
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _is_type(instance, expected_type):
        errors.append(f"{path}: expected {expected_type}, got {type(instance).__name__}")
        return
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            errors.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(instance):
                _validate(value, item_schema, f"{path}[{index}]", errors, root)
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}.{key}: missing required key")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in set(instance) - set(properties):
                errors.append(f"{path}.{key}: additional property is not allowed")
        for key, subschema in properties.items():
            if key in instance:
                _validate(instance[key], subschema, f"{path}.{key}", errors, root)

    # Combinators, evaluated generally and recursively rather than by
    # keyword-specific exception. Two gaps reached production that way: an
    # unconditional `allOf` was skipped because the loop only looked for
    # `if`, leaving task-contract-v2 verification command items unvalidated,
    # and a nested `not`/`anyOf` was invisible, so a failed probe carrying a
    # guessed packageId was accepted.
    for subschema in schema.get("allOf", []) or []:
        if not isinstance(subschema, dict):
            continue
        if "if" not in subschema:
            _validate(instance, subschema, path, errors, root)
            continue
        taken = "then" if not _errors_for(instance, subschema["if"], root) else "else"
        branch = subschema.get(taken)
        if isinstance(branch, dict):
            _validate(instance, branch, path, errors, root)

    if isinstance(schema.get("if"), dict):
        taken = "then" if not _errors_for(instance, schema["if"], root) else "else"
        branch = schema.get(taken)
        if isinstance(branch, dict):
            _validate(instance, branch, path, errors, root)

    branches = schema.get("anyOf")
    if isinstance(branches, list) and branches:
        if all(_errors_for(instance, b, root) for b in branches if isinstance(b, dict)):
            errors.append(f"{path}: matches no anyOf branch")

    branches = schema.get("oneOf")
    if isinstance(branches, list) and branches:
        matched = sum(1 for b in branches if isinstance(b, dict) and not _errors_for(instance, b, root))
        if matched != 1:
            errors.append(f"{path}: matches {matched} of {len(branches)} oneOf branches, expected exactly 1")

    excluded = schema.get("not")
    if isinstance(excluded, dict) and not _errors_for(instance, excluded, root):
        errors.append(f"{path}: must not match the excluded subschema")


def validate(instance: dict[str, Any], schema_name: str) -> list[str]:
    errors: list[str] = []
    _validate(instance, load_schema(schema_name), "$", errors)
    return errors
