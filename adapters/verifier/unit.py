"""unit category: pytest, if a tests/ directory exists in the target repo."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import common


def run(repo: Path, commit: str) -> dict[str, Any]:
    tests_dir = repo / "tests"
    if not tests_dir.exists():
        return {"status": "NOT_APPLICABLE", "detail": "no tests/ directory in this repo"}
    if not common.which("pytest"):
        return {"status": "NOT_RUN", "detail": "pytest not installed"}

    result = common.run(["pytest", "-q", "tests"], timeout=300, cwd=str(repo))
    if result.returncode == 5:  # pytest: no tests collected -- an empty suite, not a failure
        status = "NOT_APPLICABLE"
    elif result.returncode == 0:
        status = "PASS"
    else:
        status = "FAIL"
    return {"status": status, "detail": (result.stdout + result.stderr).strip()[-3000:]}
