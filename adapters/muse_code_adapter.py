"""Adapter for the specialist/adversarial role, intended for Muse Code.

Muse Code is not a discoverable installed package on this machine (checked
Homebrew, npm, and PyPI during setup -- nothing named muse-code exists in
any of those registries). This adapter is a honest stub: it always reports
not-installed rather than inventing an installation. When the real binary
or credential source is identified, wire `installed()`/`authenticated()`
here and nothing else in present-worker needs to change.
"""
from __future__ import annotations

from typing import Any

from . import common

BINARY = "muse"  # placeholder name; update when the real binary is known


def installed() -> bool:
    return common.which(BINARY) is not None


def authenticated() -> tuple[bool, dict[str, Any]]:
    return False, {}


def status() -> dict[str, Any]:
    inst = installed()
    return {"installed": inst, "authenticated": False, "ready": False}


def adversarial(task_id: str, mode: str, requirement: str, plan: str, commit: str) -> dict[str, Any]:
    if mode == "mock":
        return {
            "status": "MOCK",
            "provider": "muse_code",
            "task_id": task_id,
            "target_commit": commit,
            "candidate_attacks": [
                {"category": "malformed_input", "summary": "mock candidate -- no real model was invoked"},
            ],
            "security_certified": False,
            "production_pass": False,
        }

    if not installed():
        return common.credential_missing("muse_code")

    return {
        "status": "NOT_IMPLEMENTED",
        "provider": "muse_code",
        "reason": "Muse Code binary/credential source not yet identified on this machine",
        "retryable": False,
    }
