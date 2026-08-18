"""format category: ruff format --check on any tracked Python files."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import common


def run(repo: Path, commit: str) -> dict[str, Any]:
    if not common.which("ruff"):
        return {"status": "NOT_RUN", "detail": "ruff not installed"}

    try:
        ls = common.run(["git", "-C", str(repo), "ls-files", "*.py"], timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"status": "BLOCKED", "detail": f"could not list tracked python files: {exc}"}
    py_files = [f for f in ls.stdout.splitlines() if f.strip()]
    if not py_files:
        return {"status": "NOT_APPLICABLE", "detail": "no tracked .py files in this repo"}

    result = common.run(["ruff", "format", "--check", *py_files], timeout=60, cwd=str(repo))
    status = "PASS" if result.returncode == 0 else "FAIL"
    return {"status": status, "detail": (result.stdout + result.stderr).strip()[-2000:], "files_checked": len(py_files)}
