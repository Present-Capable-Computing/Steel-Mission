"""security category: scaffolded, no security scan tooling wired yet.

Muse Code's adversarial output feeds this category once wired, but Muse
does not declare the implementation secure -- this plugin, when built out,
must run deterministic scanners (e.g. secret-scanning, dependency audit),
not just relay Muse's advisory findings as a pass/fail.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def run(repo: Path, commit: str) -> dict[str, Any]:
    return {"status": "NOT_APPLICABLE", "detail": "no deterministic security scanner wired yet"}
