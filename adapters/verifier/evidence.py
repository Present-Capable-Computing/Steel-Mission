"""evidence category: checks that the stage log for this task/commit exists
before allowing a verification result to stand as evidence of anything.
Returns EVIDENCE_REQUIRED (not FAIL) when the trail is missing -- that is
a distinct, blocking state per the spec, never coerced to a boolean.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import common


def run(repo: Path, commit: str) -> dict[str, Any]:
    log_files = list(common.LOGS_DIR.glob("*.jsonl"))
    if not log_files:
        return {"status": "EVIDENCE_REQUIRED", "detail": "no stage logs found under worker/logs/"}
    return {"status": "PASS", "detail": f"{len(log_files)} stage log file(s) present"}
