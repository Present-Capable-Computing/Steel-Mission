"""Credential-backed repair adapter implemented with OpenAI Codex CLI."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from . import common

BINARY = "codex"
LIVE_IMPLEMENTED = True
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"enum": ["ACCEPTED", "REVIEW_REJECTED", "CHANGES_REQUESTED"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "summary", "file", "line"],
                "properties": {
                    "severity": {"enum": ["blocking", "major", "minor", "note"]},
                    "summary": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                },
            },
        },
    },
}
FIX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "addressedFindings"],
    "properties": {
        "summary": {"type": "string"},
        "addressedFindings": {"type": "array", "items": {"type": "string"}},
    },
}


def installed() -> bool:
    return common.which(BINARY) is not None


def authenticated() -> tuple[bool, dict[str, Any]]:
    if not installed():
        return False, {"probe": "ok"}
    try:
        result = common.run([BINARY, "login", "status"], timeout=15)
    except Exception as exc:  # noqa: BLE001
        # The probe itself did not run. That is not the same answer as "no
        # credential", and callers distinguish them -- see claude_adapter.
        return False, {"probe": "failed", "probeError": str(exc)[:300]}
    text = (result.stdout + result.stderr).strip()
    # Official Codex CLI behavior: exit 0 is the automation contract for
    # credentials being present. Human-readable wording is not parsed.
    return result.returncode == 0, {"probe": "ok", "status": text[:200]}


def credential_refusal(meta: dict[str, Any]) -> dict[str, Any]:
    if meta.get("probe") == "failed":
        return common.credential_probe_failed(BINARY, meta.get("probeError", ""))
    return common.credential_missing(BINARY)


def status() -> dict[str, Any]:
    if not installed():
        return {"installed": False, "authenticated": False, "ready": False,
                "live_implemented": True, "probe": "ok"}
    auth, meta = authenticated()
    probe = meta.pop("probe", "ok")
    probe_error = meta.pop("probeError", None)
    result = {"installed": True, "authenticated": auth, "live_implemented": True,
              "ready": auth, "probe": probe}
    if probe_error:
        result["probeError"] = probe_error
    if meta:
        result["meta"] = meta
    return result


def _diff_stat(worktree: Path) -> dict[str, int]:
    result = common.run(["git", "-C", str(worktree), "diff", "--numstat"], timeout=30)
    files = insertions = deletions = 0
    seen_paths: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        seen_paths.add(parts[2])
        files += 1
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    untracked = common.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"], timeout=30
    )
    for raw_path in untracked.stdout.split("\0"):
        if not raw_path or raw_path in seen_paths:
            continue
        path = worktree / raw_path
        if not path.is_file():
            continue
        files += 1
        content = path.read_bytes()
        insertions += content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
    return {"filesChanged": files, "insertions": insertions, "deletions": deletions}


def review(
    task_id: str,
    mode: str,
    requirement: str,
    plan: str,
    commit: str,
    diff: str,
    test_output: str,
    *,
    input_context: str = "",
) -> dict[str, Any]:
    """Review a candidate through Codex without granting write authority."""
    mocked = mode == "mock"
    envelope = common.canonical_envelope(
        task_id, "steel-mission review (codex)", mocked=mocked, commit=commit)
    if mocked:
        return {
            **envelope,
            "verdict": "ACCEPTED",
            "findings": [{"severity": "note", "summary": "mock review; no model was invoked"}],
        }

    auth, auth_meta = authenticated()
    if not auth:
        return credential_refusal(auth_meta)

    prompt = (
        "Review the candidate diff against the requirement and plan. Work read-only. "
        "Return concrete, prioritized findings; do not edit files, claim verification, or declare PASS.\n\n"
        f"REQUIREMENT\n{requirement}\n\nPLAN\n{plan}\n\nCOMMIT\n{commit}\n\n"
        f"WORKFLOW INPUT CONTEXT\n{input_context}\n\nDIFF\n{diff}\n\nTEST OUTPUT\n{test_output}"
    )
    tmp_root = common.WORKER_DIR / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{task_id}-codex-review-", dir=tmp_root) as temporary:
        schema_path = Path(temporary) / "review-output.schema.json"
        output_path = Path(temporary) / "last-message.json"
        schema_path.write_text(json.dumps(REVIEW_SCHEMA))
        command = [
            BINARY,
            "--ask-for-approval", "never",
            "exec",
            "--ephemeral",
            "--sandbox", "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
            "--cd", str(common.WORKER_DIR),
            "-",
        ]
        try:
            result = common.run(command, timeout=1800, input=prompt, cwd=common.WORKER_DIR)
        except Exception as exc:  # noqa: BLE001
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": str(exc), "retryable": True}
        if result.returncode != 0 or not output_path.exists():
            reason = (result.stderr or result.stdout).strip()[-1000:] or "Codex produced no final message"
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": reason, "retryable": True}
        try:
            model_output = json.loads(output_path.read_text())
        except json.JSONDecodeError as exc:
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": str(exc), "retryable": True}
    findings = model_output.get("findings", [])
    if isinstance(findings, list):
        model_output["findings"] = [
            {key: value for key, value in finding.items() if value is not None}
            if isinstance(finding, dict) else finding
            for finding in findings
        ]
    return {**envelope, **model_output}


def fix(
    task_id: str,
    mode: str,
    requirement: str,
    plan: str,
    commit: str,
    findings: list,
    failing_tests: list,
    worktree: Path,
    *,
    input_context: str = "",
) -> dict[str, Any]:
    mocked = mode == "mock"
    if mocked:
        return {
            **common.canonical_envelope(task_id, "steel-mission fix (codex)", mocked=True, commit=commit),
            "outcome": "UNCHANGED",
            "addressedFindings": [],
            "diffStat": {"filesChanged": 0, "insertions": 0, "deletions": 0},
        }

    auth, auth_meta = authenticated()
    if not auth:
        return credential_refusal(auth_meta)

    prompt = (
        "Repair the isolated worktree for the requirement and reviewed findings below. Work only in this worktree. "
        "Do not commit, push, declare PASS, or weaken tests. Make the smallest defensible change.\n\n"
        f"REQUIREMENT\n{requirement}\n\nPLAN\n{plan}\n\nINPUT COMMIT\n{commit}\n\n"
        f"WORKFLOW INPUT CONTEXT\n{input_context}\n\n"
        f"REVIEW FINDINGS\n{json.dumps(findings)}\n\nFAILING TESTS\n{json.dumps(failing_tests)}"
    )
    tmp_root = common.WORKER_DIR / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{task_id}-codex-", dir=tmp_root) as temporary:
        schema_path = Path(temporary) / "fix-output.schema.json"
        output_path = Path(temporary) / "last-message.json"
        schema_path.write_text(json.dumps(FIX_SCHEMA))
        command = [
            BINARY, "--ask-for-approval", "never", "exec", "--ephemeral", "--sandbox", "workspace-write",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path), "--cd", str(worktree), "-",
        ]
        try:
            result = common.run(command, timeout=1800, input=prompt, cwd=worktree)
        except Exception as exc:  # noqa: BLE001
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": str(exc), "retryable": True}
        if result.returncode != 0 or not output_path.exists():
            reason = (result.stderr or result.stdout).strip()[-1000:] or "Codex produced no final message"
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": reason, "retryable": True}
        try:
            model_output = json.loads(output_path.read_text())
        except json.JSONDecodeError as exc:
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": str(exc), "retryable": True}

    stat = _diff_stat(worktree)
    output_commit = commit
    outcome = "UNCHANGED"
    if stat["filesChanged"]:
        add = common.run(["git", "-C", str(worktree), "add", "-A"], timeout=30)
        committed = common.run(
            ["git", "-C", str(worktree), "commit", "-m", f"present-worker fix: {task_id}"], timeout=60
        )
        if add.returncode != 0 or committed.returncode != 0:
            reason = (add.stderr + committed.stderr).strip()[-1000:]
            return {"status": "PROVIDER_ERROR", "provider": "codex", "reason": reason, "retryable": False}
        output_commit = common.git_rev_parse(worktree) or commit
        outcome = "FIXED"

    return {
        **common.canonical_envelope(task_id, "steel-mission fix (codex)", mocked=False, commit=output_commit),
        "outcome": outcome,
        "addressedFindings": model_output.get("addressedFindings", []),
        "diffStat": stat,
    }
