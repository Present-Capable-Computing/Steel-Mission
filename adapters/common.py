"""Shared helpers for Steel Mission adapters.

Stdlib only. This tool must keep working even if a venv goes stale, since
this machine is ephemeral compute -- it can disappear and reappear with no
guarantee anything beyond the system Python survived.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_WORKER_DIR = Path(__file__).resolve().parents[1]
PRESENT_DEV = Path(os.environ.get("STEEL_MISSION_DEV") or os.environ.get("PRESENT_DEV", DEFAULT_WORKER_DIR.parent))
WORKER_DIR = Path(os.environ.get("STEEL_MISSION_WORKER_DIR") or os.environ.get("PRESENT_WORKER_DIR", DEFAULT_WORKER_DIR))
LOGS_DIR = Path(os.environ.get("STEEL_MISSION_LOGS_DIR") or os.environ.get("PRESENT_LOGS_DIR", WORKER_DIR / "logs"))
TASKS_DIR = Path(os.environ.get("STEEL_MISSION_TASKS_DIR") or os.environ.get("PRESENT_TASKS_DIR", WORKER_DIR / "tasks"))
WORKTREES_DIR = Path(os.environ.get("STEEL_MISSION_WORKTREES_DIR") or os.environ.get("PRESENT_WORKTREES_DIR", WORKER_DIR / "worktrees"))
REPOS_DIR = Path(os.environ.get("STEEL_MISSION_REPOS_DIR") or os.environ.get("PRESENT_REPOS_DIR", WORKER_DIR / "repos"))
TEST_RESULTS_DIR = Path(os.environ.get("STEEL_MISSION_TEST_RESULTS_DIR") or os.environ.get("PRESENT_TEST_RESULTS_DIR", WORKER_DIR / "test-results"))
JOBS_DIR = Path(os.environ.get("STEEL_MISSION_JOBS_DIR") or os.environ.get("PRESENT_JOBS_DIR", WORKER_DIR / "jobs"))

DEFAULT_REPO = Path(os.environ.get("STEEL_MISSION_DEFAULT_REPO", WORKER_DIR))

SCHEMA_AUTHORITY = "present-control"
MAX_BUNDLE_BYTES = 1024 * 1024
POLICY_PACK_REGISTRY_PATH = WORKER_DIR / "schemas" / "policy-packs.json"
SCHEMA_REGISTRY_PATH = WORKER_DIR / "schemas" / "schema-registry.json"
WORKER_POOL_TRUST_POLICY_PATH = WORKER_DIR / "schemas" / "worker-pool-trust-policy.json"

WORKFLOW_CAPABILITIES = {
    "plan",
    "build",
    "detached-build",
    "candidate",
    "candidate-fix",
    "fix",
    "code-review",
    "review",
    "security-review",
    "adversarial",
    "deterministic-verify",
    "verify",
    "coordination-report",
}
WORKFLOW_NODE_ID_RE = r"[a-z][a-z0-9-]{0,63}"
WORKFLOW_NODE_STATUSES = {"SUCCEEDED", "WAITING", "FAILED", "BLOCKED"}

# Tasks minted by the advisory DC13 chat so it can ask a question. They are a
# by-product of asking, never pipeline work, and they accumulate one per
# question. Declared here so the producer that writes it and the snapshot that
# filters on it cannot drift apart. Identity is the contract's producer, never
# the task-id range: DEV-999996/999997 are real pipeline tasks carrying verify
# PASS evidence, and an id-range heuristic would silently evict them.
ADVISORY_TASK_PRODUCER = "steel-mission-chat-local"
SAFE_PATH = os.pathsep.join(
    str(path)
    for path in (
        Path.home() / ".local" / "bin",
        Path.home() / ".npm-global" / "bin",
        Path.home() / ".claude" / "local",
        Path.home() / "Library" / "pnpm",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    )
)


class TaskBundleError(ValueError):
    """A task bundle is malformed or attempts to exceed its narrow contract."""


class SchemaValidationError(TaskBundleError):
    """A schema boundary rejected a payload and carries a canonical report."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(report.get("reason", "schema validation failed"))


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def which(binary: str) -> str | None:
    return shutil.which(binary, path=SAFE_PATH)


def execution_env() -> dict[str, str]:
    """Return the login-independent environment used by forced SSH verbs."""
    return {**os.environ, "PATH": SAFE_PATH}


