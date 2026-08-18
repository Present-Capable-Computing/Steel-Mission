"""fuzz category: scaffolded, no fuzz harness defined yet."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def run(repo: Path, commit: str) -> dict[str, Any]:
    return {"status": "NOT_APPLICABLE", "detail": "no fuzz harness defined for this repo yet"}
