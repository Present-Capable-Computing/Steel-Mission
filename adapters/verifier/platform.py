"""platform category: confirms the worker is running on the expected
Apple Silicon target. Real check (not a stub) since it's cheap and
directly guards against a task silently running on the wrong architecture.
"""
from __future__ import annotations

import platform as _platform
from pathlib import Path
from typing import Any


def run(repo: Path, commit: str) -> dict[str, Any]:
    machine = _platform.machine()
    system = _platform.system()
    if system == "Darwin" and machine == "arm64":
        return {"status": "PASS", "detail": f"{system}/{machine}"}
    return {"status": "FAIL", "detail": f"expected Darwin/arm64, got {system}/{machine}"}