def run(cmd: list[str], timeout: int = 30, **kwargs) -> subprocess.CompletedProcess:
    env = kwargs.pop("env", None) or execution_env()
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        **kwargs,
    )


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_policy_pack_registry() -> dict[str, Any]:
    try:
        registry = json.loads(POLICY_PACK_REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskBundleError(f"policy pack registry is unavailable: {exc}") from exc
    if not isinstance(registry, dict) or registry.get("schemaVersion") != 1:
        raise TaskBundleError("policy pack registry must be schemaVersion 1")
    packs = registry.get("packs")
    if not isinstance(packs, list):
        raise TaskBundleError("policy pack registry.packs must be an array")
    return registry


def policy_pack_registry_entry(pack_id: str, version: int) -> dict[str, Any] | None:
    registry = load_policy_pack_registry()
    for pack in registry["packs"]:
        if isinstance(pack, dict) and pack.get("id") == pack_id and pack.get("version") == version:
            return pack
    return None


def policy_pack_registry_hash(pack_id: str, version: int) -> str:
    entry = policy_pack_registry_entry(pack_id, version)
    if entry is None:
        raise TaskBundleError(f"policy pack {pack_id}@{version} is not registered")
    return canonical_hash(entry)


def load_worker_pool_trust_policy() -> dict[str, Any]:
    try:
        policy = json.loads(WORKER_POOL_TRUST_POLICY_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskBundleError(f"worker pool trust policy is unavailable: {exc}") from exc
    validate_with_schema(
        policy,
        "canonical/worker-pool-trust-policy-v1.json",
        "worker pool trust policy",
        validation_point="broker-policy-admission",
        artifact_kind="worker-pool-trust-policy",
    )
    return policy


def _load_schema_registry_unchecked() -> dict[str, Any]:
    try:
        registry = json.loads(SCHEMA_REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskBundleError(f"schema registry is unavailable: {exc}") from exc
    if not isinstance(registry, dict):
        raise TaskBundleError("schema registry must be a JSON object")
    return registry


def _schema_registry_hash_unchecked(registry: dict[str, Any]) -> str:
    return canonical_hash(registry)


def schema_registry_hash() -> str:
    return _schema_registry_hash_unchecked(load_schema_registry())


def load_schema_registry() -> dict[str, Any]:
    registry = _load_schema_registry_unchecked()
    from adapters import schema_check

    errors = schema_check.validate(registry, "canonical/schema-registry-v1.json")
    if errors:
        raise SchemaValidationError(schema_validation_error_payload(
            "canonical/schema-registry-v1.json",
            "schema registry",
            errors,
            validation_point="registry-admission",
            artifact_kind="schema-registry",
        ))
    return registry


def schema_registry_entry_for_schema(schema_name: str) -> dict[str, Any] | None:
    normalized = schema_name.removeprefix("canonical/")
    for entry in load_schema_registry()["schemas"]:
        if isinstance(entry, dict) and entry.get("schemaFile") == normalized:
            return entry
    return None


def schema_registry_entry_for_artifact(stage: str) -> dict[str, Any] | None:
    for entry in load_schema_registry()["schemas"]:
        if not isinstance(entry, dict):
            continue
        stages = entry.get("artifactStages")
        if isinstance(stages, list) and stage in stages:
            return entry
    return None


def schema_name_for_task_artifact(stage: str) -> str | None:
    entry = schema_registry_entry_for_artifact(stage)
    schema_file = entry.get("schemaFile") if isinstance(entry, dict) else None
    return f"canonical/{schema_file}" if isinstance(schema_file, str) else None


def _schema_error_items(errors: list[str]) -> list[dict[str, str]]:
    items = []
    for error in errors:
        path, _, message = error.partition(": ")
        items.append({
            "path": path or "$",
            "message": message or error,
        })
    return items


def schema_validation_error_payload(schema_name: str, label: str, errors: list[str], *,
                                    validation_point: str = "schema-validation",
                                    artifact_kind: str | None = None,
                                    artifact_stage: str | None = None,
                                    task_id: str | None = None) -> dict[str, Any]:
    entry = None
    registry_hash = None
    try:
        normalized = schema_name.removeprefix("canonical/")
        registry = _load_schema_registry_unchecked()
        registry_hash = _schema_registry_hash_unchecked(registry)
        schemas = registry.get("schemas")
        if isinstance(schemas, list):
            for candidate in schemas:
                if isinstance(candidate, dict) and candidate.get("schemaFile") == normalized:
                    entry = candidate
                    break
    except TaskBundleError:
        entry = None
    error_items = _schema_error_items(errors)
    return {
        "schemaVersion": 1,
        **({"taskId": task_id} if task_id else {}),
        "producedAt": utc_now(),
        "producer": "present-schema-validator",
        "status": "SCHEMA_VALIDATION_FAILED",
        "schemaName": schema_name,
        "schemaId": entry.get("id") if isinstance(entry, dict) else schema_name,
        **({"schemaRegistryHash": registry_hash} if registry_hash else {}),
        "validationPoint": validation_point,
        "artifactKind": artifact_kind or (entry.get("artifactKind") if isinstance(entry, dict) else label),
        **({"artifactStage": artifact_stage} if artifact_stage else {}),
        "label": label,
        "blocking": True,
        "errors": error_items,
        "failingJsonPaths": [item["path"] for item in error_items],
        "reason": f"{label} failed schema validation: {'; '.join(errors)}",
    }


def validate_with_schema(payload: dict[str, Any], schema_name: str, label: str, *,
                         validation_point: str = "schema-validation",
                         artifact_kind: str | None = None,
                         artifact_stage: str | None = None,
                         task_id: str | None = None,
                         log_failure: bool = False) -> None:
    from adapters import schema_check

    entry = schema_registry_entry_for_schema(schema_name)
    if entry is None:
        raise TaskBundleError(f"schema {schema_name!r} is not registered in schema registry")
    if entry.get("lifecycle") != "active":
        raise TaskBundleError(f"schema {schema_name!r} is not active in schema registry")
    errors = schema_check.validate(payload, schema_name)
    if errors:
        report = schema_validation_error_payload(
            schema_name,
            label,
            errors,
            validation_point=validation_point,
            artifact_kind=artifact_kind,
            artifact_stage=artifact_stage,
            task_id=task_id,
        )
        if log_failure and task_id:
            record_stage(task_id, "schema-validation", report, role=validation_point)
        raise SchemaValidationError(report)


def credential_missing(provider: str) -> dict[str, Any]:
    return {"status": "CREDENTIAL_MISSING", "provider": provider, "retryable": False}


def credential_probe_failed(provider: str, reason: str) -> dict[str, Any]:
    """The probe could not determine authentication -- infrastructure, not absence.

    Reporting this as CREDENTIAL_MISSING would state a fact about the
    credential that the worker did not establish. Retryable: the same probe
    from a session that can reach the credential store may well answer.
    """
    return {"status": "CREDENTIAL_PROBE_FAILED", "provider": provider,
            "reason": reason or "credential probe failed without a reason", "retryable": True}


def glimmer_not_ready(reason: str) -> dict[str, Any]:
    return {"status": "GLIMMER_NOT_READY", "provider": "glimmer", "reason": reason, "retryable": True}


def test_contract_disputed(reason: str) -> dict[str, Any]:
    return {"status": "TEST_CONTRACT_DISPUTED", "reason": reason, "retryable": False}


def canonical_envelope(task_id: str, producer: str, *, mocked: bool, commit: str | None = None) -> dict[str, Any]:
    provenance: dict[str, Any] = {"source": "mock-adapter" if mocked else "worker"}
    host = os.uname().nodename
    if host:
        provenance["host"] = host
    if commit:
        provenance["commit"] = commit
    return {
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": utc_now(),
        "producer": producer,
        "mock": mocked,
        "provenance": provenance,
    }


def load_task_contract(task_id: str) -> dict[str, Any] | None:
    path = task_contract_path(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_requirement(task_id: str) -> str:
    path = TASKS_DIR / task_id / "requirement.md"
    return path.read_text() if path.exists() else ""


# `pd-task new` scaffolds every task with this body and states, in the body
# itself, that a human fills it in. Nothing enforced that. A placeholder that
# reaches a generative verb delegates requirement invention to the model --
# on 2026-08-16 that produced 325 lines of content authored from a task title.
REQUIREMENT_PLACEHOLDER_MARKERS = (
    "_Infrastructure task. State the requirement here._",
    "The control plane never invents requirements.",
)


def requirement_defect(task_id: str) -> str | None:
    """Return why the requirement cannot drive a generative verb, or None.

    Argv-driven verbs (build, verify) never read the requirement text and are
    deliberately not gated on it: infrastructure acceptance runs legitimately
    carry a placeholder because their assertions live in the contract.
    """
    path = TASKS_DIR / task_id / "requirement.md"
    if not path.exists():
        return "requirement.md is absent"
    text = path.read_text()
    for marker in REQUIREMENT_PLACEHOLDER_MARKERS:
        if marker in text:
            return "requirement.md still carries the pd-task placeholder; a human has not stated the requirement"
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    if not body.strip():
        return "requirement.md has no body beyond its title"
    return None


def requirement_missing(task_id: str, reason: str) -> dict[str, Any]:
    return {"status": "REQUIREMENT_MISSING", "task_id": task_id, "reason": reason, "retryable": False}


def task_artifact_path(task_id: str, stage: str) -> Path:
    return TASKS_DIR / task_id / stage / f"{stage}.json"


def write_task_artifact(task_id: str, stage: str, payload: dict[str, Any]) -> Path:
    schema_name = schema_name_for_task_artifact(stage)
    if schema_name:
        entry = schema_registry_entry_for_artifact(stage)
        validate_with_schema(
            payload,
            schema_name,
            f"task artifact {stage}",
            validation_point="worker-write",
            artifact_kind=entry.get("artifactKind") if isinstance(entry, dict) else stage,
            artifact_stage=stage,
            task_id=task_id,
            log_failure=True,
        )
    path = task_artifact_path(task_id, stage)
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def load_task_artifact(task_id: str, stage: str) -> dict[str, Any] | None:
    path = task_artifact_path(task_id, stage)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def job_dir(task_id: str, stage: str) -> Path:
    return JOBS_DIR / f"{task_id}-{stage}"


def write_job_document(task_id: str, stage: str, name: str, payload: dict[str, Any]) -> Path:
    path = job_dir(task_id, stage) / f"{name}.json"
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def load_job_document(task_id: str, stage: str, name: str) -> dict[str, Any] | None:
    path = job_dir(task_id, stage) / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def task_contract_path(task_id: str) -> Path:
    contract = TASKS_DIR / task_id / "contract.json"
    if contract.exists():
        return contract
    nested = TASKS_DIR / task_id / "task.json"
    if nested.exists():
        return nested
    flat = TASKS_DIR / f"{task_id}.json"
    if flat.exists():
        return flat
    return nested


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate_command(command: Any, index: int, *, max_timeout: int = 900) -> None:
    if not isinstance(command, dict):
        raise TaskBundleError(f"verification.commands[{index}] must be an object")
    allowed = {"name", "argv", "expectedExitCode", "timeoutSeconds"}
    extra = set(command) - allowed
    if extra:
        raise TaskBundleError(f"verification.commands[{index}] has unknown fields: {sorted(extra)}")
    if not isinstance(command.get("name"), str) or not command["name"].strip():
        raise TaskBundleError(f"verification.commands[{index}].name must be non-empty")
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > 32:
        raise TaskBundleError(f"verification.commands[{index}].argv must contain 1..32 arguments")
    if any(not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 2048 for arg in argv):
        raise TaskBundleError(f"verification.commands[{index}].argv contains an invalid argument")
    expected = command.get("expectedExitCode")
    if not isinstance(expected, int) or isinstance(expected, bool) or not 0 <= expected <= 255:
        raise TaskBundleError(f"verification.commands[{index}].expectedExitCode must be 0..255")
    timeout = command.get("timeoutSeconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= max_timeout:
        raise TaskBundleError(f"commands[{index}].timeoutSeconds must be 1..{max_timeout}")


def _validate_command_set(section: Any, name: str, *, maximum: int, max_timeout: int) -> None:
    if not isinstance(section, dict):
        raise TaskBundleError(f"contract.{name} must be an object")
    if set(section) != {"target", "commands"}:
        raise TaskBundleError(f"contract.{name} has an unexpected shape")
    if section.get("target") not in {"worker", "present-repository"}:
        raise TaskBundleError(f"contract.{name}.target is not allowlisted")
    commands = section.get("commands")
    if not isinstance(commands, list) or not 1 <= len(commands) <= maximum:
        raise TaskBundleError(f"contract.{name}.commands must contain 1..{maximum} commands")
    for index, command in enumerate(commands):
        _validate_command(command, index, max_timeout=max_timeout)


def validate_task_contract(contract: Any, task_id: str) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise TaskBundleError("contract must be an object")
    version = contract.get("schemaVersion")
    if version not in {1, 2, 3} or contract.get("taskId") != task_id:
        raise TaskBundleError("contract schemaVersion/taskId does not match the invocation")
    required = {"schemaVersion", "taskId", "producedAt", "producer", "provenance", "verification"}
    if version == 2:
        required.add("build")
    if version == 3:
        required.add("workflow")
        required.add("policyPack")
    missing = required - set(contract)
    if missing:
        raise TaskBundleError(f"contract is missing required fields: {sorted(missing)}")
    if not isinstance(contract.get("producedAt"), str) or not contract["producedAt"]:
        raise TaskBundleError("contract.producedAt must be non-empty")
    if not isinstance(contract.get("producer"), str) or not contract["producer"].strip():
        raise TaskBundleError("contract.producer must be non-empty")
    provenance = contract.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("source") != "control-plain":
        raise TaskBundleError("contract.provenance.source must be control-plain")
    if set(provenance) - {"source", "host", "commit"}:
        raise TaskBundleError("contract.provenance has unknown fields")
    allowed = {"schemaVersion", "taskId", "producedAt", "producer", "provenance", "verification"}
    if version in {2, 3} and "build" in contract:
        allowed.add("build")
    if version == 3:
        allowed.add("workflow")
        allowed.add("policyPack")
    extra = set(contract) - allowed
    if extra:
        raise TaskBundleError(f"contract has unknown fields: {sorted(extra)}")
    _validate_command_set(contract.get("verification"), "verification", maximum=16, max_timeout=900)
    if version in {2, 3} and "build" in contract:
        _validate_command_set(contract.get("build"), "build", maximum=64, max_timeout=86400)
    if version == 3 and "policyPack" in contract:
        _validate_policy_pack(contract.get("policyPack"))
    return contract


def _validate_unique_string_array(value: Any, path: str, *, maximum: int = 32,
                                  pattern: str | None = None, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(item, str) for item in value):
        raise TaskBundleError(f"{path} must be a string array of at most {maximum} items")
    if len(value) != len(set(value)):
        raise TaskBundleError(f"{path} contains duplicates")
    for item in value:
        if pattern is not None and re.fullmatch(pattern, item) is None:
            raise TaskBundleError(f"{path} item {item!r} must match {pattern}")
        if allowed is not None and item not in allowed:
            raise TaskBundleError(f"{path} item {item!r} is unsupported")
    return value


def _validate_policy_pack(policy_pack: Any) -> dict[str, Any]:
    if not isinstance(policy_pack, dict):
        raise TaskBundleError("contract.policyPack must be an object")
    if set(policy_pack) - {
        "id",
        "version",
        "registryHash",
        "riskClass",
        "requiredCapabilities",
        "requiredEvidenceRoles",
        "forbiddenCapabilities",
        "minVerifierCount",
        "acceptancePolicy",
        "retentionPolicy",
    }:
        raise TaskBundleError("contract.policyPack has unknown fields")
    pack_id = policy_pack.get("id")
    if not isinstance(pack_id, str) or re.fullmatch(WORKFLOW_NODE_ID_RE, pack_id) is None:
        raise TaskBundleError(f"contract.policyPack.id must match {WORKFLOW_NODE_ID_RE}")
    version = policy_pack.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TaskBundleError("contract.policyPack.version must be a positive integer")
    registry_hash = policy_pack.get("registryHash")
    if not isinstance(registry_hash, str) or re.fullmatch(r"[a-f0-9]{64}", registry_hash) is None:
        raise TaskBundleError("contract.policyPack.registryHash must be a sha256 hex digest")
    required_fields = {
        "id",
        "version",
        "registryHash",
        "riskClass",
        "requiredCapabilities",
        "requiredEvidenceRoles",
        "forbiddenCapabilities",
        "minVerifierCount",
    }
    missing = required_fields - set(policy_pack)
    if missing:
        raise TaskBundleError(f"contract.policyPack is missing required fields: {sorted(missing)}")
    risk_class = policy_pack.get("riskClass")
    if risk_class not in {"low", "normal", "high", "critical"}:
        raise TaskBundleError("contract.policyPack.riskClass must be low, normal, high, or critical")
    required = _validate_unique_string_array(
        policy_pack.get("requiredCapabilities"), "contract.policyPack.requiredCapabilities",
        allowed=WORKFLOW_CAPABILITIES)
    forbidden = _validate_unique_string_array(
        policy_pack.get("forbiddenCapabilities"), "contract.policyPack.forbiddenCapabilities",
        allowed=WORKFLOW_CAPABILITIES)
    overlap = sorted(set(required) & set(forbidden))
    if overlap:
        raise TaskBundleError(f"contract.policyPack cannot both require and forbid capabilities: {overlap}")
    _validate_unique_string_array(
        policy_pack.get("requiredEvidenceRoles"), "contract.policyPack.requiredEvidenceRoles",
        pattern=WORKFLOW_NODE_ID_RE, maximum=32)
    min_verifier_count = policy_pack.get("minVerifierCount")
    if (not isinstance(min_verifier_count, int) or isinstance(min_verifier_count, bool)
            or not 0 <= min_verifier_count <= 8):
        raise TaskBundleError("contract.policyPack.minVerifierCount must be 0..8")
    acceptance_policy = policy_pack.get("acceptancePolicy")
    if acceptance_policy is not None:
        if not isinstance(acceptance_policy, dict) or set(acceptance_policy) - {
            "mode", "minVerifierCount", "requiredEvidenceRoles", "allowMockEvidence",
        }:
            raise TaskBundleError("contract.policyPack.acceptancePolicy has unknown fields")
        if acceptance_policy.get("mode") not in {"single-deterministic-verifier", "quorum", "unanimous"}:
            raise TaskBundleError("contract.policyPack.acceptancePolicy.mode is invalid")
        policy_min = acceptance_policy.get("minVerifierCount")
        if not isinstance(policy_min, int) or isinstance(policy_min, bool) or not 0 <= policy_min <= 8:
            raise TaskBundleError("contract.policyPack.acceptancePolicy.minVerifierCount must be 0..8")
        if "requiredEvidenceRoles" in acceptance_policy:
            _validate_unique_string_array(
                acceptance_policy.get("requiredEvidenceRoles"),
                "contract.policyPack.acceptancePolicy.requiredEvidenceRoles",
                pattern=WORKFLOW_NODE_ID_RE,
                maximum=32,
            )
        if "allowMockEvidence" in acceptance_policy and not isinstance(acceptance_policy["allowMockEvidence"], bool):
            raise TaskBundleError("contract.policyPack.acceptancePolicy.allowMockEvidence must be boolean")
    retention = policy_pack.get("retentionPolicy")
    if retention is not None:
        if not isinstance(retention, dict) or set(retention) - {
            "class", "ttlSeconds", "redactionRequired", "purgeOnCompletion",
        }:
            raise TaskBundleError("contract.policyPack.retentionPolicy has unknown fields")
        if retention.get("class") not in {"ephemeral", "standard", "audit", "regulated"}:
            raise TaskBundleError("contract.policyPack.retentionPolicy.class is invalid")
        ttl = retention.get("ttlSeconds")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
            raise TaskBundleError("contract.policyPack.retentionPolicy.ttlSeconds must be non-negative")
        for key in ("redactionRequired", "purgeOnCompletion"):
            if key in retention and not isinstance(retention[key], bool):
                raise TaskBundleError(f"contract.policyPack.retentionPolicy.{key} must be boolean")
    registry_entry = policy_pack_registry_entry(pack_id, version)
    if registry_entry is None:
        raise TaskBundleError(f"contract.policyPack {pack_id}@{version} is not in the registry")
    expected_hash = canonical_hash(registry_entry)
    if registry_hash != expected_hash:
        raise TaskBundleError(
            f"contract.policyPack.registryHash does not match registry entry {pack_id}@{version}")
    claimed_definition = {key: value for key, value in policy_pack.items() if key != "registryHash"}
    if claimed_definition != registry_entry:
        raise TaskBundleError(f"contract.policyPack {pack_id}@{version} differs from the registered policy")
    return policy_pack


def validate_workflow_contract(contract: Any, task_id: str) -> dict[str, Any]:
    contract = validate_task_contract(contract, task_id)
    if contract.get("schemaVersion") != 3:
        raise TaskBundleError("workflow requires task-contract-v3 with a workflow graph")
    workflow = contract.get("workflow")
    if not isinstance(workflow, dict):
        raise TaskBundleError("contract.workflow must be an object")
    if set(workflow) - {"workflowId", "maxParallel", "nodes"}:
        raise TaskBundleError("contract.workflow has unknown fields")
    workflow_id = workflow.get("workflowId")
    if workflow_id is not None and (not isinstance(workflow_id, str) or not workflow_id.strip() or len(workflow_id) > 80):
        raise TaskBundleError("contract.workflow.workflowId must be a non-empty string when present")
    max_parallel = workflow.get("maxParallel", 1)
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or not 1 <= max_parallel <= 8:
        raise TaskBundleError("contract.workflow.maxParallel must be 1..8")
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32:
        raise TaskBundleError("contract.workflow.nodes must contain 1..32 nodes")

    ids: set[str] = set()
    edges: dict[str, list[str]] = {}
    node_specs: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise TaskBundleError(f"contract.workflow.nodes[{index}] must be an object")
        if set(node) - {"id", "capability", "dependsOn", "inputs", "policy", "outputs"}:
            raise TaskBundleError(f"contract.workflow.nodes[{index}] has an unexpected shape")
        node_id = node.get("id")
        if not isinstance(node_id, str) or re.fullmatch(WORKFLOW_NODE_ID_RE, node_id) is None:
            raise TaskBundleError(f"contract.workflow.nodes[{index}].id must match {WORKFLOW_NODE_ID_RE}")
        if node_id in ids:
            raise TaskBundleError(f"contract.workflow node id {node_id!r} is duplicated")
        ids.add(node_id)
        capability = node.get("capability")
        if capability not in WORKFLOW_CAPABILITIES:
            raise TaskBundleError(f"contract.workflow node {node_id!r} names unsupported capability {capability!r}")
        depends_on = node.get("dependsOn")
        if not isinstance(depends_on, list) or any(not isinstance(dep, str) for dep in depends_on):
            raise TaskBundleError(f"contract.workflow node {node_id!r}.dependsOn must be a string array")
        if len(depends_on) != len(set(depends_on)):
            raise TaskBundleError(f"contract.workflow node {node_id!r}.dependsOn contains duplicates")
        edges[node_id] = depends_on
        node_specs[node_id] = node

        inputs = node.get("inputs")
        if inputs is not None:
            if not isinstance(inputs, dict) or set(inputs) - {"commitFrom", "artifactsFrom"}:
                raise TaskBundleError(f"contract.workflow node {node_id!r}.inputs has an unexpected shape")
            commit_from = inputs.get("commitFrom")
            if commit_from is not None and not isinstance(commit_from, str):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.inputs.commitFrom must be a string")
            artifacts_from = inputs.get("artifactsFrom", [])
            if not isinstance(artifacts_from, list) or any(not isinstance(dep, str) for dep in artifacts_from):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.inputs.artifactsFrom must be a string array")
            if len(artifacts_from) != len(set(artifacts_from)):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.inputs.artifactsFrom contains duplicates")

        policy = node.get("policy")
        if policy is not None:
            if not isinstance(policy, dict) or set(policy) - {"required", "blocksOn", "timeoutSeconds"}:
                raise TaskBundleError(f"contract.workflow node {node_id!r}.policy has an unexpected shape")
            required = policy.get("required", True)
            if not isinstance(required, bool):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.policy.required must be boolean")
            blocks_on = policy.get("blocksOn", ["FAILED", "BLOCKED"])
            if not isinstance(blocks_on, list) or any(status not in WORKFLOW_NODE_STATUSES for status in blocks_on):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.policy.blocksOn has an unsupported status")
            if len(blocks_on) != len(set(blocks_on)):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.policy.blocksOn contains duplicates")
            timeout = policy.get("timeoutSeconds")
            if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 86400):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.policy.timeoutSeconds must be 1..86400")

        outputs = node.get("outputs")
        if outputs is not None:
            if not isinstance(outputs, dict) or set(outputs) - {"schema", "evidenceRole"}:
                raise TaskBundleError(f"contract.workflow node {node_id!r}.outputs has an unexpected shape")
            schema = outputs.get("schema")
            if schema is not None and (not isinstance(schema, str) or not schema.strip() or len(schema) > 120):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.outputs.schema must be a non-empty string")
            role = outputs.get("evidenceRole")
            if role is not None and (not isinstance(role, str) or re.fullmatch(WORKFLOW_NODE_ID_RE, role) is None):
                raise TaskBundleError(f"contract.workflow node {node_id!r}.outputs.evidenceRole must match {WORKFLOW_NODE_ID_RE}")

    for node_id, deps in edges.items():
        for dep in deps:
            if dep not in ids:
                raise TaskBundleError(f"contract.workflow node {node_id!r} depends on unknown node {dep!r}")
        inputs = node_specs[node_id].get("inputs") or {}
        input_refs = [inputs["commitFrom"]] if inputs.get("commitFrom") else []
        input_refs += inputs.get("artifactsFrom", [])
        for ref in input_refs:
            if ref not in ids:
                raise TaskBundleError(f"contract.workflow node {node_id!r} inputs reference unknown node {ref!r}")
            if ref not in deps:
                raise TaskBundleError(f"contract.workflow node {node_id!r} inputs may reference only direct dependencies")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise TaskBundleError("contract.workflow graph must be acyclic")
        visiting.add(node_id)
        for dep in edges[node_id]:
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in edges:
        visit(node_id)
    return contract


COORDINATION_REPORT_VERB = "coordination-report"


def validate_coordinator_report_request(contract: Any, task_id: str) -> dict[str, Any]:
    """Validate the command-free request contract for the advisory verb.

    `task-contract-v1` is frozen and will not gain an exception, so a
    command-free DC13 request validates against `coordination-report-request-v1`
    instead. The contract binds itself to `coordination-report`, asserts `advisory` and
    denies `verificationAuthority` by construction, and carries no
    verification or build section at all -- so there is nothing for a pipeline
    verb to execute even if one were handed it.
    """
    from . import schema_check  # local import: schema_check imports nothing from here
    if not isinstance(contract, dict):
        raise TaskBundleError("contract must be an object")
    if contract.get("taskId") != task_id:
        raise TaskBundleError("contract taskId does not match the invocation")
    errors = schema_check.validate(contract, "canonical/coordination-report-request-v1.json")
    if errors:
        raise TaskBundleError(f"coordination-report request contract is invalid: {errors[0]}")
    return contract


def _validate_contract_for_verb(contract: Any, task_id: str, verb: str | None) -> dict[str, Any]:
    """Accept the advisory request contract only for the advisory verb.

    The authority permits the command-free contract solely for the
    already-allowlisted `coordination-report` token, and it must never be dispatched
    through a pipeline verb or verification path. Any other verb therefore
    falls through to the frozen task contract, which requires at least one
    deterministic verification command.
    """
    if verb == COORDINATION_REPORT_VERB and isinstance(contract, dict) and contract.get("verb") == COORDINATION_REPORT_VERB:
        return validate_coordinator_report_request(contract, task_id)
    if verb in {"workflow", "workflow-node"}:
        return validate_workflow_contract(contract, task_id)
    return validate_task_contract(contract, task_id)


def receive_task_bundle(task_id: str, verb: str | None = None) -> bool:
    """Read a bounded bundle from stdin and atomically stage its three files.

    The forced SSH command remains one of the allowlisted verbs; stdin is
    only its data channel. No client-controlled path is accepted.
    """
    if sys.stdin.isatty():
        return False
    raw = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
    if not raw.strip():
        return False
    if len(raw) > MAX_BUNDLE_BYTES:
        raise TaskBundleError(f"task bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    try:
        bundle = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskBundleError(f"task bundle is not valid JSON: {exc}") from exc
    if not isinstance(bundle, dict) or set(bundle) != {"schemaVersion", "taskId", "task", "requirement", "contract"}:
        raise TaskBundleError("task bundle has an unexpected top-level shape")
    if bundle.get("schemaVersion") != 1 or bundle.get("taskId") != task_id:
        raise TaskBundleError("task bundle identity does not match the invocation")
    task = bundle.get("task")
    if not isinstance(task, dict) or task.get("taskId") != task_id:
        raise TaskBundleError("task record identity does not match the invocation")
    requirement = bundle.get("requirement")
    if not isinstance(requirement, str) or not requirement.strip() or len(requirement.encode()) > 262144:
        raise TaskBundleError("requirement must be non-empty and at most 256 KiB")
    contract = _validate_contract_for_verb(bundle.get("contract"), task_id, verb)

    task_dir = TASKS_DIR / task_id
    _write_text_atomic(task_dir / "task.json", json.dumps(task, indent=2, sort_keys=True) + "\n")
    _write_text_atomic(task_dir / "requirement.md", requirement)
    _write_text_atomic(task_dir / "contract.json", json.dumps(contract, indent=2, sort_keys=True) + "\n")
    return True


def write_verify_result(task_id: str, result: dict[str, Any]) -> None:
    validate_with_schema(
        result,
        "canonical/verification-v1.json",
        "verification result",
        validation_point="worker-test-result-write",
        artifact_kind="verification",
        task_id=task_id,
        log_failure=True,
    )
    TEST_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (TEST_RESULTS_DIR / f"{task_id}-verify.json").write_text(json.dumps(result, indent=2, sort_keys=True))


def git_rev_parse(repo: Path, ref: str = "HEAD") -> str | None:
    try:
        result = run(["git", "-C", str(repo), "rev-parse", ref], timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def stage_log_path(task_id: str) -> Path:
    return LOGS_DIR / f"{task_id}.jsonl"


def record_stage(task_id: str, stage: str, fields: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Append a stage record. `fields` (a dict, e.g. an adapter's result) and
    `extra` (individual kwargs) are merged; `task_id`/`stage`/`timestamp`
    always win so callers can safely spread an adapter result dict without
    worrying about key collisions."""
    merged = {**(fields or {}), **extra}
    record = {**merged, "task_id": task_id, "stage": stage, "timestamp": utc_now()}
    append_jsonl(stage_log_path(task_id), record)
    return record
