"""static category: ruff check on Python, shellcheck on shell scripts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import common


def run(repo: Path, commit: str) -> dict[str, Any]:
    try:
        ls = common.run(["git", "-C", str(repo), "ls-files"], timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"status": "BLOCKED", "detail": f"could not list tracked files: {exc}"}
    files = [f for f in ls.stdout.splitlines() if f.strip()]
    py_files = [f for f in files if f.endswith(".py")]
    sh_files = [f for f in files if f.endswith(".sh")]

    if not py_files and not sh_files:
        return {"status": "NOT_APPLICABLE", "detail": "no tracked .py or .sh files in this repo"}

    failures = []
    checked = 0

    if py_files and common.which("ruff"):
        result = common.run(["ruff", "check", *py_files], timeout=60, cwd=str(repo))
        checked += len(py_files)
        if result.returncode != 0:
            failures.append({"tool": "ruff", "detail": (result.stdout + result.stderr).strip()[-2000:]})

    if sh_files and common.which("shellcheck"):
        result = common.run(["shellcheck", *sh_files], timeout=60, cwd=str(repo))
        checked += len(sh_files)
        if result.returncode != 0:
            failures.append({"tool": "shellcheck", "detail": (result.stdout + result.stderr).strip()[-2000:]})

    if checked == 0:
        return {"status": "NOT_RUN", "detail": "static analysis tools not installed"}

    status = "FAIL" if failures else "PASS"
    return {"status": status, "detail": failures, "files_checked": checked}
