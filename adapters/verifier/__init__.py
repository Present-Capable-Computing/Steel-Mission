"""Deterministic verification harness. Never calls a language model.

Each plugin module exposes `run(repo: Path, commit: str) -> dict` returning
at minimum {"status": ..., "detail": ...} where status is one of:
PASS, FAIL, NOT_APPLICABLE, NOT_RUN, BLOCKED, EVIDENCE_REQUIRED.

These are never coerced into a single boolean. `verify()` aggregates them
into PASS / FAIL / INCONCLUSIVE. Required production categories must be real
PASS values; NOT_APPLICABLE and NOT_RUN are inconclusive, never production
pass.
"""
from __future__ import annotations

import json
import platform as _platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import common

CATEGORIES = [
    "build",
    "format",
    "static",
    "unit",
    "integration",
    "security",
    "fuzz",
    "platform",
    "evidence",
]

VALID_STATUSES = {"PASS", "FAIL", "NOT_APPLICABLE", "NOT_RUN", "BLOCKED", "EVIDENCE_REQUIRED"}
REQUIRED_FOR_PRODUCTION = {"build", "unit", "integration", "security", "evidence"}
INCONCLUSIVE_STATUSES = {"NOT_APPLICABLE", "NOT_RUN", "BLOCKED", "EVIDENCE_REQUIRED"}


def _tool_version(binary: str, *args: str) -> str | None:
    if not shutil.which(binary):
        return None
    try:
        result = common.run([binary, *args], timeout=10)
    except Exception:  # noqa: BLE001
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0] if result.stdout or result.stderr else None


def run_category(category: str, repo: Path, commit: str) -> dict[str, Any]:
    import importlib

    mod = importlib.import_module(f".{category}", package=__name__)
    result = mod.run(repo, commit)
    assert result.get("status") in VALID_STATUSES, f"{category} plugin returned invalid status"
    result.setdefault("category", category)
    return result


def _counts(results: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
    for result in results.values():
        status = result["status"]
        if status == "PASS":
            counts["PASS"] += 1
        elif status == "FAIL":
            counts["FAIL"] += 1
        else:
            counts["INCONCLUSIVE"] += 1
    return counts


def _top_status(results: dict[str, dict[str, Any]], diagnostics: list[str]) -> str:
    if any(r["status"] == "FAIL" for r in results.values()):
        return "FAIL"
    if diagnostics or any(r["status"] in ("BLOCKED", "EVIDENCE_REQUIRED") for r in results.values()):
        return "INCONCLUSIVE"
    return "PASS"


def inconclusive(task_id: str, reason: str, commit: str, mocked: bool) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": common.utc_now(),
        "producer": "present-worker verify",
        "mock": mocked,
        "provenance": {"source": "worker", "host": _platform.node(), **({"commit": commit} if commit else {})},
        "result": "INCONCLUSIVE",
        "deterministic": True,
        "checks": [{"name": "contract", "passed": False, "detail": reason}],
    }


def verify_contract(task_id: str, contract: dict[str, Any], mocked: bool = False,
                    tree: Path | None = None, input_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run only the argv-based checks explicitly authorized by the contract.

    No shell is involved. The target is resolved from a fixed allowlist rather
    than from a client-supplied filesystem path. `tree` narrows that resolution
    to the worktree holding the commit under test; the caller establishes it,
    and never passes a path derived from client input.
    """
    common.validate_task_contract(contract, task_id)
    verification = contract["verification"]
    target = verification["target"]
    cwd = tree or (common.WORKER_DIR if target == "worker" else common.DEFAULT_REPO)
    commit = common.git_rev_parse(cwd) or ""
    checks: list[dict[str, Any]] = []
    resolved_inputs = input_context.get("resolvedInputs") if isinstance(input_context, dict) else {}
    workflow_inputs = resolved_inputs.get("artifacts") if isinstance(resolved_inputs, dict) else []
    if not isinstance(workflow_inputs, list):
        workflow_inputs = []

    if mocked:
        return inconclusive(task_id, "mock verification is never production evidence", commit, True)
    if not cwd.exists():
        return inconclusive(task_id, f"verification target {target!r} is unavailable", commit, False)

    for command in verification["commands"]:
        name = command["name"]
        argv = command["argv"]
        expected = command["expectedExitCode"]
        timeout = command["timeoutSeconds"]
        try:
            completed = common.run(argv, timeout=timeout, cwd=str(cwd))
            passed = completed.returncode == expected
            excerpt = (completed.stdout + completed.stderr).strip()[-1500:]
            detail = json.dumps(
                {
                    "argv": argv,
                    "actualExitCode": completed.returncode,
                    "expectedExitCode": expected,
                    "outputExcerpt": excerpt,
                    "target": target,
                    "tree": str(cwd),
                    **({"workflowInputs": workflow_inputs} if workflow_inputs else {}),
                },
                sort_keys=True,
            )
        except subprocess.TimeoutExpired:
            passed = False
            detail = json.dumps({
                "argv": argv, "timeoutSeconds": timeout, "target": target,
                **({"workflowInputs": workflow_inputs} if workflow_inputs else {}),
            }, sort_keys=True)
        except (FileNotFoundError, OSError) as exc:
            passed = False
            detail = json.dumps({
                "argv": argv, "error": str(exc), "target": target,
                **({"workflowInputs": workflow_inputs} if workflow_inputs else {}),
            }, sort_keys=True)
        checks.append({"name": name, "passed": passed, "detail": detail})

    result = "PASS" if checks and all(check["passed"] for check in checks) else "FAIL"
    return {
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": common.utc_now(),
        "producer": "present-worker verify",
        "mock": False,
        "provenance": {"source": "worker", "host": _platform.node(), **({"commit": commit} if commit else {})},
        "result": result,
        "deterministic": True,
        "checks": checks,
    }


def verify(task_id: str, repo: Path, commit: str, mocked: bool, contract: dict[str, Any]) -> dict[str, Any]:
    results = {}
    for category in CATEGORIES:
        try:
            results[category] = run_category(category, repo, commit)
        except Exception as exc:  # noqa: BLE001 -- a broken plugin blocks, never crashes the CLI
            results[category] = {"category": category, "status": "BLOCKED", "detail": f"plugin error: {exc}"}

    diagnostics: list[str] = []
    if not commit:
        diagnostics.append("missing exact commit to verify")
    if not contract:
        diagnostics.append("missing task contract")

    for category in sorted(REQUIRED_FOR_PRODUCTION):
        status = results[category]["status"]
        if status in INCONCLUSIVE_STATUSES:
            diagnostics.append(f"required check {category} is {status}")

    status = _top_status(results, diagnostics)
    counts = _counts(results)
    production_pass = (not mocked) and status == "PASS" and not diagnostics
    return {
        "schema": "verification-v1",
        "schema_authority": common.SCHEMA_AUTHORITY,
        "protocol_version": "2.1",
        "task_id": task_id,
        "commit": commit,
        "timestamp": common.utc_now(),
        "platform": {
            "machine": _platform.machine(),
            "system": _platform.system(),
            "release": _platform.release(),
        },
        "tool_versions": {
            "git": _tool_version("git", "--version"),
            "ruff": _tool_version("ruff", "--version"),
            "shellcheck": _tool_version("shellcheck", "--version"),
            "pytest": _tool_version("pytest", "--version"),
        },
        "mocked": mocked,
        "status": status,
        "checks": results,
        "results": results,
        "counts": counts,
        "diagnostics": diagnostics,
        "production_pass": production_pass,
    }
