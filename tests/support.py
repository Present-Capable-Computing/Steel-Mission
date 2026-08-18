from __future__ import annotations

from typing import Any

from adapters import common


def broker_state_document(tasks: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = common.utc_now()
    return {
        "schemaVersion": 1,
        "producedAt": now,
        "producer": "present-lease-broker",
        "updatedAt": now,
        "tasks": tasks,
        "events": events or [],
        "recoveryLedger": [],
    }
