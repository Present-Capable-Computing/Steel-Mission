"""build category: run the repo's declared build if one exists.

No build system (Makefile/package.json/etc) has been designated for the
Present corpus repo yet, so this stays NOT_APPLICABLE until a task contract
names a concrete build target.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def run(repo: Path, commit: str) -> dict[str, Any]:
    return {
        "status": "NOT_APPLICABLE",
        "detail": "no build target declared for this repo yet",
    }
