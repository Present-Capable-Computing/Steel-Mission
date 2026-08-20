#!/usr/bin/env python3
"""Local-only DC13 chat wrapper for steel-mission coordination-report."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
    from cryptography.hazmat.primitives.asymmetric import rsa
except Exception:  # noqa: BLE001
    hashes = None
    crypto_padding = None
    rsa = None

APP_DIR = Path(__file__).resolve().parent
WORKER_DIR = APP_DIR.parent
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from adapters import schema_check  # noqa: E402

TASKS_DIR = Path(os.environ.get("PRESENT_TASKS_DIR") or WORKER_DIR / "tasks")
TEST_RESULTS_DIR = Path(os.environ.get("PRESENT_TEST_RESULTS_DIR") or WORKER_DIR / "test-results")
REPOS_DIR = Path(os.environ.get("PRESENT_REPOS_DIR") or WORKER_DIR / "repos")
PRESENT_DEV_DIR = Path(os.environ.get("PRESENT_DEV") or WORKER_DIR.parent)
WORKER_BIN = WORKER_DIR / "bin" / "steel-mission"
BROKER_BIN = WORKER_DIR / "bin" / "present-lease-broker"
PRIVATE_RUNNER_BIN = WORKER_DIR / "bin" / "present-private-runner"
INDEX = APP_DIR / "index.html"
# The organisation this installation runs on, defaulting to the synthetic starter
# company shipped with the product. An installation points ORG_DIR at its own
# directory; it never replaces the contents of the shipped one.
ORG_DIR = Path(
    os.environ.get("STEEL_MISSION_ORG_DIR")
    or os.environ.get("PRESENT_ORG_DIR")
    or WORKER_DIR / "starter-company"
)
# PRESENT_CANON_DIR predates ORG_DIR and still wins where it is set, so an
# existing deployment keeps working.
CANON_DIR = Path(
    os.environ.get("STEEL_MISSION_CANON_DIR")
    or os.environ.get("PRESENT_CANON_DIR")
    or ORG_DIR / "canon"
)
ROLE_REGISTRY_PATH = CANON_DIR / "Workspace Packs" / "_build" / "role-registry.json"
ROLE_KNOWLEDGE_REGISTRY_PATH = CANON_DIR / "Workspace Packs" / "_build" / "role-knowledge-registry.json"
# Where the product is configured to read the organisation from. Separate from
# ORG_DIR, which is where the organisation's documents live: redirecting only
# ORG_DIR serves your documents under the shipped company's identity.
CONFIG_DIR = Path(
    os.environ.get("STEEL_MISSION_CONFIG_DIR")
    or os.environ.get("PRESENT_CONFIG_DIR")
    or WORKER_DIR / "config"
)
DOMAIN_CAPABILITIES_PATH = CONFIG_DIR / "domain-capabilities.json"
GENERAL_KNOWLEDGE_PATH = CONFIG_DIR / "general-knowledge.json"
ORGANIZATION_REGISTRY_PATH = Path(os.environ.get("PRESENT_ORGANIZATION_REGISTRY") or CONFIG_DIR / "organizations.json")
ORG_KNOWLEDGE_UPLOAD_ROOT = Path(os.environ.get("PRESENT_ORG_KNOWLEDGE_UPLOAD_DIR") or CONFIG_DIR / "org-knowledge-uploads")
USER_REGISTRY_PATH = CONFIG_DIR / "users.json"
RUNTIME_PROFILE_REGISTRY_PATH = CONFIG_DIR / "runtime-profiles.json"
MODEL_ROLE_REGISTRY_PATH = CONFIG_DIR / "model-role-registry.json"
CONTROL_POLICY_PATH = CONFIG_DIR / "control-plane-policy.json"
INTEGRATION_REGISTRY_PATH = CONFIG_DIR / "integration-registry.json"
AUTH_POLICY_PATH = CONFIG_DIR / "auth-policy.json"
SCHEMA_REGISTRY_PATH = WORKER_DIR / "schemas" / "schema-registry.json"
MISSION_ROOT = Path(os.environ.get("PRESENT_MISSIONS_DIR") or WORKER_DIR / "missions")
MUTATION_LEDGER_PATH = Path(os.environ.get("PRESENT_MUTATION_LEDGER") or MISSION_ROOT / "_mutation-ledger.jsonl")
AUTH_SIGNING_KEY_PATH = Path(os.environ.get("PRESENT_AUTH_SIGNING_KEY_FILE") or MISSION_ROOT / "_auth-signing-key")
AUTH_AUDIT_LEDGER_PATH = Path(os.environ.get("PRESENT_AUTH_AUDIT_LEDGER") or MISSION_ROOT / "_auth-audit.jsonl")
AUTH_REVOCATION_LEDGER_PATH = Path(os.environ.get("PRESENT_AUTH_REVOCATION_LEDGER") or MISSION_ROOT / "_auth-revocations.jsonl")
PRIVATE_RUNNER_SIGNING_KEY_PATH = Path(os.environ.get("PRESENT_PRIVATE_RUNNER_SIGNING_KEY_FILE") or MISSION_ROOT / "_private-runner-signing-key")
EVIDENCE_SIGNING_KEY_ENV = "PRESENT_EVIDENCE_SIGNING_KEY"
EVIDENCE_SIGNER_ID_ENV = "PRESENT_EVIDENCE_SIGNER_ID"
EVIDENCE_SIGNER_COMMAND_ENV = "PRESENT_EVIDENCE_SIGNER_COMMAND"
AUTH_SIGNING_KEY_ENV = "PRESENT_AUTH_SIGNING_KEY"
AUTH_SIGNER_ID_ENV = "PRESENT_AUTH_SIGNER_ID"
AUTH_IDENTITY_MODE_ENV = "PRESENT_IDENTITY_MODE"
OIDC_CLIENT_SECRET_ENV = "PRESENT_OIDC_CLIENT_SECRET"
CONNECTOR_WEBHOOK_SECRET_ENV = "PRESENT_CONNECTOR_WEBHOOK_SECRET"
PRIVATE_RUNNER_MODE_ENV = "PRESENT_PRIVATE_RUNNER_MODE"
PRIVATE_RUNNER_ALLOW_LOCAL_ENV = "PRESENT_PRIVATE_RUNNER_ALLOW_LOCAL"
PRIVATE_RUNNER_SIGNING_KEY_ENV = "PRESENT_PRIVATE_RUNNER_SIGNING_KEY"
PRIVATE_RUNNER_SIGNER_ID_ENV = "PRESENT_PRIVATE_RUNNER_SIGNER_ID"
STEEL_MISSION_EDITION_ENV = "STEEL_MISSION_EDITION"
STEEL_MISSION_LICENSE_KEY_ENV = "STEEL_MISSION_LICENSE_KEY"
STEEL_MISSION_LICENSE_KEY_SHA256_ENV = "STEEL_MISSION_LICENSE_KEY_SHA256"
MAX_REQUEST_BYTES = 128 * 1024
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = 36 * 1024 * 1024
MAX_CHAT_UPLOAD_CHARS = 24000
LIVE_TIMEOUT_SECONDS = 480
MOCK_TIMEOUT_SECONDS = 30
DELIVERY_COMMAND_TIMEOUT_SECONDS = 180
# The model call must give up before this job is killed, otherwise the outer
# kill always wins and the worker is SIGTERMed with nothing recorded -- which
# is how every live run died at 180s on 2026-08-17 while the adapter still
# believed it had a 600s budget. Inner budget < outer budget, always.
MODEL_TIMEOUT_MARGIN_SECONDS = 30
COORDINATOR_PROVIDER_ENV = "STEEL_MISSION_COORDINATOR_PROVIDER"
COORDINATOR_ROLE_ENV = "STEEL_MISSION_COORDINATOR_ROLE"
COORDINATOR_RUNTIME_PROFILE_ENV = "STEEL_MISSION_RUNTIME_PROFILE"
ACTIVE_COORDINATOR_PROVIDER: str | None = None
ACTIVE_COORDINATOR_ROLE: str | None = None
ACTIVE_RUNTIME_PROFILE: str | None = None
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
MISSION_LOCK = threading.Lock()
MISSION_ORCHESTRATORS: set[str] = set()
MISSION_ORCHESTRATORS_LOCK = threading.Lock()
WORKFLOW_INGRESS_LOCK = threading.Lock()
AUTH_LOCK = threading.Lock()
OIDC_CACHE_LOCK = threading.Lock()
OIDC_JWKS_CACHE: dict[str, Any] = {}
OIDC_LOGIN_STATES: dict[str, dict[str, Any]] = {}

MISSION_TEMPLATES: list[dict[str, Any]] = [
    {
        "templateId": "investigate",
        "title": "Investigate",
        "allowedRoles": ["owner", "admin", "publisher", "user"],
        "description": "Scope the configured knowledge sources, ask DC13 for an advisory readout, and record findings.",
        "nodes": [
            {"nodeId": "snapshot-scope", "title": "Snapshot scope", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {"nodeId": "steel-mission-readout", "title": "Delivery Coordinator readout", "kind": "coordination-report", "capability": "dc13.coordination-report"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
    {
        "templateId": "reconcile",
        "title": "Reconcile",
        "allowedRoles": ["owner", "admin", "publisher", "user"],
        "description": "Compare mission context with broker-visible state and ask DC13 for the current reconciliation.",
        "nodes": [
            {"nodeId": "snapshot-scope", "title": "Snapshot scope", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {"nodeId": "broker-overview", "title": "Broker overview", "kind": "broker-overview", "capability": "broker.state.read"},
            {"nodeId": "steel-mission-reconciliation", "title": "DC13 reconciliation", "kind": "coordination-report", "capability": "dc13.coordination-report"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
    {
        "templateId": "verify",
        "title": "Verify",
        "allowedRoles": ["owner", "admin", "publisher"],
        "description": "Run the schema gate, ask DC13 to explain verification state, and retain the evidence.",
        "nodes": [
            {"nodeId": "snapshot-scope", "title": "Snapshot scope", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {"nodeId": "schema-gate", "title": "Schema gate", "kind": "schema-gate", "capability": "schema-authority.validate"},
            {"nodeId": "steel-mission-verification-readout", "title": "DC13 verification readout", "kind": "coordination-report", "capability": "dc13.coordination-report"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
    {
        "templateId": "implement",
        "title": "Implement",
        "allowedRoles": ["owner", "admin", "publisher"],
        "description": "Prepare an implementation mission after explicit approval; this alpha records the plan and evidence.",
        "nodes": [
            {"nodeId": "snapshot-scope", "title": "Snapshot scope", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {
                "nodeId": "implementation-approval",
                "title": "Implementation approval",
                "kind": "approval",
                "capability": "mission.approve",
                "requiresApproval": True,
            },
            {"nodeId": "steel-mission-implementation-brief", "title": "DC13 implementation brief", "kind": "coordination-report", "capability": "dc13.coordination-report"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
    {
        "templateId": "delivery-execution",
        "title": "Delivery Execution",
        "allowedRoles": ["owner", "admin", "publisher"],
        "description": "Govern understand, plan, modify, build, test, inspect, repair, PR, and deploy as one auditable mission.",
        "nodes": [
            {"nodeId": "snapshot-scope", "title": "Understand snapshot", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {"nodeId": "delivery-plan", "title": "Delivery plan", "kind": "delivery-plan", "capability": "delivery.plan"},
            {
                "nodeId": "implementation-approval",
                "title": "Implementation approval",
                "kind": "approval",
                "capability": "mission.approve",
                "requiresApproval": True,
            },
            {"nodeId": "delivery-modify", "title": "Modify", "kind": "delivery-step", "phase": "modify", "capability": "delivery.modify"},
            {"nodeId": "delivery-build", "title": "Build", "kind": "delivery-step", "phase": "build", "capability": "delivery.build"},
            {"nodeId": "delivery-test", "title": "Test", "kind": "delivery-step", "phase": "test", "capability": "delivery.test"},
            {"nodeId": "delivery-inspect", "title": "Inspect", "kind": "delivery-step", "phase": "inspect", "capability": "delivery.inspect"},
            {"nodeId": "delivery-repair", "title": "Repair", "kind": "delivery-step", "phase": "repair", "capability": "delivery.repair"},
            {"nodeId": "pr-readiness", "title": "PR readiness", "kind": "delivery-step", "phase": "pr", "capability": "delivery.pr"},
            {"nodeId": "deploy-readiness", "title": "Deploy readiness", "kind": "delivery-step", "phase": "deploy", "capability": "delivery.deploy"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
    {
        "templateId": "publish",
        "title": "Publish Readiness",
        "allowedRoles": ["owner", "admin", "publisher"],
        "description": "Require human approval, run the schema gate, and ask DC13 for publish readiness without publishing.",
        "nodes": [
            {"nodeId": "snapshot-scope", "title": "Snapshot scope", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {
                "nodeId": "publish-approval",
                "title": "Publish approval",
                "kind": "approval",
                "capability": "mission.approve",
                "requiresApproval": True,
            },
            {"nodeId": "schema-gate", "title": "Schema gate", "kind": "schema-gate", "capability": "schema-authority.validate"},
            {"nodeId": "steel-mission-publish-readiness", "title": "DC13 publish readiness", "kind": "coordination-report", "capability": "dc13.coordination-report"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
    {
        "templateId": "prepare-knowledge",
        "title": "Prepare Knowledge Snapshot",
        "allowedRoles": ["owner", "admin"],
        "description": "Validate organization sources, prepare an indexable knowledge artifact, and refresh snapshot scope.",
        "nodes": [
            {"nodeId": "knowledge-source-health", "title": "Source health", "kind": "knowledge-prepare", "capability": "knowledge.prepare"},
            {"nodeId": "snapshot-scope", "title": "Snapshot scope", "kind": "snapshot", "capability": "dc13.snapshot.read"},
            {"nodeId": "mission-summary", "title": "Mission summary", "kind": "summary", "capability": "dc13.ledger.record"},
        ],
    },
]

PAGE_PATHS = {
    "/",
    "/index.html",
}

LEGACY_PAGE_PATHS = {
    "/owner",
    "/admin",
    "/publisher",
    "/user",
    "/owner/settings",
    "/admin/settings",
    "/publisher/settings",
    "/user/settings",
    "/owner/missions",
    "/admin/missions",
    "/publisher/missions",
    "/user/missions",
}


def is_page_path(path: str) -> bool:
    return path in PAGE_PATHS


def is_legacy_page_path(path: str) -> bool:
    if path in LEGACY_PAGE_PATHS:
        return True
    parts = path.strip("/").split("/")
    return (
        len(parts) == 3
        and parts[0] in {"owner", "admin", "publisher", "user"}
        and parts[1] == "missions"
        and parts[2].startswith("ms-")
    )


def active_coordinator_provider() -> str:
    provider = ACTIVE_COORDINATOR_PROVIDER or os.environ.get(COORDINATOR_PROVIDER_ENV, "claude")
    return provider if provider in {"claude", "glimmer"} else "claude"


def active_coordinator_role() -> str:
    role = ACTIVE_COORDINATOR_ROLE or os.environ.get(COORDINATOR_ROLE_ENV)
    if role:
        return role
    return "dc13.coordination-report"


def active_runtime_profile() -> str:
    profile = ACTIVE_RUNTIME_PROFILE or os.environ.get(COORDINATOR_RUNTIME_PROFILE_ENV)
    if profile:
        return profile
    provider_profiles = {
        "claude": "dc13.claude",
        "glimmer": "dc13.local",
    }
    provider = ACTIVE_COORDINATOR_PROVIDER or os.environ.get(COORDINATOR_PROVIDER_ENV)
    if provider in provider_profiles:
        return provider_profiles[provider]
    return "dc13.auto"


def actor_from_payload(payload: dict[str, Any] | None, fallback_role: str = "user") -> dict[str, str]:
    payload = payload if isinstance(payload, dict) else {}
    role = corporate_role(str(payload.get("operatorRole") or payload.get("actorRole") or fallback_role))
    actor_id = str(payload.get("actorUserId") or payload.get("actor") or role).strip()[:120]
    return {"actorId": actor_id or role, "role": role}


def actor_from_request(handler: BaseHTTPRequestHandler, fallback_role: str = "user") -> dict[str, str]:
    role = handler.headers.get("X-Present-Role") or fallback_role
    actor_id = handler.headers.get("X-Present-Actor") or corporate_role(role)
    return actor_from_payload({"operatorRole": role, "actorUserId": actor_id}, fallback_role)


def require_actor_role(actor: dict[str, str], allowed: set[str]) -> None:
    if actor.get("role") not in allowed:
        raise PermissionError("actor is not allowed to perform this action")


def authorize_mission_bindings(actor: dict[str, Any], user_ids: list[str], capability_keys: list[str]) -> None:
    role = corporate_role(str(actor.get("role") or "user"))
    actor_capabilities = set(clean_string_list(actor.get("capabilities"), limit=200))
    if role not in {"owner", "admin"}:
        unauthorized = sorted(set(capability_keys) - actor_capabilities)
        if unauthorized:
            raise PermissionError("actor is not assigned requested capabilities: " + ", ".join(unauthorized))
    actor_orgs = set(clean_string_list(actor.get("organizationIds"), limit=50))
    if actor.get("organizationId"):
        actor_orgs.add(str(actor.get("organizationId")))
    for user_id in user_ids:
        user = registered_user(user_id)
        if not user or user.get("status") != "active":
            raise PermissionError(f"mission user {user_id} is not active")
        user_orgs = set(clean_string_list(user.get("organizationIds"), limit=50))
        if actor_orgs and user_orgs and not actor_orgs.intersection(user_orgs):
            raise PermissionError(f"mission user {user_id} is outside the actor organization scope")


def resolve_runtime_profile(profile: str | None = None) -> dict[str, Any]:
    selected_profile = profile or active_runtime_profile()
    failure: dict[str, Any] = {}
    try:
        result = subprocess.run(
            [str(WORKER_BIN), "runtime-profile-resolve", selected_profile],
            cwd=str(WORKER_DIR),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
        if result.returncode == 0 and isinstance(payload, dict) and isinstance(payload.get("runtimeProfile"), dict):
            return payload
        failure = payload if isinstance(payload, dict) else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    registry = read_json_file(RUNTIME_PROFILE_REGISTRY_PATH)
    profiles = registry.get("profiles", []) if isinstance(registry, dict) else []
    registered_profile = next(
        (
            item
            for item in profiles
            if isinstance(item, dict) and item.get("id") == selected_profile
        ),
        None,
    )
    if not registered_profile:
        reason = str(failure.get("reason") or f"unknown runtime profile {selected_profile!r}")
        raise ValueError(reason)
    if registered_profile.get("status") != "active":
        raise ValueError(f"runtime profile {selected_profile!r} is disabled")
    model_policy = resolve_model_policy()
    return {
        "schemaVersion": 1,
        "producedAt": utc_now(),
        "producer": "steel-mission-chat-local-fallback",
        "runtimeProfile": {
            "schemaVersion": 1,
            "id": selected_profile,
            "label": selected_profile,
            "status": "active",
            "modelRole": model_policy.get("role") or active_coordinator_role(),
            "modelProvider": str(model_policy.get("provider") or active_coordinator_provider()),
            "requiredProviderCapabilities": list(model_policy.get("requiredProviderCapabilities", [])),
            "snapshotProfile": model_policy.get("snapshotProfile") or "worker-local-default",
            "defaultFor": ["steel-mission-chat"],
            "editableBy": ["local-user"],
            "visibilityRoleKeys": ["DC13"],
            "registryPath": "unavailable",
            "registryHash": "0000000000000000000000000000000000000000000000000000000000000000",
            "resolvedAt": utc_now(),
        },
        "modelPolicy": model_policy,
        "snapshotPolicy": default_snapshot_policy(
            str(model_policy.get("provider") or active_coordinator_provider()),
            str(model_policy.get("snapshotProfile") or "worker-local-default"),
        ),
    }


def runtime_profile_registry() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(WORKER_BIN), "runtime-profiles"],
            cwd=str(WORKER_DIR),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
        if result.returncode == 0 and isinstance(payload, dict):
            return payload
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"schemaVersion": 1, "profiles": []}


def model_role_registry() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(WORKER_BIN), "model-roles"],
            cwd=str(WORKER_DIR),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
        if result.returncode == 0 and isinstance(payload, dict):
            return payload
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"schemaVersion": 1, "models": [], "roles": []}


def knowledge_registry() -> dict[str, Any]:
    try:
        role_registry = json.loads(ROLE_REGISTRY_PATH.read_text())
        knowledge = json.loads(ROLE_KNOWLEDGE_REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "roles": [],
            "capabilities": [],
            "foundations": [],
            "knowledgeDomains": [],
            "generalKnowledge": general_knowledge_registry(),
        }
    knowledge_domains = [
        {
            "domainKey": item.get("role_key"),
            "fNumber": item.get("role_key"),
            "displayName": item.get("display_name"),
            "canonPath": item.get("canon_path"),
        }
        for item in role_registry.get("foundations", [])
        if isinstance(item, dict)
    ]
    roles = []
    for item in role_registry.get("roles", []):
        if not isinstance(item, dict):
            continue
        key = item.get("role_key")
        role_knowledge = knowledge.get("roles", {}).get(key, {}) if isinstance(knowledge.get("roles"), dict) else {}
        sources = role_knowledge.get("domainSources", []) if isinstance(role_knowledge, dict) else []
        capability = {
            "capabilityKey": key,
            "roleKey": key,
            "currentFNumber": item.get("current_f_number"),
            "fNumber": item.get("current_f_number"),
            "targetFNumber": item.get("target_f_number"),
            "displayName": item.get("display_name"),
            "canonPath": item.get("canon_path"),
            "sourceCount": len(sources) if isinstance(sources, list) else 0,
            "domainSources": sources if isinstance(sources, list) else [],
        }
        roles.append(capability)
    return {
        "ok": True,
        "revision": knowledge.get("revision"),
        "rule": knowledge.get("rule"),
        "epoch": role_registry.get("epoch"),
        "foundations": role_registry.get("foundations", []),
        "knowledgeDomains": knowledge_domains,
        "roles": roles,
        "capabilities": roles,
        "generalKnowledge": general_knowledge_registry(),
        "effectiveKnowledge": effective_knowledge_sources(),
        "knowledgeQuality": knowledge_quality_report(),
        "organizationRegistry": organization_registry(),
        "activeOrganization": active_organization(),
    }


def normalize_general_knowledge_registry(payload: dict[str, Any]) -> dict[str, Any]:
    def metadata(item: dict[str, Any]) -> dict[str, Any]:
        max_age = item.get("maxAgeDays")
        try:
            max_age_days = max(1, min(int(max_age), 3650)) if max_age not in {None, ""} else None
        except (TypeError, ValueError):
            max_age_days = None
        provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        return {
            **({"owner": str(item["owner"]).strip()[:160]} if item.get("owner") else {}),
            **({"lastReviewedAt": str(item["lastReviewedAt"]).strip()[:64]} if item.get("lastReviewedAt") else {}),
            **({"expiresAt": str(item["expiresAt"]).strip()[:64]} if item.get("expiresAt") else {}),
            **({"maxAgeDays": max_age_days} if max_age_days is not None else {}),
            "required": bool_from_payload(item.get("required"), True),
            "authoritative": bool_from_payload(item.get("authoritative"), False),
            **({"provenance": provenance} if provenance else {}),
        }

    repositories = []
    for index, item in enumerate(payload.get("repositories", [])):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item.get("path").strip():
            continue
        path = item["path"].strip()
        source_path = resolve_config_path(path)
        repositories.append({
            "name": str(item.get("name") or f"repo-{index + 1}").strip(),
            "path": path,
            "exists": source_path.exists(),
            "sourceKind": "repository",
            **metadata(item),
            **({"description": str(item["description"]).strip()} if item.get("description") else {}),
        })
    documents = []
    for index, item in enumerate(payload.get("documents", [])):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item.get("path").strip():
            continue
        path = item["path"].strip()
        source_path = resolve_config_path(path)
        documents.append({
            "title": str(item.get("title") or Path(path).name or f"document-{index + 1}").strip(),
            "path": path,
            "exists": source_path.exists(),
            "sourceKind": "document",
            **metadata(item),
            **({"kind": str(item["kind"]).strip()} if item.get("kind") else {}),
            **({"description": str(item["description"]).strip()} if item.get("description") else {}),
        })
    return {
        "schemaVersion": 1,
        "producedAt": str(payload.get("producedAt") or utc_now()),
        "producer": "steel-mission-chat",
        "repositories": repositories,
        "documents": documents,
    }


def general_knowledge_registry() -> dict[str, Any]:
    try:
        payload = json.loads(GENERAL_KNOWLEDGE_PATH.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    return normalize_general_knowledge_registry(payload if isinstance(payload, dict) else {})


def safe_org_id(value: str, fallback: str = "organization") -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return cleaned[:80] or fallback


def knowledge_catalog() -> dict[str, list[dict[str, Any]]]:
    try:
        role_registry = json.loads(ROLE_REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        role_registry = {}
    knowledge_domains = [
        {
            "domainKey": str(item.get("role_key") or ""),
            "displayName": str(item.get("display_name") or item.get("role_key") or ""),
            "canonPath": str(item.get("canon_path") or ""),
        }
        for item in role_registry.get("foundations", [])
        if isinstance(item, dict) and item.get("role_key")
    ]
    capabilities = [
        {
            "capabilityKey": str(item.get("role_key") or ""),
            "fNumber": str(item.get("current_f_number") or item.get("role_key") or ""),
            "displayName": str(item.get("display_name") or item.get("role_key") or ""),
            "canonPath": str(item.get("canon_path") or ""),
        }
        for item in role_registry.get("roles", [])
        if isinstance(item, dict) and item.get("role_key")
    ]
    return {"knowledgeDomains": knowledge_domains, "capabilities": capabilities}


def default_organization_registry() -> dict[str, Any]:
    catalog = knowledge_catalog()
    return {
        "schemaVersion": 1,
        "producedAt": utc_now(),
        "producer": "steel-mission-chat",
        "activeOrganizationId": "northstar-forge",
        "organizations": [
            {
                "id": "northstar-forge",
                "name": "Northstar Forge",
                "slug": "northstar-forge",
                "identifiers": {
                    "legalName": "Northstar Forge Ltd.",
                    "domain": "northstar.example",
                    "country": "CH",
                    "environment": "starter",
                    "dataClassification": "synthetic-starter",
                },
                "knowledgeDomainKeys": [
                    item["domainKey"] for item in catalog["knowledgeDomains"] if item.get("domainKey")
                ] or ["KD01", "KD02", "KD03"],
                "domainCapabilityKeys": [
                    item["capabilityKey"] for item in catalog["capabilities"] if item.get("capabilityKey")
                ] or [f"DC{i:02d}" for i in range(1, 14)],
                "knowledgeSources": {
                    "repositories": [
                        {"name": "steel-mission-product", "path": "${WORKER_DIR}", "owner": "platform-owner", "lastReviewedAt": utc_now(), "maxAgeDays": 90, "required": True, "authoritative": True},
                        {"name": "starter-company", "path": "${ORG_DIR}", "owner": "organization-owner", "lastReviewedAt": utc_now(), "maxAgeDays": 90, "required": True, "authoritative": True},
                    ],
                    "documents": [
                        {"title": "Starter Organization Operating Context", "path": "${ORG_DIR}/canon/KD01 Operating Context.md", "owner": "organization-owner", "lastReviewedAt": utc_now(), "maxAgeDays": 90, "required": True, "authoritative": True},
                        {"title": "Starter Organization Team Doctrine", "path": "${ORG_DIR}/canon/KD02 Team Doctrine.md", "owner": "organization-owner", "lastReviewedAt": utc_now(), "maxAgeDays": 90, "required": True, "authoritative": True},
                        {"title": "Starter Organization Team Roster and Workflow", "path": "${ORG_DIR}/canon/KD03 Team Roster and Workflow.md", "owner": "organization-owner", "lastReviewedAt": utc_now(), "maxAgeDays": 90, "required": True, "authoritative": True},
                        {"title": "Starter Organization Capability Map", "path": "${ORG_DIR}/canon/Domain Capabilities.md", "owner": "organization-owner", "lastReviewedAt": utc_now(), "maxAgeDays": 90, "required": True, "authoritative": True},
                    ],
                },
                "notes": "Synthetic starter organization for first-run demonstrations. Owners and admins can rename it or create additional organizations.",
            }
        ],
    }


def normalize_organization_registry(payload: dict[str, Any]) -> dict[str, Any]:
    catalog = knowledge_catalog()
    valid_kds = {item["domainKey"] for item in catalog["knowledgeDomains"] if item.get("domainKey")}
    valid_dcs = {item["capabilityKey"] for item in catalog["capabilities"] if item.get("capabilityKey")}
    organizations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(payload.get("organizations", [])):
        if not isinstance(item, dict):
            continue
        name = clean_optional_string(item.get("name") or item.get("displayName") or f"Organization {index + 1}", limit=120)
        org_id = safe_org_id(str(item.get("id") or item.get("slug") or name), f"organization-{index + 1}")
        if org_id in seen:
            suffix = 2
            base = org_id[:70] or "organization"
            while f"{base}-{suffix}" in seen:
                suffix += 1
            org_id = f"{base}-{suffix}"
        seen.add(org_id)
        identifiers = item.get("identifiers") if isinstance(item.get("identifiers"), dict) else {}
        sources = normalize_general_knowledge_registry(item.get("knowledgeSources") if isinstance(item.get("knowledgeSources"), dict) else {})
        kd_keys = [
            value for value in clean_string_list(item.get("knowledgeDomainKeys"), limit=80)
            if not valid_kds or value in valid_kds
        ]
        dc_keys = [
            value for value in clean_string_list(item.get("domainCapabilityKeys"), limit=120)
            if not valid_dcs or value in valid_dcs
        ]
        organizations.append({
            "id": org_id,
            "name": name or org_id,
            "slug": safe_org_id(str(item.get("slug") or org_id), org_id),
            "identifiers": {
                "legalName": clean_optional_string(identifiers.get("legalName") or item.get("legalName"), limit=160),
                "domain": clean_optional_string(identifiers.get("domain") or item.get("domain"), limit=160),
                "country": clean_optional_string(identifiers.get("country") or item.get("country"), limit=80),
                "environment": clean_optional_string(identifiers.get("environment") or item.get("environment") or "starter", limit=80),
                "dataClassification": clean_optional_string(
                    identifiers.get("dataClassification") or item.get("dataClassification") or "synthetic-starter",
                    limit=120,
                ),
            },
            "knowledgeDomainKeys": (
                kd_keys if isinstance(item.get("knowledgeDomainKeys"), list) else sorted(valid_kds)
            ),
            "domainCapabilityKeys": (
                dc_keys if isinstance(item.get("domainCapabilityKeys"), list) else sorted(valid_dcs)
            ),
            "knowledgeSources": {
                "repositories": sources.get("repositories", []),
                "documents": sources.get("documents", []),
            },
            "notes": clean_optional_string(item.get("notes"), limit=800),
        })
    if not organizations:
        return normalize_organization_registry(default_organization_registry())
    active_id = safe_org_id(str(payload.get("activeOrganizationId") or ""), "")
    if active_id not in {item["id"] for item in organizations}:
        active_id = organizations[0]["id"]
    return {
        "schemaVersion": 1,
        "producedAt": str(payload.get("producedAt") or utc_now()),
        "producer": "steel-mission-chat",
        "activeOrganizationId": active_id,
        "organizations": organizations,
    }


def organization_registry() -> dict[str, Any]:
    payload = read_json_file(ORGANIZATION_REGISTRY_PATH) or default_organization_registry()
    return normalize_organization_registry(payload)


def active_organization() -> dict[str, Any]:
    registry = organization_registry()
    active_id = registry.get("activeOrganizationId")
    for organization in registry.get("organizations", []):
        if isinstance(organization, dict) and organization.get("id") == active_id:
            return organization
    organizations = registry.get("organizations", [])
    return organizations[0] if organizations and isinstance(organizations[0], dict) else {}


def merge_knowledge_sources(*registries: dict[str, Any]) -> dict[str, Any]:
    repositories: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    repo_keys: set[tuple[str, str]] = set()
    doc_keys: set[tuple[str, str]] = set()
    registry_scopes = ["shared-registry", "organization-registry"]
    for registry_index, registry in enumerate(registries):
        normalized = normalize_general_knowledge_registry(registry if isinstance(registry, dict) else {})
        registry_scope = registry_scopes[registry_index] if registry_index < len(registry_scopes) else f"registry-{registry_index + 1}"
        for item in normalized.get("repositories", []):
            key = (str(item.get("name") or ""), str(item.get("path") or ""))
            if key[1] and key not in repo_keys:
                provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
                repositories.append({
                    **item,
                    "provenance": {
                        "registry": provenance.get("registry") or registry_scope,
                        "registryProducedAt": provenance.get("registryProducedAt") or normalized.get("producedAt") or "",
                        "sourcePath": item.get("path") or "",
                    },
                })
                repo_keys.add(key)
        for item in normalized.get("documents", []):
            key = (str(item.get("title") or ""), str(item.get("path") or ""))
            if key[1] and key not in doc_keys:
                provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
                documents.append({
                    **item,
                    "provenance": {
                        "registry": provenance.get("registry") or registry_scope,
                        "registryProducedAt": provenance.get("registryProducedAt") or normalized.get("producedAt") or "",
                        "sourcePath": item.get("path") or "",
                    },
                })
                doc_keys.add(key)
    return {
        "schemaVersion": 1,
        "producedAt": utc_now(),
        "producer": "steel-mission-chat",
        "repositories": repositories,
        "documents": documents,
    }


def effective_knowledge_sources() -> dict[str, Any]:
    organization = active_organization()
    return merge_knowledge_sources(
        general_knowledge_registry(),
        organization.get("knowledgeSources", {}) if isinstance(organization.get("knowledgeSources"), dict) else {},
    )


def parse_knowledge_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def knowledge_quality_report() -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    effective = effective_knowledge_sources()
    sources = [
        *[item for item in effective.get("repositories", []) if isinstance(item, dict)],
        *[item for item in effective.get("documents", []) if isinstance(item, dict)],
    ]
    issues: list[dict[str, Any]] = []
    assessed: list[dict[str, Any]] = []
    for item in sources:
        source_kind = str(item.get("sourceKind") or "knowledge")
        label = str(item.get("name") or item.get("title") or item.get("path") or "knowledge source")
        source_id = f"{source_kind}:{label.strip().lower()}"
        path = resolve_config_path(str(item.get("path") or ""))
        exists = path.exists()
        owner = str(item.get("owner") or "").strip()
        required = item.get("required") is not False
        reviewed_at = parse_knowledge_timestamp(item.get("lastReviewedAt"))
        expires_at = parse_knowledge_timestamp(item.get("expiresAt"))
        max_age_days = item.get("maxAgeDays") if isinstance(item.get("maxAgeDays"), int) else None
        stale = bool(expires_at and expires_at <= now)
        if reviewed_at and max_age_days is not None:
            stale = stale or reviewed_at + dt.timedelta(days=max_age_days) <= now
        freshness = "expired" if expires_at and expires_at <= now else "stale" if stale else "current" if reviewed_at or expires_at else "unknown"
        if not exists:
            issues.append({
                "id": "missing-source",
                "severity": "error" if required else "warning",
                "sourceId": source_id,
                "message": f"{label} is unavailable at {path}.",
                "owner": owner,
            })
        if stale:
            issues.append({
                "id": "stale-source",
                "severity": "error" if required else "warning",
                "sourceId": source_id,
                "message": f"{label} is {freshness} and requires review.",
                "owner": owner,
            })
        elif freshness == "unknown":
            issues.append({
                "id": "unknown-freshness",
                "severity": "warning",
                "sourceId": source_id,
                "message": f"{label} has no review or expiration metadata.",
                "owner": owner,
            })
        if not owner:
            issues.append({
                "id": "unowned-source",
                "severity": "warning",
                "sourceId": source_id,
                "message": f"{label} has no accountable owner.",
                "owner": "",
            })
        assessed.append({
            "sourceId": source_id,
            "sourceKind": source_kind,
            "label": label,
            "path": str(path),
            "exists": exists,
            "required": required,
            "authoritative": item.get("authoritative") is True,
            "owner": owner,
            "lastReviewedAt": str(item.get("lastReviewedAt") or ""),
            "expiresAt": str(item.get("expiresAt") or ""),
            "maxAgeDays": max_age_days,
            "freshness": freshness,
            "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
        })

    logical_sources: dict[str, list[dict[str, Any]]] = {}
    for registry_name, registry in [
        ("shared-registry", general_knowledge_registry()),
        ("organization-registry", active_organization().get("knowledgeSources", {})),
    ]:
        normalized = normalize_general_knowledge_registry(registry if isinstance(registry, dict) else {})
        for item in [*normalized.get("repositories", []), *normalized.get("documents", [])]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("name") or item.get("title") or "").strip().lower()
            kind = str(item.get("sourceKind") or "knowledge")
            logical_sources.setdefault(f"{kind}:{label}", []).append({**item, "registry": registry_name})
    conflicts: list[dict[str, Any]] = []
    for source_id, candidates in logical_sources.items():
        paths = {str(item.get("path") or "") for item in candidates}
        if len(paths) <= 1:
            continue
        authoritative = [item for item in candidates if item.get("authoritative") is True]
        severity = "error" if len(authoritative) > 1 else "warning"
        conflict = {
            "id": "conflicting-source",
            "severity": severity,
            "sourceId": source_id,
            "message": f"{source_id} resolves to multiple source paths: {', '.join(sorted(paths))}.",
            "candidates": [
                {"path": item.get("path") or "", "registry": item.get("registry") or "", "owner": item.get("owner") or "", "authoritative": item.get("authoritative") is True}
                for item in candidates
            ],
        }
        conflicts.append(conflict)
        issues.append(conflict)

    if not sources:
        issues.append({"id": "no-knowledge-sources", "severity": "error", "sourceId": "knowledge", "message": "No organizational knowledge sources are configured."})
    elif not any(item["exists"] for item in assessed):
        issues.append({"id": "no-available-knowledge", "severity": "error", "sourceId": "knowledge", "message": "No configured organizational knowledge source is currently available."})
    error_count = len([item for item in issues if item.get("severity") == "error"])
    warning_count = len(issues) - error_count
    context_sufficient = error_count == 0
    status = "healthy" if not issues else "warning" if context_sufficient else "insufficient"
    directive = (
        "Organizational context is insufficient. Do not infer missing or conflicting organizational facts; identify the gaps and request owner input."
        if not context_sufficient else
        "Organizational context has quality warnings. Cite source provenance and state uncertainty where warnings affect the answer."
        if issues else
        "Organizational context passed configured availability, freshness, ownership, and conflict checks."
    )
    report = {
        "schemaVersion": 1,
        "status": status,
        "contextSufficient": context_sufficient,
        "checkedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sourceCount": len(assessed),
        "availableSourceCount": len([item for item in assessed if item["exists"]]),
        "staleSourceCount": len([item for item in assessed if item["freshness"] in {"stale", "expired"}]),
        "unownedSourceCount": len([item for item in assessed if not item["owner"]]),
        "conflictCount": len(conflicts),
        "errorCount": error_count,
        "warningCount": warning_count,
        "issues": issues,
        "sources": assessed,
        "confidenceDirective": directive,
    }
    return {**report, "qualityHash": canonical_json_hash(report)}


def save_organization_registry(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can manage organizations")
    before = read_json_file(ORGANIZATION_REGISTRY_PATH)
    registry = normalize_organization_registry({**payload, "producedAt": utc_now()})
    validate_configuration_write(
        "organizations-saved",
        role,
        ORGANIZATION_REGISTRY_PATH,
        before,
        registry,
        "organization-registry-v1.json",
    )
    atomic_write_json(ORGANIZATION_REGISTRY_PATH, registry)
    record_mutation(
        "organizations-saved",
        role,
        ORGANIZATION_REGISTRY_PATH,
        before=before,
        after=registry,
        details={
            "organizations": len(registry.get("organizations", [])),
            "activeOrganizationId": registry.get("activeOrganizationId") or "",
        },
    )
    return registry


def save_general_knowledge_registry(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can manage organization knowledge")
    before = read_json_file(GENERAL_KNOWLEDGE_PATH)
    registry = normalize_general_knowledge_registry({**payload, "producedAt": utc_now()})
    tmp = GENERAL_KNOWLEDGE_PATH.with_name(f".{GENERAL_KNOWLEDGE_PATH.name}.{os.getpid()}.tmp")
    GENERAL_KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, GENERAL_KNOWLEDGE_PATH)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    record_mutation(
        "general-knowledge-saved",
        role,
        GENERAL_KNOWLEDGE_PATH,
        before=before,
        after=registry,
        details={"repositories": len(registry.get("repositories", [])), "documents": len(registry.get("documents", []))},
    )
    return registry


def upload_organization_knowledge(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can upload organization knowledge")
    label = str(payload.get("label") or payload.get("name") or "organization-knowledge").strip()
    source_kind = str(payload.get("sourceKind") or "files").strip()
    files = normalize_upload_files(payload.get("files"), max_files=200)
    if not files:
        raise ValueError("at least one file is required")
    root = save_uploaded_knowledge_files(files, label)
    current_registry = organization_registry()
    requested_org = safe_org_id(str(payload.get("organizationId") or current_registry.get("activeOrganizationId") or ""), "")
    organizations = [item for item in current_registry.get("organizations", []) if isinstance(item, dict)]
    org_index = next((index for index, item in enumerate(organizations) if item.get("id") == requested_org), 0)
    organization = organizations[org_index] if organizations else active_organization()
    current = organization.get("knowledgeSources", {}) if isinstance(organization.get("knowledgeSources"), dict) else {}
    repositories = [item for item in current.get("repositories", []) if isinstance(item, dict)]
    documents = [item for item in current.get("documents", []) if isinstance(item, dict)]
    if source_kind == "folder" or len(files) > 1:
        repositories.append({
            "name": safe_path_part(label, "uploaded-folder"),
            "path": str(root),
            "description": f"Uploaded organization knowledge folder with {len(files)} files.",
            "owner": role,
            "lastReviewedAt": utc_now(),
            "maxAgeDays": 90,
            "required": True,
            "authoritative": False,
        })
    else:
        stored_files = sorted(path for path in root.rglob("*") if path.is_file())
        target = stored_files[0] if stored_files else root
        documents.append({
            "title": label or files[0]["name"],
            "path": str(target),
            "kind": files[0].get("type") or "uploaded-file",
            "description": "Uploaded organization knowledge document.",
            "owner": role,
            "lastReviewedAt": utc_now(),
            "maxAgeDays": 90,
            "required": True,
            "authoritative": False,
        })
    organization = {
        **organization,
        "knowledgeSources": {"repositories": repositories, "documents": documents},
    }
    if organizations:
        organizations[org_index] = organization
    else:
        organizations = [organization]
    registry = save_organization_registry({
        **current_registry,
        "activeOrganizationId": organization.get("id") or current_registry.get("activeOrganizationId") or "",
        "organizations": organizations,
    }, role)
    mission = start_orchestrated_mission(
        "prepare-knowledge",
        f"Prepare the first snapshot after adding organization knowledge: {label}",
        mock=False,
        profile=str(payload.get("profile") or active_runtime_profile()),
        operator_role=role,
    )
    return {
        "schemaVersion": 1,
        "ok": True,
        "uploadRoot": str(root),
        "fileCount": len(files),
        "organizationId": organization.get("id") or "",
        "registry": organization.get("knowledgeSources", {"repositories": [], "documents": []}),
        "organizationRegistry": registry,
        "mission": mission,
    }


def knowledge_file_sample(root: Path, *, limit: int = 120) -> dict[str, Any]:
    if not root.exists():
        return {"exists": False, "fileCount": 0, "byteCount": 0, "sample": [], "omitted": 0}
    paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    sample: list[dict[str, Any]] = []
    byte_count = 0
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        byte_count += stat.st_size
        if len(sample) < limit:
            sample.append({
                "path": str(path),
                "relativePath": str(path.relative_to(root)) if root.is_dir() else path.name,
                "bytes": stat.st_size,
                "sha256": file_sha256(path) or "",
            })
    return {
        "exists": True,
        "fileCount": len(paths),
        "byteCount": byte_count,
        "sample": sample,
        "omitted": max(0, len(paths) - len(sample)),
    }


def prepare_knowledge_snapshot_payload(profile: str | None = None) -> dict[str, Any]:
    organization = active_organization()
    registry = effective_knowledge_sources()
    sources: list[dict[str, Any]] = []
    for item in registry.get("repositories", []):
        if not isinstance(item, dict):
            continue
        path = resolve_config_path(str(item.get("path") or ""))
        sample = knowledge_file_sample(path)
        sources.append({
            "sourceKind": "repository",
            "name": item.get("name") or path.name,
            "path": str(path),
            **{key: item[key] for key in ("owner", "lastReviewedAt", "expiresAt", "maxAgeDays", "required", "authoritative", "provenance") if key in item},
            **sample,
        })
    for item in registry.get("documents", []):
        if not isinstance(item, dict):
            continue
        path = resolve_config_path(str(item.get("path") or ""))
        sample = knowledge_file_sample(path)
        sources.append({
            "sourceKind": "document",
            "title": item.get("title") or path.name,
            "path": str(path),
            **{key: item[key] for key in ("owner", "lastReviewedAt", "expiresAt", "maxAgeDays", "required", "authoritative", "provenance") if key in item},
            **sample,
        })
    missing = [source for source in sources if not source.get("exists")]
    quality = knowledge_quality_report()
    return {
        "schemaVersion": 1,
        "profile": profile or active_runtime_profile(),
        "organization": {
            "id": organization.get("id") or "",
            "name": organization.get("name") or "",
            "slug": organization.get("slug") or "",
            "identifiers": organization.get("identifiers", {}),
            "knowledgeDomainKeys": organization.get("knowledgeDomainKeys", []),
            "domainCapabilityKeys": organization.get("domainCapabilityKeys", []),
        },
        "registryHash": canonical_json_hash(registry),
        "sourceCount": len(sources),
        "availableSourceCount": len(sources) - len(missing),
        "missingSourceCount": len(missing),
        "fileCount": sum(int(source.get("fileCount") or 0) for source in sources),
        "byteCount": sum(int(source.get("byteCount") or 0) for source in sources),
        "sources": sources,
        "knowledgeQuality": quality,
        "contextSufficient": quality.get("contextSufficient") is True,
        "warnings": quality.get("issues", []),
        "preparedAt": utc_now(),
        "producer": "steel-mission-chat knowledge-preparer",
    }


def corporate_role(value: str | None) -> str:
    aliases = {
        "owner": "owner",
        "admin": "admin",
        "org-admin": "admin",
        "publisher": "publisher",
        "user": "user",
        "local-user": "user",
    }
    return aliases.get(str(value or "").strip(), "user")


def worker_operator_role(value: str | None) -> str:
    role = corporate_role(value)
    if role in {"owner", "admin"}:
        return "org-admin"
    if role == "publisher":
        return "publisher"
    return "local-user"


def normalize_work_mode(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return "normal" if text in {"normal", "chat", "normal-chat"} else "domain-capabilities"


def normalize_user_registry(payload: dict[str, Any]) -> dict[str, Any]:
    valid_domain_capabilities = {
        str(role.get("roleKey"))
        for role in knowledge_registry().get("roles", [])
        if isinstance(role, dict) and role.get("roleKey")
    }
    organizations = organization_registry()
    valid_organization_ids = {
        str(item.get("id")) for item in organizations.get("organizations", [])
        if isinstance(item, dict) and item.get("id")
    }
    default_organization_id = str(organizations.get("activeOrganizationId") or "")
    users = []
    for index, item in enumerate(payload.get("users", [])):
        if not isinstance(item, dict):
            continue
        role = corporate_role(str(item.get("role") or "user"))
        principal_id = safe_path_part(str(item.get("id") or item.get("email") or f"user-{index + 1}"), f"user-{index + 1}")
        source_assignments = item.get("assignedCapabilities")
        if not isinstance(source_assignments, list):
            source_assignments = item.get("assignedCapabilities", [])
        assigned = [
            str(value).strip()
            for value in source_assignments
            if str(value).strip() and (not valid_domain_capabilities or str(value).strip() in valid_domain_capabilities)
        ]
        assigned = sorted(set(assigned))
        organization_ids = [
            value for value in clean_string_list(item.get("organizationIds"), limit=50)
            if not valid_organization_ids or value in valid_organization_ids
        ]
        if not organization_ids and default_organization_id:
            organization_ids = [default_organization_id]
        identity_subjects = sorted(set(clean_string_list(item.get("identitySubjects"), limit=50)))
        external = item.get("externalIdentities") if isinstance(item.get("externalIdentities"), dict) else {}
        external_identities = {
            source: sorted(set(clean_string_list(external.get(source), limit=50)))
            for source in ("github", "slack", "jira")
        }
        users.append({
            "id": principal_id,
            "name": str(item.get("name") or principal_id).strip(),
            "email": str(item.get("email") or "").strip(),
            "role": role,
            "status": "disabled" if str(item.get("status") or "").strip() == "disabled" else "active",
            "assignedCapabilities": assigned,
            "organizationIds": organization_ids,
            "identitySubjects": identity_subjects,
            "externalIdentities": external_identities,
        })
    if not users:
        users = [
            {"id": "owner", "name": "Owner", "email": "", "role": "owner", "status": "active", "assignedCapabilities": [], "organizationIds": [default_organization_id] if default_organization_id else [], "identitySubjects": [], "externalIdentities": {}},
            {"id": "admin", "name": "Admin", "email": "", "role": "admin", "status": "active", "assignedCapabilities": [], "organizationIds": [default_organization_id] if default_organization_id else [], "identitySubjects": [], "externalIdentities": {}},
            {"id": "publisher", "name": "Publisher", "email": "", "role": "publisher", "status": "active", "assignedCapabilities": ["DC13"], "organizationIds": [default_organization_id] if default_organization_id else [], "identitySubjects": [], "externalIdentities": {}},
            {"id": "user", "name": "User", "email": "", "role": "user", "status": "active", "assignedCapabilities": ["DC13"], "organizationIds": [default_organization_id] if default_organization_id else [], "identitySubjects": [], "externalIdentities": {}},
        ]
    return {
        "schemaVersion": 1,
        "producedAt": str(payload.get("producedAt") or utc_now()),
        "producer": "steel-mission-chat",
        "users": users,
    }


def user_registry() -> dict[str, Any]:
    payload = read_json_file(USER_REGISTRY_PATH) or {}
    return normalize_user_registry(payload)


def registered_user(user_id: str) -> dict[str, Any] | None:
    selected = clean_optional_string(user_id, limit=200)
    return next((dict(item) for item in user_registry().get("users", [])
                 if isinstance(item, dict) and item.get("id") == selected), None)


def resolve_registered_identity(claims: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any] | None:
    selected_policy = policy or auth_policy()
    issuer = clean_optional_string(claims.get("iss"), limit=500)
    subject = clean_optional_string(claims.get("sub"), limit=500)
    email = clean_optional_string(claims.get("email"), limit=320).lower()
    subject_keys = {value for value in (subject, f"{issuer}|{subject}" if issuer and subject else "") if value}
    for item in user_registry().get("users", []):
        if not isinstance(item, dict) or item.get("status") != "active":
            continue
        configured_subjects = set(clean_string_list(item.get("identitySubjects"), limit=50))
        configured_email = clean_optional_string(item.get("email"), limit=320).lower()
        if subject_keys.intersection(configured_subjects) or (email and configured_email and hmac.compare_digest(email, configured_email)):
            return actor_from_registered_user(item, selected_policy)
    return None


def actor_from_registered_user(user: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    organizations = clean_string_list(user.get("organizationIds"), limit=50)
    active_id = str(organization_registry().get("activeOrganizationId") or "")
    organization_id = active_id if active_id in organizations else (organizations[0] if organizations else "")
    return {
        "actorId": str(user.get("id") or "registered-user"),
        "role": corporate_role(str(user.get("role") or "user")),
        "capabilities": clean_string_list(user.get("assignedCapabilities"), limit=200),
        "organizationIds": organizations,
        "organizationId": organization_id,
        "identitySource": "user-registry",
        "authPolicyHash": canonical_json_hash(policy or auth_policy()),
    }


def resolve_external_identity(source: str, external_actor: dict[str, Any], connector: dict[str, Any]) -> dict[str, Any] | None:
    service_user_id = clean_optional_string(connector.get("serviceUserId"), limit=200)
    if service_user_id:
        service_user = registered_user(service_user_id)
        if service_user and service_user.get("status") == "active":
            return actor_from_registered_user(service_user)
    candidates = {
        str(value).strip().lower() for value in (external_actor.get("id"), external_actor.get("name"), external_actor.get("login"))
        if str(value or "").strip()
    }
    for user in user_registry().get("users", []):
        if not isinstance(user, dict) or user.get("status") != "active":
            continue
        external = user.get("externalIdentities") if isinstance(user.get("externalIdentities"), dict) else {}
        configured = {value.lower() for value in clean_string_list(external.get(source), limit=50)}
        if candidates.intersection(configured):
            return actor_from_registered_user(user)
    return None


def save_user_registry(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can manage users")
    users = payload.get("users")
    if not isinstance(users, list) or not users:
        raise ValueError("users must contain at least one user")
    unsupported_statuses = sorted({
        str(item.get("status")).strip()
        for item in users
        if isinstance(item, dict)
        and item.get("status") not in {None, "", "active", "disabled"}
    })
    if unsupported_statuses:
        raise ValueError(
            "unsupported user statuses: " + ", ".join(unsupported_statuses)
            + "; accepted values are active and disabled"
        )
    valid_domain_capabilities = {
        str(item.get("roleKey"))
        for item in knowledge_registry().get("roles", [])
        if isinstance(item, dict) and item.get("roleKey")
    }
    submitted_capabilities = {
        str(value).strip()
        for item in users
        if isinstance(item, dict) and isinstance(item.get("assignedCapabilities"), list)
        for value in item.get("assignedCapabilities", [])
        if str(value).strip()
    }
    unknown_capabilities = sorted(submitted_capabilities - valid_domain_capabilities)
    if unknown_capabilities:
        raise ValueError("unknown assigned capability keys: " + ", ".join(unknown_capabilities))
    before = read_json_file(USER_REGISTRY_PATH)
    registry = normalize_user_registry({**payload, "producedAt": utc_now()})
    validate_configuration_write(
        "users-saved",
        role,
        USER_REGISTRY_PATH,
        before,
        registry,
        "user-registry-v1.json",
    )
    atomic_write_json(USER_REGISTRY_PATH, registry)
    record_mutation(
        "users-saved",
        role,
        USER_REGISTRY_PATH,
        before=before,
        after=registry,
        details={"users": len(registry.get("users", []))},
    )
    return registry


def default_domain_capabilities() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "producedAt": utc_now(),
        "producer": "steel-mission-chat",
        "userAssignments": [
            {"userId": "publisher", "role": "publisher", "assignedCapabilities": ["DC13"]},
            {"userId": "user", "role": "user", "assignedCapabilities": ["DC13"]},
        ],
    }


def normalize_assignment_registry(payload: dict[str, Any]) -> dict[str, Any]:
    knowledge = knowledge_registry()
    roles = {
        role.get("roleKey"): role
        for role in knowledge.get("roles", [])
        if isinstance(role, dict) and role.get("roleKey")
    }
    user_assignments: dict[str, dict[str, Any]] = {}
    # `assignments` is the shipped authoring shape and is authoritative when a
    # normalized file contains both representations. `userAssignments` remains
    # the fallback for normalized-only registries written by older versions.
    if isinstance(payload.get("assignments"), list):
        for item in payload.get("assignments", []):
            if not isinstance(item, dict):
                continue
            if item.get("roleKey") not in roles:
                continue
            role_key = str(item.get("roleKey"))
            for user_id in [str(value).strip() for value in item.get("publishers", []) if str(value).strip()]:
                entry = user_assignments.setdefault(safe_path_part(user_id), {"userId": safe_path_part(user_id), "role": "publisher", "assignedCapabilities": []})
                entry["assignedCapabilities"].append(role_key)
            for user_id in [str(value).strip() for value in item.get("users", []) if str(value).strip()]:
                entry = user_assignments.setdefault(safe_path_part(user_id), {"userId": safe_path_part(user_id), "role": "user", "assignedCapabilities": []})
                entry["assignedCapabilities"].append(role_key)
    elif isinstance(payload.get("userAssignments"), list):
        for item in payload.get("userAssignments", []):
            if not isinstance(item, dict):
                continue
            user_id = safe_path_part(str(item.get("userId") or item.get("id") or ""), "")
            if not user_id:
                continue
            assigned = [
                str(value).strip()
                for value in item.get("assignedCapabilities", [])
                if str(value).strip() in roles
            ]
            user_assignments[user_id] = {
                "userId": user_id,
                "role": corporate_role(str(item.get("role") or "user")),
                "assignedCapabilities": sorted(set(assigned)),
            }
    for entry in user_assignments.values():
        entry["assignedCapabilities"] = sorted(set(entry.get("assignedCapabilities", [])))
    assignments = []
    for key, role in roles.items():
        publishers = [
            entry["userId"] for entry in user_assignments.values()
            if entry.get("role") == "publisher" and key in entry.get("assignedCapabilities", [])
        ]
        users = [
            entry["userId"] for entry in user_assignments.values()
            if entry.get("role") == "user" and key in entry.get("assignedCapabilities", [])
        ]
        assignments.append({
            "roleKey": key,
            "fNumber": role.get("currentFNumber"),
            "displayName": role.get("displayName"),
            "publishers": sorted(set(publishers)),
            "users": sorted(set(users)),
        })
    return {
        "schemaVersion": 1,
        "producedAt": str(payload.get("producedAt") or utc_now()),
        "producer": "steel-mission-chat",
        "userAssignments": sorted(user_assignments.values(), key=lambda item: item["userId"]),
        "assignments": assignments,
    }


def domain_capability_registry() -> dict[str, Any]:
    try:
        payload = json.loads(DOMAIN_CAPABILITIES_PATH.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        record_mutation(
            "domain-capability-registry-read",
            "user",
            DOMAIN_CAPABILITIES_PATH,
            status="failed",
            details={"error": str(exc)[:500], "errorType": type(exc).__name__},
        )
        raise RuntimeError(f"domain capability registry could not be read: {exc}") from exc
    return normalize_assignment_registry(payload if isinstance(payload, dict) else {})


def capability_assignment_user_ids(payload: dict[str, Any]) -> set[str]:
    user_ids: set[str] = set()
    if isinstance(payload.get("assignments"), list):
        for item in payload.get("assignments", []):
            if not isinstance(item, dict):
                continue
            for field in ("publishers", "users"):
                values = item.get(field)
                if isinstance(values, list):
                    user_ids.update(str(value).strip() for value in values if str(value).strip())
    elif isinstance(payload.get("userAssignments"), list):
        for item in payload.get("userAssignments", []):
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("userId") or item.get("id") or "").strip()
            if user_id:
                user_ids.add(user_id)
    return user_ids


def save_domain_capability_registry(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can assign domain capabilities")
    submitted_assignments = (
        payload.get("assignments")
        if isinstance(payload.get("assignments"), list)
        else payload.get("userAssignments")
        if isinstance(payload.get("userAssignments"), list)
        else None
    )
    if not submitted_assignments:
        raise ValueError("assignments must contain at least one assignment")
    registered_ids = {
        str(item.get("id"))
        for item in user_registry().get("users", [])
        if isinstance(item, dict) and item.get("id")
    }
    unknown_ids = sorted(capability_assignment_user_ids(payload) - registered_ids)
    if unknown_ids:
        raise ValueError("capability assignments name unknown users: " + ", ".join(unknown_ids))
    before = read_json_file(DOMAIN_CAPABILITIES_PATH)
    registry = normalize_assignment_registry({**payload, "producedAt": utc_now()})
    validate_configuration_write(
        "domain-capabilities-saved",
        role,
        DOMAIN_CAPABILITIES_PATH,
        before,
        registry,
        "domain-capability-registry-v1.json",
    )
    tmp = DOMAIN_CAPABILITIES_PATH.with_name(f".{DOMAIN_CAPABILITIES_PATH.name}.{os.getpid()}.tmp")
    DOMAIN_CAPABILITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, DOMAIN_CAPABILITIES_PATH)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    record_mutation(
        "domain-capabilities-saved",
        role,
        DOMAIN_CAPABILITIES_PATH,
        before=before,
        after=registry,
        details={"assignments": len(registry.get("assignments", []))},
    )
    return registry


def corporate_workspace(role: str) -> dict[str, Any]:
    selected = corporate_role(role)
    assignments = domain_capability_registry()
    users = user_registry().get("users", [])
    knowledge = knowledge_registry()
    organization = active_organization()
    by_key = {
        item.get("roleKey"): item
        for item in knowledge.get("roles", [])
        if isinstance(item, dict)
    }
    active_users = [user for user in users if isinstance(user, dict) and user.get("status") == "active"]
    user_assigned_keys = {
        str(key)
        for user in active_users
        if selected in {"owner", "admin"} or user.get("role") == selected
        for key in user.get("assignedCapabilities", [])
        if str(key) in by_key
    }
    if not user_assigned_keys and selected not in {"owner", "admin"}:
        for assignment in assignments.get("assignments", []):
            if selected == "publisher" and assignment.get("publishers"):
                user_assigned_keys.add(str(assignment.get("roleKey")))
            if selected == "user" and assignment.get("users"):
                user_assigned_keys.add(str(assignment.get("roleKey")))
    visible = []
    for role_key, role_payload in by_key.items():
        allowed = selected in {"owner", "admin"} or role_key in user_assigned_keys
        if allowed:
            visible.append({
                "capabilityKey": role_key,
                "roleKey": role_key,
                "fNumber": role_payload.get("currentFNumber"),
                "displayName": role_payload.get("displayName"),
                "knowledge": role_payload,
            })
    return {
        "ok": True,
        "role": selected,
        "canAssign": selected in {"owner", "admin"},
        "canPublish": selected in {"owner", "admin", "publisher"},
        "canUse": True,
        "foundations": knowledge.get("foundations", []),
        "knowledgeDomains": knowledge.get("knowledgeDomains", []),
        "generalKnowledge": knowledge.get("generalKnowledge", {"repositories": [], "documents": []}),
        "effectiveKnowledge": knowledge.get("effectiveKnowledge", {"repositories": [], "documents": []}),
        "activeOrganization": organization,
        "organizationRegistry": organization_registry() if selected in {"owner", "admin"} else {
            "schemaVersion": 1,
            "activeOrganizationId": organization.get("id") or "",
            "organizations": [organization] if organization else [],
        },
        "canManageOrganizations": selected in {"owner", "admin"},
        "assignments": assignments.get("assignments", []),
        "userAssignments": assignments.get("userAssignments", []),
        "users": active_users if selected in {"owner", "admin"} else [
            {key: user[key] for key in ("id", "name", "role", "status", "assignedCapabilities") if key in user}
            for user in active_users if user.get("role") == selected
        ],
        "visibleRoles": visible,
        "visibleCapabilities": visible,
    }


def worker_json_command(args: list[str], payload: dict[str, Any] | None = None, timeout: int = 15) -> tuple[int, dict[str, Any]]:
    try:
        result = subprocess.run(
            [str(WORKER_BIN), *args],
            input=json.dumps(payload) if payload is not None else None,
            cwd=str(WORKER_DIR),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 503, {"ok": False, "error": str(exc)}
    data = parse_json(result.stdout) or parse_json(result.stderr)
    if result.returncode != 0:
        return 400, {"ok": False, "error": data.get("reason") or "worker command failed", "details": data}
    return 200, {"ok": True, "payload": data}


def resolve_model_policy(role: str | None = None) -> dict[str, Any]:
    selected_role = role or active_coordinator_role()
    try:
        result = subprocess.run(
            [str(WORKER_BIN), "model-role-resolve", selected_role],
            cwd=str(WORKER_DIR),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
        if result.returncode == 0 and isinstance(payload, dict) and payload.get("provider") in {"claude", "glimmer"}:
            return payload
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    provider = active_coordinator_provider()
    native_capabilities = {
        "claude": ["model-effort-controls", "provider-native-cli", "streaming-progress", "structured-output"],
        "glimmer": ["local-inference", "offline-operation", "persistent-model-residency", "structured-json-output"],
    }
    return {
        "schemaVersion": 1,
        "role": selected_role,
        "selectedModel": "qwen2.5-coder:14b" if provider == "glimmer" else "claude-sonnet-5",
        "provider": provider,
        "transport": "ollama" if provider == "glimmer" else "claude-code",
        "snapshotProfile": "worker-local-glimmer-fallback" if provider == "glimmer" else "worker-local-default",
        "capabilityMode": "provider-native-with-governance-envelope",
        "nativeCapabilities": native_capabilities[provider],
        "requiredProviderCapabilities": [],
        "governanceCapabilities": ["audit-evidence", "bounded-snapshot", "guarded-execution", "role-binding"],
        "resolvedAt": utc_now(),
    }


def cos_provider_summary() -> dict[str, Any]:
    runtime = resolve_runtime_profile()
    profile = runtime.get("runtimeProfile") if isinstance(runtime.get("runtimeProfile"), dict) else {}
    policy = runtime.get("modelPolicy") if isinstance(runtime.get("modelPolicy"), dict) else resolve_model_policy()
    provider = str(policy.get("provider") or active_coordinator_provider())
    summary: dict[str, Any] = {
        "profile": profile.get("id") or active_runtime_profile(),
        "profileLabel": profile.get("label") or active_runtime_profile(),
        "role": policy.get("role") or active_coordinator_role(),
        "provider": provider,
        "model": policy.get("selectedModel"),
    }
    if provider == "glimmer":
        try:
            result = subprocess.run(
                [str(WORKER_BIN), "glimmer", "status"],
                cwd=str(WORKER_DIR),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            payload = json.loads(result.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            payload = {}
        summary["model"] = payload.get("model") or summary.get("model") or "qwen2.5-coder:14b"
        summary["modelLoaded"] = payload.get("model_loaded") is True
        summary["ready"] = payload.get("ready") is True
    else:
        summary["model"] = summary.get("model") or "claude-sonnet-5"
        summary["modelLoaded"] = True
        summary["ready"] = True
    return summary


def configured_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def configured_or_default(name: str, default: Path) -> Path:
    return configured_path(name) or default


def resolve_config_path(value: str) -> Path:
    text = str(value or "")
    replacements = {
        "${WORKER_DIR}": str(WORKER_DIR),
        "${ORG_DIR}": str(ORG_DIR),
        "${APP_DIR}": str(APP_DIR),
        "${PRESENT_DEV}": str(PRESENT_DEV_DIR),
        "${PRESENT_TASKS_DIR}": str(TASKS_DIR),
        "${PRESENT_LOGS_DIR}": str(configured_or_default("PRESENT_LOGS_DIR", WORKER_DIR / "logs")),
        "${PRESENT_JOBS_DIR}": str(configured_or_default("PRESENT_JOBS_DIR", WORKER_DIR / "jobs")),
        "${PRESENT_MISSIONS_DIR}": str(MISSION_ROOT),
        "${PRESENT_TEST_RESULTS_DIR}": str(TEST_RESULTS_DIR),
        "${PRESENT_REPOS_DIR}": str(REPOS_DIR),
    }
    for marker, replacement in replacements.items():
        text = text.replace(marker, replacement)
    return Path(os.path.expandvars(text)).expanduser()


def default_snapshot_policy(provider: str = "claude", source_profile: str | None = None) -> dict[str, Any]:
    logs_dir = configured_or_default("PRESENT_LOGS_DIR", WORKER_DIR / "logs")
    jobs_dir = configured_or_default("PRESENT_JOBS_DIR", WORKER_DIR / "jobs")
    limits = {
        "tasks": 30,
        "advisoryTasks": 5,
        "buildJobs": 25,
        "verifyResults": 30,
        "distributedWorkflows": 12,
        "brokerStateSources": 8,
        "brokerArtifacts": 10,
        "operatorAudit": 8,
        "generalDocuments": 12,
        "missions": 25,
    }
    source_profile = source_profile or "worker-local-default"
    if provider == "glimmer" or source_profile == "worker-local-glimmer-fallback":
        source_profile = "worker-local-glimmer-fallback"
        limits = {
            "tasks": 12,
            "advisoryTasks": 2,
            "buildJobs": 12,
            "verifyResults": 12,
            "distributedWorkflows": 4,
            "brokerStateSources": 4,
            "brokerArtifacts": 6,
            "operatorAudit": 4,
            "generalDocuments": 8,
            "missions": 12,
        }
    general = effective_knowledge_sources()
    general_repos = [
        {"name": item["name"], "path": item["path"]}
        for item in general.get("repositories", [])
        if isinstance(item, dict) and item.get("name") and item.get("path")
    ]
    general_documents = [
        {"title": item["title"], "path": item["path"], **({"kind": item["kind"]} if item.get("kind") else {})}
        for item in general.get("documents", [])
        if isinstance(item, dict) and item.get("title") and item.get("path")
    ]
    return {
        "schemaVersion": 1,
        "sourceProfile": source_profile,
        "includeCollections": [
            "tasks",
            "advisoryTasks",
            "buildJobs",
            "verifyResults",
            "distributedWorkflows",
            "generalDocuments",
            "missions",
        ],
        "limits": limits,
        "taskSelector": {"mode": "latest"},
        "sources": {
            "taskRoots": [str(TASKS_DIR)],
            "logRoots": [str(logs_dir)],
            "buildJobRoots": [str(jobs_dir)],
            "missionRoots": [str(MISSION_ROOT)],
            "verifyResultRoots": [str(TEST_RESULTS_DIR)],
            "brokerStatePaths": [
                str(WORKER_DIR / "broker-state.json"),
                str(WORKER_DIR / "state.json"),
                str(PRESENT_DEV_DIR / "broker-state.json"),
                str(PRESENT_DEV_DIR / "state.json"),
                str(TASKS_DIR.parent / "broker-state.json"),
                str(TASKS_DIR.parent / "state.json"),
            ],
            "repositoryRoots": [
                {"name": "worker", "path": str(WORKER_DIR)},
                {"name": "starter-company", "path": str(ORG_DIR)},
                *general_repos,
            ],
            "documentPaths": general_documents,
        },
    }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def safe_path_part(value: str, fallback: str = "upload") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:120] or fallback


def decode_upload_content(item: dict[str, Any]) -> bytes:
    encoded = item.get("contentBase64")
    text = item.get("text")
    if isinstance(encoded, str) and encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("upload content is not valid base64") from exc
    if isinstance(text, str):
        return text.encode("utf-8")
    raise ValueError("upload content is required")


def normalize_upload_files(value: Any, *, max_files: int = 80) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    files: list[dict[str, Any]] = []
    total = 0
    for index, item in enumerate(value[:max_files]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"upload-{index + 1}.txt")
        relative = str(item.get("relativePath") or name)
        content = decode_upload_content(item)
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError("uploaded files exceed the 24 MiB limit")
        files.append({
            "name": name[:240],
            "relativePath": relative[:500],
            "type": str(item.get("type") or ""),
            "size": len(content),
            "content": content,
        })
    return files


def chat_upload_context(value: Any) -> tuple[list[dict[str, Any]], str]:
    files = normalize_upload_files(value, max_files=12)
    summaries: list[dict[str, Any]] = []
    sections: list[str] = []
    for item in files:
        name = item["name"]
        content = item["content"]
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = f"[binary file omitted from inline chat context; {len(content)} bytes]"
        excerpt = text[:MAX_CHAT_UPLOAD_CHARS]
        summaries.append({
            "name": name,
            "relativePath": item["relativePath"],
            "type": item["type"],
            "size": item["size"],
            "truncated": len(text) > len(excerpt),
        })
        sections.append(
            f"### Uploaded file: {item['relativePath']}\n"
            f"Type: {item['type'] or 'unknown'}; Size: {item['size']} bytes\n\n{excerpt}"
        )
    return summaries, "\n\n".join(sections)


def save_uploaded_knowledge_files(files: list[dict[str, Any]], label: str) -> Path:
    upload_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    root = ORG_KNOWLEDGE_UPLOAD_ROOT / f"{safe_path_part(label, 'org-knowledge')}-{upload_id}"
    for item in files:
        parts = [
            safe_path_part(part, "file")
            for part in Path(str(item["relativePath"])).parts
            if part not in {"", ".", ".."}
        ]
        target = root.joinpath(*parts) if parts else root / safe_path_part(str(item["name"]), "file")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item["content"])
    return root


def mission_dir(mission_id: str) -> Path:
    return MISSION_ROOT / mission_id


def mission_record_path(mission_id: str) -> Path:
    return mission_dir(mission_id) / "mission.json"


def mission_audit_path(mission_id: str) -> Path:
    return mission_dir(mission_id) / "audit.jsonl"


def mission_evidence_dir(mission_id: str) -> Path:
    return mission_dir(mission_id) / "evidence"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def validate_configuration_write(
    action: str,
    actor: str,
    target: Path,
    before: dict[str, Any] | None,
    payload: dict[str, Any],
    schema_file: str,
) -> None:
    errors: list[str] = []
    try:
        registry = json.loads(SCHEMA_REGISTRY_PATH.read_text())
        registered = any(
            isinstance(item, dict)
            and item.get("schemaFile") == schema_file
            and item.get("lifecycle") == "active"
            for item in registry.get("schemas", [])
        )
        if not registered:
            errors.append(f"schema {schema_file!r} is not active in the schema registry")
        else:
            errors.extend(schema_check.validate(payload, f"canonical/{schema_file}"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f"schema authority could not be read: {exc}")
    if not errors:
        return
    record_mutation(
        action,
        actor,
        target,
        before=before,
        after=before,
        status="rejected",
        details={"schemaFile": schema_file, "errors": errors[:20]},
    )
    raise ValueError(f"{action} payload violates registered schema {schema_file}: {errors[0]}")


def atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def mission_integrity_path(mission_id: str) -> Path:
    return mission_dir(mission_id) / "integrity-chain.jsonl"


def evidence_signing_key_path() -> Path:
    return MISSION_ROOT / "_evidence-signing-key"


def evidence_signing_key() -> bytes:
    configured = os.environ.get(EVIDENCE_SIGNING_KEY_ENV)
    if configured:
        return configured.encode("utf-8")
    path = evidence_signing_key_path()
    try:
        if path.exists():
            return path.read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return key
    except OSError:
        return f"local-alpha-fallback:{MISSION_ROOT}".encode("utf-8")


def auth_signing_key() -> bytes:
    configured = os.environ.get(AUTH_SIGNING_KEY_ENV)
    if configured:
        return configured.encode("utf-8")
    try:
        if AUTH_SIGNING_KEY_PATH.exists():
            return AUTH_SIGNING_KEY_PATH.read_bytes()
        AUTH_SIGNING_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        descriptor = os.open(AUTH_SIGNING_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return key
    except FileExistsError:
        return AUTH_SIGNING_KEY_PATH.read_bytes()
    except OSError:
        return f"local-alpha-auth-fallback:{MISSION_ROOT}".encode("utf-8")


def identity_mode(policy: dict[str, Any] | None = None) -> str:
    configured = str(os.environ.get(AUTH_IDENTITY_MODE_ENV) or "").strip().lower()
    if configured in {"development-local", "oidc-required"}:
        return configured
    selected = policy or auth_policy()
    boundary = selected.get("identityBoundary") if isinstance(selected.get("identityBoundary"), dict) else {}
    return "oidc-required" if boundary.get("mode") == "oidc-required" else "development-local"


def private_runner_signing_key() -> bytes:
    configured = os.environ.get(PRIVATE_RUNNER_SIGNING_KEY_ENV)
    if configured:
        return configured.encode("utf-8")
    try:
        if PRIVATE_RUNNER_SIGNING_KEY_PATH.exists():
            return PRIVATE_RUNNER_SIGNING_KEY_PATH.read_bytes()
        PRIVATE_RUNNER_SIGNING_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        descriptor = os.open(PRIVATE_RUNNER_SIGNING_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return key
    except FileExistsError:
        return PRIVATE_RUNNER_SIGNING_KEY_PATH.read_bytes()
    except OSError:
        return f"local-alpha-private-runner-fallback:{MISSION_ROOT}".encode("utf-8")


def license_entitlement() -> dict[str, Any]:
    edition = str(os.environ.get(STEEL_MISSION_EDITION_ENV) or "core").strip().lower() or "core"
    license_key = str(os.environ.get(STEEL_MISSION_LICENSE_KEY_ENV) or "")
    expected_hash = str(os.environ.get(STEEL_MISSION_LICENSE_KEY_SHA256_ENV) or "").strip().lower()
    actual_hash = hashlib.sha256(license_key.encode("utf-8")).hexdigest() if license_key else ""
    enterprise_enabled = edition == "enterprise" and bool(license_key) and bool(expected_hash) and hmac.compare_digest(actual_hash, expected_hash)
    if enterprise_enabled:
        status = "enterprise-active"
        reason = ""
    elif edition == "enterprise" and not license_key:
        status = "enterprise-license-missing"
        reason = "Enterprise edition is selected but no license key is configured."
    elif edition == "enterprise" and not expected_hash:
        status = "enterprise-license-hash-missing"
        reason = "Enterprise edition is selected but no license key hash is configured."
    elif edition == "enterprise":
        status = "enterprise-license-invalid"
        reason = "Enterprise license key does not match the configured license hash."
    else:
        status = "core"
        reason = "Core edition is active."
    return {
        "schemaVersion": 1,
        "edition": "enterprise" if edition == "enterprise" else "core",
        "status": status,
        "enterpriseEnabled": enterprise_enabled,
        "licenseConfigured": bool(license_key),
        "licenseHashConfigured": bool(expected_hash),
        "reason": reason,
        "features": {},
        "commercialBoundary": "managed-scale-governance-and-support",
    }


def external_evidence_signer_command() -> str:
    configured = os.environ.get(EVIDENCE_SIGNER_COMMAND_ENV)
    if configured:
        return configured
    policy = auth_policy()
    kms = policy.get("kms") if isinstance(policy.get("kms"), dict) else {}
    return str(kms.get("signCommand") or "")


def external_signing_required(policy: dict[str, Any] | None = None) -> bool:
    selected = policy if isinstance(policy, dict) else auth_policy()
    kms = selected.get("kms") if isinstance(selected.get("kms"), dict) else {}
    configured = str(os.environ.get("PRESENT_REQUIRE_EXTERNAL_SIGNING") or "").strip().lower()
    return (
        kms.get("requireExternalSigning") is True
        or configured in {"1", "true", "yes", "required"}
    )


def external_sign_payload(record_hash: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    command = external_evidence_signer_command().strip()
    if not command:
        return None
    blocked = command_matches_blocked_pattern(command, control_policy())
    if blocked:
        return {
            "ok": False,
            "status": "blocked",
            "error": "external signer command is blocked by control policy",
            "blockedPatterns": blocked,
        }
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"ok": False, "status": "blocked", "error": f"external signer command could not be parsed: {exc}"}
    try:
        result = subprocess.run(
            argv,
            input=json.dumps({"recordHash": record_hash, "payload": payload}, sort_keys=True),
            cwd=str(WORKER_DIR),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "status": "failed", "error": str(exc)}
    signature = (result.stdout or "").strip().splitlines()[-1] if result.stdout.strip() else ""
    return {
        "ok": result.returncode == 0 and bool(signature),
        "status": "succeeded" if result.returncode == 0 and signature else "failed",
        "signature": signature[:4096],
        "exitCode": result.returncode,
        "stderr": limited_text(result.stderr or "", limit=2000) if "limited_text" in globals() else result.stderr[:2000],
    }


def evidence_signer_health(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = policy if isinstance(policy, dict) else auth_policy()
    kms = selected.get("kms") if isinstance(selected.get("kms"), dict) else {}
    command = str(os.environ.get(EVIDENCE_SIGNER_COMMAND_ENV) or kms.get("signCommand") or "").strip()
    if not command:
        return {
            "ok": False,
            "status": "not_configured",
            "required": external_signing_required(selected),
            "provider": str(kms.get("provider") or "customer-managed"),
            "keyId": str(kms.get("keyId") or ""),
            "commandConfigured": False,
        }
    probe = {
        "kind": "evidence-signer-health",
        "producer": "steel-mission-chat auth-control",
        "producedAt": utc_now(),
        "keyId": str(kms.get("keyId") or ""),
    }
    result = external_sign_payload("health-" + canonical_json_hash(probe), probe) or {}
    return {
        "ok": result.get("ok") is True,
        "status": result.get("status") or "unknown",
        "required": external_signing_required(selected),
        "provider": str(kms.get("provider") or "customer-managed"),
        "keyId": str(kms.get("keyId") or ""),
        "commandConfigured": True,
        "signatureScheme": "external-kms-or-signer",
        "error": str(result.get("error") or result.get("stderr") or "")[:2000],
    }


def b64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def b64url_decode(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode((payload + padding).encode("ascii"))


def decode_jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes] | tuple[None, None, bytes, bytes]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None, None, b"", b""
    try:
        header = json.loads(b64url_decode(parts[0]).decode("utf-8"))
        claims = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        signature = b64url_decode(parts[2])
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None, None, b"", b""
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None, None, b"", b""
    return header, claims, signature, f"{parts[0]}.{parts[1]}".encode("utf-8")


def load_oidc_jwks(policy: dict[str, Any], *, force_refresh: bool = False) -> dict[str, Any]:
    oidc = policy.get("oidc") if isinstance(policy.get("oidc"), dict) else {}
    if isinstance(oidc.get("jwks"), dict):
        return oidc["jwks"]
    jwks_path = str(oidc.get("jwksPath") or "").strip()
    if jwks_path:
        try:
            payload = json.loads(Path(jwks_path).expanduser().read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
    jwks_url = str(oidc.get("jwksUrl") or "").strip()
    if jwks_url:
        ttl = max(30, min(int(oidc.get("jwksCacheSeconds") or 300), 86400))
        with OIDC_CACHE_LOCK:
            cached = OIDC_JWKS_CACHE.get(jwks_url)
            if not force_refresh and isinstance(cached, dict) and float(cached.get("expiresEpoch") or 0) > time.time():
                return cached.get("payload") if isinstance(cached.get("payload"), dict) else {}
        try:
            with urlopen(jwks_url, timeout=10) as response:
                payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
                if isinstance(payload, dict):
                    with OIDC_CACHE_LOCK:
                        OIDC_JWKS_CACHE[jwks_url] = {"payload": payload, "expiresEpoch": time.time() + ttl}
                    return payload
                return {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def jwk_to_rsa_public_key(jwk: dict[str, Any]) -> Any:
    if rsa is None:
        raise RuntimeError("cryptography is not available")
    n_raw = jwk.get("n")
    e_raw = jwk.get("e")
    if not isinstance(n_raw, str) or not isinstance(e_raw, str):
        raise ValueError("RSA JWK is missing n or e")
    n = int.from_bytes(b64url_decode(n_raw), "big")
    e = int.from_bytes(b64url_decode(e_raw), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def verify_oidc_rs256_session(token: str, header: dict[str, Any], claims: dict[str, Any], signature: bytes, signing_input: bytes, policy: dict[str, Any]) -> dict[str, Any]:
    if hashes is None or crypto_padding is None or rsa is None:
        return {"ok": False, "error": "RS256 verification is unavailable because cryptography is not installed"}
    oidc = policy.get("oidc") if isinstance(policy.get("oidc"), dict) else {}
    if oidc.get("enabled") is not True:
        return {"ok": False, "error": "OIDC verification is not enabled"}
    jwks = load_oidc_jwks(policy)
    keys = jwks.get("keys") if isinstance(jwks.get("keys"), list) else []
    kid = str(header.get("kid") or "")
    candidates = [
        key for key in keys
        if isinstance(key, dict) and key.get("kty") == "RSA" and (not kid or key.get("kid") == kid)
    ]
    if not candidates and kid and str(oidc.get("jwksUrl") or "").strip():
        jwks = load_oidc_jwks(policy, force_refresh=True)
        keys = jwks.get("keys") if isinstance(jwks.get("keys"), list) else []
        candidates = [key for key in keys if isinstance(key, dict) and key.get("kty") == "RSA" and key.get("kid") == kid]
    if not candidates:
        return {"ok": False, "error": "OIDC signing key is unavailable"}
    errors: list[str] = []
    for jwk in candidates:
        try:
            public_key = jwk_to_rsa_public_key(jwk)
            public_key.verify(signature, signing_input, crypto_padding.PKCS1v15(), hashes.SHA256())
            return {"ok": True, "claims": claims, "header": header, "token": token}
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    return {"ok": False, "error": "OIDC signature is invalid", "details": errors[:3]}


def default_auth_policy() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "policyId": "present.control-plane-auth.alpha",
        "producer": "steel-mission-chat auth-control",
        "enforcementMode": "signed-session-required-for-control-plane",
        "sessionTtlSeconds": 3600,
        "acceptedIssuers": ["present-local-alpha"],
        "acceptedAudiences": ["present-control-plane"],
        "roleClaims": ["present_role", "role"],
        "subjectClaims": ["sub", "email", "preferred_username"],
        "identityBoundary": {
            "mode": "development-local",
            "allowLoopbackDevelopmentIdentity": True,
        },
        "authorization": {
            "preventSelfApproval": True,
        },
        "oidc": {
            "enabled": False,
            "issuer": "",
            "audience": "present-control-plane",
            "jwksUrl": "",
            "authorizationEndpoint": "",
            "tokenEndpoint": "",
            "clientId": "",
            "clientSecretEnv": OIDC_CLIENT_SECRET_ENV,
            "redirectUri": "",
            "scopes": ["openid", "profile", "email"],
            "jwksCacheSeconds": 300,
        },
        "kms": {
            "enabled": False,
            "provider": "customer-managed",
            "keyId": "",
            "signCommand": "",
            "requireExternalSigning": False,
        },
    }


def auth_policy() -> dict[str, Any]:
    configured = read_json_file(AUTH_POLICY_PATH)
    policy = default_auth_policy()
    if not configured:
        return attach_auth_edition_metadata({**policy, "configuredPath": str(AUTH_POLICY_PATH), "configured": False})
    merged = {**policy, **configured}
    for key in ["identityBoundary", "authorization", "oidc", "kms"]:
        if isinstance(policy.get(key), dict) and isinstance(configured.get(key), dict):
            merged[key] = {**policy[key], **configured[key]}
    return attach_auth_edition_metadata({**merged, "configuredPath": str(AUTH_POLICY_PATH), "configured": True})


def attach_auth_edition_metadata(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        **policy,
        "entitlement": license_entitlement(),
    }


def normalize_auth_policy(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("policy") if isinstance(payload.get("policy"), dict) else payload
    base = default_auth_policy()
    oidc = source.get("oidc") if isinstance(source.get("oidc"), dict) else {}
    kms = source.get("kms") if isinstance(source.get("kms"), dict) else {}
    boundary = source.get("identityBoundary") if isinstance(source.get("identityBoundary"), dict) else {}
    authorization = source.get("authorization") if isinstance(source.get("authorization"), dict) else {}
    ttl = source.get("sessionTtlSeconds")
    try:
        ttl_int = int(ttl)
    except (TypeError, ValueError):
        ttl_int = int(base["sessionTtlSeconds"])
    try:
        jwks_cache_seconds = int(oidc.get("jwksCacheSeconds") or 300)
    except (TypeError, ValueError):
        jwks_cache_seconds = 300
    return {
        "schemaVersion": 1,
        "policyId": clean_optional_string(source.get("policyId"), limit=160) or base["policyId"],
        "producer": "steel-mission-chat auth-control",
        "producedAt": utc_now(),
        "enforcementMode": clean_choice(
            source.get("enforcementMode"),
            {"signed-session-required-for-control-plane", "local-development"},
            base["enforcementMode"],
        ),
        "sessionTtlSeconds": max(60, min(ttl_int, 86400)),
        "acceptedIssuers": clean_string_list(source.get("acceptedIssuers"), limit=20) or base["acceptedIssuers"],
        "acceptedAudiences": clean_string_list(source.get("acceptedAudiences"), limit=20) or base["acceptedAudiences"],
        "roleClaims": clean_string_list(source.get("roleClaims"), limit=20) or base["roleClaims"],
        "subjectClaims": clean_string_list(source.get("subjectClaims"), limit=20) or base["subjectClaims"],
        "identityBoundary": {
            "mode": clean_choice(boundary.get("mode"), {"development-local", "oidc-required"}, "development-local"),
            "allowLoopbackDevelopmentIdentity": bool_from_payload(boundary.get("allowLoopbackDevelopmentIdentity"), True),
        },
        "authorization": {
            "preventSelfApproval": bool_from_payload(authorization.get("preventSelfApproval"), True),
        },
        "oidc": {
            "enabled": bool_from_payload(oidc.get("enabled"), False),
            "issuer": clean_optional_string(oidc.get("issuer"), limit=500),
            "audience": clean_optional_string(oidc.get("audience"), limit=300) or "present-control-plane",
            "jwksUrl": clean_optional_string(oidc.get("jwksUrl"), limit=1000),
            "jwksPath": clean_optional_string(oidc.get("jwksPath"), limit=1000),
            "authorizationEndpoint": clean_optional_string(oidc.get("authorizationEndpoint"), limit=1000),
            "tokenEndpoint": clean_optional_string(oidc.get("tokenEndpoint"), limit=1000),
            "clientId": clean_optional_string(oidc.get("clientId"), limit=500),
            "clientSecretEnv": clean_optional_string(oidc.get("clientSecretEnv"), limit=200) or OIDC_CLIENT_SECRET_ENV,
            "redirectUri": clean_optional_string(oidc.get("redirectUri"), limit=1000),
            "scopes": clean_string_list(oidc.get("scopes"), limit=20) or ["openid", "profile", "email"],
            "jwksCacheSeconds": max(30, min(jwks_cache_seconds, 86400)),
            **({"jwks": oidc.get("jwks")} if isinstance(oidc.get("jwks"), dict) else {}),
        },
        "kms": {
            "enabled": bool_from_payload(kms.get("enabled"), False),
            "provider": clean_optional_string(kms.get("provider"), limit=120) or "customer-managed",
            "keyId": clean_optional_string(kms.get("keyId"), limit=500),
            "signCommand": clean_optional_string(kms.get("signCommand"), limit=1000),
            "requireExternalSigning": bool_from_payload(kms.get("requireExternalSigning"), False),
        },
    }


def save_auth_policy(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can manage auth policy")
    before = read_json_file(AUTH_POLICY_PATH)
    policy = normalize_auth_policy(payload)
    validate_configuration_write(
        "auth-policy-saved",
        role,
        AUTH_POLICY_PATH,
        before,
        policy,
        "auth-policy-v1.json",
    )
    atomic_write_json(AUTH_POLICY_PATH, policy)
    record_mutation(
        "auth-policy-saved",
        role,
        AUTH_POLICY_PATH,
        before=before,
        after=policy,
        details={
            "policyId": policy.get("policyId"),
            "enforcementMode": policy.get("enforcementMode"),
            "oidcEnabled": policy.get("oidc", {}).get("enabled") is True,
            "kmsEnabled": policy.get("kms", {}).get("enabled") is True,
        },
    )
    return attach_auth_edition_metadata(policy)


def sign_control_plane_session(claims: dict[str, Any]) -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": os.environ.get(AUTH_SIGNER_ID_ENV, "present-auth-session")}
    encoded_header = b64url_encode(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    encoded_payload = b64url_encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(auth_signing_key(), f"{encoded_header}.{encoded_payload}".encode("utf-8"), hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{b64url_encode(signature)}"


def append_auth_audit(action: str, actor_id: str = "", *, ok: bool = True, details: dict[str, Any] | None = None) -> None:
    event = {
        "schemaVersion": 1,
        "producedAt": utc_now(),
        "action": action,
        "actorId": clean_optional_string(actor_id, limit=200),
        "ok": ok,
        "details": details or {},
    }
    with AUTH_LOCK:
        append_jsonl(AUTH_AUDIT_LEDGER_PATH, event)


def revoked_session_ids() -> set[str]:
    revoked: set[str] = set()
    try:
        for line in AUTH_REVOCATION_LEDGER_PATH.read_text().splitlines():
            item = json.loads(line)
            if isinstance(item, dict) and item.get("jti"):
                revoked.add(str(item["jti"]))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return revoked


def revoke_control_plane_session(token: str, actor_id: str = "") -> dict[str, Any]:
    verification = verify_control_plane_session(token, allow_revoked=True)
    claims = verification.get("claims") if isinstance(verification.get("claims"), dict) else {}
    jti = clean_optional_string(claims.get("jti"), limit=200)
    if not jti:
        raise ValueError("session token is invalid")
    event = {"schemaVersion": 1, "jti": jti, "revokedAt": utc_now(), "actorId": actor_id or verification.get("actorId") or ""}
    with AUTH_LOCK:
        append_jsonl(AUTH_REVOCATION_LEDGER_PATH, event)
    append_auth_audit("session-revoked", str(event["actorId"]), details={"jti": jti})
    return {"ok": True, "revoked": True, "jti": jti}


def issue_control_plane_session(
    actor_id: str,
    role: str,
    *,
    ttl_seconds: int | None = None,
    authn_method: str = "local-development",
    actor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = auth_policy()
    if identity_mode(policy) == "oidc-required" and authn_method != "oidc-exchange":
        raise PermissionError("local self-issued sessions are disabled by the OIDC identity boundary")
    ttl = ttl_seconds or int(policy.get("sessionTtlSeconds") or 3600)
    now = int(time.time())
    actor_details = actor if isinstance(actor, dict) else {}
    actor_subject = clean_optional_string(actor_id, limit=200) or corporate_role(role)
    claims = {
        "iss": "present-local-alpha",
        "aud": "present-control-plane",
        "sub": actor_subject,
        "present_role": corporate_role(role),
        "authn_method": authn_method,
        "organization_ids": clean_string_list(actor_details.get("organizationIds"), limit=50),
        "organization_id": clean_optional_string(actor_details.get("organizationId"), limit=120),
        "capabilities": clean_string_list(actor_details.get("capabilities"), limit=200),
        "iat": now,
        "nbf": now - 5,
        "exp": now + max(60, min(int(ttl), 86400)),
        "jti": "ps-" + secrets.token_hex(12),
    }
    return {
        "schemaVersion": 1,
        "tokenType": "Bearer",
        "accessToken": sign_control_plane_session(claims),
        "expiresAt": dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "claims": claims,
        "authPolicyHash": canonical_json_hash(policy),
    }


def issue_oidc_exchange_session(token: str, *, expected_nonce: str = "") -> dict[str, Any]:
    policy = auth_policy()
    verified = verify_control_plane_session(token, expected_nonce=expected_nonce, oidc_only=True)
    if verified.get("ok") is not True:
        raise PermissionError(str(verified.get("error") or "OIDC token is invalid"))
    user_actor = verified.get("actor") if isinstance(verified.get("actor"), dict) else None
    if not user_actor:
        raise PermissionError("OIDC identity is not registered or is disabled")
    session = issue_control_plane_session(
        str(user_actor.get("actorId") or ""),
        str(user_actor.get("role") or "user"),
        authn_method="oidc-exchange",
        actor=user_actor,
    )
    append_auth_audit("oidc-session-issued", str(user_actor.get("actorId") or ""), details={"issuer": verified.get("claims", {}).get("iss")})
    return session


def verify_control_plane_session(
    token: str,
    *,
    expected_nonce: str = "",
    oidc_only: bool = False,
    allow_revoked: bool = False,
) -> dict[str, Any]:
    policy = auth_policy()
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {"ok": False, "error": "session token is malformed"}
    header, claims, signature, signing_input = decode_jwt_parts(token)
    if not header or not claims:
        return {"ok": False, "error": "session claims are invalid"}
    algorithm = str(header.get("alg") or "")
    if algorithm == "HS256":
        if oidc_only:
            return {"ok": False, "error": "an OIDC RS256 token is required"}
        expected = b64url_encode(hmac.new(auth_signing_key(), signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(expected, parts[2]):
            return {"ok": False, "error": "session signature is invalid"}
        issuer_kind = "local-hs256"
    elif algorithm == "RS256":
        oidc_result = verify_oidc_rs256_session(token, header, claims, signature, signing_input, policy)
        if oidc_result.get("ok") is not True:
            return oidc_result
        issuer_kind = "oidc-rs256"
    else:
        return {"ok": False, "error": f"session algorithm {algorithm or 'unknown'} is not accepted"}
    issuer = str(claims.get("iss") or "")
    oidc = policy.get("oidc") if isinstance(policy.get("oidc"), dict) else {}
    if issuer_kind == "oidc-rs256" and oidc.get("issuer") and issuer != str(oidc.get("issuer")):
        return {"ok": False, "error": "OIDC token issuer does not match the configured provider"}
    accepted_issuers = set(policy.get("acceptedIssuers") or [])
    if issuer_kind == "local-hs256":
        accepted_issuers.add("present-local-alpha")
    if oidc.get("enabled") is True and oidc.get("issuer"):
        accepted_issuers.add(str(oidc.get("issuer")))
    if issuer not in accepted_issuers:
        return {"ok": False, "error": "session issuer is not accepted"}
    audience_claim = claims.get("aud")
    audiences = {str(value) for value in audience_claim} if isinstance(audience_claim, list) else {str(audience_claim or "")}
    accepted_audiences = set(policy.get("acceptedAudiences") or [])
    if issuer_kind == "local-hs256":
        accepted_audiences.add("present-control-plane")
    if oidc.get("audience"):
        accepted_audiences.add(str(oidc.get("audience")))
    if issuer_kind == "oidc-rs256" and oidc.get("clientId"):
        accepted_audiences.add(str(oidc.get("clientId")))
    if not audiences.intersection(accepted_audiences):
        return {"ok": False, "error": "session audience is not accepted"}
    if issuer_kind == "oidc-rs256" and isinstance(audience_claim, list) and len(audience_claim) > 1 and oidc.get("clientId"):
        if str(claims.get("azp") or "") != str(oidc.get("clientId")):
            return {"ok": False, "error": "OIDC authorized party is invalid"}
    now = int(time.time())
    try:
        expires = int(claims.get("exp") or 0)
        issued = int(claims.get("iat") or 0)
        not_before = int(claims.get("nbf") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "session timestamps are invalid"}
    if expires < now:
        return {"ok": False, "error": "session is expired"}
    if not_before and not_before > now + 30:
        return {"ok": False, "error": "session is not active yet"}
    if issued and issued > now + 30:
        return {"ok": False, "error": "session issued-at time is invalid"}
    if expected_nonce and not hmac.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
        return {"ok": False, "error": "OIDC nonce is invalid"}
    jti = clean_optional_string(claims.get("jti"), limit=200)
    if not allow_revoked and jti and jti in revoked_session_ids():
        return {"ok": False, "error": "session is revoked"}
    role_claims = policy.get("roleClaims") if isinstance(policy.get("roleClaims"), list) else ["present_role", "role"]
    subject_claims = policy.get("subjectClaims") if isinstance(policy.get("subjectClaims"), list) else ["sub", "email", "preferred_username"]
    actor_id = ""
    for claim in subject_claims:
        actor_id = clean_optional_string(claims.get(str(claim)), limit=200)
        if actor_id:
            break
    role_value = ""
    for claim in role_claims:
        role_value = clean_optional_string(claims.get(str(claim)), limit=80)
        if role_value:
            break
    mode = identity_mode(policy)
    registered_actor: dict[str, Any] | None = None
    if issuer_kind == "oidc-rs256":
        registered_actor = resolve_registered_identity(claims, policy)
        if mode == "oidc-required" and not registered_actor:
            return {"ok": False, "error": "OIDC identity is not registered or is disabled"}
    elif mode == "oidc-required":
        if claims.get("authn_method") != "oidc-exchange":
            return {"ok": False, "error": "local development sessions are not accepted in OIDC-required mode"}
        user = registered_user(str(claims.get("sub") or ""))
        if not user or user.get("status") != "active":
            return {"ok": False, "error": "session subject is not registered or is disabled"}
        registered_actor = actor_from_registered_user(user, policy)
    role = corporate_role(str(registered_actor.get("role") if registered_actor else role_value or "user"))
    resolved_actor_id = str(registered_actor.get("actorId") if registered_actor else actor_id or "session-subject")
    result_actor = registered_actor or {
        "actorId": resolved_actor_id,
        "role": role,
        "capabilities": clean_string_list(claims.get("capabilities"), limit=200),
        "organizationIds": clean_string_list(claims.get("organization_ids"), limit=50),
        "organizationId": clean_optional_string(claims.get("organization_id"), limit=120),
        "identitySource": issuer_kind,
    }
    return {
        "ok": True,
        "actorId": resolved_actor_id,
        "role": role,
        "actor": result_actor,
        "claims": claims,
        "issuerKind": issuer_kind,
        "algorithm": algorithm,
        "authPolicyHash": canonical_json_hash(policy),
    }


def session_token_and_source(handler: BaseHTTPRequestHandler) -> tuple[str, str]:
    """The session token and where it came from.

    The source matters when the token turns out to be invalid. A bearer header or
    an explicit session header is a deliberate assertion by the caller, and a bad
    one is an error. A cookie is ambient: the browser attaches it because some
    other instance set it, and cookies are not scoped by port -- a session issued
    by a container on one port is sent to a development server on another. An
    expired credential the user never chose to present should not lock them out of
    a server that would otherwise have admitted them.
    """
    authorization = handler.headers.get("Authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip(), "header"
    explicit = handler.headers.get("X-Present-Session") or ""
    if explicit:
        return explicit, "header"
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie") or "")
    except Exception:  # noqa: BLE001
        return "", ""
    value = cookie.get("present_session")
    return (value.value, "cookie") if value else ("", "")


def bearer_token_from_handler(handler: BaseHTTPRequestHandler) -> str:
    return session_token_and_source(handler)[0]


def request_cookie(handler: BaseHTTPRequestHandler, name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie") or "")
    except Exception:  # noqa: BLE001
        return ""
    return cookie.get(name).value if cookie.get(name) else ""


def is_loopback_request(handler: BaseHTTPRequestHandler) -> bool:
    address = str(handler.client_address[0] if handler.client_address else "")
    return address in {"127.0.0.1", "::1", "localhost"}


def authenticate_http_request(handler: BaseHTTPRequestHandler, path: str, method: str) -> dict[str, Any]:
    policy = auth_policy()
    token, token_source = session_token_and_source(handler)
    cookie_authenticated = bool(request_cookie(handler, "present_session")) and not bool((handler.headers.get("Authorization") or "").strip())
    if token:
        verified = verify_control_plane_session(token)
        if verified.get("ok") is not True:
            boundary = policy.get("identityBoundary") if isinstance(policy.get("identityBoundary"), dict) else {}
            development_available = (
                identity_mode(policy) == "development-local"
                and boundary.get("allowLoopbackDevelopmentIdentity") is True
                and is_loopback_request(handler)
            )
            if token_source == "cookie" and development_available:
                # A stale cookie from another instance, on a request development
                # identity would have accepted anyway. Discard it and carry on
                # rather than sending the caller to sign in for a credential they
                # never offered. Recorded, because a rejected credential is worth
                # seeing even when it is survivable.
                append_auth_audit("stale-session-cookie-discarded", "", ok=True,
                                  details={"path": path, "error": verified.get("error")})
                # Clear it on the way out, so the browser stops sending it and
                # the next request is clean without anyone opening devtools.
                expiring = list(getattr(handler, "response_headers", None) or [])
                expiring += [
                    ("Set-Cookie", "present_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"),
                    ("Set-Cookie", "present_csrf=; Path=/; SameSite=Lax; Max-Age=0"),
                ]
                handler.response_headers = expiring
                token = ""
                cookie_authenticated = False
            else:
                append_auth_audit("request-denied", "", ok=False, details={"path": path, "error": verified.get("error")})
                raise PermissionError(str(verified.get("error") or "valid session is required"))
        else:
            actor = dict(verified.get("actor") if isinstance(verified.get("actor"), dict) else {})
            actor.update({
                "actorId": verified.get("actorId"),
                "role": verified.get("role"),
                "sessionVerified": True,
                "authPolicyHash": verified.get("authPolicyHash"),
                "claims": verified.get("claims"),
                "accessToken": token,
                "cookieAuthenticated": cookie_authenticated,
            })
            if cookie_authenticated and method in {"POST", "PUT", "PATCH", "DELETE"}:
                csrf_cookie = request_cookie(handler, "present_csrf")
                csrf_header = str(handler.headers.get("X-Present-CSRF") or "")
                if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
                    raise PermissionError("CSRF validation failed")
            return actor
    boundary = policy.get("identityBoundary") if isinstance(policy.get("identityBoundary"), dict) else {}
    if identity_mode(policy) == "oidc-required":
        raise PermissionError("OIDC-authenticated session is required")
    if boundary.get("allowLoopbackDevelopmentIdentity") is not True or not is_loopback_request(handler):
        # Say where the request came from. Behind published container ports the
        # peer is the bridge gateway, never 127.0.0.1, so this fires for every
        # browser on a containerised run and the address is the whole diagnosis.
        origin = str(handler.client_address[0] if handler.client_address else "unknown")
        raise PermissionError(
            "development identity is restricted to loopback requests; this request came "
            f"from {origin}. Sign in with a development session at /auth/login."
        )
    route_parts = path.strip("/").split("/")
    fallback_role = route_parts[1] if len(route_parts) > 2 and route_parts[0] == "api" and route_parts[1] in {"owner", "admin", "publisher", "user"} else "user"
    actor = actor_from_request(handler, fallback_role)
    user = registered_user(str(actor.get("actorId") or ""))
    if user and user.get("status") == "active":
        actor = actor_from_registered_user(user, policy)
    else:
        actor.update({"organizationIds": [str(organization_registry().get("activeOrganizationId") or "")], "organizationId": str(organization_registry().get("activeOrganizationId") or ""), "capabilities": [], "identitySource": "loopback-development"})
    actor.update({"sessionVerified": False, "authPolicyHash": canonical_json_hash(policy), "cookieAuthenticated": False})
    return actor


DEVELOPMENT_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Steel Mission \u2014 development sign-in</title>
<style>
 body{font:15px/1.6 system-ui,sans-serif;max-width:44rem;margin:6vh auto;padding:0 1.5rem;color:#111}
 code,textarea{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
 textarea{width:100%;height:7rem;font-size:12px;padding:.6rem;box-sizing:border-box}
 pre{background:#f4f4f5;padding:.8rem;overflow-x:auto;font-size:12px}
 button{font-size:15px;padding:.5rem 1.1rem;margin-top:.6rem}
 .why{color:#555;font-size:14px}
</style>
<h1>Development sign-in</h1>
<p class="why">This server is in <code>development-local</code> mode, so there is no
identity provider to redirect to. Development identity is also refused for this
request because it did not arrive from a loopback address &mdash; behind a published
container port the client is the container network gateway, never
<code>127.0.0.1</code>.</p>
<p>Issue a session where the server is running, then paste it below:</p>
<pre>docker exec -i __CONTAINER__ bin/present-control-plane session \\
  --actor &lt;your-user-id&gt; --role admin</pre>
<form method="post" action="/auth/login">
  <label for="t">Session token (<code>session.accessToken</code>)</label>
  <textarea id="t" name="token" autofocus spellcheck="false"></textarea>
  <button type="submit">Sign in</button>
</form>
<p class="why">The token is the credential and is verified on arrival; this page
grants nothing on its own. Issuing one requires access to the machine or container
the server runs in.</p>
"""


def development_login_available(policy: dict[str, Any] | None = None) -> bool:
    """Whether /auth/login can complete without an identity provider.

    In development-local mode there is no provider to redirect to, so starting an
    OIDC flow is a dead end by construction: the browser follows the login path it
    was given and is told OIDC is disabled.
    """
    return identity_mode(policy) == "development-local"


def login_path_for(policy: dict[str, Any] | None = None) -> str | None:
    """The login path to advertise on a 401, or None when none can work."""
    selected = policy or auth_policy()
    if development_login_available(selected):
        return "/auth/login"
    oidc = selected.get("oidc") if isinstance(selected.get("oidc"), dict) else {}
    return "/auth/login" if oidc.get("enabled") is True else None


def unauthenticated_payload(error: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": error}
    path = login_path_for(policy)
    if path:
        payload["loginPath"] = path
    return payload


def oidc_redirect_uri(handler: BaseHTTPRequestHandler, oidc: dict[str, Any]) -> str:
    configured = clean_optional_string(oidc.get("redirectUri"), limit=1000)
    if configured:
        return configured
    host = str(handler.headers.get("Host") or "127.0.0.1:8765")
    scheme = "https" if str(handler.headers.get("X-Forwarded-Proto") or "").lower() == "https" else "http"
    return f"{scheme}://{host}/auth/callback"


def begin_oidc_login(handler: BaseHTTPRequestHandler) -> str:
    policy = auth_policy()
    oidc = policy.get("oidc") if isinstance(policy.get("oidc"), dict) else {}
    if identity_mode(policy) != "oidc-required" or oidc.get("enabled") is not True:
        raise RuntimeError("OIDC login is not enabled")
    authorization_endpoint = clean_optional_string(oidc.get("authorizationEndpoint"), limit=1000)
    client_id = clean_optional_string(oidc.get("clientId"), limit=500)
    if not authorization_endpoint or not client_id:
        raise RuntimeError("OIDC authorization endpoint and client ID are required")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    redirect_uri = oidc_redirect_uri(handler, oidc)
    with AUTH_LOCK:
        cutoff = time.time() - 600
        for key in [key for key, value in OIDC_LOGIN_STATES.items() if float(value.get("createdEpoch") or 0) < cutoff]:
            OIDC_LOGIN_STATES.pop(key, None)
        OIDC_LOGIN_STATES[state] = {"nonce": nonce, "verifier": verifier, "redirectUri": redirect_uri, "createdEpoch": time.time()}
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(clean_string_list(oidc.get("scopes"), limit=20) or ["openid", "profile", "email"]),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    append_auth_audit("oidc-login-started", details={"redirectUri": redirect_uri})
    return f"{authorization_endpoint}?{urlencode(params)}"


def complete_oidc_login(handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> tuple[dict[str, Any], str]:
    state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    if not state or not code:
        raise PermissionError((query.get("error_description") or query.get("error") or ["OIDC callback is missing code or state"])[0])
    state_cookie = request_cookie(handler, "present_oidc_state")
    if not state_cookie or not hmac.compare_digest(state_cookie, state):
        raise PermissionError("OIDC login state is not bound to this browser")
    with AUTH_LOCK:
        login = OIDC_LOGIN_STATES.pop(state, None)
    if not isinstance(login, dict) or time.time() - float(login.get("createdEpoch") or 0) > 600:
        raise PermissionError("OIDC login state is invalid or expired")
    policy = auth_policy()
    oidc = policy.get("oidc") if isinstance(policy.get("oidc"), dict) else {}
    token_endpoint = clean_optional_string(oidc.get("tokenEndpoint"), limit=1000)
    client_id = clean_optional_string(oidc.get("clientId"), limit=500)
    if not token_endpoint or not client_id:
        raise RuntimeError("OIDC token endpoint and client ID are required")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": str(login.get("redirectUri") or ""),
        "client_id": client_id,
        "code_verifier": str(login.get("verifier") or ""),
    }
    secret_env = clean_optional_string(oidc.get("clientSecretEnv"), limit=200) or OIDC_CLIENT_SECRET_ENV
    client_secret = str(os.environ.get(secret_env) or "")
    if client_secret:
        form["client_secret"] = client_secret
    request = Request(token_endpoint, data=urlencode(form).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        token_payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    id_token = str(token_payload.get("id_token") or "") if isinstance(token_payload, dict) else ""
    if not id_token:
        raise PermissionError("OIDC provider did not return an ID token")
    session = issue_oidc_exchange_session(id_token, expected_nonce=str(login.get("nonce") or ""))
    csrf = secrets.token_urlsafe(32)
    return session, csrf


def latest_integrity_hash(mission_id: str) -> str:
    path = mission_integrity_path(mission_id)
    latest = ""
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("chainHash"), str):
                latest = row["chainHash"]
    except OSError:
        return ""
    return latest


def sign_integrity_record(mission_id: str, record_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    produced_at = utc_now()
    payload_hash = canonical_json_hash(payload)
    policy = auth_policy()
    basis = {
        "missionId": mission_id,
        "recordKind": record_kind,
        "previousHash": latest_integrity_hash(mission_id),
        "payloadHash": payload_hash,
        "producedAt": produced_at,
    }
    record_hash = canonical_json_hash(basis)
    external_signature = external_sign_payload(record_hash, basis)
    kms = policy.get("kms") if isinstance(policy.get("kms"), dict) else {}
    if external_signature and external_signature.get("ok") is True:
        signature = str(external_signature.get("signature") or "")
        signature_scheme = "external-kms-or-signer-v1"
        signer_id = str(kms.get("keyId") or os.environ.get(EVIDENCE_SIGNER_ID_ENV, "customer-managed-key"))
    else:
        if external_signing_required(policy):
            reason = "external evidence signer is required but unavailable"
            if external_signature and external_signature.get("error"):
                reason = f"{reason}: {external_signature.get('error')}"
            raise RuntimeError(reason)
        signature = hmac.new(evidence_signing_key(), record_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        signature_scheme = "hmac-sha256-local-alpha"
        signer_id = os.environ.get(EVIDENCE_SIGNER_ID_ENV, "present-local-alpha")
    return {
        "schemaVersion": 1,
        "recordKind": record_kind,
        "missionId": mission_id,
        "producedAt": produced_at,
        "signatureScheme": signature_scheme,
        "signerId": signer_id,
        "previousHash": basis["previousHash"],
        "payloadHash": payload_hash,
        "recordHash": record_hash,
        "signature": signature,
        **({"externalSigner": {key: external_signature.get(key) for key in ("status", "exitCode") if key in external_signature}}
           if external_signature else {}),
    }


def append_integrity_record(mission_id: str, integrity: dict[str, Any]) -> dict[str, Any]:
    entry = {**integrity, "chainHash": canonical_json_hash(integrity)}
    append_jsonl(mission_integrity_path(mission_id), entry)
    return entry


def default_control_policy() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "policyId": "present.delivery-control.alpha",
        "producer": "steel-mission-chat control-plane",
        "modelIndependence": {
            "required": True,
            "description": "The same pre-execution policy is applied regardless of model provider.",
        },
        "customerBoundary": {
            "required": True,
            "deployment": "customer-vpc-or-private-cloud",
            "description": "Missions run against customer-bound repositories, documents, and infrastructure.",
        },
        "executionBoundary": {
            "guardedRunnerRequired": True,
            "directCommandMode": "block",
            "privateRunnerRequired": True,
            "privateRunnerMode": "development-local",
            "privateRunnerCommand": [str(PRIVATE_RUNNER_BIN), "execute"],
            "privateRunnerStatusCommand": [str(PRIVATE_RUNNER_BIN), "status"],
            "requiredProductionIsolation": "container",
            "allowedEnvironment": ["GH_TOKEN", "GITHUB_TOKEN"],
            "guardedEntrypoints": ["bin/present-control-plane", "/api/control-plane/execute"],
            "description": "Executable agent actions must enter through the signed guarded runner and execute on an attested private-worker surface.",
        },
        "blockedCommandPatterns": [
            r"\bsudo\b",
            r"\brm\s+-rf\s+/",
            r"\bcurl\b.*\|\s*(?:sh|bash)\b",
            r"\bwget\b.*\|\s*(?:sh|bash)\b",
            r"\bchmod\s+-R\s+777\b",
            r"\bdd\s+if=",
            r"\bmkfs(?:\.[A-Za-z0-9]+)?\b",
            r":\(\)\{\s*:\|:&\s*\};:",
        ],
        "approvalRequired": {
            "phases": ["modify", "repair"],
            "prModes": ["draft", "create"],
            "deployProviders": ["command", "sites"],
            "deployEnvironments": ["production", "prod"],
        },
        "autoApprovedPhases": ["build", "test", "inspect"],
        "complianceMappings": {
            "SOC 2": ["CC6.1", "CC7.2", "CC8.1", "CC8.5"],
            "ISO 27001": ["A.5.15", "A.5.23", "A.8.9", "A.8.16", "A.8.32"],
            "ISO 42001": ["A.6.2", "A.7.4", "A.8.2"],
        },
    }


def control_policy() -> dict[str, Any]:
    configured = read_json_file(CONTROL_POLICY_PATH)
    policy = default_control_policy()
    if not configured:
        return {**policy, "configuredPath": str(CONTROL_POLICY_PATH), "configured": False}
    merged = {**policy, **configured}
    for key in ["modelIndependence", "customerBoundary", "executionBoundary", "approvalRequired", "complianceMappings"]:
        if isinstance(policy.get(key), dict) and isinstance(configured.get(key), dict):
            merged[key] = {**policy[key], **configured[key]}
    return {**merged, "configuredPath": str(CONTROL_POLICY_PATH), "configured": True}


def normalize_control_policy(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("policy") if isinstance(payload.get("policy"), dict) else payload
    base = default_control_policy()
    approval_source = source.get("approvalRequired") if isinstance(source.get("approvalRequired"), dict) else {}
    compliance_source = source.get("complianceMappings") if isinstance(source.get("complianceMappings"), dict) else {}
    execution_source = source.get("executionBoundary") if isinstance(source.get("executionBoundary"), dict) else {}
    runner_command = execution_source.get("privateRunnerCommand")
    runner_status_command = execution_source.get("privateRunnerStatusCommand")
    return {
        "schemaVersion": 1,
        "policyId": clean_optional_string(source.get("policyId"), limit=160) or base["policyId"],
        "producer": "steel-mission-chat control-plane",
        "producedAt": utc_now(),
        "modelIndependence": {
            **base["modelIndependence"],
            **(source.get("modelIndependence") if isinstance(source.get("modelIndependence"), dict) else {}),
            "required": True,
        },
        "customerBoundary": {
            **base["customerBoundary"],
            **(source.get("customerBoundary") if isinstance(source.get("customerBoundary"), dict) else {}),
        },
        "executionBoundary": {
            **base["executionBoundary"],
            **execution_source,
            "guardedRunnerRequired": True,
            "directCommandMode": "block",
            "privateRunnerRequired": True,
            "privateRunnerMode": clean_choice(
                execution_source.get("privateRunnerMode"), {"container", "development-local"}, "development-local"
            ),
            "privateRunnerCommand": [
                str(item).strip() for item in runner_command[:8]
                if isinstance(item, str) and item.strip()
            ] if isinstance(runner_command, list) else base["executionBoundary"]["privateRunnerCommand"],
            "privateRunnerStatusCommand": [
                str(item).strip() for item in runner_status_command[:8]
                if isinstance(item, str) and item.strip()
            ] if isinstance(runner_status_command, list) else base["executionBoundary"]["privateRunnerStatusCommand"],
            "requiredProductionIsolation": "container",
            "allowedEnvironment": clean_string_list(execution_source.get("allowedEnvironment"), limit=30)
            or base["executionBoundary"]["allowedEnvironment"],
        },
        "blockedCommandPatterns": clean_string_list(source.get("blockedCommandPatterns"), limit=120) or base["blockedCommandPatterns"],
        "approvalRequired": {
            "phases": clean_string_list(approval_source.get("phases"), limit=20) or base["approvalRequired"]["phases"],
            "prModes": clean_string_list(approval_source.get("prModes"), limit=20) or base["approvalRequired"]["prModes"],
            "deployProviders": clean_string_list(approval_source.get("deployProviders"), limit=20) or base["approvalRequired"]["deployProviders"],
            "deployEnvironments": clean_string_list(approval_source.get("deployEnvironments"), limit=40) or base["approvalRequired"]["deployEnvironments"],
        },
        "autoApprovedPhases": clean_string_list(source.get("autoApprovedPhases"), limit=20) or base["autoApprovedPhases"],
        "complianceMappings": {
            standard: clean_string_list(controls, limit=80)
            for standard, controls in compliance_source.items()
            if isinstance(standard, str) and isinstance(controls, list)
        } or base["complianceMappings"],
    }


def save_control_policy(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can manage control policy")
    before = read_json_file(CONTROL_POLICY_PATH)
    policy = normalize_control_policy(payload)
    atomic_write_json(CONTROL_POLICY_PATH, policy)
    record_mutation(
        "control-policy-saved",
        role,
        CONTROL_POLICY_PATH,
        before=before,
        after=policy,
        details={
            "policyId": policy.get("policyId"),
            "blockedCommandPatterns": len(policy.get("blockedCommandPatterns", [])),
            "complianceStandards": sorted(policy.get("complianceMappings", {}).keys()),
        },
    )
    return policy


def default_integration_registry() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "producer": "steel-mission-chat integration-registry",
        "producedAt": utc_now(),
        "controlPlane": {
            "deploymentBoundary": "customer-vpc-or-private-cloud",
            "policyPath": str(CONTROL_POLICY_PATH),
            "evidenceRoot": str(MISSION_ROOT),
            "modelIndependent": True,
        },
        "workflowEmbedding": {
            "strategy": "existing-tools-first",
            "controlSurfaceRole": "administration-investigation-and-fallback",
            "inboundChannels": ["scm-events", "issue-events", "chat-commands", "ci-events", "ide-provider-events"],
            "returnChannels": ["status", "approval-request", "control-decision", "evidence-link", "completion"],
            "requirements": [
                "preserve-originating-identity-and-thread",
                "return-status-approvals-decisions-and-evidence-to-source",
                "deep-link-to-investigation-without-forced-relocation",
            ],
        },
        "modelProviders": [
            {"id": "claude", "label": "Claude", "status": "configured-by-runtime-profile"},
            {"id": "openai", "label": "OpenAI", "status": "provider-adapter-ready"},
            {"id": "glimmer", "label": "Glimmer/local", "status": "configured-by-runtime-profile"},
            {"id": "local", "label": "Local model", "status": "provider-adapter-ready"},
        ],
        "connectors": [
            {"id": "github", "label": "GitHub", "kind": "scm-pr-ci", "status": "alpha-native-bidirectional", "enabled": True, "mode": "native", "adapter": "github", "tokenEnv": "GITHUB_TOKEN", "secretEnv": "GITHUB_WEBHOOK_SECRET", "ingressRole": "user", "events": ["status", "approval-requested", "control-decision", "evidence", "mission-completed"]},
            {"id": "gitlab", "label": "GitLab", "kind": "scm-pr-ci", "status": "alpha-command-or-webhook", "enabled": False, "mode": "registry", "events": ["pr", "ci"]},
            {"id": "jira", "label": "Jira", "kind": "work-tracking", "status": "alpha-native-bidirectional", "enabled": False, "mode": "native", "adapter": "jira", "tokenEnv": "JIRA_API_TOKEN", "secretEnv": "JIRA_WEBHOOK_SECRET", "baseUrlEnv": "JIRA_BASE_URL", "ingressRole": "user", "events": ["status", "approval-requested", "control-decision", "evidence", "mission-completed"]},
            {"id": "linear", "label": "Linear", "kind": "work-tracking", "status": "alpha-command-or-webhook", "enabled": False, "mode": "registry", "events": ["approval-requested", "mission-completed"]},
            {"id": "slack", "label": "Slack", "kind": "approval-notifications", "status": "alpha-native-bidirectional", "enabled": False, "mode": "native", "adapter": "slack", "tokenEnv": "SLACK_BOT_TOKEN", "secretEnv": "SLACK_SIGNING_SECRET", "ingressRole": "user", "events": ["status", "approval-requested", "control-decision", "evidence", "mission-completed"]},
            {"id": "ci-cd", "label": "CI/CD pipelines", "kind": "build-test-deploy", "status": "alpha-command-and-github-actions", "enabled": True, "mode": "registry", "events": ["build", "test", "deploy"]},
            {"id": "siem", "label": "SIEM/security monitoring", "kind": "security-evidence-export", "status": "core-export-ready", "enabled": False, "mode": "registry", "events": ["audit", "evidence", "control-decision"]},
        ],
    }


def normalize_connector(item: dict[str, Any]) -> dict[str, Any]:
    connector_id = safe_path_part(str(item.get("id") or item.get("label") or "connector"), "connector")
    mode = clean_choice(item.get("mode"), {"registry", "outbox", "command", "webhook", "native"}, "registry")
    kind = clean_optional_string(item.get("kind"), limit=120) or "integration"
    default_adapter = connector_id if connector_id in {"github", "slack", "jira"} else "generic"
    return {
        "id": connector_id,
        "label": clean_optional_string(item.get("label"), limit=120) or connector_id,
        "kind": kind,
        "status": clean_optional_string(item.get("status"), limit=120) or "registry-ready",
        "enabled": bool_from_payload(item.get("enabled"), False),
        "mode": mode,
        "adapter": clean_choice(item.get("adapter"), {"generic", "github", "slack", "jira"}, default_adapter),
        "command": clean_optional_string(item.get("command"), limit=1000),
        "webhookUrl": clean_optional_string(item.get("webhookUrl"), limit=1000),
        "webhookSecretEnv": clean_optional_string(item.get("webhookSecretEnv"), limit=120) or CONNECTOR_WEBHOOK_SECRET_ENV,
        "tokenEnv": clean_optional_string(item.get("tokenEnv"), limit=120),
        "secretEnv": clean_optional_string(item.get("secretEnv"), limit=120),
        "baseUrl": clean_optional_string(item.get("baseUrl"), limit=1000),
        "baseUrlEnv": clean_optional_string(item.get("baseUrlEnv"), limit=120),
        # Signed workflow events represent an external actor, never a local
        # control-plane administrator. Privilege elevation remains an explicit
        # approval inside the mission lifecycle.
        "ingressRole": "user",
        "serviceUserId": clean_optional_string(item.get("serviceUserId"), limit=200),
        "exportPath": clean_optional_string(item.get("exportPath"), limit=1000),
        "events": clean_string_list(item.get("events"), limit=40),
    }


def normalize_integration_registry(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("registry") if isinstance(payload.get("registry"), dict) else payload
    base = default_integration_registry()
    by_id = {
        str(item.get("id")): normalize_connector(item)
        for item in base.get("connectors", [])
        if isinstance(item, dict) and item.get("id")
    }
    for item in source.get("connectors", []) if isinstance(source.get("connectors"), list) else []:
        if not isinstance(item, dict):
            continue
        connector = normalize_connector(item)
        previous = by_id.get(connector["id"], {})
        by_id[connector["id"]] = {**previous, **connector}
    model_providers: list[dict[str, Any]] = []
    provider_source = source.get("modelProviders") if isinstance(source.get("modelProviders"), list) else base.get("modelProviders", [])
    for item in provider_source:
        if not isinstance(item, dict):
            continue
        provider_id = safe_path_part(str(item.get("id") or item.get("label") or "provider"), "provider")
        model_providers.append({
            "id": provider_id,
            "label": clean_optional_string(item.get("label"), limit=120) or provider_id,
            "status": clean_optional_string(item.get("status"), limit=120) or "provider-adapter-ready",
        })
    control = source.get("controlPlane") if isinstance(source.get("controlPlane"), dict) else {}
    workflow_embedding = source.get("workflowEmbedding") if isinstance(source.get("workflowEmbedding"), dict) else base["workflowEmbedding"]
    return {
        "schemaVersion": 1,
        "producer": "steel-mission-chat integration-registry",
        "producedAt": utc_now(),
        "controlPlane": {
            "deploymentBoundary": clean_optional_string(control.get("deploymentBoundary"), limit=200) or "customer-vpc-or-private-cloud",
            "policyPath": clean_optional_string(control.get("policyPath"), limit=1000) or str(CONTROL_POLICY_PATH),
            "evidenceRoot": clean_optional_string(control.get("evidenceRoot"), limit=1000) or str(MISSION_ROOT),
            "modelIndependent": True,
        },
        "workflowEmbedding": {
            "strategy": "existing-tools-first",
            "controlSurfaceRole": "administration-investigation-and-fallback",
            "inboundChannels": clean_string_list(workflow_embedding.get("inboundChannels"), limit=20) or base["workflowEmbedding"]["inboundChannels"],
            "returnChannels": clean_string_list(workflow_embedding.get("returnChannels"), limit=20) or base["workflowEmbedding"]["returnChannels"],
            "requirements": clean_string_list(workflow_embedding.get("requirements"), limit=20) or base["workflowEmbedding"]["requirements"],
        },
        "modelProviders": model_providers or base["modelProviders"],
        "connectors": sorted(by_id.values(), key=lambda connector: connector["id"]),
    }


def integration_registry(role: str = "user") -> dict[str, Any]:
    configured = read_json_file(INTEGRATION_REGISTRY_PATH)
    registry = normalize_integration_registry(configured) if configured else normalize_integration_registry(default_integration_registry())
    registry = attach_integration_edition_metadata(registry)
    selected = corporate_role(role)
    payload = {**registry, "ok": True, "role": selected, "configuredPath": str(INTEGRATION_REGISTRY_PATH), "configured": bool(configured)}
    if selected not in {"owner", "admin"}:
        payload["controlPlane"] = {
            "deploymentBoundary": payload.get("controlPlane", {}).get("deploymentBoundary"),
            "modelIndependent": True,
        }
        payload["connectors"] = [
            {key: item[key] for key in ("id", "label", "kind", "status") if key in item}
            for item in payload.get("connectors", [])
            if isinstance(item, dict)
        ]
    return payload


def save_integration_registry(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    role = corporate_role(actor)
    if role not in {"owner", "admin"}:
        raise ValueError("only owner and admin endpoints can manage integrations")
    before = read_json_file(INTEGRATION_REGISTRY_PATH)
    registry = normalize_integration_registry(payload)
    atomic_write_json(INTEGRATION_REGISTRY_PATH, registry)
    record_mutation(
        "integration-registry-saved",
        role,
        INTEGRATION_REGISTRY_PATH,
        before=before,
        after=registry,
        details={
            "connectors": len(registry.get("connectors", [])),
            "enabled": len([item for item in registry.get("connectors", []) if isinstance(item, dict) and item.get("enabled")]),
        },
    )
    return attach_integration_edition_metadata(registry)


def attach_integration_edition_metadata(registry: dict[str, Any]) -> dict[str, Any]:
    entitlement = license_entitlement()
    return {
        **registry,
        "connectors": [
            {
                **item,
                **({"nativeReadiness": native_connector_readiness(item)} if item.get("mode") == "native" else {}),
            }
            for item in registry.get("connectors", [])
            if isinstance(item, dict)
        ],
        "entitlement": entitlement,
    }


def compliance_evidence(control: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = control_policy()
    mapping = policy.get("complianceMappings") if isinstance(policy.get("complianceMappings"), dict) else {}
    return {
        "schemaVersion": 1,
        "standards": mapping,
        "evidenceUse": [
            "pre-execution decision",
            "risk classification",
            "approval evidence",
            "command or provider result",
            "signed integrity chain",
        ],
        "controlsSatisfied": [
            "unsafe actions are blocked before execution",
            "high-risk delivery actions require human approval",
            "evidence records are tamper-evident through a hash chain and HMAC signature",
        ],
        "decision": control or {},
    }


def compliance_control_matrix(decisions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    decision_count = len(decisions or [])
    return [
        {
            "standard": "SOC 2",
            "controls": ["CC6.1", "CC7.2", "CC8.1", "CC8.5"],
            "evidenceTypes": ["identity/session", "pre-execution decision", "approval record", "change evidence", "integrity chain"],
            "implemented": True,
            "evidenceCount": decision_count,
        },
        {
            "standard": "ISO 27001",
            "controls": ["A.5.15", "A.5.23", "A.8.9", "A.8.16", "A.8.32"],
            "evidenceTypes": ["access control", "cloud/service boundary", "configuration control", "monitoring", "change control"],
            "implemented": True,
            "evidenceCount": decision_count,
        },
        {
            "standard": "ISO 42001",
            "controls": ["A.6.2", "A.7.4", "A.8.2"],
            "evidenceTypes": ["AI-system governance", "traceability", "risk treatment", "human oversight"],
            "implemented": True,
            "evidenceCount": decision_count,
        },
    ]


def control_plane_production_readiness(role: str = "admin") -> dict[str, Any]:
    policy = control_policy()
    auth = auth_policy()
    registry = integration_registry(role)
    entitlement = license_entitlement()
    connectors = registry.get("connectors") if isinstance(registry.get("connectors"), list) else []
    enabled = [item for item in connectors if isinstance(item, dict) and item.get("enabled") and not item.get("locked")]
    oidc = auth.get("oidc") if isinstance(auth.get("oidc"), dict) else {}
    authorization = auth.get("authorization") if isinstance(auth.get("authorization"), dict) else {}
    kms = auth.get("kms") if isinstance(auth.get("kms"), dict) else {}
    execution_boundary = policy.get("executionBoundary") if isinstance(policy.get("executionBoundary"), dict) else {}
    signer = evidence_signer_health(auth)
    private_runner = private_runner_health()
    native_connectors = [
        item for item in connectors
        if isinstance(item, dict) and item.get("adapter") in {"github", "slack", "jira"}
    ]
    native_ready = [
        item for item in native_connectors
        if item.get("enabled")
        and isinstance(item.get("nativeReadiness"), dict)
        and item["nativeReadiness"].get("ingressReady") is True
        and item["nativeReadiness"].get("egressReady") is True
    ]
    wrapper_path = WORKER_DIR / "bin" / "present-control-plane"
    runner_enforced = (
        execution_boundary.get("guardedRunnerRequired") is True
        and execution_boundary.get("directCommandMode") == "block"
        and wrapper_path.exists()
    )
    checks = [
        {
            "id": "model-independent",
            "label": "Model-independent runtime",
            "alpha": True,
            "production": True,
            "detail": "Runtime profiles bind models separately from domain capabilities and guarded execution runs outside the model.",
        },
        {
            "id": "customer-controlled",
            "label": "Customer-controlled deployment",
            "alpha": True,
            "production": bool(registry.get("controlPlane", {}).get("deploymentBoundary")) if isinstance(registry.get("controlPlane"), dict) else False,
            "detail": "Policy, auth metadata, evidence, repos, and connector configuration are customer-owned files/endpoints.",
        },
        {
            "id": "pre-execution-blocking",
            "label": "Pre-execution blocking",
            "alpha": bool(policy.get("blockedCommandPatterns")),
            "production": bool(policy.get("blockedCommandPatterns") and runner_enforced),
            "detail": "The guarded CLI/API runs policy before command or provider execution, and direct command paths are blocked.",
        },
        {
            "id": "isolated-private-runner",
            "label": "Isolated private runner",
            "alpha": private_runner.get("ok") is True,
            "production": private_runner.get("productionEligible") is True and private_runner.get("isolationLevel") == "container",
            "detail": "Executable delivery and provider commands cross an attested private-runner boundary; production requires the hardened ephemeral container surface.",
        },
        {
            "id": "tamper-evident-evidence",
            "label": "Tamper-evident evidence",
            "alpha": True,
            "production": bool(kms.get("requireExternalSigning") and signer.get("ok")),
            "detail": "Mission records are signed and hash chained; production requires a healthy external signer or customer KMS command.",
        },
        {
            "id": "compliance-evidence",
            "label": "Compliance evidence",
            "alpha": bool(policy.get("complianceMappings")),
            "production": set((policy.get("complianceMappings") or {}).keys()) >= {"SOC 2", "ISO 27001", "ISO 42001"},
            "detail": "Proof/report/SIEM exports include standards mappings and evidence matrix rows.",
        },
        {
            "id": "risk-approvals",
            "label": "Risk-based approvals",
            "alpha": bool(policy.get("approvalRequired")),
            "production": bool((policy.get("approvalRequired") or {}).get("phases") and (policy.get("approvalRequired") or {}).get("deployEnvironments")),
            "detail": "Low-risk phases can run automatically; high-risk phases and deploy targets require approval.",
        },
        {
            "id": "tool-integrations",
            "label": "Existing tool integrations",
            "alpha": {item.get("adapter") for item in native_connectors} >= {"github", "slack", "jira"},
            "production": bool(native_ready),
            "detail": "Signed GitHub, Slack, and Jira ingress preserves source/thread identity and native egress returns status, approvals, decisions, evidence, and completion.",
        },
        {
            "id": "baseline-auth",
            "label": "Baseline identity",
            "alpha": auth.get("enforcementMode") == "signed-session-required-for-control-plane",
            "production": bool(
                identity_mode(auth) == "oidc-required"
                and oidc.get("enabled")
                and oidc.get("issuer")
                and (oidc.get("jwksUrl") or oidc.get("jwksPath") or oidc.get("jwks"))
                and oidc.get("authorizationEndpoint")
                and oidc.get("tokenEndpoint")
                and oidc.get("clientId")
                and authorization.get("preventSelfApproval") is True
            ),
            "detail": "Production fails closed on OIDC, maps identities to server-owned user/org authorization, and prevents self-approval.",
        },
    ]
    alpha_score = round(100 * sum(1 for item in checks if item["alpha"]) / len(checks))
    production_score = round(100 * sum(1 for item in checks if item["production"]) / len(checks))
    return {
        "ok": True,
        "schemaVersion": 1,
        "alphaScore": alpha_score,
        "productionScore": production_score,
        "target": {"alpha": 95, "production": 75},
        "meetsAlphaTarget": alpha_score >= 95,
        "meetsProductionTarget": production_score >= 75,
        "guardedEntrypoints": {
            "cli": str(wrapper_path),
            "api": "/api/control-plane/execute",
            "requiresSignedSession": auth.get("enforcementMode") == "signed-session-required-for-control-plane",
            "directCommandMode": str(execution_boundary.get("directCommandMode") or ""),
            "guardedRunnerRequired": execution_boundary.get("guardedRunnerRequired") is True,
            "privateRunnerRequired": execution_boundary.get("privateRunnerRequired") is True,
            "privateRunner": private_runner,
        },
        "entitlement": entitlement,
        "evidenceSigner": signer,
        "checks": checks,
        "remainingProductionHardening": [
            item for item in [
                None if private_runner.get("productionEligible") and private_runner.get("isolationLevel") == "container" else "Build and select the hardened private-runner image for container-isolated execution.",
                None if signer.get("ok") and kms.get("requireExternalSigning") else "Back evidence signing with a customer-controlled KMS or external signing command.",
                None if identity_mode(auth) == "oidc-required" and oidc.get("enabled") and oidc.get("issuer") and oidc.get("authorizationEndpoint") and oidc.get("tokenEndpoint") and oidc.get("clientId") else "Configure the fail-closed OIDC browser/session boundary, issuer/JWKS, and client endpoints.",
                None if authorization.get("preventSelfApproval") is True else "Enable separation of duties for mission approvals.",
                None if native_ready else "Configure signed ingress and native egress credentials for at least one GitHub, Slack, or Jira workflow.",
                None if runner_enforced else "Force executable agent actions through the guarded control-plane runner.",
            ]
            if item
        ],
        "producedAt": utc_now(),
    }


def connector_by_id(connector_id: str, role: str = "admin") -> dict[str, Any] | None:
    registry = integration_registry(role)
    for item in registry.get("connectors", []) if isinstance(registry.get("connectors"), list) else []:
        if isinstance(item, dict) and item.get("id") == connector_id:
            return item
    return None


def connector_supports_event(connector: dict[str, Any], event_type: str) -> bool:
    events = connector.get("events") if isinstance(connector.get("events"), list) else []
    return not events or event_type in events or "*" in events


def connector_outbox_path(connector: dict[str, Any], event_id: str) -> Path:
    configured = str(connector.get("exportPath") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.suffix == ".jsonl":
            return path
        return path / f"{event_id}.json"
    return MISSION_ROOT / "_connector-outbox" / safe_path_part(str(connector.get("id") or "connector"), "connector") / f"{event_id}.json"


def normalize_workflow_origin(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    source_system = clean_choice(source.get("sourceSystem"), {"github", "slack", "jira", "api", "ui"}, "")
    if not source_system:
        return {}
    fields = {
        "sourceSystem": source_system,
        "sourceType": clean_optional_string(source.get("sourceType"), limit=80),
        "sourceId": clean_optional_string(source.get("sourceId"), limit=240),
        "threadId": clean_optional_string(source.get("threadId"), limit=240),
        "actorId": clean_optional_string(source.get("actorId"), limit=160),
        "returnChannel": clean_optional_string(source.get("returnChannel"), limit=80),
        "returnTarget": clean_optional_string(source.get("returnTarget"), limit=500),
        "deepLink": clean_optional_string(source.get("deepLink"), limit=1000),
        "repository": clean_optional_string(source.get("repository"), limit=240),
        "issueNumber": clean_optional_string(source.get("issueNumber"), limit=40),
        "channelId": clean_optional_string(source.get("channelId"), limit=160),
        "threadTs": clean_optional_string(source.get("threadTs"), limit=160),
        "issueKey": clean_optional_string(source.get("issueKey"), limit=120),
    }
    return {key: item for key, item in fields.items() if item}


def connector_payload_origin(payload: dict[str, Any]) -> dict[str, Any]:
    return normalize_workflow_origin(payload.get("origin") or payload.get("workflowOrigin"))


def native_connector_readiness(connector: dict[str, Any]) -> dict[str, Any]:
    adapter = str(connector.get("adapter") or connector.get("id") or "generic")
    token_env = str(connector.get("tokenEnv") or {
        "github": "GITHUB_TOKEN", "slack": "SLACK_BOT_TOKEN", "jira": "JIRA_API_TOKEN"
    }.get(adapter, ""))
    secret_env = str(connector.get("secretEnv") or {
        "github": "GITHUB_WEBHOOK_SECRET", "slack": "SLACK_SIGNING_SECRET", "jira": "JIRA_WEBHOOK_SECRET"
    }.get(adapter, ""))
    base_url_env = str(connector.get("baseUrlEnv") or ("JIRA_BASE_URL" if adapter == "jira" else ""))
    token_configured = bool(token_env and os.environ.get(token_env)) or (adapter == "github" and bool(os.environ.get("GH_TOKEN")))
    base_url = str(connector.get("baseUrl") or (os.environ.get(base_url_env) if base_url_env else "") or "").rstrip("/")
    egress_ready = token_configured and (adapter != "jira" or bool(base_url))
    return {
        "adapter": adapter,
        "native": adapter in {"github", "slack", "jira"},
        "tokenEnv": token_env,
        "tokenConfigured": token_configured,
        "secretEnv": secret_env,
        "ingressSecretConfigured": bool(secret_env and os.environ.get(secret_env)),
        "baseUrlEnv": base_url_env,
        "baseUrlConfigured": bool(base_url) if adapter == "jira" else True,
        "ingressReady": bool(secret_env and os.environ.get(secret_env)),
        "egressReady": egress_ready,
    }


def connector_action_plan(connector: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    origin = connector_payload_origin(payload)
    return {
        "schemaVersion": 1,
        "interface": ["plan", "preflight", "execute", "observe", "evidence", "rollback/export"],
        "connectorId": connector.get("id") or "",
        "connectorLabel": connector.get("label") or connector.get("id") or "",
        "kind": connector.get("kind") or "",
        "eventType": event_type,
        "mode": connector.get("mode") or "registry",
        "enabled": bool(connector.get("enabled")),
        "interactionModel": "workflow-embedded",
        "controlSurfaceRole": "administration-investigation-and-fallback",
        "origin": origin,
        "originContextPreserved": bool(origin),
        "returnToOrigin": bool(origin and origin.get("sourceSystem") == connector.get("adapter")),
        "investigationPath": payload.get("investigationPath") or "",
        "payloadHash": canonical_json_hash(payload),
    }


def connector_action_preflight(connector: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    plan = connector_action_plan(connector, event_type, payload)
    blockers: list[str] = []
    if not connector.get("enabled"):
        blockers.append("connector is disabled")
    if not connector_supports_event(connector, event_type):
        blockers.append(f"connector does not subscribe to {event_type}")
    mode = str(connector.get("mode") or "registry")
    command = str(connector.get("command") or "")
    if mode == "command" and not command:
        blockers.append("connector command is not configured")
    if mode == "webhook" and not str(connector.get("webhookUrl") or "").strip():
        blockers.append("connector webhookUrl is not configured")
    origin = connector_payload_origin(payload)
    native_readiness = native_connector_readiness(connector) if mode == "native" else {}
    native_targets_origin = bool(origin and origin.get("sourceSystem") == connector.get("adapter"))
    if mode == "native" and native_targets_origin and native_readiness.get("egressReady") is not True:
        blockers.append(f"native {connector.get('adapter') or connector.get('id')} egress credentials are not configured")
    blocked_patterns = command_matches_blocked_pattern(command, control_policy()) if command else []
    blockers.extend(f"connector command matches blocked policy pattern: {pattern}" for pattern in blocked_patterns)
    decision = "allow" if not blockers else "block"
    return {
        "schemaVersion": 1,
        "decision": decision,
        "ok": decision == "allow",
        "plan": plan,
        "risk": "medium" if mode in {"command", "webhook"} else "low",
        "blockers": blockers,
        "modelIndependent": True,
        "customerControlled": True,
        "nativeReadiness": native_readiness,
        "policyHash": canonical_json_hash(control_policy()),
    }


def run_connector_command(command: str, payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any]:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    payload_hash = canonical_json_hash(payload)
    mission_id = str(nested.get("missionId") or "")
    if not re.fullmatch(r"ms-[a-f0-9]{24}", mission_id):
        mission_id = "ms-" + payload_hash[:24]
    task_id = str(nested.get("taskId") or "")
    if not re.fullmatch(r"DEV-[0-9]{6}", task_id):
        task_id = f"DEV-{int(payload_hash[24:36], 16) % 1_000_000:06d}"
    return run_delivery_private_runner(
        command,
        WORKER_DIR,
        {"missionId": mission_id, "taskId": task_id},
        "inspect",
        timeout=timeout,
        stdin_text=json.dumps(payload, sort_keys=True),
        request_environment={"PRESENT_CONNECTOR_PAYLOAD_SHA256": payload_hash},
    )


def post_connector_webhook(connector: dict[str, Any], payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    started = time.time()
    url = str(connector.get("webhookUrl") or "")
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    secret_env = str(connector.get("webhookSecretEnv") or CONNECTOR_WEBHOOK_SECRET_ENV)
    secret = os.environ.get(secret_env, "")
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8") or auth_signing_key(), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Steel-Mission-Control-Plane/ga",
        "X-Present-Timestamp": timestamp,
        "X-Present-Signature": f"sha256={signature}",
    }
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read(65536).decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 300,
                "status": "succeeded" if 200 <= response.status < 300 else "failed",
                "httpStatus": response.status,
                "durationSeconds": round(time.time() - started, 1),
                "body": limited_text(response_body, limit=4000),
                "signatureHeader": "X-Present-Signature",
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "error": str(exc), "durationSeconds": round(time.time() - started, 1)}


def workflow_event_message(envelope: dict[str, Any]) -> str:
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    event_type = str(envelope.get("eventType") or "status")
    mission_id = str(payload.get("missionId") or "")
    title = {
        "approval-requested": "Approval needed",
        "control-decision": "Control decision recorded",
        "evidence": "Evidence recorded",
        "mission-completed": "Mission completed",
        "status": "Mission status",
    }.get(event_type, event_type.replace("-", " ").title())
    detail = str(payload.get("summary") or payload.get("title") or payload.get("state") or payload.get("decision") or "").strip()
    investigation = str(payload.get("investigationUrl") or payload.get("investigationPath") or "").strip()
    lines = [f"Steel Mission · {title}"]
    if mission_id:
        lines.append(f"Mission: {mission_id}")
    if detail:
        lines.append(detail[:1500])
    if investigation:
        lines.append(f"Investigate: {investigation}")
    return "\n".join(lines)


def post_native_json(url: str, body: dict[str, Any], headers: dict[str, str], *, timeout: int = 30) -> dict[str, Any]:
    started = time.time()
    request = Request(
        url,
        data=json.dumps(body, sort_keys=True).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Steel-Mission-Control-Plane/alpha", **headers},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            try:
                response_payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                response_payload = {"body": limited_text(raw, limit=4000)}
            ok = 200 <= int(response.status) < 300
            return {
                "ok": ok,
                "status": "succeeded" if ok else "failed",
                "httpStatus": int(response.status),
                "durationSeconds": round(time.time() - started, 1),
                "response": response_payload,
                "url": url,
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "error": str(exc), "durationSeconds": round(time.time() - started, 1), "url": url}


def run_native_github_connector(connector: dict[str, Any], envelope: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
    repository = str(origin.get("repository") or "").strip()
    issue_number = str(origin.get("issueNumber") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not issue_number.isdigit():
        return {"ok": False, "status": "blocked", "error": "GitHub origin is missing repository or issue number"}
    token_env = str(connector.get("tokenEnv") or "GITHUB_TOKEN")
    token = str(os.environ.get(token_env) or os.environ.get("GH_TOKEN") or "")
    if not token:
        return {"ok": False, "status": "blocked", "error": f"GitHub token is unavailable in {token_env}"}
    url = f"https://api.github.com/repos/{repository}/issues/{quote(issue_number, safe='')}/comments"
    return post_native_json(
        url,
        {"body": workflow_event_message(envelope)},
        {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )


def run_native_slack_connector(connector: dict[str, Any], envelope: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
    channel = str(origin.get("channelId") or origin.get("returnTarget") or "").strip()
    if not channel:
        return {"ok": False, "status": "blocked", "error": "Slack origin is missing channel id"}
    token_env = str(connector.get("tokenEnv") or "SLACK_BOT_TOKEN")
    token = str(os.environ.get(token_env) or "")
    if not token:
        return {"ok": False, "status": "blocked", "error": f"Slack token is unavailable in {token_env}"}
    body: dict[str, Any] = {"channel": channel, "text": workflow_event_message(envelope), "unfurl_links": False}
    if origin.get("threadTs"):
        body["thread_ts"] = origin["threadTs"]
    result = post_native_json("https://slack.com/api/chat.postMessage", body, {"Authorization": f"Bearer {token}"})
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    if result.get("ok") is True and response.get("ok") is False:
        return {**result, "ok": False, "status": "failed", "error": str(response.get("error") or "Slack API rejected the message")}
    return result


def run_native_jira_connector(connector: dict[str, Any], envelope: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
    issue_key = str(origin.get("issueKey") or origin.get("returnTarget") or "").strip()
    base_url_env = str(connector.get("baseUrlEnv") or "JIRA_BASE_URL")
    base_url = str(connector.get("baseUrl") or os.environ.get(base_url_env) or "").rstrip("/")
    if not issue_key or not base_url:
        return {"ok": False, "status": "blocked", "error": "Jira origin or base URL is not configured"}
    token_env = str(connector.get("tokenEnv") or "JIRA_API_TOKEN")
    token = str(os.environ.get(token_env) or "")
    if not token:
        return {"ok": False, "status": "blocked", "error": f"Jira token is unavailable in {token_env}"}
    url = f"{base_url}/rest/api/2/issue/{quote(issue_key, safe='')}/comment"
    return post_native_json(url, {"body": workflow_event_message(envelope)}, {"Authorization": f"Bearer {token}", "Accept": "application/json"})


def run_native_connector(connector: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    origin = connector_payload_origin(envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {})
    adapter = str(connector.get("adapter") or connector.get("id") or "")
    if not origin or origin.get("sourceSystem") != adapter:
        return {
            "ok": True,
            "status": "skipped",
            "message": "native connector does not own the originating workflow",
            "originSourceSystem": origin.get("sourceSystem") or "",
        }
    if adapter == "github":
        return run_native_github_connector(connector, envelope, origin)
    if adapter == "slack":
        return run_native_slack_connector(connector, envelope, origin)
    if adapter == "jira":
        return run_native_jira_connector(connector, envelope, origin)
    return {"ok": False, "status": "blocked", "error": f"native connector adapter {adapter!r} is not supported"}


def normalized_request_headers(headers: Any) -> dict[str, str]:
    if isinstance(headers, dict):
        items = headers.items()
    elif hasattr(headers, "items"):
        items = headers.items()
    else:
        items = []
    return {str(key).lower(): str(value) for key, value in items}


def verify_workflow_ingress_signature(source: str, connector: dict[str, Any], headers: Any, raw_body: bytes) -> dict[str, Any]:
    normalized = normalized_request_headers(headers)
    secret_env = str(connector.get("secretEnv") or {
        "github": "GITHUB_WEBHOOK_SECRET", "slack": "SLACK_SIGNING_SECRET", "jira": "JIRA_WEBHOOK_SECRET"
    }.get(source, ""))
    secret = str(os.environ.get(secret_env) or "")
    if not secret:
        return {"ok": False, "error": f"{source} ingress secret is unavailable in {secret_env}"}
    if source == "slack":
        timestamp = normalized.get("x-slack-request-timestamp", "")
        signature = normalized.get("x-slack-signature", "")
        try:
            replay_safe = abs(int(time.time()) - int(timestamp)) <= 300
        except (TypeError, ValueError):
            replay_safe = False
        expected = "v0=" + hmac.new(secret.encode("utf-8"), b"v0:" + timestamp.encode("ascii", errors="ignore") + b":" + raw_body, hashlib.sha256).hexdigest()
        return {
            "ok": replay_safe and bool(signature) and hmac.compare_digest(signature, expected),
            "algorithm": "slack-v0-hmac-sha256",
            "replaySafe": replay_safe,
            **({"error": "Slack signature is invalid or outside the five-minute replay window"} if not (replay_safe and signature and hmac.compare_digest(signature, expected)) else {}),
        }
    signature_header = "x-hub-signature-256" if source == "github" else "x-steel-mission-signature"
    signature = normalized.get(signature_header, "")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    ok = bool(signature) and hmac.compare_digest(signature, expected)
    return {
        "ok": ok,
        "algorithm": "hmac-sha256",
        **({"error": f"{source} webhook signature is invalid"} if not ok else {}),
    }


def parse_workflow_ingress_payload(source: str, raw_body: bytes, content_type: str) -> dict[str, Any]:
    text = raw_body.decode("utf-8")
    if source == "slack" and "application/x-www-form-urlencoded" in content_type.lower():
        form = parse_qs(text, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in form.items()}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("workflow ingress payload must be an object")
    return payload


def command_after_steel_mission(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(?:^|\s)/?steel-mission(?:\s+|$)(.*)", text, flags=re.I | re.S)
    if match:
        return match.group(1).strip()
    text = re.sub(r"<@[A-Z0-9]+>", "", text, flags=re.I).strip()
    return text


def normalize_github_ingress(headers: dict[str, str], payload: dict[str, Any], raw_body: bytes) -> dict[str, Any]:
    event_type = headers.get("x-github-event", "unknown")
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    pull_request = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    subject = issue or pull_request
    comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
    actor = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    repo_name = str(repository.get("full_name") or "")
    number = str(subject.get("number") or payload.get("number") or "")
    body = str(comment.get("body") or subject.get("body") or "")
    labels = [
        str(item.get("name") or "").strip().lower()
        for item in subject.get("labels", []) if isinstance(item, dict)
    ] if isinstance(subject.get("labels"), list) else []
    explicit = bool(re.search(r"(?:^|\s)/?steel-mission(?:\s|$)", body, flags=re.I))
    accepted = explicit or "steel-mission" in labels
    command = command_after_steel_mission(body) if explicit else ""
    objective = command or "\n\n".join(item for item in [str(subject.get("title") or "").strip(), body.strip()] if item)
    source_type = "pull-request" if pull_request else "issue"
    return {
        "schemaVersion": 1,
        "eventId": headers.get("x-github-delivery") or "github-" + hashlib.sha256(raw_body).hexdigest()[:24],
        "sourceSystem": "github",
        "eventType": event_type,
        "action": str(payload.get("action") or ""),
        "accepted": accepted and bool(objective),
        "reason": "explicit command or steel-mission label" if accepted else "event does not request Steel Mission",
        "objective": objective[:12000],
        "actor": {"id": str(actor.get("id") or actor.get("login") or ""), "name": str(actor.get("login") or "")},
        "origin": normalize_workflow_origin({
            "sourceSystem": "github",
            "sourceType": source_type,
            "sourceId": f"{repo_name}#{number}" if repo_name and number else repo_name,
            "threadId": f"github:{repo_name}:{number}" if repo_name and number else "",
            "actorId": str(actor.get("login") or actor.get("id") or ""),
            "returnChannel": "github-comment",
            "returnTarget": f"{repo_name}#{number}" if repo_name and number else "",
            "deepLink": str(comment.get("html_url") or subject.get("html_url") or ""),
            "repository": repo_name,
            "issueNumber": number,
        }),
        "payloadHash": hashlib.sha256(raw_body).hexdigest(),
        "receivedAt": utc_now(),
    }


def normalize_slack_ingress(headers: dict[str, str], payload: dict[str, Any], raw_body: bytes) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    is_slash = bool(payload.get("command"))
    text = str(payload.get("text") if is_slash else event.get("text") or "")
    command = command_after_steel_mission(text)
    channel = str(payload.get("channel_id") if is_slash else event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or event.get("ts") or payload.get("trigger_id") or "")
    actor_id = str(payload.get("user_id") if is_slash else event.get("user") or "")
    event_type = str(event.get("type") or "")
    explicit = bool(re.search(r"(?:^|\s)/?steel-mission(?:\s|$)", text, flags=re.I))
    from_bot = bool(event.get("bot_id") or event.get("subtype") == "bot_message")
    accepted = not from_bot and (is_slash or event_type == "app_mention" or (event_type == "message" and explicit))
    return {
        "schemaVersion": 1,
        "eventId": str(payload.get("event_id") or headers.get("x-slack-request-id") or "slack-" + hashlib.sha256(raw_body).hexdigest()[:24]),
        "sourceSystem": "slack",
        "eventType": "slash-command" if is_slash else str(event.get("type") or payload.get("type") or "event"),
        "action": "request",
        "accepted": accepted and bool(command),
        "reason": "signed Slack command or app mention" if accepted else "Slack event does not request Steel Mission",
        "objective": command[:12000],
        "actor": {"id": actor_id, "name": str(payload.get("user_name") or actor_id)},
        "origin": normalize_workflow_origin({
            "sourceSystem": "slack",
            "sourceType": "thread",
            "sourceId": channel,
            "threadId": f"slack:{channel}:{thread_ts}" if channel and thread_ts else channel,
            "actorId": actor_id,
            "returnChannel": "slack-thread",
            "returnTarget": channel,
            "channelId": channel,
            "threadTs": thread_ts,
        }),
        "payloadHash": hashlib.sha256(raw_body).hexdigest(),
        "receivedAt": utc_now(),
    }


def normalize_jira_ingress(headers: dict[str, str], payload: dict[str, Any], raw_body: bytes) -> dict[str, Any]:
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
    actor = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    issue_key = str(issue.get("key") or "")
    body = str(comment.get("body") or fields.get("description") or "")
    labels = [str(item).lower() for item in fields.get("labels", [])] if isinstance(fields.get("labels"), list) else []
    explicit = bool(re.search(r"(?:^|\s)/?steel-mission(?:\s|$)", body, flags=re.I))
    accepted = explicit or "steel-mission" in labels
    command = command_after_steel_mission(body) if explicit else ""
    objective = command or "\n\n".join(item for item in [str(fields.get("summary") or "").strip(), body.strip()] if item)
    return {
        "schemaVersion": 1,
        "eventId": headers.get("x-atlassian-webhook-identifier") or "jira-" + hashlib.sha256(raw_body).hexdigest()[:24],
        "sourceSystem": "jira",
        "eventType": str(payload.get("webhookEvent") or "jira:issue_updated"),
        "action": "request",
        "accepted": accepted and bool(objective),
        "reason": "explicit command or steel-mission label" if accepted else "Jira event does not request Steel Mission",
        "objective": objective[:12000],
        "actor": {"id": str(actor.get("accountId") or actor.get("key") or ""), "name": str(actor.get("displayName") or "")},
        "origin": normalize_workflow_origin({
            "sourceSystem": "jira",
            "sourceType": "issue",
            "sourceId": issue_key,
            "threadId": f"jira:{issue_key}",
            "actorId": str(actor.get("accountId") or actor.get("key") or ""),
            "returnChannel": "jira-comment",
            "returnTarget": issue_key,
            "deepLink": str(issue.get("self") or ""),
            "issueKey": issue_key,
        }),
        "payloadHash": hashlib.sha256(raw_body).hexdigest(),
        "receivedAt": utc_now(),
    }


def workflow_ingress_receipt_path(source: str, event_id: str) -> Path:
    return MISSION_ROOT / "_workflow-ingress" / safe_path_part(source, "source") / f"{safe_path_part(event_id, 'event')}.json"


def process_workflow_ingress(source: str, headers: Any, raw_body: bytes, content_type: str = "application/json") -> tuple[int, dict[str, Any]]:
    if source not in {"github", "slack", "jira"}:
        return 404, {"ok": False, "error": "workflow ingress source is not supported"}
    connector = connector_by_id(source, "admin")
    if not connector or connector.get("enabled") is not True or connector.get("mode") != "native":
        return 404, {"ok": False, "error": f"native {source} connector is not enabled"}
    signature = verify_workflow_ingress_signature(source, connector, headers, raw_body)
    if signature.get("ok") is not True:
        return 401, {"ok": False, "error": signature.get("error") or "workflow ingress signature is invalid", "signature": signature}
    try:
        payload = parse_workflow_ingress_payload(source, raw_body, content_type)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    if source == "slack" and payload.get("type") == "url_verification":
        return 200, {"ok": True, "challenge": str(payload.get("challenge") or "")}
    normalized_headers = normalized_request_headers(headers)
    event = {
        "github": normalize_github_ingress,
        "slack": normalize_slack_ingress,
        "jira": normalize_jira_ingress,
    }[source](normalized_headers, payload, raw_body)
    receipt_path = workflow_ingress_receipt_path(source, str(event.get("eventId") or "event"))
    with WORKFLOW_INGRESS_LOCK:
        existing = read_json_file(receipt_path)
        if existing:
            return 200, {**existing, "duplicate": True}
        if event.get("accepted") is not True:
            receipt = {"schemaVersion": 1, "ok": True, "status": "ignored", "duplicate": False, "event": event}
            atomic_write_json(receipt_path, receipt)
            return 202, receipt
        atomic_write_json(receipt_path, {
            "schemaVersion": 1,
            "ok": True,
            "status": "accepting",
            "duplicate": False,
            "event": event,
        })
    actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
    mapped_actor = resolve_external_identity(source, actor, connector)
    if not mapped_actor and identity_mode() == "oidc-required":
        denied = {
            "schemaVersion": 1,
            "ok": False,
            "status": "denied",
            "duplicate": False,
            "event": event,
            "error": f"{source} actor is not mapped to an active registered user",
        }
        atomic_write_json(receipt_path, denied)
        append_auth_audit("workflow-identity-denied", ok=False, details={"source": source, "externalActor": actor.get("id") or actor.get("name")})
        return 403, denied
    if not mapped_actor:
        mapped_actor = {
            "actorId": f"{source}:{actor.get('id') or actor.get('name') or 'external-user'}",
            "role": corporate_role(str(connector.get("ingressRole") or "user")),
            "organizationId": str(organization_registry().get("activeOrganizationId") or ""),
        }
    try:
        started = start_orchestrated_mission(
            "investigate",
            str(event.get("objective") or "")[:12000],
            mock=False,
            profile=active_runtime_profile(),
            operator_role=corporate_role(str(mapped_actor.get("role") or "user")),
            actor_user_id=str(mapped_actor.get("actorId") or "external-user"),
            organization_id=str(mapped_actor.get("organizationId") or ""),
            workflow_origin=event.get("origin"),
        )
    except Exception as exc:  # noqa: BLE001
        failed = {"schemaVersion": 1, "ok": False, "status": "failed", "duplicate": False, "event": event, "error": str(exc)}
        atomic_write_json(receipt_path, failed)
        return 500, failed
    receipt = {
        "schemaVersion": 1,
        "ok": True,
        "status": "accepted",
        "duplicate": False,
        "event": event,
        "mission": started,
        "investigationPath": f"/mission/{started.get('missionId')}",
    }
    atomic_write_json(receipt_path, receipt)
    return 202, receipt


def write_connector_outbox(connector: dict[str, Any], event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = connector_outbox_path(connector, event_id)
    if path.suffix == ".jsonl":
        append_jsonl(path, payload)
    else:
        atomic_write_json(path, payload)
    return {"ok": True, "status": "queued", "path": str(path), "sha256": file_sha256(path) or ""}


def execute_connector_action(connector_id: str, event_type: str, payload: dict[str, Any], *, role: str = "admin") -> dict[str, Any]:
    connector = connector_by_id(connector_id, role)
    if not connector:
        return {"ok": False, "status": "missing", "error": "connector is not registered", "connectorId": connector_id}
    event_id = "ce-" + canonical_json_hash({"connectorId": connector_id, "eventType": event_type, "payload": payload, "at": utc_now()})[:24]
    envelope = {
        "schemaVersion": 1,
        "eventId": event_id,
        "eventType": event_type,
        "connector": {key: connector.get(key) for key in ("id", "label", "kind", "mode", "status", "enabled")},
        "payload": payload,
        "payloadHash": canonical_json_hash(payload),
        "producedAt": utc_now(),
        "producer": "steel-mission-chat connector-runtime",
    }
    preflight = connector_action_preflight(connector, event_type, payload)
    execution: dict[str, Any]
    if preflight.get("ok") is not True:
        execution = {"ok": False, "status": "blocked", "blockers": preflight.get("blockers", [])}
    elif connector.get("mode") == "command":
        execution = run_connector_command(str(connector.get("command") or ""), envelope)
    elif connector.get("mode") == "webhook":
        execution = post_connector_webhook(connector, envelope)
    elif connector.get("mode") == "native":
        execution = run_native_connector(connector, envelope)
    elif connector.get("mode") == "outbox":
        execution = write_connector_outbox(connector, event_id, {**envelope, "preflight": preflight})
    else:
        execution = {"ok": True, "status": "observed", "message": "connector is registered but has no execution mode"}
    return {
        **envelope,
        "ok": execution.get("ok") is True,
        "status": execution.get("status") or "unknown",
        "plan": preflight.get("plan"),
        "preflight": preflight,
        "execution": execution,
        "observe": {
            "status": execution.get("status") or "unknown",
            "ok": execution.get("ok") is True,
            "observedAt": utc_now(),
        },
        "evidence": {
            "payloadHash": envelope["payloadHash"],
            "executionHash": canonical_json_hash(execution),
        },
        "rollbackOrExport": {
            "rollbackConfigured": False,
            "exportPath": execution.get("path") or connector.get("exportPath") or "",
        },
    }


def execute_configured_connectors(event_type: str, payload: dict[str, Any], *, role: str = "admin") -> list[dict[str, Any]]:
    registry = integration_registry(role)
    origin = connector_payload_origin(payload)
    results: list[dict[str, Any]] = []
    for connector in registry.get("connectors", []) if isinstance(registry.get("connectors"), list) else []:
        if not isinstance(connector, dict) or not connector.get("enabled") or not connector_supports_event(connector, event_type):
            continue
        if connector.get("mode") == "native" and (
            not origin or origin.get("sourceSystem") != connector.get("adapter")
        ):
            # Native workflow adapters are return channels, not broadcast
            # sinks. Do not create empty/skipped delivery evidence for a
            # mission that did not originate in that provider.
            continue
        results.append(execute_connector_action(str(connector.get("id") or ""), event_type, payload, role=role))
    return results


def write_connector_event_evidence(
    record: dict[str, Any],
    node_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    connector_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    mission_id = str(record.get("missionId") or "")
    if not mission_id:
        return []
    task_id = str(record.get("taskId") or "") or None
    job_id = str(record.get("jobId") or "") or None
    operator = corporate_role(str(record.get("operatorRole") or "user"))
    workflow_origin = normalize_workflow_origin(record.get("workflowOrigin"))
    public_base = str(os.environ.get("STEEL_MISSION_PUBLIC_URL") or "").rstrip("/")
    investigation_path = f"/mission/{mission_id}"
    connector_payload = {
        **payload,
        "investigationPath": investigation_path,
        "investigationUrl": f"{public_base}{investigation_path}" if public_base else investigation_path,
        "returnToOrigin": bool(workflow_origin),
        **({"origin": workflow_origin} if workflow_origin else {}),
    }
    results = [
        execute_connector_action(connector_id, event_type, connector_payload, role="admin")
        for connector_id in connector_ids
    ] if connector_ids else execute_configured_connectors(event_type, connector_payload, role="admin")
    refs: list[dict[str, Any]] = []
    for result in results:
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "connector-event",
            result,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary=f"Connector {result.get('connector', {}).get('id') or result.get('connectorId') or 'event'} handled {event_type}.",
        )
        refs.append(ref)
    return refs


def mutation_ledger_path() -> Path:
    return Path(os.environ.get("PRESENT_MUTATION_LEDGER") or MUTATION_LEDGER_PATH)


def record_mutation(
    action: str,
    actor: str,
    target: Path,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    status: str = "applied",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    produced_at = utc_now()
    target_path = str(target)
    before_hash = canonical_json_hash(before) if isinstance(before, dict) else file_sha256(target)
    after_hash = canonical_json_hash(after) if isinstance(after, dict) else file_sha256(target)
    basis = {
        "action": action,
        "actor": corporate_role(actor),
        "target": target_path,
        "producedAt": produced_at,
        "beforeHash": before_hash,
        "afterHash": after_hash,
    }
    event = {
        "schemaVersion": 1,
        "mutationId": "mu-" + canonical_json_hash(basis)[:24],
        "producedAt": produced_at,
        "producer": "steel-mission-chat mutation-ledger",
        "actorRole": corporate_role(actor),
        "action": action,
        "status": status,
        "targetPath": target_path,
        "beforeHash": before_hash or "",
        "afterHash": after_hash or "",
        "changed": before_hash != after_hash,
        "details": details or {},
    }
    append_jsonl(mutation_ledger_path(), event)
    return event


def read_mutation_ledger(role: str = "user", limit: int = 80) -> dict[str, Any]:
    selected = corporate_role(role)
    if selected not in {"owner", "admin"}:
        return {"ok": False, "error": "mutation ledger is not visible from this endpoint"}
    path = mutation_ledger_path()
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "mutations": []}
    rows.sort(key=lambda item: str(item.get("producedAt") or ""), reverse=True)
    return {"ok": True, "role": selected, "ledgerPath": str(path), "mutations": rows[:max(1, min(limit, 200))]}


def snapshot_policy_summary(policy: dict[str, Any] | None) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else {}
    sources = policy.get("sources") if isinstance(policy.get("sources"), dict) else {}
    limits = policy.get("limits") if isinstance(policy.get("limits"), dict) else {}
    return {
        "sourceProfile": policy.get("sourceProfile") or "",
        "policyHash": canonical_json_hash(policy) if policy else "",
        "includeCollections": list(policy.get("includeCollections", []))
        if isinstance(policy.get("includeCollections"), list) else [],
        "limits": {key: limits.get(key) for key in sorted(limits) if limits.get(key) is not None},
        "sourceCounts": {
            key: len(value) for key, value in sorted(sources.items())
            if isinstance(value, list)
        },
    }


def read_mission_audit(mission_id: str, limit: int = 80) -> list[dict[str, Any]]:
    path = mission_audit_path(mission_id)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    except OSError:
        return []
    return rows[-limit:] if limit > 0 else rows


def read_mission_record(mission_id: str, *, include_audit: bool = False) -> dict[str, Any] | None:
    path = mission_record_path(mission_id)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if include_audit:
        payload = {**payload, "audit": read_mission_audit(mission_id)}
    return payload


def write_mission_record(record: dict[str, Any]) -> dict[str, Any]:
    mission_id = str(record.get("missionId") or "")
    if not mission_id:
        raise ValueError("missionId is required")
    updated = {**record, "updatedAt": utc_now()}
    audit = read_mission_audit(mission_id, limit=0)
    updated["auditCount"] = len(audit)
    if audit:
        updated["latestAudit"] = audit[-1]
    atomic_write_json(mission_record_path(mission_id), updated)
    return updated


def update_mission(mission_id: str | None, **fields: Any) -> dict[str, Any] | None:
    if not mission_id:
        return None
    with MISSION_LOCK:
        current = read_mission_record(mission_id) or {
            "schemaVersion": 1,
            "missionId": mission_id,
            "createdAt": utc_now(),
            "producer": "steel-mission-chat mission-control",
            "state": "unknown",
        }
        return write_mission_record({**current, **{key: value for key, value in fields.items() if value is not None}})


def mission_template(template_id: str | None) -> dict[str, Any] | None:
    for template in MISSION_TEMPLATES:
        if template.get("templateId") == template_id:
            return template
    return None


def public_mission_templates(role: str = "user") -> dict[str, Any]:
    selected = corporate_role(role)
    return {
        "ok": True,
        "role": selected,
        "templates": [
            {**template, "available": selected in set(template.get("allowedRoles", []))}
            for template in MISSION_TEMPLATES
            if selected in set(template.get("allowedRoles", [])) or selected in {"owner", "admin"}
        ],
    }


def clean_string_list(value: Any, *, limit: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(set(str(item).strip() for item in value[:limit] if str(item).strip()))


def clean_optional_string(value: Any, *, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def clean_choice(value: Any, allowed: set[str], fallback: str, *, limit: int = 80) -> str:
    selected = clean_optional_string(value, limit=limit)
    return selected if selected in allowed else fallback


def bool_from_payload(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        selected = value.strip().lower()
        if selected in {"1", "true", "yes", "on"}:
            return True
        if selected in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_delivery_context(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    repair_budget = payload.get("repairBudget")
    try:
        repair_budget_int = int(repair_budget)
    except (TypeError, ValueError):
        repair_budget_int = 1
    repair_budget_int = max(0, min(repair_budget_int, 10))
    worktree_mode = clean_optional_string(
        payload.get("worktreeMode") or payload.get("workspaceMode"), limit=40
    )
    if not worktree_mode:
        worktree_mode = "in-place"
    elif worktree_mode not in {"isolated", "in-place"}:
        raise ValueError("worktreeMode must be one of: isolated, in-place")
    return {
        "repositoryPath": clean_optional_string(payload.get("repositoryPath"), limit=1000),
        "branch": clean_optional_string(payload.get("branch"), limit=200),
        "baseBranch": clean_optional_string(payload.get("baseBranch"), limit=200),
        "deliveryBranch": clean_optional_string(payload.get("deliveryBranch"), limit=200),
        "worktreeMode": worktree_mode,
        "worktreePath": clean_optional_string(payload.get("worktreePath"), limit=1000),
        "prProvider": clean_choice(payload.get("prProvider"), {"github", "command", "manual", "none"}, "github"),
        "githubRepository": clean_optional_string(payload.get("githubRepository"), limit=300),
        "prMode": clean_choice(payload.get("prMode"), {"readiness", "draft", "create"}, "readiness"),
        "pushBeforePr": bool_from_payload(payload.get("pushBeforePr"), False),
        "ciProvider": clean_choice(payload.get("ciProvider"), {"github-actions", "command", "manual", "none"}, "github-actions"),
        "ciRequired": bool_from_payload(payload.get("ciRequired"), False),
        "ciWait": bool_from_payload(payload.get("ciWait"), False),
        "ciCommand": clean_optional_string(payload.get("ciCommand"), limit=500),
        "deployProvider": clean_choice(payload.get("deployProvider"), {"sites", "command", "manual", "none"}, "manual"),
        "deployEnvironment": clean_optional_string(payload.get("deployEnvironment"), limit=160),
        "deployUrl": clean_optional_string(payload.get("deployUrl"), limit=500),
        "rollbackCommand": clean_optional_string(payload.get("rollbackCommand"), limit=500),
        "modifyCommand": clean_optional_string(payload.get("modifyCommand"), limit=500),
        "buildCommand": clean_optional_string(payload.get("buildCommand"), limit=500),
        "testCommand": clean_optional_string(payload.get("testCommand"), limit=500),
        "inspectCommand": clean_optional_string(payload.get("inspectCommand"), limit=500),
        "repairCommand": clean_optional_string(payload.get("repairCommand"), limit=500),
        "prCommand": clean_optional_string(payload.get("prCommand"), limit=500),
        "prTarget": clean_optional_string(payload.get("prTarget"), limit=300),
        "prTitle": clean_optional_string(payload.get("prTitle"), limit=300),
        "prBody": clean_optional_string(payload.get("prBody"), limit=2000),
        "deployCommand": clean_optional_string(payload.get("deployCommand"), limit=500),
        "deployTarget": clean_optional_string(payload.get("deployTarget"), limit=300),
        "deployHealthCommand": clean_optional_string(payload.get("deployHealthCommand"), limit=500),
        "repairBudget": repair_budget_int,
    }


def mission_user_bindings(user_ids: Any) -> list[dict[str, Any]]:
    selected_ids = set(clean_string_list(user_ids, limit=50))
    users = [
        user for user in user_registry().get("users", [])
        if isinstance(user, dict) and user.get("status") == "active"
    ]
    if not selected_ids:
        return []
    return [
        {key: user[key] for key in ("id", "name", "role", "assignedCapabilities") if key in user}
        for user in users
        if str(user.get("id")) in selected_ids
    ]


def domain_capability_work_set(role_keys: Any) -> list[dict[str, Any]]:
    selected = set(clean_string_list(role_keys, limit=50))
    roles = [
        role for role in knowledge_registry().get("capabilities", [])
        if isinstance(role, dict) and (role.get("capabilityKey") or role.get("roleKey"))
    ]
    if not selected:
        return []
    return [
        {
            "roleKey": role.get("roleKey"),
            "capabilityKey": role.get("capabilityKey") or role.get("roleKey"),
            "fNumber": role.get("currentFNumber"),
            "displayName": role.get("displayName"),
            "sourceCount": role.get("sourceCount", 0),
        }
        for role in roles
        if str(role.get("capabilityKey") or role.get("roleKey")) in selected
    ]


def new_mission_nodes(template: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = template.get("nodes") if isinstance(template.get("nodes"), list) else []
    return [
        {
            **node,
            "state": "pending",
            "evidenceRefs": [],
            "attempts": 0,
        }
        for node in nodes
        if isinstance(node, dict) and node.get("nodeId")
    ]


def mission_node_index(record: dict[str, Any], node_id: str) -> int | None:
    nodes = record.get("nodes") if isinstance(record.get("nodes"), list) else []
    for index, node in enumerate(nodes):
        if isinstance(node, dict) and node.get("nodeId") == node_id:
            return index
    return None


def update_mission_node(mission_id: str, node_id: str, **fields: Any) -> dict[str, Any] | None:
    with MISSION_LOCK:
        current = read_mission_record(mission_id)
        if not current:
            return None
        nodes = [dict(node) for node in current.get("nodes", []) if isinstance(node, dict)]
        for index, node in enumerate(nodes):
            if node.get("nodeId") == node_id:
                nodes[index] = {**node, **{key: value for key, value in fields.items() if value is not None}}
                return write_mission_record({**current, "nodes": nodes})
    return None


def append_mission_evidence_ref(mission_id: str, ref: dict[str, Any], node_id: str | None = None) -> dict[str, Any] | None:
    with MISSION_LOCK:
        current = read_mission_record(mission_id)
        if not current:
            return None
        ledger = [item for item in current.get("evidenceLedger", []) if isinstance(item, dict)]
        ledger.append(ref)
        nodes = [dict(node) for node in current.get("nodes", []) if isinstance(node, dict)]
        if node_id:
            for index, node in enumerate(nodes):
                if node.get("nodeId") == node_id:
                    refs = [item for item in node.get("evidenceRefs", []) if isinstance(item, dict)]
                    nodes[index] = {**node, "evidenceRefs": [*refs, ref]}
                    break
        return write_mission_record({**current, "evidenceLedger": ledger, "nodes": nodes})


def write_mission_evidence(
    mission_id: str,
    node_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    task_id: str | None = None,
    job_id: str | None = None,
    operator_role: str = "user",
    summary: str = "",
) -> dict[str, Any]:
    produced_at = utc_now()
    basis = {
        "missionId": mission_id,
        "nodeId": node_id,
        "kind": kind,
        "producedAt": produced_at,
        "payloadHash": canonical_json_hash(payload),
    }
    evidence_id = "me-" + canonical_json_hash(basis)[:24]
    artifact = {
        "schemaVersion": 1,
        "evidenceId": evidence_id,
        "missionId": mission_id,
        **({"taskId": task_id} if task_id else {}),
        **({"jobId": job_id} if job_id else {}),
        "nodeId": node_id,
        "kind": kind,
        "producedAt": produced_at,
        "producer": "steel-mission-chat mission-orchestrator",
        "payload": payload,
    }
    integrity = sign_integrity_record(mission_id, f"evidence:{kind}", artifact)
    artifact["integrity"] = integrity
    path = mission_evidence_dir(mission_id) / f"{node_id}-{kind}-{evidence_id}.json"
    atomic_write_json(path, artifact)
    chain_entry = append_integrity_record(mission_id, integrity)
    sha = file_sha256(path) or ""
    ref = {
        "evidenceId": evidence_id,
        "nodeId": node_id,
        "kind": kind,
        "path": str(path),
        "sha256": sha,
        "integrityHash": chain_entry.get("chainHash") or "",
        "signature": integrity.get("signature") or "",
        "producedAt": produced_at,
        "summary": summary or kind.replace("-", " "),
    }
    append_mission_evidence_ref(mission_id, ref, node_id)
    append_mission_audit(
        mission_id,
        "evidence-recorded",
        task_id=task_id,
        job_id=job_id,
        actor="mission-orchestrator",
        operator_role=operator_role,
        summary=summary or f"Recorded {kind} evidence for {node_id}.",
        details={
            "evidenceId": evidence_id,
            "nodeId": node_id,
            "kind": kind,
            "sha256": sha,
            "integrityHash": chain_entry.get("chainHash") or "",
        },
        artifact_refs=[{"kind": kind, "path": str(path), "sha256": sha}],
    )
    return ref


def append_mission_audit(
    mission_id: str | None,
    action: str,
    *,
    task_id: str | None = None,
    job_id: str | None = None,
    actor: str = "system",
    operator_role: str = "user",
    summary: str = "",
    details: dict[str, Any] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not mission_id:
        return None
    produced_at = utc_now()
    refs = [
        {key: item[key] for key in ("kind", "path", "sha256") if key in item}
        for item in (artifact_refs or [])
        if isinstance(item, dict) and item.get("kind") and item.get("path")
    ]
    basis = {
        "missionId": mission_id,
        "action": action,
        "taskId": task_id,
        "jobId": job_id,
        "actor": actor,
        "producedAt": produced_at,
        "details": details or {},
    }
    event = {
        "schemaVersion": 1,
        "auditId": "ma-" + canonical_json_hash(basis)[:24],
        "missionId": mission_id,
        **({"taskId": task_id} if task_id else {}),
        **({"jobId": job_id} if job_id else {}),
        "producedAt": produced_at,
        "actor": actor,
        "operatorRole": operator_role,
        "action": action,
        "summary": summary or action.replace("-", " "),
        "details": details or {},
        "artifactRefs": refs,
    }
    integrity = sign_integrity_record(mission_id, f"audit:{action}", event)
    event["integrity"] = integrity
    with MISSION_LOCK:
        append_jsonl(mission_audit_path(mission_id), event)
        append_integrity_record(mission_id, integrity)
        current = read_mission_record(mission_id)
        if current:
            write_mission_record({
                **current,
                "state": current.get("state", "unknown"),
                "lastAuditAction": action,
                "lastAuditAt": produced_at,
            })
    return event


def mission_visible_to_actor(record: dict[str, Any], actor: dict[str, Any]) -> bool:
    role = corporate_role(str(actor.get("role") or "user"))
    actor_id = str(actor.get("actorId") or "")
    actor_orgs = set(clean_string_list(actor.get("organizationIds"), limit=50))
    if actor.get("organizationId"):
        actor_orgs.add(str(actor.get("organizationId")))
    mission_org = str(record.get("organizationId") or "")
    if mission_org and actor_orgs and mission_org not in actor_orgs:
        return False
    mission_users = record.get("missionUsers") if isinstance(record.get("missionUsers"), list) else []
    assigned_ids = {str(user.get("id")) for user in mission_users if isinstance(user, dict) and user.get("id")}
    return role in {"owner", "admin"} or actor_id == str(record.get("actorUserId") or "") or actor_id in assigned_ids


def mission_list(role: str = "user", limit: int = 25, actor: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = corporate_role(role)
    selected_actor = actor or {"actorId": selected, "role": selected, "organizationIds": []}
    records: list[dict[str, Any]] = []
    for path in sorted(MISSION_ROOT.glob("ms-*/mission.json")) if MISSION_ROOT.exists() else []:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        legacy_visible = selected in {"owner", "admin"} or payload.get("operatorRole") in {selected, "user"}
        if mission_visible_to_actor(payload, selected_actor) if actor is not None else legacy_visible:
            records.append(payload)
    records.sort(key=lambda item: str(item.get("updatedAt") or item.get("createdAt") or ""), reverse=True)
    return {
        "ok": True,
        "role": selected,
        "missionRoot": str(MISSION_ROOT),
        "missions": records[:max(1, min(limit, 100))],
    }


def mission_detail(mission_id: str, role: str = "user", actor: dict[str, Any] | None = None) -> dict[str, Any]:
    record = read_mission_record(mission_id, include_audit=True)
    if record is None:
        return {"ok": False, "error": "mission not found"}
    selected = corporate_role(role)
    selected_actor = actor or {"actorId": selected, "role": selected, "organizationIds": []}
    mission_users = record.get("missionUsers") if isinstance(record.get("missionUsers"), list) else []
    mission_roles = {corporate_role(str(user.get("role"))) for user in mission_users if isinstance(user, dict)}
    legacy_visible = selected in {"owner", "admin"} or record.get("operatorRole") in {selected, "user"} or selected in mission_roles
    if not (mission_visible_to_actor(record, selected_actor) if actor is not None else legacy_visible):
        return {"ok": False, "error": "mission is not visible from this endpoint"}
    return {"ok": True, "role": selected, "mission": record}


def snapshot_scope(profile: str | None = None) -> list[dict[str, Any]]:
    """What the pending run will reconcile, for the waiting page.

    Read from the worker's own snapshot builder rather than recounted here, so
    the figure shown can never drift from the figure used. The scan is a
    filesystem walk costing tens of milliseconds. It is a preview and nothing
    depends on it, so any failure returns an empty scope and the page simply
    omits the line -- a cosmetic detail must never break asking a question.
    """
    try:
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("present_worker_cli", str(WORKER_BIN))
        spec = importlib.util.spec_from_loader("present_worker_cli", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        runtime = resolve_runtime_profile(profile)
        snapshot_policy = runtime.get("snapshotPolicy")
        if not isinstance(snapshot_policy, dict):
            snapshot_policy = module.DEFAULT_COORDINATOR_SNAPSHOT_POLICY
        return module._coordinator_state_snapshot(snapshot_policy).get("snapshotCollections", [])
    except Exception:  # noqa: BLE001
        return []


def new_task_id() -> str:
    return f"DEV-{secrets.randbelow(100000) + 900000:06d}"


def steering_events_path(task_id: str | None) -> Path | None:
    if not task_id:
        return None
    return TASKS_DIR / task_id / "steel-mission-steering-events.json"


def persist_steering_events(task_id: str | None, events: list[dict[str, Any]]) -> None:
    path = steering_events_path(task_id)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schemaVersion": 1, "taskId": task_id, "events": events}, indent=2, sort_keys=True))
    tmp.replace(path)


def steering_events_text(events: list[dict[str, Any]], max_items: int = 6) -> str:
    lines: list[str] = []
    for event in events[-max_items:]:
        if not isinstance(event, dict):
            continue
        summary = steering_event_summary(event)
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines)


def steering_event_summary(event: dict[str, Any]) -> str:
    content = str(event.get("content") or "").strip()
    effect = str(event.get("effect") or "")
    intent = str(event.get("intent") or "")
    if effect == "report-progress":
        return "You asked for progress. DC13 kept the current run alive and refreshed this status view."
    if effect == "cancel-active-job":
        return "You cancelled the job."
    if effect == "pause-active-job":
        return "You paused the job."
    if effect == "resume-active-job":
        return "You resumed the job."
    if effect == "answer-decision":
        selected = str(event.get("selectedOptionLabel") or "").strip()
        if selected:
            return f"You chose: {selected}."
        return "You answered the decision request."
    if intent == "scope-change":
        return f"You changed the focus: {content}" if content else "You changed the focus for this run."
    if intent == "decision-request-demo":
        return "You asked DC13 to show a decision request."
    return f"You added context: {content}" if content else "You added follow-up context."


def decision_request_text(request: dict[str, Any]) -> str:
    if not request:
        return ""
    lines = [str(request.get("question") or "Decision needed").strip()]
    context = str(request.get("context") or "").strip()
    if context:
        lines += ["", context]
    options = request.get("options") if isinstance(request.get("options"), list) else []
    if options:
        lines += ["", "Options:"]
        for option in options:
            if not isinstance(option, dict):
                continue
            default = " (default)" if option.get("default") else ""
            lines.append(f"- {option.get('label', option.get('id', 'option'))}{default}: {option.get('description', '')}")
    lines.append("You can choose one option and add free text.")
    return "\n".join(line for line in lines if line is not None)


def job_steering_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("followUps")
    return events if isinstance(events, list) else []


def job_decision_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("decisionRequest")
    return request if isinstance(request, dict) else {}


def normalize_decision_options(options: Any, default_option_id: str | None = None) -> list[dict[str, str]]:
    if not isinstance(options, list) or not 2 <= len(options) <= 3:
        raise ValueError("decision request requires 2-3 options")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, option in enumerate(options, start=1):
        if not isinstance(option, dict):
            raise ValueError("decision option must be an object")
        option_id = str(option.get("id") or f"option-{index}").strip()
        label = str(option.get("label") or "").strip()
        description = str(option.get("description") or "").strip()
        if not option_id or option_id in seen:
            raise ValueError("decision option ids must be unique")
        if not label or not description:
            raise ValueError("decision options require label and description")
        seen.add(option_id)
        normalized.append({
            "id": option_id[:80],
            "label": label[:120],
            "description": description[:500],
            "effect": str(option.get("effect") or "restart-active-run")[:80],
        })
    default_id = default_option_id or normalized[0]["id"]
    if default_id not in {option["id"] for option in normalized}:
        raise ValueError("default decision option must match one option id")
    for option in normalized:
        option["default"] = option["id"] == default_id
    return normalized


def build_decision_request(question: str, context: str, options: list[dict[str, str]],
                           default_option_id: str | None = None) -> dict[str, Any]:
    normalized = normalize_decision_options(options, default_option_id)
    return {
        "id": f"decision-{secrets.token_hex(8)}",
        "createdAt": utc_now(),
        "question": question.strip()[:500],
        "context": context.strip()[:2000],
        "options": normalized,
        "defaultOptionId": next(option["id"] for option in normalized if option.get("default")),
        "freeText": {
            "enabled": True,
            "label": "Add context",
            "placeholder": "Add details, constraints, or a different preference...",
        },
    }


def demo_decision_request() -> dict[str, Any]:
    return build_decision_request(
        "How should DC13 continue this running job?",
        "This is a local demo decision so you can inspect the exact choice UI. "
        "The default keeps the current job narrow and resumes with the least extra work.",
        [
            {
                "id": "continue-narrow",
                "label": "Continue narrow",
                "description": "Default: keep the current scope and continue with the smallest useful answer.",
            },
            {
                "id": "broaden-check",
                "label": "Broaden check",
                "description": "Include adjacent broker, worker, and acceptance context before answering.",
            },
            {
                "id": "pause",
                "label": "Pause",
                "description": "Hold the job until I add more context in the free-text field.",
            },
        ],
        "continue-narrow",
    )


def steering_event_label(event: dict[str, Any]) -> str:
    effect = event.get("effect")
    if effect == "cancel-active-job":
        return "You cancelled the job"
    if effect == "pause-active-job":
        return "You paused the job"
    if effect == "resume-active-job":
        return "You resumed the job"
    if effect == "answer-decision":
        return "You answered a decision request"
    if effect == "report-progress":
        return "You asked for progress"
    if event.get("intent") == "scope-change":
        return "You changed the focus"
    return "You added follow-up context"


def steering_event_timeline_detail(event: dict[str, Any]) -> str:
    effect = str(event.get("effect") or "")
    intent = str(event.get("intent") or "")
    content = str(event.get("content") or "").strip()
    if effect == "report-progress":
        return "DC13 kept the current run alive and refreshed this status view."
    if effect == "cancel-active-job":
        return "Delivery Coordinator stopped the active run."
    if effect == "pause-active-job":
        return "Delivery Coordinator paused the run. Press play when you want it to continue."
    if effect == "resume-active-job":
        return "Delivery Coordinator is continuing from the paused job."
    if effect == "answer-decision":
        selected = str(event.get("selectedOptionLabel") or "").strip()
        return f"Continuing with: {selected}." if selected else "Continuing with the selected option."
    if intent == "scope-change":
        return f"Updated focus: {content[:500]}" if content else "Delivery Coordinator restarted with the updated focus."
    if intent == "decision-request-demo":
        return "Delivery Coordinator is opening a decision request with options and free text."
    return f"Added context: {content[:500]}" if content else "Delivery Coordinator received additional context."


def request_user_decision(job_id: str, question: str, context: str, options: list[dict[str, str]],
                          default_option_id: str | None = None) -> dict[str, Any]:
    request = build_decision_request(question, context, options, default_option_id)
    pid: int | None = None
    mission_id: str | None = None
    task_id: str | None = None
    operator_role = "user"
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("chat job not found")
        if job.get("state") not in {"running", "waiting_for_decision"}:
            raise RuntimeError("chat job cannot request a decision in its current state")
        job["state"] = "waiting_for_decision"
        job["decisionRequest"] = request
        job["phase"] = "Delivery Coordinator needs your decision before continuing."
        job["steeringRevision"] = int(job.get("steeringRevision") or 0) + 1
        pid = job.get("activePid") if isinstance(job.get("activePid"), int) else None
        mission_id = job.get("missionId") if isinstance(job.get("missionId"), str) else None
        task_id = job.get("taskId") if isinstance(job.get("taskId"), str) else None
        operator_role = corporate_role(str(job.get("operatorRole") or "user"))
    update_mission(mission_id, state="waiting_for_decision", decisionRequest=request)
    append_mission_audit(
        mission_id,
        "decision-requested",
        task_id=task_id,
        job_id=job_id,
        actor="system",
        operator_role=operator_role,
        summary=str(request.get("question") or "Decision requested"),
        details={"decisionRequestId": request.get("id"), "options": request.get("options", [])},
    )
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
    return request


def append_decision_response(job_id: str, option_id: str, free_text: str = "") -> dict[str, Any]:
    selected = option_id.strip()
    note = free_text.strip()
    if len(note.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("decision free text is too large")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("chat job not found")
        request = job_decision_request(job)
        if not request:
            raise RuntimeError("chat job is not waiting for a decision")
        options = request.get("options") if isinstance(request.get("options"), list) else []
        by_id = {str(option.get("id")): option for option in options if isinstance(option, dict)}
        if not selected:
            selected = str(request.get("defaultOptionId") or "")
        if selected not in by_id:
            raise ValueError("selected option is not available")
        option = by_id[selected]
        revision = len(job_steering_events(job)) + 1
        content = f"Decision: {option.get('label')}"
        if note:
            content += f"\nContext: {note[:12000]}"
        event = {
            "id": f"steer-{secrets.token_hex(8)}",
            "revision": revision,
            "createdAt": utc_now(),
            "role": "user",
            "content": content,
            "intent": "user-decision",
            "effect": "answer-decision",
            "reason": "user selected an option for a pending decision request",
            "decisionRequestId": request.get("id"),
            "selectedOptionId": selected,
            "selectedOptionLabel": option.get("label"),
            "freeText": note[:12000],
        }
        events = [*job_steering_events(job), event]
        job["followUps"] = events
        job.pop("decisionRequest", None)
        job["state"] = "running"
        job["phase"] = "You answered the decision request; Delivery Coordinator is continuing with that choice."
        job["steeringRevision"] = int(job.get("steeringRevision") or 0) + 1
        job["restartRequested"] = True
        task_id = job.get("taskId")
        mission_id = job.get("missionId") if isinstance(job.get("missionId"), str) else None
        operator_role = corporate_role(str(job.get("operatorRole") or "user"))
    persist_steering_events(str(task_id) if task_id else None, events)
    update_mission(
        mission_id,
        state="running",
        steeringRevision=job.get("steeringRevision"),
        followUpCount=len(events),
        decisionRequest=None,
    )
    append_mission_audit(
        mission_id,
        "decision-answered",
        task_id=str(task_id) if task_id else None,
        job_id=job_id,
        actor="user",
        operator_role=operator_role,
        summary=steering_event_summary(event),
        details={key: event[key] for key in (
            "revision", "decisionRequestId", "selectedOptionId", "selectedOptionLabel", "freeText"
        ) if key in event},
    )
    return event


def classify_follow_up(content: str) -> dict[str, str]:
    text = " ".join(content.lower().strip().split())
    cancel_phrases = [
        "stop", "stop it", "stop the job", "cancel", "cancel it", "cancel the job",
        "abort", "abort it", "abort the job", "halt", "kill it", "kill the job",
        "nevermind", "never mind", "do not continue", "don't continue",
    ]
    if text in cancel_phrases or any(text.startswith(f"{phrase} ") for phrase in cancel_phrases):
        return {
            "intent": "cancel",
            "effect": "cancel-active-job",
            "reason": "user asked to stop the running job",
            "phase": "You cancelled this Delivery Coordinator job.",
        }
    pause_phrases = [
        "pause", "pause it", "pause the job", "pause this job", "hold", "hold it",
    ]
    if text in pause_phrases or any(text.startswith(f"{phrase} ") for phrase in pause_phrases):
        return {
            "intent": "pause",
            "effect": "pause-active-job",
            "reason": "user asked to pause the running job",
            "phase": "Paused. Press play when you want DC13 to continue.",
        }
    resume_phrases = [
        "resume", "resume it", "resume the job", "resume this job", "play", "play the job",
    ]
    if text in resume_phrases or any(text.startswith(f"{phrase} ") for phrase in resume_phrases):
        return {
            "intent": "resume",
            "effect": "resume-active-job",
            "reason": "user asked to resume the paused job",
            "phase": "Resuming. Delivery Coordinator is continuing this job.",
        }
    status_terms = [
        "status", "progress", "what is happening", "what are you doing",
        "where are we", "how far", "are you stuck", "is it stuck", "still running",
    ]
    decision_demo_terms = [
        "ask me to decide", "show decision", "show decision ui", "decision demo",
        "need my decision", "user decision needed", "founder decision needed",
    ]
    if text in decision_demo_terms or any(term in text for term in decision_demo_terms):
        return {
            "intent": "decision-request-demo",
            "effect": "request-user-decision",
            "reason": "user asked to see the decision request flow",
            "phase": "Delivery Coordinator needs your decision before continuing.",
        }
    if any(term in text for term in status_terms) and not any(
        term in text for term in ["instead", "only", "focus", "narrow", "ignore", "use this", "change"]
    ):
        return {
            "intent": "progress-check",
            "effect": "report-progress",
            "reason": "user asked for status without changing the answer scope",
            "phase": "You asked for live progress; Delivery Coordinator is continuing the current run.",
        }
    if any(term in text for term in ["instead", "only", "focus", "narrow", "ignore", "replace", "change"]):
        return {
            "intent": "scope-change",
            "effect": "restart-active-run",
            "reason": "user changed the requested scope while the job was running",
            "phase": "You changed the focus; Delivery Coordinator is restarting with that update.",
        }
    return {
        "intent": "additional-context",
        "effect": "restart-active-run",
        "reason": "user added context while the job was running",
        "phase": "You added context; Delivery Coordinator is restarting with that update.",
    }


def interrupt_process_groups(*pgids: int | None) -> None:
    seen: set[int] = set()
    for pgid in pgids:
        if not isinstance(pgid, int) or pgid <= 1 or pgid in seen:
            continue
        seen.add(pgid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass


def append_follow_up(job_id: str, content: str) -> dict[str, Any]:
    text = content.strip()
    if not text:
        raise ValueError("follow-up is required")
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("follow-up is too large")
    pid: int | None = None
    should_interrupt = False
    decision = classify_follow_up(text)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise KeyError("chat job not found")
        state = job.get("state")
        if state not in {"running", "paused"}:
            raise RuntimeError("chat job is not running")
        if state == "paused" and decision["effect"] == "pause-active-job":
            raise RuntimeError("chat job is already paused")
        if state == "running" and decision["effect"] == "resume-active-job":
            raise RuntimeError("chat job is already running")
        revision = len(job_steering_events(job)) + 1
        event = {
            "id": f"steer-{secrets.token_hex(8)}",
            "revision": revision,
            "createdAt": utc_now(),
            "role": "user",
            "content": text[:12000],
            "intent": decision["intent"],
            "effect": decision["effect"],
            "reason": decision["reason"],
        }
        events = [*job_steering_events(job), event]
        job["followUps"] = events
        job["phase"] = decision["phase"]
        pid = job.get("activePid") if isinstance(job.get("activePid"), int) else None
        task_id = job.get("taskId")
        mission_id = job.get("missionId") if isinstance(job.get("missionId"), str) else None
        operator_role = corporate_role(str(job.get("operatorRole") or "user"))
        if decision["effect"] == "cancel-active-job":
            job["state"] = "cancelled"
            job["ok"] = False
            job["cancelledAt"] = event["createdAt"]
            job["durationSeconds"] = round(time.time() - job.get("startedEpoch", time.time()), 1)
            job["error"] = "Delivery Coordinator job cancelled by your follow-up."
            should_interrupt = True
        elif decision["effect"] == "pause-active-job":
            job["state"] = "paused"
            job["steeringRevision"] = int(job.get("steeringRevision") or 0) + 1
            job["restartRequested"] = False
            should_interrupt = pid is not None
        elif decision["effect"] == "resume-active-job":
            job["state"] = "running"
            job["steeringRevision"] = int(job.get("steeringRevision") or 0) + 1
            job["restartRequested"] = True
            should_interrupt = False
        elif decision["effect"] == "request-user-decision":
            job["state"] = "waiting_for_decision"
            job["decisionRequest"] = demo_decision_request()
            job["steeringRevision"] = int(job.get("steeringRevision") or 0) + 1
            job["restartRequested"] = False
            should_interrupt = pid is not None
        elif decision["effect"] == "restart-active-run":
            job["steeringRevision"] = int(job.get("steeringRevision") or 0) + 1
            job["restartRequested"] = True
            should_interrupt = pid is not None
        else:
            job["restartRequested"] = False
    with JOBS_LOCK:
        latest = JOBS.get(job_id, {})
        events_to_persist = list(job_steering_events(latest))
        task_id = latest.get("taskId") or task_id
    persist_steering_events(str(task_id) if task_id else None, events_to_persist)
    with JOBS_LOCK:
        latest_job = dict(JOBS.get(job_id, {}))
    update_mission(
        mission_id,
        state=str(latest_job.get("state") or "running"),
        steeringRevision=latest_job.get("steeringRevision"),
        followUpCount=len(events_to_persist),
        restartCount=latest_job.get("restartCount"),
        completedAt=latest_job.get("cancelledAt") if latest_job.get("state") == "cancelled" else None,
        durationSeconds=latest_job.get("durationSeconds"),
        error=latest_job.get("error"),
    )
    append_mission_audit(
        mission_id,
        str(decision["effect"]),
        task_id=str(task_id) if task_id else None,
        job_id=job_id,
        actor="user",
        operator_role=operator_role,
        summary=steering_event_summary(event),
        details={key: event[key] for key in ("revision", "intent", "effect", "reason", "content") if key in event},
    )
    if pid and should_interrupt:
        progress = read_progress(str(task_id) if task_id else None)
        model_pgid = progress.get("modelPgid")
        interrupt_process_groups(model_pgid if isinstance(model_pgid, int) else None, pid)
    return event


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    for name, value in getattr(handler, "response_headers", []):
        handler.send_header(str(name), str(value))
    handler.response_headers = []
    handler.end_headers()
    handler.wfile.write(data)


def html_response(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    data = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    for name, value in getattr(handler, "response_headers", []):
        handler.send_header(str(name), str(value))
    handler.response_headers = []
    handler.end_headers()
    handler.wfile.write(data)


def binary_response(handler: BaseHTTPRequestHandler, status: int, data: bytes, content_type: str, filename: str | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    data = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def parse_json(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def broker_command(*args: str, timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, str(BROKER_BIN), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKER_DIR),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "BROKER_UNAVAILABLE", "reason": str(exc)}
    return parse_json(result.stdout) or parse_json(result.stderr)


def broker_overview() -> dict[str, Any]:
    state = configured_path("PRESENT_BROKER_STATE")
    pool = configured_path("PRESENT_WORKER_POOL")
    registry = configured_path("PRESENT_PROVISIONER_REGISTRY") or (WORKER_DIR / "schemas" / "provisioners.json")
    state_args = [] if state is None else ["--state", str(state)]
    workflows = broker_command("workflow-status", *state_args)
    workers = {"workers": []}
    if pool is not None:
        workers = broker_command("worker-status", "--pool", str(pool), *state_args)
    provisioners = broker_command(
        "provisioner-status",
        *(["--pool", str(pool)] if pool is not None else []),
        "--provisioner-registry", str(registry),
    )
    return {
        "ok": True,
        "statePath": workflows.get("statePath") or str(state or ""),
        "workflows": workflows.get("workflows", []),
        "workers": workers.get("workers", []),
        "provisioners": provisioners.get("provisioners", []),
    }


def broker_narratives(limit: int = 3) -> list[dict[str, Any]]:
    overview = broker_overview()
    state = configured_path("PRESENT_BROKER_STATE")
    state_args = [] if state is None else ["--state", str(state)]
    workflows = overview.get("workflows") if isinstance(overview.get("workflows"), list) else []
    narratives: list[dict[str, Any]] = []
    for item in list(reversed(workflows))[:limit]:
        if not isinstance(item, dict) or not item.get("taskId"):
            continue
        response = broker_command("job-narrative", str(item["taskId"]), *state_args)
        narrative = response.get("narrative") if isinstance(response, dict) else None
        if isinstance(narrative, dict):
            narratives.append(narrative)
    return narratives


def broker_acceptance_diagnostics(limit: int = 3) -> list[dict[str, Any]]:
    overview = broker_overview()
    state = configured_path("PRESENT_BROKER_STATE")
    state_args = [] if state is None else ["--state", str(state)]
    workflows = overview.get("workflows") if isinstance(overview.get("workflows"), list) else []
    diagnostics: list[dict[str, Any]] = []
    for item in list(reversed(workflows))[:limit]:
        if not isinstance(item, dict) or not item.get("taskId"):
            continue
        response = broker_command("acceptance-gate", str(item["taskId"]), *state_args)
        if isinstance(response, dict) and response.get("status") == "ACCEPTANCE_GATE":
            diagnostics.append({
                "taskId": item["taskId"],
                "decision": response.get("decision"),
                "checks": response.get("checks", []),
                "remediation": response.get("remediation", []),
                "reason": response.get("acceptanceManifest", {}).get("reason") if isinstance(
                    response.get("acceptanceManifest"), dict) else "",
            })
    return diagnostics


def narrative_progress_text(narratives: list[dict[str, Any]], max_lines: int = 10) -> str:
    lines: list[str] = []
    for narrative in narratives[:3]:
        if not isinstance(narrative, dict):
            continue
        if narrative.get("summary"):
            lines.append(str(narrative["summary"]))
        plain_text = str(narrative.get("plainText") or "").strip()
        if plain_text:
            lines.extend(plain_text.splitlines()[:max_lines])
    return "\n".join(lines)


def acceptance_diagnostics_text(diagnostics: list[dict[str, Any]], max_items: int = 8) -> str:
    lines: list[str] = []
    for item in diagnostics[:3]:
        if not isinstance(item, dict):
            continue
        lines.append(f"{item.get('taskId', 'task')}: acceptance {item.get('decision', 'UNKNOWN')}.")
        for check in item.get("checks", [])[:max_items]:
            if isinstance(check, dict) and check.get("status") != "PASS":
                lines.append(f"- {check.get('id')}: {check.get('status')} - {check.get('detail')}")
        for remediation in item.get("remediation", [])[:max_items]:
            if isinstance(remediation, dict):
                lines.append(f"- Next: {remediation.get('action')}")
    return "\n".join(lines)


def chat_index() -> str:
    return INDEX.read_text()


def escape_html(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_shell(content: str, *, refresh: bool = False) -> str:
    refresh_tag = '<meta http-equiv="refresh" content="2">' if refresh else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_tag}
  <title>Delivery Coordinator</title>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f4ee; color: #20242a; }}
    .shell {{ max-width: 980px; margin: 0 auto; padding: 26px 18px 40px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: baseline; border-bottom: 1px solid #d9d4c9; padding-bottom: 16px; margin-bottom: 20px; }}
    h1 {{ margin: 0; font-size: 24px; letter-spacing: 0; }}
    a {{ color: #245f73; font-weight: 650; }}
    .panel {{ background: white; border: 1px solid #d9d4c9; border-radius: 8px; padding: 18px; box-shadow: 0 12px 34px rgba(35,31,24,.12); }}
    .answer {{ user-select: text; }}
    .answer-text {{ white-space: pre-wrap; font-size: 15px; line-height: 1.55; }}
    textarea {{ width: 100%; min-height: 120px; resize: vertical; border: 1px solid #d9d4c9; border-radius: 8px; padding: 12px; font: inherit; font-size: 15px; box-sizing: border-box; }}
    button {{ border: 0; border-radius: 7px; background: #245f73; color: white; padding: 10px 14px; font: inherit; font-weight: 650; cursor: pointer; }}
    label {{ display: inline-flex; align-items: center; gap: 8px; color: #69717d; font-size: 14px; }}
    .row {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 12px; }}
    .meta {{ color: #69717d; font-size: 13px; }}
    .item {{ border-bottom: 1px solid rgba(0,0,0,.08); padding: 0 0 10px; margin: 10px 0; }}
    .badge {{ display: inline-block; border-radius: 999px; padding: 3px 8px; background: #e7eef0; color: #245f73; font-size: 12px; font-weight: 700; }}
    pre {{ overflow: auto; max-height: 420px; padding: 12px; background: #1f2429; color: #f4f7f8; border-radius: 8px; font-size: 12px; }}
  </style>
</head>
<body><main class="shell">
  <header><h1>Delivery Coordinator</h1><div class="meta">coordination-report · protocol 2.1 · advisory</div></header>
  {content}
</main></body></html>"""


def render_home() -> str:
    return render_shell("""
<section class="panel">
  <form action="/ask" method="post">
    <textarea name="question" placeholder="Ask Delivery Coordinator: Where are we, what is unverified, and what needs your attention?" required></textarea>
    <div class="row">
      <label><input name="mock" type="checkbox"> mock run</label>
      <button type="submit">Ask Delivery Coordinator</button>
    </div>
  </form>
  <p class="meta">This version uses plain page loads instead of browser fetch, so it works even when local JavaScript requests are blocked.</p>
</section>""")


def plain_answer_text(report: dict[str, Any]) -> str:
    lines = [str(report.get("summary") or "I checked the worker-visible state.")]
    items = report.get("items") if isinstance(report.get("items"), list) else []
    if items:
        lines += ["", "What I found:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            status = f" [{item['status']}]" if item.get("status") else ""
            note = f" {item['note']}" if item.get("note") else ""
            source = f" Source: {item['source']}." if item.get("source") else ""
            lines.append(f"- {item.get('subject', 'Untitled')}{status}.{note}{source}")
    not_checked = report.get("notChecked") if isinstance(report.get("notChecked"), list) else []
    if not_checked:
        lines += ["", "What I could not check:"]
        for item in not_checked:
            if isinstance(item, dict):
                lines.append(f"- {item.get('subject', 'Unknown')}: {item.get('reason', 'not available')}")
    contradictions = report.get("contradictions") if isinstance(report.get("contradictions"), list) else []
    if contradictions:
        lines += ["", "Contradictions:"]
        for item in contradictions:
            if not isinstance(item, dict):
                continue
            sources = item.get("sources") if isinstance(item.get("sources"), list) else []
            suffix = f" Sources: {', '.join(str(source) for source in sources)}." if sources else ""
            lines.append(f"- {item.get('subject', 'Unknown')}: {item.get('detail', 'conflicting evidence')}{suffix}")
    if report.get("advisoryNote"):
        lines += ["", str(report["advisoryNote"])]
    follow_ups = report.get("followUps") if isinstance(report.get("followUps"), list) else []
    follow_up_text = steering_events_text(follow_ups)
    if follow_up_text:
        lines += ["", "Follow-up updates:", follow_up_text]
    narratives = report.get("jobNarratives") if isinstance(report.get("jobNarratives"), list) else []
    if narratives:
        lines += ["", "What happened in the background:"]
        for narrative in narratives[:3]:
            if not isinstance(narrative, dict):
                continue
            summary = narrative.get("summary")
            if summary:
                lines.append(str(summary))
            plain_text = str(narrative.get("plainText") or "").strip()
            if plain_text:
                lines.extend(plain_text.splitlines()[:12])
    diagnostics = report.get("acceptanceDiagnostics") if isinstance(report.get("acceptanceDiagnostics"), list) else []
    diagnostic_text = acceptance_diagnostics_text(diagnostics)
    if diagnostic_text:
        lines += ["", "Acceptance status:", diagnostic_text]
    return "\n".join(lines)


def format_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def read_progress(task_id: str | None) -> dict[str, Any]:
    """Live progress the worker publishes while the model is still running."""
    if not task_id:
        return {}
    try:
        data = json.loads((TASKS_DIR / task_id / "progress.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def job_api_payload(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = {"ok": payload.get("state") == "done", "jobId": job_id, **payload}
    mission_id = payload.get("missionId") if isinstance(payload.get("missionId"), str) else None
    progress = read_progress(payload.get("taskId"))
    if payload.get("workMode"):
        progress = {**progress, "workMode": normalize_work_mode(str(payload.get("workMode")))}
    steering_events = job_steering_events(payload)
    if steering_events:
        progress = {**progress, "steeringEvents": steering_events}
        if payload.get("phase"):
            progress["phase"] = str(payload.get("phase") or "Your follow-up was received; Delivery Coordinator is restarting.")
        timeline = progress.get("timeline") if isinstance(progress.get("timeline"), list) else []
        latest = steering_events[-1]
        progress["timeline"] = [*timeline[-11:], {
            "elapsedSeconds": round(time.time() - payload.get("startedEpoch", time.time()), 3),
            "label": steering_event_label(latest),
            "detail": steering_event_timeline_detail(latest),
        }]
    decision_request = job_decision_request(payload)
    if decision_request:
        progress = {**progress, "decisionRequest": decision_request}
        progress["phase"] = str(payload.get("phase") or "Delivery Coordinator needs your decision before continuing.")
        timeline = progress.get("timeline") if isinstance(progress.get("timeline"), list) else []
        progress["timeline"] = [*timeline[-11:], {
            "elapsedSeconds": round(time.time() - payload.get("startedEpoch", time.time()), 3),
            "label": "Your decision is needed",
            "detail": str(decision_request.get("question") or "Decision needed")[:500],
        }]
    try:
        narratives = broker_narratives()
    except Exception:  # noqa: BLE001
        narratives = []
    if narratives:
        progress = {**progress, "jobNarratives": narratives}
    try:
        diagnostics = broker_acceptance_diagnostics()
    except Exception:  # noqa: BLE001
        diagnostics = []
    if diagnostics:
        progress = {**progress, "acceptanceDiagnostics": diagnostics}
    if progress:
        started = payload.get("startedEpoch")
        if isinstance(started, (int, float)):
            job_elapsed = round(time.time() - started, 3)
            progress = {**progress, "jobElapsedSeconds": job_elapsed}
            progress_elapsed = progress.get("elapsedSeconds")
            if isinstance(progress_elapsed, (int, float)):
                progress["silentSeconds"] = round(max(0, job_elapsed - progress_elapsed), 3)
                if progress["silentSeconds"] >= 8:
                    timeline = progress.get("timeline") if isinstance(progress.get("timeline"), list) else []
                    quiet = {
                        "elapsedSeconds": round(job_elapsed, 3),
                        "label": "Waiting for stream update",
                        "detail": f"No model event for {format_elapsed(progress['silentSeconds'])}; process is still running.",
                    }
                    progress["timeline"] = [*timeline[-11:], quiet]
        progress_action: str | None = None
        progress_details: dict[str, Any] = {}
        events_seen = int(progress.get("events") or 0)
        progress_key = {
            "events": events_seen,
            "latestEventType": progress.get("latestEventType"),
            "latestEventSubtype": progress.get("latestEventSubtype"),
            "thinkingTokens": progress.get("thinkingTokens"),
        }
        silent_bucket = int(float(progress.get("silentSeconds") or 0) // 30)
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                if events_seen and job.get("lastAuditedProgressKey") != progress_key:
                    job["lastAuditedProgressKey"] = progress_key
                    progress_action = "model-event-observed"
                    progress_details = progress_key
                if silent_bucket > 0 and job.get("lastAuditedSilentBucket") != silent_bucket:
                    job["lastAuditedSilentBucket"] = silent_bucket
                    progress_action = "model-stream-quiet"
                    progress_details = {
                        "silentSeconds": progress.get("silentSeconds"),
                        "latestEventType": progress.get("latestEventType"),
                        "latestEventSubtype": progress.get("latestEventSubtype"),
                    }
        if progress_action:
            append_mission_audit(
                mission_id,
                progress_action,
                task_id=payload.get("taskId") if isinstance(payload.get("taskId"), str) else None,
                job_id=job_id,
                actor="worker",
                operator_role=corporate_role(str(payload.get("operatorRole") or "user")),
                summary=str(progress.get("phase") or progress_action.replace("-", " ")),
                details=progress_details,
            )
        update_mission(
            mission_id,
            state=str(payload.get("state") or "running"),
            lastPhase=str(progress.get("phase") or ""),
            progress={
                key: progress[key] for key in (
                    "phase", "provider", "providerLabel", "model", "thinkingTokens",
                    "events", "elapsedSeconds", "silentSeconds", "latestEventType", "latestEventSubtype",
                ) if key in progress
            },
        )
        data["progress"] = progress
    if mission_id:
        mission = read_mission_record(mission_id, include_audit=True)
        if mission:
            data["mission"] = mission
    return data


def render_running(job_id: str, payload: dict[str, Any]) -> str:
    """The waiting page, which is most of what asking DC13 actually looks like.

    It previously showed a fixed sentence, so second 3 and second 150 looked
    identical: a caller could not tell a fast run from a hung one, nor that a
    narrow question was already being answered quickly. Nothing here fakes
    progress -- the model call is opaque and reports nothing until it
    finishes. What is shown is only what is genuinely known: how long this run
    has actually taken, what was asked, the state being reconciled, and
    measured durations to compare against.
    """
    elapsed = time.time() - payload.get("startedEpoch", time.time())
    question = payload.get("question") or ""
    asked = (f'<p>Asked: &ldquo;{escape_html(question[:300])}&rdquo;</p>' if question else "")
    progress = read_progress(payload.get("taskId"))
    steering_events = job_steering_events(payload)
    if steering_events:
        progress = {**progress, "steeringEvents": steering_events}
        if payload.get("phase"):
            progress["phase"] = str(payload.get("phase") or "Your follow-up was received; Delivery Coordinator is restarting.")
    decision_request = job_decision_request(payload)
    if decision_request:
        progress = {**progress, "decisionRequest": decision_request}
        progress["phase"] = str(payload.get("phase") or "Delivery Coordinator needs your decision before continuing.")
    try:
        narratives = broker_narratives()
    except Exception:  # noqa: BLE001
        narratives = []
    if narratives:
        progress = {**progress, "jobNarratives": narratives}
    try:
        diagnostics = broker_acceptance_diagnostics()
    except Exception:  # noqa: BLE001
        diagnostics = []
    if diagnostics:
        progress = {**progress, "acceptanceDiagnostics": diagnostics}
    if progress.get("phase"):
        thinking = progress.get("thinkingTokens") or 0
        counted = (f" &middot; {escape_html(f'{thinking:,}')} thinking tokens so far"
                   if thinking else "")
        progress_html = (f'<p><strong>Now:</strong> {escape_html(progress["phase"])}{counted}</p>')
        timeline = progress.get("timeline") if isinstance(progress.get("timeline"), list) else []
        if timeline:
            items = []
            for event in timeline[-8:]:
                label = escape_html(event.get("label", "Progress update"))
                detail = escape_html(event.get("detail", ""))
                elapsed_event = escape_html(format_elapsed(event.get("elapsedSeconds", 0)))
                detail_html = f"<p>{detail}</p>" if detail else ""
                checkpoint_html = ""
                if event.get("checkpointId"):
                    checkpoint = escape_html(str(event.get("checkpointId")))
                    checkpoint_path = escape_html(str(event.get("checkpointPath") or ""))
                    checkpoint_html = f"<p>Checkpoint {checkpoint}{(' at ' + checkpoint_path) if checkpoint_path else ''}</p>"
                items.append(
                    f'<li><time>{elapsed_event}</time><div><strong>{label}</strong>{detail_html}{checkpoint_html}</div></li>')
            progress_html += (
                '<ol class="progress-timeline" style="list-style:none;padding:0;margin:10px 0 0;'
                'border-top:1px solid #d9d4c9;">'
                + "".join(
                    item.replace("<li>", '<li style="display:grid;grid-template-columns:54px minmax(0,1fr);'
                                 'gap:10px;padding:9px 0;border-bottom:1px solid #d9d4c9;">')
                    for item in items
                )
                + "</ol>"
            )
        narrative_text = narrative_progress_text(progress.get("jobNarratives", []))
        if narrative_text:
            progress_html += (
                '<h3>What Is Happening In The Background</h3>'
                f'<div class="answer-text">{escape_html(narrative_text)}</div>'
            )
        diagnostic_text = acceptance_diagnostics_text(progress.get("acceptanceDiagnostics", []))
        if diagnostic_text:
            progress_html += (
                '<h3>Acceptance Status</h3>'
                f'<div class="answer-text">{escape_html(diagnostic_text)}</div>'
            )
        steering_text = steering_events_text(progress.get("steeringEvents", []))
        if steering_text:
            progress_html += (
                '<h3>Follow-Up Updates</h3>'
                f'<div class="answer-text">{escape_html(steering_text)}</div>'
            )
        decision_text = decision_request_text(progress.get("decisionRequest", {}))
        if decision_text:
            progress_html += (
                '<h3>Decision Needed</h3>'
                f'<div class="answer-text">{escape_html(decision_text)}</div>'
            )
    else:
        progress_html = ('<p class="meta">No progress reported yet &mdash; the run has not '
                         'produced its first event.</p>')
        narrative_text = narrative_progress_text(progress.get("jobNarratives", []))
        if narrative_text:
            progress_html += (
                '<h3>What Is Happening In The Background</h3>'
                f'<div class="answer-text">{escape_html(narrative_text)}</div>'
            )
        diagnostic_text = acceptance_diagnostics_text(progress.get("acceptanceDiagnostics", []))
        if diagnostic_text:
            progress_html += (
                '<h3>Acceptance Status</h3>'
                f'<div class="answer-text">{escape_html(diagnostic_text)}</div>'
            )
        steering_text = steering_events_text(progress.get("steeringEvents", []))
        if steering_text:
            progress_html += (
                '<h3>Follow-Up Updates</h3>'
                f'<div class="answer-text">{escape_html(steering_text)}</div>'
            )
        decision_text = decision_request_text(progress.get("decisionRequest", {}))
        if decision_text:
            progress_html += (
                '<h3>Decision Needed</h3>'
                f'<div class="answer-text">{escape_html(decision_text)}</div>'
            )
    scope = payload.get("scope") or []
    scope_html = ""
    if scope:
        parts = ", ".join(
            f"{escape_html(c.get('returned'))} of {escape_html(c.get('totalAvailable'))} "
            f"{escape_html(c.get('name'))}" for c in scope)
        scope_html = f'<p class="meta">Reconciling {parts}.</p>'
    return render_shell(f"""
<section class="panel">
  <h2>Delivery Coordinator is reconciling the worker-visible state&hellip;</h2>
  <p><strong>{escape_html(format_elapsed(elapsed))}</strong> elapsed
     &middot; <span class="meta">refreshing every 2 seconds</span></p>
  {asked}
  {scope_html}
  <p class="meta">Measured on this worker: a question about one task takes about 40 seconds;
     a full &ldquo;where are we?&rdquo; takes roughly 2&ndash;3&frac12; minutes, because the
     answer is longer, not because more is read. This run gives up at
     {LIVE_TIMEOUT_SECONDS // 60} minutes.</p>
  {progress_html}
  <p class="meta">Job {escape_html(job_id)} &middot; task {escape_html(payload.get("taskId", "unknown"))}.</p>
</section>""", refresh=True)


def render_job(job_id: str, payload: dict[str, Any]) -> str:
    state = payload.get("state")
    if state in {"running", "waiting_for_decision", "paused"}:
        return render_running(job_id, payload)
    if state == "cancelled":
        follow_up_text = steering_events_text(job_steering_events(payload))
        follow_up_html = (
            f'<h3>Follow-Up Updates</h3><div class="answer-text">{escape_html(follow_up_text)}</div>'
            if follow_up_text else ""
        )
        return render_shell(f"""
<section class="panel">
  <h2>Delivery Coordinator job cancelled</h2>
  <p>{escape_html(payload.get("error", "Cancelled by your follow-up."))}</p>
  {follow_up_html}
  <p><a href="/">Ask another question</a></p>
</section>""")
    if state == "error":
        return render_shell(f"""
<section class="panel">
  <h2>Delivery Coordinator request failed</h2>
  <p>{escape_html(payload.get("error", "Unknown error"))}</p>
  <p><a href="/">Ask another question</a></p>
</section>""")
    if payload.get("ok") is not True:
        return render_shell(f"""
<section class="panel">
  <h2>Delivery Coordinator request failed</h2>
  <p>The worker did not return a successful coordination report.</p>
  <details open><summary>Raw result</summary><pre>{escape_html(json.dumps(payload, indent=2, sort_keys=True))}</pre></details>
  <p><a href="/">Ask another question</a></p>
</section>""")
    report = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    identity = report.get("packIdentity") if isinstance(report.get("packIdentity"), dict) else {}
    if not report.get("summary") or identity.get("probe") != "ok":
        return render_shell(f"""
<section class="panel">
  <h2>Delivery Coordinator report incomplete</h2>
  <p>The worker response was missing required coordination report fields, so it is not being shown as a valid Delivery Coordinator answer.</p>
  <details open><summary>Raw result</summary><pre>{escape_html(json.dumps(payload, indent=2, sort_keys=True))}</pre></details>
  <p><a href="/">Ask another question</a></p>
</section>""")
    items = report.get("items") if isinstance(report.get("items"), list) else []
    not_checked = report.get("notChecked") if isinstance(report.get("notChecked"), list) else []
    contradictions = report.get("contradictions") if isinstance(report.get("contradictions"), list) else []
    item_html = "".join(
        f"""<div class="item"><strong>{escape_html(item.get("subject", "Untitled"))}</strong>
        <span class="badge">{escape_html(item.get("status", "UNKNOWN"))}</span>
        <p class="meta">{escape_html(item.get("stateClass", "unknown"))} · {escape_html(item.get("freshness", "freshness unknown"))}</p>
        <p>{escape_html(item.get("note", ""))}</p></div>"""
        for item in items if isinstance(item, dict)
    ) or '<p class="meta">No checked items were returned.</p>'
    unchecked_html = "".join(
        f"""<div class="item"><strong>{escape_html(item.get("subject", "Untitled"))}</strong>
        <p class="meta">{escape_html(item.get("reason", ""))}</p></div>"""
        for item in not_checked if isinstance(item, dict)
    ) or '<p class="meta">No unchecked sources were listed.</p>'
    contradiction_html = "".join(
        f"""<div class="item"><strong>{escape_html(item.get("subject", "Untitled"))}</strong>
        <p>{escape_html(item.get("detail", ""))}</p></div>"""
        for item in contradictions if isinstance(item, dict)
    ) or '<p class="meta">No contradictions were listed.</p>'
    answer = plain_answer_text(report)
    return render_shell(f"""
<article class="answer">
  <div class="answer-text">{escape_html(answer)}</div>
  <p class="meta">Task {escape_html(payload.get("taskId", ""))} · Pack gen {escape_html(identity.get("corpusGeneration", "unknown"))}{" · answered in " + escape_html(format_elapsed(payload["durationSeconds"])) if payload.get("durationSeconds") else ""}</p>
  <h3>Checked State</h3>{item_html}
  <h3>Not Checked</h3>{unchecked_html}
  <h3>Contradictions</h3>{contradiction_html}
  <p class="meta">{escape_html(report.get("advisoryNote", "Advisory only."))}</p>
  <details><summary>Raw JSON</summary><pre>{escape_html(json.dumps(report, indent=2, sort_keys=True))}</pre></details>
  <p><a href="/">Ask another question</a></p>
</article>""")


def read_json(handler: BaseHTTPRequestHandler, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0 or length > max_bytes:
        raise ValueError(f"request body must be 1..{max_bytes} bytes")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def read_request_bytes(handler: BaseHTTPRequestHandler, max_bytes: int = MAX_REQUEST_BYTES) -> bytes:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0 or length > max_bytes:
        raise ValueError(f"request body must be 1..{max_bytes} bytes")
    return handler.rfile.read(length)


def clean_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in value[-12:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            cleaned.append({"role": role, "content": content[:6000]})
    return cleaned


def build_requirement(question: str, messages: list[dict[str, str]],
                      follow_ups: list[dict[str, Any]] | None = None) -> str:
    transcript = []
    for message in messages:
        if str(message.get("content", "")).startswith("# Work mode"):
            continue
        speaker = "User" if message["role"] == "user" else "DC13"
        transcript.append(f"### {speaker}\n\n{message['content']}")
    context = "\n\n".join(transcript).strip() or "No prior browser transcript supplied."
    steering = steering_events_text(follow_ups or [])
    steering_context = (
        "No active follow-up updates supplied."
        if not steering else
        "The user sent these follow-ups while the job was already running. "
        "Treat the latest follow-up as the active instruction; where it conflicts "
        f"with the original question, the follow-up wins.\n\n{steering}"
    )
    raw_mode_line = next(
        (message["content"] for message in messages if message.get("role") == "user"
         and str(message.get("content", "")).startswith("# Work mode")),
        "# Work mode\n\nDomain Capabilities mode: answer through the assigned domain capability and knowledge lens. "
        "The normal chat transcript still matters as context."
    )
    mode_line = str(raw_mode_line).removeprefix("# Work mode").strip()
    return (
        "# Delivery Coordinator status question\n\n"
        f"## Current question\n\n{question}\n\n"
        "## Work mode\n\n"
        f"{mode_line}\n\n"
        "## Browser transcript context\n\n"
        f"{context}\n\n"
        "## Active follow-up updates\n\n"
        f"{steering_context}\n\n"
        "## Response frame\n\n"
        "Answer as DC13 / Delivery Coordinator. Reconcile only the worker-visible "
        "state available to this run. Distinguish conversation-state, work-product, "
        "and canonical/execution state. Name anything material that was not checked "
        "or cannot be established. If active follow-up updates are present, say how "
        "it changed the scope of the answer. Do not approve, adopt, certify, gate, or claim PASS."
    )


def build_bundle(task_id: str, requirement: str, snapshot_policy: dict[str, Any] | None = None,
                 model_policy: dict[str, Any] | None = None,
                 runtime_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = resolve_runtime_profile()
    runtime_profile = runtime_profile or runtime.get("runtimeProfile")
    model_policy = model_policy or runtime.get("modelPolicy") or resolve_model_policy()
    snapshot_policy = snapshot_policy or default_snapshot_policy(
        str(model_policy.get("provider") or "claude"),
        str(model_policy.get("snapshotProfile") or "worker-local-default"),
    )
    contract = {
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": utc_now(),
        "producer": "steel-mission-chat-local",
        # This client is worker-local, not the control plane; the request
        # schema has a source for exactly that, and claiming otherwise
        # would misstate where the request came from.
        "provenance": {"source": "worker-local-advisory-client"},
        # A status question is not a verifiable claim, and `task-contract-v1`
        # is frozen and will not gain an exception for that. The authority's
        # additive `coordination-report-request-v1` is the contract for it: bound to
        # the advisory verb, asserting `advisory` and denying
        # `verificationAuthority` by construction, and carrying no
        # verification or build section for any pipeline verb to execute.
        "verb": "coordination-report",
        "advisory": True,
        "verificationAuthority": False,
    }
    if snapshot_policy is not None:
        contract["runtimeProfile"] = runtime_profile
        contract["modelPolicy"] = model_policy
        contract["snapshotPolicy"] = snapshot_policy
    return {
        "schemaVersion": 1,
        "taskId": task_id,
        "task": {
            "schemaVersion": 1,
            "taskId": task_id,
            "title": "DC13 browser chat question",
            "state": "active",
            "createdAt": utc_now(),
        },
        "requirement": requirement,
        "contract": contract,
    }


def run_coordinator_report(task_id: str, question: str, messages: list[dict[str, str]], mock: bool,
                   follow_ups: list[dict[str, Any]] | None = None, *,
                   job_id: str | None = None, revision: int = 0,
                   profile: str | None = None) -> dict[str, Any]:
    budget = MOCK_TIMEOUT_SECONDS if mock else LIVE_TIMEOUT_SECONDS
    runtime = resolve_runtime_profile(profile)
    runtime_profile = runtime.get("runtimeProfile") if isinstance(runtime.get("runtimeProfile"), dict) else None
    model_policy = runtime.get("modelPolicy") if isinstance(runtime.get("modelPolicy"), dict) else resolve_model_policy()
    snapshot_policy = runtime.get("snapshotPolicy") if isinstance(runtime.get("snapshotPolicy"), dict) else None
    provider = str(model_policy.get("provider") or active_coordinator_provider())
    bundle = build_bundle(task_id, build_requirement(question, messages, follow_ups),
                          snapshot_policy or default_snapshot_policy(provider, str(model_policy.get("snapshotProfile") or "")),
                          model_policy,
                          runtime_profile)
    cmd = [str(WORKER_BIN), "coordination-report", task_id,
           "--timeout-seconds", str(max(1, budget - MODEL_TIMEOUT_MARGIN_SECONDS)),
           "--profile", str((runtime_profile or {}).get("id") or active_runtime_profile())]
    if mock:
        cmd.append("--mock")
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(WORKER_DIR),
        start_new_session=True,
    )
    mission_id: str | None = None
    operator_role = "user"
    if job_id:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job and job.get("state") == "running" and int(job.get("steeringRevision") or 0) == revision:
                job["activePid"] = process.pid
                job["activeRevision"] = revision
                mission_id = job.get("missionId") if isinstance(job.get("missionId"), str) else None
                operator_role = corporate_role(str(job.get("operatorRole") or "user"))
        append_mission_audit(
            mission_id,
            "model-process-started",
            task_id=task_id,
            job_id=job_id,
            actor="worker",
            operator_role=operator_role,
            summary=f"Started {provider} model process for revision {revision}.",
            details={"pid": process.pid, "provider": provider, "model": model_policy.get("selectedModel"), "revision": revision},
        )
    try:
        stdout, stderr = process.communicate(json.dumps(bundle), timeout=budget)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass
        raise subprocess.TimeoutExpired(cmd, exc.timeout) from exc
    finally:
        if job_id:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job and job.get("activePid") == process.pid:
                    job.pop("activePid", None)
    if job_id:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job and job.get("state") == "cancelled":
                return {
                    "ok": False,
                    "taskId": task_id,
                    "exitCode": process.returncode,
                    "cancelled": True,
                    "payload": {"reason": job.get("error") or "cancelled by follow-up"},
                }
    text = stdout if process.returncode == 0 else stderr
    append_mission_audit(
        mission_id,
        "model-process-finished",
        task_id=task_id,
        job_id=job_id,
        actor="worker",
        operator_role=operator_role,
        summary=f"{provider} model process exited with code {process.returncode}.",
        details={"exitCode": process.returncode, "provider": provider, "model": model_policy.get("selectedModel"), "revision": revision},
    )
    if job_id:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job and int(job.get("steeringRevision") or 0) != revision:
                return {
                    "ok": False,
                    "taskId": task_id,
                    "exitCode": process.returncode,
                    "steered": True,
                    "payload": {"reason": "superseded by active follow-up"},
                }
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        payload = {"raw": text.strip()}
    return {
        "ok": process.returncode == 0,
        "taskId": task_id,
        "exitCode": process.returncode,
        "payload": payload,
    }


def start_job(question: str, messages: list[dict[str, str]], mock: bool, profile: str | None = None,
              operator_role: str | None = None, uploads: Any = None, work_mode: str | None = None,
              actor_user_id: str | None = None, organization_id: str | None = None) -> str:
    job_id = secrets.token_urlsafe(12)
    mission_id = "ms-" + secrets.token_hex(12)
    # The task id is minted here, not inside the run, so that a failed job still
    # names the task directory it produced. Dropping it on the error path left
    # failures uncorrelatable to their DEV-NNNNNN artifacts.
    task_id = new_task_id()
    started = time.time()
    selected_profile = profile or active_runtime_profile()
    runtime = resolve_runtime_profile(selected_profile)
    runtime_profile = runtime.get("runtimeProfile") if isinstance(runtime.get("runtimeProfile"), dict) else {}
    model_policy = runtime.get("modelPolicy") if isinstance(runtime.get("modelPolicy"), dict) else {}
    snapshot_policy = runtime.get("snapshotPolicy") if isinstance(runtime.get("snapshotPolicy"), dict) else {}
    operator = corporate_role(operator_role)
    actor_id = clean_optional_string(actor_user_id, limit=120) or operator
    selected_organization_id = clean_optional_string(organization_id, limit=120) or str(organization_registry().get("activeOrganizationId") or "")
    mode = normalize_work_mode(work_mode)
    mode_context = (
        "# Work mode\n\n"
        "Normal chat mode: answer conversationally and directly. Keep the assigned domain capability, "
        "snapshot policy, mission state, and prior Domain Capabilities context available when they help the user."
        if mode == "normal" else
        "# Work mode\n\n"
        "Domain Capabilities mode: answer through the assigned domain capability and knowledge lens. Keep the normal "
        "chat transcript available as user intent and conversational context."
    )
    messages = [*messages, {"role": "user", "content": mode_context}]
    knowledge_quality = knowledge_quality_report()
    if knowledge_quality.get("issues"):
        issue_lines = [
            f"- [{str(item.get('severity') or 'warning').upper()}] {item.get('message') or item.get('id') or 'knowledge issue'}"
            for item in knowledge_quality.get("issues", [])[:8]
            if isinstance(item, dict)
        ]
        messages = [*messages, {
            "role": "user",
            "content": "# Knowledge quality\n\n"
            + str(knowledge_quality.get("confidenceDirective") or "")
            + ("\n\n" + "\n".join(issue_lines) if issue_lines else ""),
        }]
    upload_summaries, upload_context = chat_upload_context(uploads)
    if upload_context:
        messages = [*messages, {"role": "user", "content": "# Uploaded chat context\n\n" + upload_context}]
    scope = [] if mock else snapshot_scope(selected_profile)
    initial_decision_demo = classify_follow_up(question).get("effect") == "request-user-decision"
    with JOBS_LOCK:
        job = {"state": "running", "createdAt": utc_now(), "startedEpoch": started,
               "taskId": task_id, "mock": mock, "question": question, "scope": scope,
               "profile": selected_profile, "missionId": mission_id, "operatorRole": operator,
               "actorUserId": actor_id,
               "organizationId": selected_organization_id,
               "workMode": mode,
               "messages": messages,
               "chatUploads": upload_summaries,
               "knowledgeQuality": knowledge_quality,
               "followUps": [], "steeringRevision": 0, "restartRequested": False,
               "restartCount": 0}
        if initial_decision_demo:
            job["state"] = "waiting_for_decision"
            job["decisionRequest"] = demo_decision_request()
            job["phase"] = "Delivery Coordinator needs your decision before continuing."
            job["steeringRevision"] = 1
        JOBS[job_id] = job
    update_mission(
        mission_id,
        jobId=job_id,
        taskId=task_id,
        state="waiting_for_decision" if initial_decision_demo else "running",
        operatorRole=operator,
        actorUserId=actor_id,
        organizationId=selected_organization_id,
        mock=mock,
        missionKind="advisory-chat",
        question=question[:12000],
        workMode=mode,
        chatUploads=upload_summaries,
        profile=selected_profile,
        runtimeProfile=runtime_profile,
        modelPolicy=model_policy,
        snapshotPolicySummary=snapshot_policy_summary(snapshot_policy),
        snapshotCollections=scope,
        knowledgeQuality=knowledge_quality,
        steeringRevision=1 if initial_decision_demo else 0,
        followUpCount=0,
        restartCount=0,
        startedAt=dt.datetime.fromtimestamp(started, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    append_mission_audit(
        mission_id,
        "mission-started",
        task_id=task_id,
        job_id=job_id,
        actor=actor_id,
        operator_role=operator,
        summary=f"Started Delivery Coordinator mission for profile {selected_profile}.",
        details={
            "mock": mock,
            "question": question[:500],
            "runtimeProfileId": runtime_profile.get("id"),
            "provider": model_policy.get("provider"),
            "model": model_policy.get("selectedModel"),
            "workMode": mode,
            "snapshotPolicyHash": snapshot_policy_summary(snapshot_policy).get("policyHash"),
            "knowledgeQualityStatus": knowledge_quality.get("status"),
            "knowledgeContextSufficient": knowledge_quality.get("contextSufficient") is True,
            "knowledgeQualityHash": knowledge_quality.get("qualityHash"),
            "chatUploadCount": len(upload_summaries),
        },
    )
    if initial_decision_demo:
        append_mission_audit(
            mission_id,
            "decision-requested",
            task_id=task_id,
            job_id=job_id,
            actor="system",
            operator_role=operator,
            summary="Initial question opened a decision request.",
            details={"decisionRequestId": job.get("decisionRequest", {}).get("id")},
        )

    def fail(error: str) -> None:
        with JOBS_LOCK:
            created_at = JOBS[job_id]["createdAt"]
            follow_ups = list(job_steering_events(JOBS[job_id]))
            mission = JOBS[job_id].get("missionId")
            JOBS[job_id] = {
                "state": "error",
                "createdAt": created_at,
                "taskId": task_id,
                "missionId": mission,
                "operatorRole": operator,
                "mock": mock,
                "ok": False,
                "durationSeconds": round(time.time() - started, 1),
                "followUps": follow_ups,
                "error": error,
            }
        update_mission(
            mission_id,
            state="error",
            completedAt=utc_now(),
            durationSeconds=round(time.time() - started, 1),
            error=error,
            followUpCount=len(follow_ups),
        )
        append_mission_audit(
            mission_id,
            "mission-failed",
            task_id=task_id,
            job_id=job_id,
            actor="system",
            operator_role=operator,
            summary=error,
            details={"error": error},
        )

    def worker() -> None:
        try:
            while True:
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if not job:
                        return
                    if job.get("state") == "cancelled":
                        return
                    if job.get("state") in {"waiting_for_decision", "paused"}:
                        wait = True
                    else:
                        wait = False
                if wait:
                    time.sleep(0.2)
                    continue
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if not job:
                        return
                    revision = int(job.get("steeringRevision") or 0)
                    follow_ups = list(job_steering_events(job))
                    job["restartRequested"] = False
                    if follow_ups:
                        job["phase"] = "Delivery Coordinator is answering with your latest follow-up."
                append_mission_audit(
                    mission_id,
                    "run-attempt-started",
                    task_id=task_id,
                    job_id=job_id,
                    actor="system",
                    operator_role=operator,
                    summary=f"Started run attempt at steering revision {revision}.",
                    details={"revision": revision, "followUpCount": len(follow_ups)},
                )
                result = run_coordinator_report(task_id, question, messages, mock, follow_ups,
                                        job_id=job_id, revision=revision,
                                        profile=str(job.get("profile") or selected_profile))
                with JOBS_LOCK:
                    latest_state = JOBS.get(job_id, {}).get("state")
                    if latest_state == "cancelled":
                        return
                    latest_revision = int(JOBS.get(job_id, {}).get("steeringRevision") or 0)
                    if result.get("steered") or latest_revision != revision:
                        job = JOBS.get(job_id)
                        if job:
                            job["restartCount"] = int(job.get("restartCount") or 0) + 1
                            job["restartRequested"] = False
                            if latest_state != "paused":
                                job["phase"] = "Delivery Coordinator restarted with your latest follow-up."
                            update_mission(
                                mission_id,
                                state=str(latest_state or "running"),
                                restartCount=job["restartCount"],
                                steeringRevision=latest_revision,
                                followUpCount=len(job_steering_events(job)),
                            )
                            append_mission_audit(
                                mission_id,
                                "run-attempt-superseded",
                                task_id=task_id,
                                job_id=job_id,
                                actor="system",
                                operator_role=operator,
                                summary="Run attempt was superseded by newer steering.",
                                details={"revision": revision, "latestRevision": latest_revision},
                            )
                        continue
                break
            payload = result.get("payload")
            with JOBS_LOCK:
                if JOBS.get(job_id, {}).get("state") == "cancelled":
                    return
                follow_ups = list(job_steering_events(JOBS.get(job_id, {})))
            if isinstance(payload, dict) and payload.get("summary"):
                try:
                    narratives = broker_narratives()
                except Exception:  # noqa: BLE001
                    narratives = []
            else:
                narratives = []
            if narratives:
                result["payload"] = {**payload, "jobNarratives": narratives}
                payload = result["payload"]
            try:
                diagnostics = broker_acceptance_diagnostics()
            except Exception:  # noqa: BLE001
                diagnostics = []
            if diagnostics and isinstance(payload, dict):
                result["payload"] = {**payload, "acceptanceDiagnostics": diagnostics}
                payload = result["payload"]
            if isinstance(payload, dict) and follow_ups:
                result["payload"] = {**payload, "followUps": follow_ups}
            with JOBS_LOCK:
                created_at = JOBS[job_id]["createdAt"]
                restart_count = int(JOBS[job_id].get("restartCount") or 0)
                JOBS[job_id] = {"state": "done", "createdAt": created_at, "mock": mock,
                                "missionId": mission_id,
                                "operatorRole": operator,
                                "durationSeconds": round(time.time() - started, 1),
                                "followUps": follow_ups, "restartCount": restart_count, **result}
            update_mission(
                mission_id,
                state="done",
                completedAt=utc_now(),
                durationSeconds=round(time.time() - started, 1),
                exitCode=result.get("exitCode"),
                ok=result.get("ok") is True,
                followUpCount=len(follow_ups),
                restartCount=restart_count,
            )
            append_mission_audit(
                mission_id,
                "mission-completed",
                task_id=task_id,
                job_id=job_id,
                actor="system",
                operator_role=operator,
                summary="Delivery Coordinator mission completed.",
                details={"ok": result.get("ok"), "exitCode": result.get("exitCode")},
                artifact_refs=[{
                    "kind": "coordination-report",
                    "path": str(TASKS_DIR / task_id / "coordination-report" / "coordination-report.json"),
                    **({"sha256": file_sha256(TASKS_DIR / task_id / "coordination-report" / "coordination-report.json")}
                       if file_sha256(TASKS_DIR / task_id / "coordination-report" / "coordination-report.json") else {}),
                }],
            )
        except subprocess.TimeoutExpired:
            budget = MOCK_TIMEOUT_SECONDS if mock else LIVE_TIMEOUT_SECONDS
            mode = "mock" if mock else "live"
            fail(f"Delivery Coordinator did not answer within {budget} seconds ({mode} budget). "
                 f"Task {task_id} was stopped; ask a narrower status question or check the worker.")
        except Exception as exc:  # noqa: BLE001
            fail(str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def mission_visible_to(record: dict[str, Any], role: str, actor: dict[str, Any] | None = None) -> bool:
    selected = corporate_role(role)
    if actor is None:
        return selected in {"owner", "admin"} or record.get("operatorRole") in {selected, "user"}
    return mission_visible_to_actor(record, actor)


def mission_node_approved(record: dict[str, Any], node_id: str) -> bool:
    approvals = record.get("approvals") if isinstance(record.get("approvals"), list) else []
    return any(
        isinstance(item, dict) and item.get("nodeId") == node_id and item.get("decision") == "approved"
        for item in approvals
    )


def mission_objective_for_node(record: dict[str, Any], node: dict[str, Any]) -> str:
    objective = str(record.get("objective") or record.get("question") or "").strip()
    template_title = str(record.get("templateTitle") or "Mission").strip()
    node_title = str(node.get("title") or node.get("nodeId") or "node").strip()
    kind = str(node.get("kind") or "mission").strip()
    mission_users = record.get("missionUsers") if isinstance(record.get("missionUsers"), list) else []
    work_set = record.get("capabilityWorkSet") if isinstance(record.get("capabilityWorkSet"), list) else []
    delivery = record.get("deliveryContext") if isinstance(record.get("deliveryContext"), dict) else {}
    binding_context = "\n".join([
        "Mission users: " + (", ".join(
            f"{user.get('name') or user.get('id')} ({user.get('role')})"
            for user in mission_users if isinstance(user, dict)
        ) or "not bound"),
        "Capability work set: " + (", ".join(
            f"{item.get('fNumber') or item.get('roleKey')} {item.get('roleKey') or ''}".strip()
            for item in work_set if isinstance(item, dict)
        ) or "not bound"),
        "Delivery repository: " + (str(delivery.get("repositoryPath") or "").strip() or "not bound"),
        "Delivery branch: " + (str(delivery.get("branch") or "").strip() or "not bound"),
    ])
    if kind == "schema-gate":
        return (
            f"{template_title}: explain the result of the schema gate for this mission.\n\n"
            f"Mission objective: {objective}\n\n"
            f"{binding_context}\n\n"
            "Name whether schema authority evidence supports continuing and what still needs human attention."
        )
    if kind == "coordination-report":
        return (
            f"{template_title} / {node_title}\n\n"
            f"Mission objective: {objective}\n\n"
            f"{binding_context}\n\n"
            "Use the attached snapshot policy as the only source boundary for this job. "
            "Give an advisory Delivery Coordinator readout, name missing sources, and separate evidence from recommendation."
        )
    return (
        f"{template_title} / {node_title}\n\n"
        f"Mission objective: {objective}\n\n"
        f"{binding_context}\n\n"
        "Summarize the current mission state and the evidence recorded so far."
    )


DELIVERY_PHASES = ["understand", "plan", "modify", "build", "test", "inspect", "repair", "pr", "deploy"]


def delivery_context_summary(record: dict[str, Any]) -> dict[str, Any]:
    context = record.get("deliveryContext") if isinstance(record.get("deliveryContext"), dict) else {}
    return {
        "repositoryPath": context.get("repositoryPath") or "",
        "branch": context.get("branch") or "",
        "baseBranch": context.get("baseBranch") or "",
        "deliveryBranch": context.get("deliveryBranch") or "",
        "worktreeMode": context.get("worktreeMode") or "in-place",
        "worktreePath": context.get("worktreePath") or "",
        "prProvider": context.get("prProvider") or "github",
        "githubRepository": context.get("githubRepository") or "",
        "prMode": context.get("prMode") or "readiness",
        "pushBeforePr": bool(context.get("pushBeforePr")),
        "ciProvider": context.get("ciProvider") or "github-actions",
        "ciRequired": bool(context.get("ciRequired")),
        "ciWait": bool(context.get("ciWait")),
        "ciCommand": context.get("ciCommand") or "",
        "deployProvider": context.get("deployProvider") or "manual",
        "deployEnvironment": context.get("deployEnvironment") or "",
        "deployUrl": context.get("deployUrl") or "",
        "rollbackCommand": context.get("rollbackCommand") or "",
        "modifyCommand": context.get("modifyCommand") or "",
        "buildCommand": context.get("buildCommand") or "",
        "testCommand": context.get("testCommand") or "",
        "inspectCommand": context.get("inspectCommand") or "",
        "repairCommand": context.get("repairCommand") or "",
        "prCommand": context.get("prCommand") or "",
        "prTarget": context.get("prTarget") or "",
        "prTitle": context.get("prTitle") or "",
        "prBody": context.get("prBody") or "",
        "deployCommand": context.get("deployCommand") or "",
        "deployTarget": context.get("deployTarget") or "",
        "deployHealthCommand": context.get("deployHealthCommand") or "",
        "repairBudget": int(context.get("repairBudget") or 0),
    }


def delivery_plan_payload(record: dict[str, Any]) -> dict[str, Any]:
    nodes = [node for node in record.get("nodes", []) if isinstance(node, dict)]
    return {
        "objective": record.get("objective") or record.get("question") or "",
        "executionMode": "alpha-control-plane",
        "lifecycle": DELIVERY_PHASES,
        "deliveryContext": delivery_context_summary(record),
        "missionUsers": record.get("missionUsers") if isinstance(record.get("missionUsers"), list) else [],
        "capabilityWorkSet": record.get("capabilityWorkSet") if isinstance(record.get("capabilityWorkSet"), list) else [],
        "snapshotPolicySummary": record.get("snapshotPolicySummary") if isinstance(record.get("snapshotPolicySummary"), dict) else {},
        "nodes": [
            {
                "nodeId": node.get("nodeId"),
                "title": node.get("title"),
                "kind": node.get("kind"),
                "phase": node.get("phase"),
                "capability": node.get("capability"),
                "requiresApproval": bool(node.get("requiresApproval")),
            }
            for node in nodes
        ],
        "controls": [
            "Use the mission snapshot policy as the source boundary.",
            "Require approval before modify/build/test/inspect/repair/PR/deploy readiness stages.",
            "Record every lifecycle stage as mission evidence before marking the mission complete.",
            "Run configured lifecycle commands only after approval and with command output recorded as evidence.",
            "Use isolated delivery worktree mode when the mission must protect the source checkout from mutation.",
        ],
    }


def mission_has_delivery_approval(record: dict[str, Any]) -> bool:
    approvals = record.get("approvals") if isinstance(record.get("approvals"), list) else []
    return any(
        isinstance(item, dict)
        and item.get("decision") == "approved"
        and str(item.get("nodeId") or "") in {"implementation-approval", "publish-approval"}
        for item in approvals
    )


def command_matches_blocked_pattern(command: str, policy: dict[str, Any]) -> list[str]:
    blocked: list[str] = []
    patterns = policy.get("blockedCommandPatterns") if isinstance(policy.get("blockedCommandPatterns"), list) else []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            if re.search(pattern, command, flags=re.I | re.S):
                blocked.append(pattern)
        except re.error:
            continue
    return blocked


def delivery_action_risk(phase: str, context: dict[str, Any], command: str, policy: dict[str, Any]) -> dict[str, Any]:
    approval_policy = policy.get("approvalRequired") if isinstance(policy.get("approvalRequired"), dict) else {}
    reasons: list[str] = []
    blocked_patterns = command_matches_blocked_pattern(command, policy) if command else []
    if blocked_patterns:
        return {
            "level": "critical",
            "requiresApproval": False,
            "blocked": True,
            "reasons": [f"command matches blocked policy pattern: {pattern}" for pattern in blocked_patterns],
            "blockedPatterns": blocked_patterns,
        }
    phase_requires = phase in set(approval_policy.get("phases") if isinstance(approval_policy.get("phases"), list) else [])
    pr_requires = (
        phase == "pr"
        and str(context.get("prMode") or "readiness") in set(approval_policy.get("prModes") if isinstance(approval_policy.get("prModes"), list) else [])
    )
    deploy_providers = set(approval_policy.get("deployProviders") if isinstance(approval_policy.get("deployProviders"), list) else [])
    deploy_envs = set(str(item).lower() for item in (approval_policy.get("deployEnvironments") if isinstance(approval_policy.get("deployEnvironments"), list) else []))
    deploy_requires = phase == "deploy" and (
        str(context.get("deployProvider") or "") in deploy_providers
        or str(context.get("deployEnvironment") or "").lower() in deploy_envs
        or bool(command)
    )
    if phase_requires:
        reasons.append(f"{phase} is a workspace mutation phase")
    if pr_requires:
        reasons.append(f"PR mode {context.get('prMode')} creates provider state")
    if deploy_requires:
        reasons.append("deploy action can affect a runtime environment")
    if reasons:
        return {"level": "high", "requiresApproval": True, "blocked": False, "reasons": reasons, "blockedPatterns": []}
    if phase in {"build", "test", "inspect"}:
        return {"level": "low", "requiresApproval": False, "blocked": False, "reasons": ["phase is eligible for automatic execution"], "blockedPatterns": []}
    if phase in {"pr", "deploy"}:
        return {"level": "medium", "requiresApproval": False, "blocked": False, "reasons": ["readiness evidence only"], "blockedPatterns": []}
    return {"level": "medium", "requiresApproval": False, "blocked": False, "reasons": ["delivery governance evidence"], "blockedPatterns": []}


def delivery_action_preflight(
    record: dict[str, Any],
    phase: str,
    command: str,
    context: dict[str, Any],
    workspace: dict[str, Any],
) -> dict[str, Any]:
    policy = control_policy()
    risk = delivery_action_risk(phase, context, command, policy)
    approved = mission_has_delivery_approval(record)
    decision = "allow"
    blockers: list[str] = []
    if risk.get("blocked"):
        decision = "block"
        blockers.extend(str(item) for item in risk.get("reasons", []) if item)
    execution_boundary = policy.get("executionBoundary") if isinstance(policy.get("executionBoundary"), dict) else {}
    runner_health = private_runner_health() if command and execution_boundary.get("privateRunnerRequired") is True else {}
    if (
        command
        and execution_boundary.get("guardedRunnerRequired") is True
        and execution_boundary.get("directCommandMode") == "block"
        and record.get("controlPlaneExecution") is not True
    ):
        decision = "block"
        blockers.append("command execution must use the signed guarded control-plane runner")
    elif command and execution_boundary.get("privateRunnerRequired") is True and runner_health.get("ok") is not True:
        decision = "block"
        blockers.append(str(runner_health.get("error") or "private runner is not ready"))
    elif risk.get("requiresApproval") and not approved:
        decision = "require_approval"
        blockers.append("human approval is required before this action can execute")
        blockers.extend(str(item) for item in risk.get("reasons", []) if item)
    return {
        "schemaVersion": 1,
        "policyId": policy.get("policyId") or "present.delivery-control.alpha",
        "policyHash": canonical_json_hash({key: value for key, value in policy.items() if key not in {"configuredPath", "configured"}}),
        "decision": decision,
        "ok": decision == "allow",
        "risk": risk,
        "approvalSatisfied": approved,
        "modelIndependent": bool(policy.get("modelIndependence", {}).get("required", True)) if isinstance(policy.get("modelIndependence"), dict) else True,
        "customerControlled": bool(policy.get("customerBoundary", {}).get("required", True)) if isinstance(policy.get("customerBoundary"), dict) else True,
        "deploymentBoundary": policy.get("customerBoundary", {}).get("deployment", "customer-vpc-or-private-cloud") if isinstance(policy.get("customerBoundary"), dict) else "customer-vpc-or-private-cloud",
        "guardedRunnerRequired": execution_boundary.get("guardedRunnerRequired") is True,
        "controlPlaneExecution": record.get("controlPlaneExecution") is True,
        "privateRunnerRequired": execution_boundary.get("privateRunnerRequired") is True,
        "privateRunner": runner_health,
        "phase": phase,
        "commandConfigured": bool(command),
        "workspacePath": workspace.get("path") or "",
        "blockers": blockers,
        "complianceEvidence": compliance_evidence({"phase": phase, "decision": decision, "risk": risk.get("level")}),
    }


def limited_text(value: str, *, limit: int = 12000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def delivery_repo_path(context: dict[str, Any]) -> Path | None:
    raw = str(context.get("repositoryPath") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        return path.resolve()
    except OSError:
        return path


def safe_delivery_branch(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", value.strip()).strip("./-")
    cleaned = re.sub(r"/+", "/", cleaned)
    return cleaned[:180] or fallback


def run_git_command(args: list[str], cwd: Path, *, timeout: int = 30) -> dict[str, Any]:
    started = time.time()
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "status": "failed", "error": str(exc), "argv": ["git", *args]}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "status": "timeout",
            "argv": ["git", *args],
            "timeoutSeconds": timeout,
            "stdout": limited_text(exc.stdout or ""),
            "stderr": limited_text(exc.stderr or ""),
        }
    return {
        "ok": result.returncode == 0,
        "status": "succeeded" if result.returncode == 0 else "failed",
        "argv": ["git", *args],
        "exitCode": result.returncode,
        "durationSeconds": round(time.time() - started, 1),
        "stdout": limited_text(result.stdout or ""),
        "stderr": limited_text(result.stderr or ""),
    }


def git_current_branch(repo: Path) -> str:
    result = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], repo, timeout=15)
    return str(result.get("stdout") or "").strip() if result.get("ok") else ""


def git_branch_exists(repo: Path, branch: str) -> bool:
    if not branch:
        return False
    result = run_git_command(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo, timeout=15)
    return result.get("ok") is True


def delivery_workspace_path(record: dict[str, Any], context: dict[str, Any]) -> Path:
    configured = str(context.get("worktreePath") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    mission_id = safe_path_part(str(record.get("missionId") or "mission"), "mission")
    return (mission_dir(mission_id) / "delivery-worktree").resolve()


def ensure_delivery_workspace(record: dict[str, Any]) -> dict[str, Any]:
    context = delivery_context_summary(record)
    source_repo = delivery_repo_path(context)
    mode = str(context.get("worktreeMode") or "in-place")
    mission_id = str(record.get("missionId") or "")
    base_branch = safe_delivery_branch(str(context.get("baseBranch") or context.get("branch") or "HEAD"), "HEAD")
    delivery_branch = safe_delivery_branch(
        str(context.get("deliveryBranch") or ""),
        f"codex/delivery-{mission_id[3:11] if mission_id.startswith('ms-') else secrets.token_hex(4)}",
    )
    workspace = source_repo
    payload: dict[str, Any] = {
        "ok": False,
        "mode": mode,
        "sourceRepositoryPath": str(source_repo) if source_repo else "",
        "baseBranch": base_branch,
        "deliveryBranch": delivery_branch,
        "gitPath": shutil.which("git") or "",
        "created": False,
    }
    if source_repo is None:
        return {**payload, "error": "repositoryPath is not bound"}
    if not source_repo.exists() or not source_repo.is_dir():
        return {**payload, "error": "repositoryPath does not exist or is not a directory"}
    source_state = delivery_git_state(source_repo)
    payload["sourceGitState"] = source_state
    if source_state.get("ok") is not True:
        return {**payload, "error": str(source_state.get("error") or "source repository is not a git worktree")}
    if mode != "isolated":
        payload.update({
            "ok": True,
            "path": str(source_repo),
            "branch": str(source_state.get("branch") or ""),
            "gitState": source_state,
        })
        return payload

    workspace = delivery_workspace_path(record, context)
    payload["path"] = str(workspace)
    if workspace.exists():
        state = delivery_git_state(workspace)
        if state.get("ok") is not True:
            return {**payload, "error": "configured worktreePath exists but is not a git worktree", "gitState": state}
        payload.update({"ok": True, "branch": str(state.get("branch") or ""), "gitState": state})
    else:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add"]
        if git_branch_exists(source_repo, delivery_branch):
            args += [str(workspace), delivery_branch]
        else:
            args += ["-b", delivery_branch, str(workspace), base_branch]
        result = run_git_command(args, source_repo, timeout=60)
        payload["createResult"] = result
        if result.get("ok") is not True:
            return {**payload, "error": str(result.get("stderr") or result.get("error") or "git worktree add failed").strip()}
        state = delivery_git_state(workspace)
        payload.update({"ok": state.get("ok") is True, "created": True, "branch": str(state.get("branch") or ""), "gitState": state})
    update_mission(mission_id, deliveryWorkspace=payload)
    return payload


def run_delivery_subprocess(command: str, cwd: Path, *, timeout: int = DELIVERY_COMMAND_TIMEOUT_SECONDS) -> dict[str, Any]:
    if not command.strip():
        return {"ok": False, "status": "planned", "error": "command is not configured"}
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"ok": False, "status": "blocked", "error": f"command could not be parsed: {exc}"}
    if not argv:
        return {"ok": False, "status": "planned", "error": "command is not configured"}
    started = time.time()
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "status": "failed", "error": str(exc), "argv": argv}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "status": "timeout",
            "argv": argv,
            "timeoutSeconds": timeout,
            "stdout": limited_text(exc.stdout or ""),
            "stderr": limited_text(exc.stderr or ""),
        }
    return {
        "ok": result.returncode == 0,
        "status": "succeeded" if result.returncode == 0 else "failed",
        "argv": argv,
        "exitCode": result.returncode,
        "durationSeconds": round(time.time() - started, 1),
        "stdout": limited_text(result.stdout or ""),
        "stderr": limited_text(result.stderr or ""),
    }


def private_runner_command(status: bool = False) -> list[str]:
    boundary = control_policy().get("executionBoundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    field = "privateRunnerStatusCommand" if status else "privateRunnerCommand"
    configured = boundary.get(field)
    fallback = [str(PRIVATE_RUNNER_BIN), "status" if status else "execute"]
    raw = configured if isinstance(configured, list) and configured else fallback
    return [
        str(item).replace("${WORKER_DIR}", str(WORKER_DIR)).replace("${PRIVATE_RUNNER_BIN}", str(PRIVATE_RUNNER_BIN))
        for item in raw
        if isinstance(item, str) and item.strip()
    ] or fallback


def private_runner_process_environment(mode: str, signing_key: bytes | None = None) -> dict[str, str]:
    selected_mode = os.environ.get(PRIVATE_RUNNER_MODE_ENV) or ("docker" if mode == "container" else "local")
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        PRIVATE_RUNNER_MODE_ENV: selected_mode,
        PRIVATE_RUNNER_ALLOW_LOCAL_ENV: "1" if selected_mode == "local" and mode == "development-local" else "0",
        PRIVATE_RUNNER_SIGNER_ID_ENV: "steel-mission-private-runner",
    }
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR", "PRESENT_PRIVATE_RUNNER_IMAGE",
                "PRESENT_PRIVATE_RUNNER_NETWORK", "PRESENT_PRIVATE_RUNNER_USER", "PRESENT_PRIVATE_RUNNER_MEMORY",
                "PRESENT_PRIVATE_RUNNER_CPUS", "PRESENT_PRIVATE_RUNNER_PIDS"):
        if os.environ.get(key):
            env[key] = str(os.environ[key])
    if signing_key is not None:
        env[PRIVATE_RUNNER_SIGNING_KEY_ENV] = signing_key.decode("utf-8", errors="surrogateescape")
    return env


def private_runner_health() -> dict[str, Any]:
    boundary = control_policy().get("executionBoundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    mode = str(boundary.get("privateRunnerMode") or "development-local")
    command = private_runner_command(status=True)
    try:
        completed = subprocess.run(
            command,
            cwd=str(WORKER_DIR),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=private_runner_process_environment(mode),
        )
        raw = completed.stdout or completed.stderr or "{}"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("private-runner status must be an object")
        return {
            **payload,
            "ok": completed.returncode == 0 and payload.get("ok") is True,
            "command": command,
            "configuredMode": mode,
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return {
            "schemaVersion": 1,
            "ok": False,
            "status": "blocked",
            "error": str(exc),
            "command": command,
            "configuredMode": mode,
        }


def verify_private_runner_attestation(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    attestation = payload.get("attestation") if isinstance(payload.get("attestation"), dict) else {}
    basis = {field: value for field, value in payload.items() if field != "attestation"}
    payload_hash = canonical_json_hash(basis)
    expected = hmac.new(key, payload_hash.encode("ascii"), hashlib.sha256).hexdigest()
    signature = str(attestation.get("signature") or "")
    valid = (
        attestation.get("algorithm") == "hmac-sha256"
        and attestation.get("signerId") == "steel-mission-private-runner"
        and hmac.compare_digest(str(attestation.get("payloadHash") or ""), payload_hash)
        and bool(signature)
        and hmac.compare_digest(signature, expected)
    )
    return {
        "valid": valid,
        "algorithm": attestation.get("algorithm") or "",
        "signerId": attestation.get("signerId") or "",
        "payloadHash": payload_hash,
    }


def run_delivery_private_runner(
    command: str,
    cwd: Path,
    record: dict[str, Any],
    phase: str,
    *,
    timeout: int = DELIVERY_COMMAND_TIMEOUT_SECONDS,
    stdin_text: str = "",
    request_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not command.strip():
        return {"ok": False, "status": "planned", "error": "command is not configured"}
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"ok": False, "status": "blocked", "error": f"command could not be parsed: {exc}"}
    if not argv:
        return {"ok": False, "status": "planned", "error": "command is not configured"}
    boundary = control_policy().get("executionBoundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    mode = str(boundary.get("privateRunnerMode") or "development-local")
    allowed_environment = boundary.get("allowedEnvironment") if isinstance(boundary.get("allowedEnvironment"), list) else []
    scoped_environment = {
        str(name): str(os.environ[name])
        for name in allowed_environment
        if isinstance(name, str) and name in os.environ and os.environ[name]
    }
    for name, value in (request_environment or {}).items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", str(name)) and isinstance(value, str):
            scoped_environment[str(name)] = value
    identity_hash = canonical_json_hash({
        "workspacePath": str(cwd.resolve()),
        "phase": phase,
        "argv": argv,
        "missionId": record.get("missionId") or "",
        "taskId": record.get("taskId") or "",
    })
    mission_id = str(record.get("missionId") or "")
    if not re.fullmatch(r"ms-[a-f0-9]{24}", mission_id):
        mission_id = "ms-" + identity_hash[:24]
    task_id = str(record.get("taskId") or "")
    if not re.fullmatch(r"DEV-[0-9]{6}", task_id):
        task_id = f"DEV-{int(identity_hash[24:36], 16) % 1_000_000:06d}"
    request = {
        "schemaVersion": 1,
        "requestId": "pre-" + secrets.token_hex(12),
        "missionId": mission_id,
        "taskId": task_id,
        "phase": phase,
        "workspacePath": str(cwd.resolve()),
        "argv": argv,
        "timeoutSeconds": timeout,
        "environment": scoped_environment,
        "stdin": stdin_text,
    }
    signing_key = base64.b64encode(private_runner_signing_key())
    request_hash = canonical_json_hash(request)
    request["attestation"] = {
        "algorithm": "hmac-sha256",
        "signerId": "steel-mission-control-plane",
        "payloadHash": request_hash,
        "signature": hmac.new(signing_key, request_hash.encode("ascii"), hashlib.sha256).hexdigest(),
    }
    runner = private_runner_command(status=False)
    started = time.time()
    try:
        completed = subprocess.run(
            runner,
            cwd=str(WORKER_DIR),
            input=json.dumps(request, sort_keys=True),
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=False,
            env=private_runner_process_environment(mode, signing_key),
        )
        raw = completed.stdout or completed.stderr or "{}"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("private-runner result must be an object")
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "status": "timeout",
            "runnerCommand": runner,
            "timeoutSeconds": timeout + 30,
            "durationSeconds": round(time.time() - started, 1),
            "stdout": limited_text(exc.stdout or ""),
            "stderr": limited_text(exc.stderr or ""),
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "status": "blocked",
            "runnerCommand": runner,
            "error": str(exc),
            "durationSeconds": round(time.time() - started, 1),
        }
    verification = verify_private_runner_attestation(payload, signing_key)
    expected_request_hash = canonical_json_hash({
        key: request[key] for key in request if key != "attestation"
    })
    binding_valid = (
        payload.get("requestId") == request["requestId"]
        and payload.get("missionId") == request["missionId"]
        and payload.get("taskId") == request["taskId"]
        and payload.get("phase") == request["phase"]
        and payload.get("requestHash") == expected_request_hash
    )
    verification["requestBindingValid"] = binding_valid
    verification["expectedRequestHash"] = expected_request_hash
    if verification.get("valid") is not True or not binding_valid:
        return {
            **payload,
            "ok": False,
            "status": "blocked",
            "error": "private-runner result attestation or request binding is missing or invalid",
            "attestationVerification": verification,
            "runnerCommand": runner,
        }
    return {
        **payload,
        "ok": completed.returncode == 0 and payload.get("ok") is True,
        "attestationVerification": verification,
        "runnerCommand": runner,
    }


def delivery_git_state(repo: Path | None) -> dict[str, Any]:
    if repo is None:
        return {"ok": False, "error": "repositoryPath is not bound"}
    if not repo.exists() or not repo.is_dir():
        return {"ok": False, "error": "repositoryPath does not exist or is not a directory", "repositoryPath": str(repo)}
    if not (repo / ".git").exists():
        probe = run_delivery_subprocess("git rev-parse --show-toplevel", repo, timeout=15)
        if not probe.get("ok"):
            return {"ok": False, "error": "repositoryPath is not a git worktree", "repositoryPath": str(repo)}
    branch = run_delivery_subprocess("git rev-parse --abbrev-ref HEAD", repo, timeout=15)
    status = run_delivery_subprocess("git status --short", repo, timeout=15)
    diff = run_delivery_subprocess("git diff --stat", repo, timeout=15)
    return {
        "ok": branch.get("ok") is True and status.get("ok") is True,
        "repositoryPath": str(repo),
        "branch": (branch.get("stdout") or "").strip(),
        "statusShort": status.get("stdout") or "",
        "diffStat": diff.get("stdout") or "",
    }


def delivery_change_set(repo: Path | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if repo is None:
        return {"ok": False, "error": "repositoryPath is not bound", "files": [], "patchHash": ""}
    state = delivery_git_state(repo)
    if state.get("ok") is not True:
        return {"ok": False, "error": state.get("error") or "git state unavailable", "files": [], "gitState": state, "patchHash": ""}
    status = run_git_command(["status", "--porcelain=v1"], repo, timeout=30)
    stat = run_git_command(["diff", "--stat"], repo, timeout=30)
    patch = run_git_command(["diff", "--no-ext-diff", "--unified=3"], repo, timeout=30)
    raw_names = str(status.get("stdout") or "").splitlines()
    files = []
    for line in raw_names:
        if not line:
            continue
        files.append({"status": line[:2].strip() or "modified", "path": line[3:].strip()})
    patch_text = str(patch.get("stdout") or "")
    return {
        "ok": True,
        "repositoryPath": str(repo),
        "branch": state.get("branch") or "",
        "baseBranch": (context or {}).get("baseBranch") or (context or {}).get("branch") or "",
        "files": files,
        "fileCount": len(files),
        "diffStat": stat.get("stdout") or "",
        "patchHash": hashlib.sha256(patch_text.encode()).hexdigest() if patch_text else "",
        "patchPreview": limited_text(patch_text, limit=16000),
        "gitState": state,
    }


def delivery_adapter_manifest(phase: str, context: dict[str, Any], workspace: dict[str, Any], command: str) -> dict[str, Any]:
    pr_provider = str(context.get("prProvider") or "github")
    ci_provider = str(context.get("ciProvider") or "github-actions")
    deploy_provider = str(context.get("deployProvider") or "manual")
    adapter_kind = {
        "modify": "command.modify" if command else "git.change-readiness",
        "build": "command.build",
        "test": "command.test",
        "inspect": "command.inspect",
        "repair": "command.repair" if command else "git.change-readiness",
        "pr": "github.pr" if pr_provider == "github" else "command.pr" if command else "pr.readiness",
        "deploy": "sites.deploy" if deploy_provider == "sites" else "deploy.command" if command else "deploy.readiness",
    }.get(phase, "delivery.readiness")
    mutating = phase in {"modify", "repair", "pr", "deploy"}
    return {
        "schemaVersion": 1,
        "kind": adapter_kind,
        "provider": {"pr": pr_provider, "inspect": ci_provider, "deploy": deploy_provider}.get(phase, ""),
        "phase": phase,
        "mode": context.get("worktreeMode") or "in-place",
        "executesCommand": bool(command),
        "mutatesWorkspace": bool(command and mutating),
        "requiresApproval": phase in {"modify", "build", "test", "inspect", "repair", "pr", "deploy"},
        "sourceRepositoryPath": workspace.get("sourceRepositoryPath") or context.get("repositoryPath") or "",
        "workspacePath": workspace.get("path") or "",
        "baseBranch": workspace.get("baseBranch") or context.get("baseBranch") or context.get("branch") or "",
        "deliveryBranch": workspace.get("deliveryBranch") or context.get("deliveryBranch") or "",
        "evidence": ["command-result", "git-state", "change-set"],
    }


def github_repo_from_remote(repo: Path | None) -> str:
    if repo is None:
        return ""
    result = run_git_command(["config", "--get", "remote.origin.url"], repo, timeout=15)
    remote = str(result.get("stdout") or "").strip()
    if not remote:
        return ""
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s.]+)(?:\.git)?$", remote)
    return match.group(1) if match else ""


def provider_tool_state(tool_name: str) -> dict[str, Any]:
    path = shutil.which(tool_name)
    payload: dict[str, Any] = {"tool": tool_name, "available": bool(path), "path": path or ""}
    if tool_name == "gh" and path:
        status = subprocess.run([path, "auth", "status"], text=True, capture_output=True, check=False, timeout=20)
        payload.update({
            "authenticated": status.returncode == 0,
            "status": "ready" if status.returncode == 0 else "not_authenticated",
            "stdout": limited_text(status.stdout or "", limit=2000),
            "stderr": limited_text(status.stderr or "", limit=2000),
        })
    return payload


def github_pr_adapter(record: dict[str, Any], workspace: dict[str, Any], change_set: dict[str, Any], repo: Path | None, mock: bool) -> dict[str, Any]:
    context = delivery_context_summary(record)
    target = context.get("githubRepository") or context.get("prTarget") or github_repo_from_remote(repo)
    head = workspace.get("branch") or workspace.get("deliveryBranch") or ""
    base = context.get("baseBranch") or context.get("branch") or workspace.get("baseBranch") or ""
    title = context.get("prTitle") or f"{record.get('missionId')}: {record.get('objective') or 'Delivery mission'}"
    body = context.get("prBody") or (
        f"Mission: {record.get('missionId')}\n\n"
        f"Objective: {record.get('objective') or record.get('question') or ''}\n\n"
        "Proof bundle and delivery report are attached to the mission record."
    )
    mode = context.get("prMode") or "readiness"
    command = ["gh", "pr", "create"]
    if target:
        command += ["--repo", target]
    if base:
        command += ["--base", base]
    if head:
        command += ["--head", head]
    command += ["--title", title[:300], "--body", body[:2000]]
    if mode == "draft":
        command.append("--draft")
    gh_path = shutil.which("gh")
    tool = {"tool": "gh", "available": bool(gh_path), "path": gh_path or ""}
    if mode != "readiness":
        tool = provider_tool_state("gh")
    blockers = []
    if not target:
        blockers.append("github repository is not configured and could not be inferred from origin")
    if not head:
        blockers.append("head branch is unavailable")
    payload: dict[str, Any] = {
        "provider": "github",
        "mode": mode,
        "target": target,
        "title": title[:300],
        "body": body[:2000],
        "headBranch": head,
        "baseBranch": base,
        "fileCount": change_set.get("fileCount") or 0,
        "tool": tool,
        "commandPreview": command,
        "pushBeforePr": bool(context.get("pushBeforePr")),
        "ok": not blockers,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
    }
    if blockers or mode == "readiness":
        return payload
    if not tool.get("available") or not tool.get("authenticated"):
        return {**payload, "ok": False, "status": "blocked", "blockers": [*blockers, "GitHub CLI is not ready"]}
    if repo is None:
        return {**payload, "ok": False, "status": "blocked", "blockers": [*blockers, "workspace is unavailable"]}
    if mock:
        return {**payload, "status": "mocked", "url": "mock://github-pr"}
    if context.get("pushBeforePr"):
        push = run_delivery_private_runner("git push -u origin HEAD", repo, record, "pr", timeout=120)
        payload["pushResult"] = push
        if push.get("ok") is not True:
            return {**payload, "ok": False, "status": "failed", "blockers": [*blockers, "git push failed"]}
    result = run_delivery_private_runner(shlex.join(command), repo, record, "pr", timeout=120)
    url = str(result.get("stdout") or "").strip().splitlines()[-1] if str(result.get("stdout") or "").strip() else ""
    return {
        **payload,
        "ok": result.get("ok") is True,
        "status": result.get("status") or "failed",
        "exitCode": result.get("exitCode"),
        "stdout": limited_text(str(result.get("stdout") or "")),
        "stderr": limited_text(str(result.get("stderr") or "")),
        "privateRunner": result,
        "url": url,
    }


def github_actions_ci_adapter(record: dict[str, Any], workspace: dict[str, Any], repo: Path | None, mock: bool) -> dict[str, Any]:
    context = delivery_context_summary(record)
    provider = context.get("ciProvider") or "github-actions"
    if provider == "none":
        return {"provider": "none", "status": "disabled", "ok": True, "required": False}
    if provider == "manual":
        return {"provider": "manual", "status": "ready", "ok": not context.get("ciRequired"), "required": bool(context.get("ciRequired"))}
    if provider == "command" and context.get("ciCommand") and repo is not None:
        result = {"ok": True, "status": "mocked", "command": context.get("ciCommand")} if mock else run_delivery_private_runner(str(context.get("ciCommand") or ""), repo, record, "inspect")
        return {"provider": "command", "status": result.get("status"), "ok": result.get("ok") is True, "required": bool(context.get("ciRequired")), "commandResult": result}
    target = context.get("githubRepository") or context.get("prTarget") or github_repo_from_remote(repo)
    branch = workspace.get("branch") or workspace.get("deliveryBranch") or context.get("branch") or ""
    payload: dict[str, Any] = {
        "provider": "github-actions",
        "target": target,
        "branch": branch,
        "required": bool(context.get("ciRequired")),
        "wait": bool(context.get("ciWait")),
        "tool": {"tool": "gh", "available": bool(shutil.which("gh")), "path": shutil.which("gh") or ""},
        "runs": [],
        "ok": not bool(context.get("ciRequired")),
        "status": "observed",
    }
    if not context.get("ciRequired") and not context.get("ciWait"):
        return payload
    tool = provider_tool_state("gh")
    payload["tool"] = tool
    if not target or not branch:
        return {**payload, "ok": False, "status": "blocked", "blockers": ["GitHub repository or branch is unavailable"]}
    if not tool.get("available") or not tool.get("authenticated"):
        return {**payload, "ok": False, "status": "blocked", "blockers": ["GitHub CLI is not ready"]}
    if mock:
        return {**payload, "ok": True, "status": "mocked", "runs": [{"name": "mock-ci", "status": "completed", "conclusion": "success"}]}
    result = run_delivery_private_runner(
        shlex.join(["gh", "run", "list", "--repo", target, "--branch", branch, "--limit", "5", "--json", "databaseId,name,status,conclusion,url,createdAt"]),
        repo or WORKER_DIR,
        record,
        "inspect",
        timeout=60,
    )
    runs: list[dict[str, Any]] = []
    try:
        parsed = json.loads(str(result.get("stdout") or "[]"))
        if isinstance(parsed, list):
            runs = [item for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError:
        runs = []
    ok = any(run.get("status") == "completed" and run.get("conclusion") == "success" for run in runs)
    return {
        **payload,
        "ok": ok if context.get("ciRequired") else result.get("ok") is True,
        "status": "succeeded" if ok else "blocked" if context.get("ciRequired") else "observed",
        "exitCode": result.get("exitCode"),
        "stdout": limited_text(str(result.get("stdout") or "")),
        "stderr": limited_text(str(result.get("stderr") or "")),
        "privateRunner": result,
        "runs": runs,
    }


def delivery_pr_readiness(record: dict[str, Any], workspace: dict[str, Any], change_set: dict[str, Any]) -> dict[str, Any]:
    context = delivery_context_summary(record)
    repo = Path(str(workspace.get("path"))).expanduser().resolve() if workspace.get("path") else None
    provider = context.get("prProvider") or "github"
    github = github_pr_adapter(record, workspace, change_set, repo, bool(record.get("mock"))) if provider == "github" else {}
    payload = {
        "provider": provider,
        "target": context.get("prTarget") or "",
        "githubRepository": context.get("githubRepository") or github.get("target") or "",
        "title": github.get("title") or "",
        "body": github.get("body") or "",
        "headBranch": workspace.get("branch") or workspace.get("deliveryBranch") or "",
        "baseBranch": workspace.get("baseBranch") or context.get("baseBranch") or context.get("branch") or "",
        "fileCount": change_set.get("fileCount") or 0,
        "commandConfigured": bool(context.get("prCommand")),
        "github": github,
    }
    return payload


def delivery_deploy_readiness(record: dict[str, Any], workspace: dict[str, Any], change_set: dict[str, Any]) -> dict[str, Any]:
    context = delivery_context_summary(record)
    provider = context.get("deployProvider") or "manual"
    hosting_config = Path(str(workspace.get("path") or "")) / ".openai" / "hosting.json" if workspace.get("path") else None
    payload = {
        "provider": provider,
        "target": context.get("deployTarget") or "",
        "environment": context.get("deployEnvironment") or "",
        "url": context.get("deployUrl") or "",
        "workspacePath": workspace.get("path") or "",
        "branch": workspace.get("branch") or "",
        "fileCount": change_set.get("fileCount") or 0,
        "commandConfigured": bool(context.get("deployCommand")),
        "healthCommandConfigured": bool(context.get("deployHealthCommand")),
        "rollbackConfigured": bool(context.get("rollbackCommand")),
        "sites": {
            "hostingConfigPath": str(hosting_config) if hosting_config else "",
            "hostingConfigPresent": bool(hosting_config and hosting_config.exists()),
        } if provider == "sites" else {},
    }
    return payload


def read_mission_evidence_artifacts(record: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    refs = record.get("evidenceLedger") if isinstance(record.get("evidenceLedger"), list) else []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            continue
        try:
            payload = json.loads(Path(ref["path"]).read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            artifacts.append(payload)
    return artifacts


def delivery_evidence_by_phase(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        if artifact.get("kind") != "delivery-step":
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        phase = str(payload.get("phase") or "").strip()
        if phase:
            by_phase.setdefault(phase, []).append(artifact)
    return by_phase


def delivery_closure_gate(record: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    if record.get("templateId") != "delivery-execution":
        return {"status": "not_delivery", "ok": True, "blockers": [], "readyForPr": False, "readyForDeploy": False}
    by_phase = delivery_evidence_by_phase(artifacts)
    required = ["modify", "build", "test", "inspect", "repair", "pr", "deploy"]
    blockers: list[str] = []
    stage_status: dict[str, str] = {}
    for phase in required:
        entries = by_phase.get(phase) or []
        if not entries:
            blockers.append(f"{phase} evidence is missing")
            stage_status[phase] = "missing"
            continue
        payload = entries[-1].get("payload") if isinstance(entries[-1].get("payload"), dict) else {}
        status = str(payload.get("status") or "unknown")
        stage_status[phase] = status
        if payload.get("ok") is not True:
            detail = ", ".join(payload.get("blockers") or []) if isinstance(payload.get("blockers"), list) else status
            blockers.append(f"{phase} did not pass: {detail or status}")
    context = delivery_context_summary(record)
    workspace = record.get("deliveryWorkspace") if isinstance(record.get("deliveryWorkspace"), dict) else {}
    repo = Path(str(workspace.get("path"))).expanduser().resolve() if workspace.get("path") else delivery_repo_path(context)
    git_state = delivery_git_state(repo) if repo else {"ok": False, "error": "repositoryPath is not bound"}
    if git_state.get("ok") is not True:
        blockers.append(str(git_state.get("error") or "repository git state is unavailable"))
    status = "deployed"
    if blockers:
        status = "blocked"
    elif not context.get("prCommand") and not context.get("prTarget"):
        status = "ready_for_pr"
    elif not context.get("deployCommand") or not context.get("deployTarget"):
        status = "ready_for_deploy"
    return {
        "status": status,
        "ok": not blockers,
        "blockers": blockers,
        "stageStatus": stage_status,
        "readyForPr": not blockers and status in {"ready_for_pr", "ready_for_deploy", "deployed"},
        "readyForDeploy": not blockers and status in {"ready_for_deploy", "deployed"},
        "gitState": git_state,
        "repairAttempts": int(record.get("repairAttemptsUsed") or 0),
        "repairBudget": int(context.get("repairBudget") or 0),
    }


def delivery_payloads_by_phase(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.get("kind") not in {"delivery-step", "delivery-repair-attempt"}:
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        phase = str(payload.get("phase") or "").strip()
        if phase:
            latest[phase] = payload
    return latest


def delivery_proof_adapter_manifest(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for artifact in artifacts:
        if artifact.get("kind") != "delivery-step":
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        adapter = payload.get("adapter") if isinstance(payload.get("adapter"), dict) else {}
        if not adapter:
            continue
        manifest.append({
            "phase": payload.get("phase") or artifact.get("nodeId"),
            "kind": adapter.get("kind") or "",
            "mode": adapter.get("mode") or "",
            "executesCommand": bool(adapter.get("executesCommand")),
            "mutatesWorkspace": bool(adapter.get("mutatesWorkspace")),
            "workspacePath": adapter.get("workspacePath") or "",
        })
    return manifest


def delivery_control_decisions(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for artifact in artifacts:
        if artifact.get("kind") not in {"delivery-step", "delivery-repair-attempt"}:
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
        if not preflight:
            continue
        risk = preflight.get("risk") if isinstance(preflight.get("risk"), dict) else {}
        decisions.append({
            "phase": payload.get("phase") or preflight.get("phase") or artifact.get("nodeId"),
            "decision": preflight.get("decision") or "",
            "risk": risk.get("level") or "",
            "approvalSatisfied": bool(preflight.get("approvalSatisfied")),
            "modelIndependent": bool(preflight.get("modelIndependent")),
            "customerControlled": bool(preflight.get("customerControlled")),
            "policyHash": preflight.get("policyHash") or "",
            "blockers": preflight.get("blockers") if isinstance(preflight.get("blockers"), list) else [],
            "evidenceId": artifact.get("evidenceId") or "",
        })
    return decisions


def delivery_compliance_evidence(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = delivery_control_decisions(artifacts)
    blocked = [item for item in decisions if item.get("decision") == "block"]
    approval_required = [item for item in decisions if item.get("decision") == "require_approval"]
    return {
        **compliance_evidence(),
        "controlDecisionCount": len(decisions),
        "blockedBeforeExecutionCount": len(blocked),
        "approvalRequiredCount": len(approval_required),
        "controlMatrix": compliance_control_matrix(decisions),
        "decisionEvidence": decisions,
    }


def mission_integrity_chain(mission_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in mission_integrity_path(mission_id).read_text(errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    except OSError:
        return []
    return rows[-limit:] if limit > 0 else rows


def delivery_proof_change_set(record: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    by_phase = delivery_payloads_by_phase(artifacts)
    for phase in ["deploy", "pr", "repair", "inspect", "test", "build", "modify"]:
        payload = by_phase.get(phase) or {}
        change_set = payload.get("changeSet") if isinstance(payload.get("changeSet"), dict) else {}
        if change_set:
            return change_set
    workspace = record.get("deliveryWorkspace") if isinstance(record.get("deliveryWorkspace"), dict) else {}
    repo = Path(str(workspace.get("path"))).expanduser().resolve() if workspace.get("path") else delivery_repo_path(delivery_context_summary(record))
    return delivery_change_set(repo, delivery_context_summary(record))


def delivery_proof_bundle_payload(record: dict[str, Any]) -> dict[str, Any]:
    artifacts = read_mission_evidence_artifacts(record)
    gate = delivery_closure_gate(record, artifacts)
    by_phase = delivery_payloads_by_phase(artifacts)
    change_set = delivery_proof_change_set(record, artifacts)
    return {
        "schemaVersion": 1,
        "missionId": record.get("missionId"),
        "templateId": record.get("templateId"),
        "objective": record.get("objective") or record.get("question") or "",
        "profile": record.get("profile"),
        "operatorRole": record.get("operatorRole"),
        "deliveryContext": delivery_context_summary(record),
        "missionUsers": record.get("missionUsers") if isinstance(record.get("missionUsers"), list) else [],
        "capabilityWorkSet": record.get("capabilityWorkSet") if isinstance(record.get("capabilityWorkSet"), list) else [],
        "snapshotPolicySummary": record.get("snapshotPolicySummary") if isinstance(record.get("snapshotPolicySummary"), dict) else {},
        "deliveryWorkspace": record.get("deliveryWorkspace") if isinstance(record.get("deliveryWorkspace"), dict) else {},
        "adapterManifest": delivery_proof_adapter_manifest(artifacts),
        "controlDecisions": delivery_control_decisions(artifacts),
        "complianceEvidence": delivery_compliance_evidence(artifacts),
        "integrityChain": {
            "path": str(mission_integrity_path(str(record.get("missionId") or ""))),
            "recordCount": len(mission_integrity_chain(str(record.get("missionId") or ""), limit=0)),
            "latestHash": latest_integrity_hash(str(record.get("missionId") or "")),
            "signatureScheme": "hmac-sha256-local-alpha",
        },
        "changeSet": {
            key: value for key, value in change_set.items()
            if key not in {"patchPreview"}
        } if isinstance(change_set, dict) else {},
        "prReadiness": by_phase.get("pr", {}).get("prReadiness") if isinstance(by_phase.get("pr"), dict) else {},
        "ciReadiness": by_phase.get("pr", {}).get("ciReadiness") if isinstance(by_phase.get("pr"), dict) else {},
        "deployReadiness": by_phase.get("deploy", {}).get("deployReadiness") if isinstance(by_phase.get("deploy"), dict) else {},
        "closureGate": gate,
        "evidence": [
            {
                "evidenceId": artifact.get("evidenceId"),
                "nodeId": artifact.get("nodeId"),
                "kind": artifact.get("kind"),
                "producedAt": artifact.get("producedAt"),
                "payloadHash": canonical_json_hash(artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}),
                "integrity": artifact.get("integrity") if isinstance(artifact.get("integrity"), dict) else {},
            }
            for artifact in artifacts
        ],
        "auditCount": int(record.get("auditCount") or 0),
        "producedAt": utc_now(),
        "producer": "steel-mission-chat delivery-closure",
    }


def write_delivery_proof_bundle(record: dict[str, Any]) -> dict[str, Any]:
    mission_id = str(record.get("missionId") or "")
    task_id = str(record.get("taskId") or "") or None
    job_id = str(record.get("jobId") or "") or None
    operator = corporate_role(str(record.get("operatorRole") or "user"))
    payload = delivery_proof_bundle_payload(record)
    ref = write_mission_evidence(
        mission_id,
        "delivery-proof",
        "delivery-proof-bundle",
        payload,
        task_id=task_id,
        job_id=job_id,
        operator_role=operator,
        summary=f"Recorded delivery proof bundle: {payload['closureGate']['status']}.",
    )
    update_mission(
        mission_id,
        deliveryClosure=payload["closureGate"],
        deliveryProofRef=ref,
        deliveryProofProducedAt=payload["producedAt"],
    )
    try:
        proof_artifact = json.loads(Path(ref["path"]).read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        proof_artifact = {"payload": payload}
    report_path = mission_evidence_dir(mission_id) / "delivery-report.md"
    report_text = delivery_report_markdown_from_proof(proof_artifact)
    atomic_write_text(report_path, report_text)
    report_ref = {
        "kind": "delivery-report",
        "path": str(report_path),
        "sha256": file_sha256(report_path) or "",
    }
    pack_ref = write_delivery_proof_pack(record, ref, report_ref, proof_artifact)
    update_mission(mission_id, deliveryReportRef=report_ref, deliveryProofPackRef=pack_ref)
    append_mission_audit(
        mission_id,
        "delivery-proof-recorded",
        task_id=task_id,
        job_id=job_id,
        actor="mission-orchestrator",
        operator_role=operator,
        summary=f"Delivery proof bundle recorded with closure status {payload['closureGate']['status']}.",
        details=payload["closureGate"],
        artifact_refs=[ref, report_ref, pack_ref],
    )
    return ref


def write_delivery_proof_pack(
    record: dict[str, Any],
    proof_ref: dict[str, Any],
    report_ref: dict[str, Any],
    proof_artifact: dict[str, Any],
) -> dict[str, Any]:
    mission_id = str(record.get("missionId") or "")
    evidence_dir = mission_evidence_dir(mission_id)
    pack_path = evidence_dir / "delivery-proof-pack.zip"
    artifacts = read_mission_evidence_artifacts(record)
    change_set = delivery_proof_change_set(record, artifacts)
    patch_path = evidence_dir / "changes.patch"
    atomic_write_text(patch_path, str(change_set.get("patchPreview") or ""))
    siem_path = evidence_dir / "siem-events.jsonl"
    siem_payload = mission_siem_jsonl(mission_id, corporate_role(str(record.get("operatorRole") or "user")))
    atomic_write_text(siem_path, str(siem_payload.get("jsonl") or ""))
    siem_ref = {"kind": "siem-jsonl", "path": str(siem_path), "sha256": file_sha256(siem_path) or ""}
    manifest = {
        "schemaVersion": 1,
        "missionId": mission_id,
        "producer": "steel-mission-chat delivery-proof-pack",
        "producedAt": utc_now(),
        "proof": proof_ref,
        "report": report_ref,
        "patch": {"kind": "patch", "path": str(patch_path), "sha256": file_sha256(patch_path) or ""},
        "siem": siem_ref,
        "evidenceCount": len(record.get("evidenceLedger") if isinstance(record.get("evidenceLedger"), list) else []),
    }
    manifest_path = evidence_dir / "proof-pack-manifest.json"
    atomic_write_json(manifest_path, manifest)
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, arcname in [
            (Path(str(proof_ref.get("path") or "")), "proof.json"),
            (Path(str(report_ref.get("path") or "")), "delivery-report.md"),
            (patch_path, "changes.patch"),
            (manifest_path, "proof-pack-manifest.json"),
        ]:
            if source.exists():
                archive.write(source, arcname)
        if siem_path.exists():
            archive.write(siem_path, "siem-events.jsonl")
        refs = record.get("evidenceLedger") if isinstance(record.get("evidenceLedger"), list) else []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                continue
            source = Path(ref["path"])
            if source.exists() and source != pack_path:
                archive.write(source, f"evidence/{source.name}")
    return {"kind": "delivery-proof-pack", "path": str(pack_path), "sha256": file_sha256(pack_path) or ""}


def mission_proof_bundle(mission_id: str, role: str = "user") -> dict[str, Any]:
    detail = mission_detail(mission_id, role)
    if not detail.get("ok"):
        return detail
    mission = detail.get("mission") if isinstance(detail.get("mission"), dict) else {}
    proof_ref = mission.get("deliveryProofRef") if isinstance(mission.get("deliveryProofRef"), dict) else {}
    proof_path = proof_ref.get("path")
    if not isinstance(proof_path, str):
        return {"ok": False, "error": "delivery proof is not available"}
    try:
        proof = json.loads(Path(proof_path).read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "role": corporate_role(role), "missionId": mission_id, "proof": proof, "proofRef": proof_ref}


def mission_siem_events(mission_id: str, role: str = "user") -> dict[str, Any]:
    detail = mission_detail(mission_id, role)
    if not detail.get("ok"):
        return detail
    mission = detail.get("mission") if isinstance(detail.get("mission"), dict) else {}
    artifacts = read_mission_evidence_artifacts(mission)
    events: list[dict[str, Any]] = []
    for event in mission.get("audit", []) if isinstance(mission.get("audit"), list) else []:
        if not isinstance(event, dict):
            continue
        events.append({
            "schemaVersion": 1,
            "stream": "present.control-plane.audit",
            "eventType": "audit",
            "missionId": mission_id,
            "producedAt": event.get("producedAt") or "",
            "action": event.get("action") or "",
            "actor": event.get("actor") or "",
            "operatorRole": event.get("operatorRole") or "",
            "summary": event.get("summary") or "",
            "details": event.get("details") if isinstance(event.get("details"), dict) else {},
            "artifactRefs": event.get("artifactRefs") if isinstance(event.get("artifactRefs"), list) else [],
            "integrity": event.get("integrity") if isinstance(event.get("integrity"), dict) else {},
        })
    for artifact in artifacts:
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        events.append({
            "schemaVersion": 1,
            "stream": "present.control-plane.evidence",
            "eventType": "evidence",
            "missionId": mission_id,
            "producedAt": artifact.get("producedAt") or "",
            "evidenceId": artifact.get("evidenceId") or "",
            "nodeId": artifact.get("nodeId") or "",
            "kind": artifact.get("kind") or "",
            "payloadHash": canonical_json_hash(payload),
            "integrity": artifact.get("integrity") if isinstance(artifact.get("integrity"), dict) else {},
        })
        preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
        if preflight:
            risk = preflight.get("risk") if isinstance(preflight.get("risk"), dict) else {}
            events.append({
                "schemaVersion": 1,
                "stream": "present.control-plane.decision",
                "eventType": "control-decision",
                "missionId": mission_id,
                "producedAt": artifact.get("producedAt") or "",
                "evidenceId": artifact.get("evidenceId") or "",
                "phase": preflight.get("phase") or payload.get("phase") or "",
                "decision": preflight.get("decision") or "",
                "risk": risk.get("level") or preflight.get("risk") or "",
                "approvalSatisfied": bool(preflight.get("approvalSatisfied")),
                "modelIndependent": bool(preflight.get("modelIndependent")),
                "customerControlled": bool(preflight.get("customerControlled")),
                "policyHash": preflight.get("policyHash") or "",
                "blockers": preflight.get("blockers") if isinstance(preflight.get("blockers"), list) else [],
            })
    for item in mission_integrity_chain(mission_id, limit=0):
        if isinstance(item, dict):
            events.append({
                "schemaVersion": 1,
                "stream": "present.control-plane.integrity",
                "eventType": "integrity",
                "missionId": mission_id,
                "producedAt": item.get("producedAt") or "",
                "recordKind": item.get("recordKind") or "",
                "payloadHash": item.get("payloadHash") or "",
                "previousHash": item.get("previousHash") or "",
                "chainHash": item.get("chainHash") or "",
                "signatureScheme": item.get("signatureScheme") or "",
            })
    events.sort(key=lambda item: str(item.get("producedAt") or ""))
    return {
        "ok": True,
        "role": corporate_role(role),
        "missionId": mission_id,
        "stream": "present.control-plane.siem",
        "eventCount": len(events),
        "events": events,
    }


def mission_siem_jsonl(mission_id: str, role: str = "user") -> dict[str, Any]:
    payload = mission_siem_events(mission_id, role)
    if not payload.get("ok"):
        return payload
    lines = [
        json.dumps(item, sort_keys=True, separators=(",", ":"))
        for item in payload.get("events", [])
        if isinstance(item, dict)
    ]
    return {**payload, "jsonl": "\n".join(lines) + ("\n" if lines else "")}


def delivery_report_markdown_from_proof(proof_artifact: dict[str, Any]) -> str:
    payload = proof_artifact.get("payload") if isinstance(proof_artifact.get("payload"), dict) else {}
    closure = payload.get("closureGate") if isinstance(payload.get("closureGate"), dict) else {}
    context = payload.get("deliveryContext") if isinstance(payload.get("deliveryContext"), dict) else {}
    workspace = payload.get("deliveryWorkspace") if isinstance(payload.get("deliveryWorkspace"), dict) else {}
    change_set = payload.get("changeSet") if isinstance(payload.get("changeSet"), dict) else {}
    adapters = payload.get("adapterManifest") if isinstance(payload.get("adapterManifest"), list) else []
    pr_readiness = payload.get("prReadiness") if isinstance(payload.get("prReadiness"), dict) else {}
    ci_readiness = payload.get("ciReadiness") if isinstance(payload.get("ciReadiness"), dict) else {}
    deploy_readiness = payload.get("deployReadiness") if isinstance(payload.get("deployReadiness"), dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    control_decisions = payload.get("controlDecisions") if isinstance(payload.get("controlDecisions"), list) else []
    compliance = payload.get("complianceEvidence") if isinstance(payload.get("complianceEvidence"), dict) else {}
    integrity = payload.get("integrityChain") if isinstance(payload.get("integrityChain"), dict) else {}
    blockers = closure.get("blockers") if isinstance(closure.get("blockers"), list) else []
    lines = [
        "# Agentic Software Delivery Proof",
        "",
        f"- Mission: `{payload.get('missionId') or proof_artifact.get('missionId') or ''}`",
        f"- Objective: {payload.get('objective') or ''}",
        f"- Closure: `{closure.get('status') or 'unknown'}`",
        f"- Repository: `{context.get('repositoryPath') or ''}`",
        f"- Branch: `{context.get('branch') or ''}`",
        f"- Workspace mode: `{context.get('worktreeMode') or 'in-place'}`",
        f"- Workspace path: `{workspace.get('path') or context.get('repositoryPath') or ''}`",
        f"- Profile: `{payload.get('profile') or ''}`",
        f"- Snapshot policy: `{(payload.get('snapshotPolicySummary') or {}).get('policyHash') or ''}`",
        f"- Repair usage: `{closure.get('repairAttempts') or 0}/{closure.get('repairBudget') or 0}`",
        f"- Integrity chain: `{integrity.get('recordCount') or 0}` records · `{integrity.get('latestHash') or ''}`",
        "",
        "## Gate",
        "",
        f"- Ready for PR: `{bool(closure.get('readyForPr'))}`",
        f"- Ready for deploy: `{bool(closure.get('readyForDeploy'))}`",
        f"- OK: `{bool(closure.get('ok'))}`",
    ]
    if blockers:
        lines += ["", "## Blockers", ""]
        lines.extend(f"- {item}" for item in blockers)
    lines += ["", "## Change Set", ""]
    lines.extend([
        f"- Branch: `{change_set.get('branch') or ''}`",
        f"- Files changed: `{change_set.get('fileCount') or 0}`",
        f"- Patch hash: `{change_set.get('patchHash') or ''}`",
    ])
    files = change_set.get("files") if isinstance(change_set.get("files"), list) else []
    for item in files[:20]:
        if isinstance(item, dict):
            lines.append(f"- `{item.get('status') or ''}` {item.get('path') or ''}")
    lines += ["", "## Adapters", ""]
    for item in adapters:
        if isinstance(item, dict):
            lines.append(
                f"- `{item.get('phase')}` · `{item.get('kind')}` · mode `{item.get('mode')}` · command `{bool(item.get('executesCommand'))}`"
            )
    lines += ["", "## Pre-Execution Controls", ""]
    for item in control_decisions:
        if isinstance(item, dict):
            blockers_text = "; ".join(str(blocker) for blocker in item.get("blockers", []) if blocker) if isinstance(item.get("blockers"), list) else ""
            lines.append(
                f"- `{item.get('phase')}` · decision `{item.get('decision')}` · risk `{item.get('risk')}` · approved `{bool(item.get('approvalSatisfied'))}`"
                + (f" · {blockers_text}" if blockers_text else "")
            )
    standards = compliance.get("standards") if isinstance(compliance.get("standards"), dict) else {}
    control_matrix = compliance.get("controlMatrix") if isinstance(compliance.get("controlMatrix"), list) else []
    lines += ["", "## Compliance Evidence", ""]
    lines.extend([
        f"- Control decisions: `{compliance.get('controlDecisionCount') or 0}`",
        f"- Blocked before execution: `{compliance.get('blockedBeforeExecutionCount') or 0}`",
        f"- Approval gates: `{compliance.get('approvalRequiredCount') or 0}`",
    ])
    for standard, controls in standards.items():
        if isinstance(controls, list):
            lines.append(f"- {standard}: `{', '.join(str(item) for item in controls)}`")
    for row in control_matrix:
        if isinstance(row, dict):
            lines.append(
                f"- Matrix `{row.get('standard')}` · controls `{', '.join(str(item) for item in row.get('controls', []) if item)}` · evidence `{row.get('evidenceCount') or 0}`"
            )
    lines += ["", "## PR Readiness", ""]
    lines.extend([
        f"- Provider: `{pr_readiness.get('provider') or ''}`",
        f"- Target: `{pr_readiness.get('target') or ''}`",
        f"- Head branch: `{pr_readiness.get('headBranch') or ''}`",
        f"- Base branch: `{pr_readiness.get('baseBranch') or ''}`",
        f"- Command configured: `{bool(pr_readiness.get('commandConfigured'))}`",
    ])
    lines += ["", "## CI Readiness", ""]
    lines.extend([
        f"- Provider: `{ci_readiness.get('provider') or ''}`",
        f"- Required: `{bool(ci_readiness.get('required'))}`",
        f"- Status: `{ci_readiness.get('status') or ''}`",
        f"- OK: `{bool(ci_readiness.get('ok'))}`",
    ])
    lines += ["", "## Deploy Readiness", ""]
    lines.extend([
        f"- Provider: `{deploy_readiness.get('provider') or ''}`",
        f"- Target: `{deploy_readiness.get('target') or ''}`",
        f"- Environment: `{deploy_readiness.get('environment') or ''}`",
        f"- Command configured: `{bool(deploy_readiness.get('commandConfigured'))}`",
        f"- Health command configured: `{bool(deploy_readiness.get('healthCommandConfigured'))}`",
        f"- Rollback configured: `{bool(deploy_readiness.get('rollbackConfigured'))}`",
    ])
    lines += ["", "## Evidence", ""]
    for item in evidence:
        if isinstance(item, dict):
            lines.append(f"- `{item.get('kind')}` · `{item.get('nodeId')}` · `{item.get('payloadHash')}`")
    lines += ["", "Generated by `steel-mission-chat delivery-closure`."]
    return "\n".join(lines) + "\n"


def mission_report_markdown(mission_id: str, role: str = "user") -> dict[str, Any]:
    proof = mission_proof_bundle(mission_id, role)
    if not proof.get("ok"):
        return proof
    artifact = proof.get("proof") if isinstance(proof.get("proof"), dict) else {}
    return {"ok": True, "missionId": mission_id, "markdown": delivery_report_markdown_from_proof(artifact)}


def render_mission_detail_page(mission_id: str, role: str = "user") -> str:
    detail = mission_detail(mission_id, role)
    if not detail.get("ok"):
        return render_shell(f"<section class=\"panel\"><h2>Mission unavailable</h2><p>{escape_html(detail.get('error') or 'Mission not found')}</p></section>")
    mission = detail.get("mission") if isinstance(detail.get("mission"), dict) else {}
    closure = mission.get("deliveryClosure") if isinstance(mission.get("deliveryClosure"), dict) else {}
    nodes = mission.get("nodes") if isinstance(mission.get("nodes"), list) else []
    evidence = mission.get("evidenceLedger") if isinstance(mission.get("evidenceLedger"), list) else []
    audit = mission.get("audit") if isinstance(mission.get("audit"), list) else []
    proof_link = f"/api/missions/{mission_id}/proof?role={corporate_role(role)}"
    report_link = f"/api/missions/{mission_id}/report?role={corporate_role(role)}"
    siem_link = f"/api/missions/{mission_id}/siem?role={corporate_role(role)}"
    export_link = f"/api/missions/{mission_id}/export?role={corporate_role(role)}"
    node_html = "".join(
        f"<div class=\"item\"><b>{escape_html(node.get('title') or node.get('nodeId'))}</b> "
        f"<span class=\"badge\">{escape_html(node.get('state') or '')}</span>"
        f"<p class=\"meta\">{escape_html(node.get('kind') or '')} · {escape_html(node.get('capability') or '')}</p></div>"
        for node in nodes if isinstance(node, dict)
    )
    audit_html = "".join(
        f"<div class=\"item\"><b>{escape_html(item.get('action') or 'audit')}</b><p>{escape_html(item.get('summary') or '')}</p></div>"
        for item in audit[-12:] if isinstance(item, dict)
    )
    blockers = closure.get("blockers") if isinstance(closure.get("blockers"), list) else []
    blocker_html = "".join(f"<li>{escape_html(item)}</li>" for item in blockers) or "<li>None</li>"
    return render_shell(f"""
<section class="panel">
  <h2>{escape_html(mission_id)}</h2>
  <p class="meta">{escape_html(mission.get('templateTitle') or mission.get('templateId') or '')} · {escape_html(mission.get('state') or '')}</p>
  <p>{escape_html(mission.get('objective') or '')}</p>
  <p><span class="badge">closure {escape_html(closure.get('status') or 'pending')}</span></p>
  <p><a href="{escape_html(proof_link)}">Proof JSON</a> · <a href="{escape_html(report_link)}">Delivery report</a> · <a href="{escape_html(siem_link)}">SIEM JSONL</a> · <a href="{escape_html(export_link)}">Proof pack</a></p>
  <h3>Blockers</h3>
  <ul>{blocker_html}</ul>
  <h3>Nodes</h3>
  {node_html}
  <h3>Evidence</h3>
  <p class="meta">{len(evidence)} records</p>
  <h3>Audit</h3>
  {audit_html}
</section>""")


def delivery_step_payload(record: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    phase = str(node.get("phase") or node.get("nodeId") or "delivery").strip()
    context = delivery_context_summary(record)
    mock = bool(record.get("mock"))
    repair_attempts_used = int(record.get("repairAttemptsUsed") or 0)
    workspace = ensure_delivery_workspace(record)
    repo = Path(str(workspace.get("path"))).expanduser().resolve() if workspace.get("path") else None
    command_by_phase = {
        "modify": context.get("modifyCommand") or "",
        "build": context.get("buildCommand") or "",
        "test": context.get("testCommand") or "",
        "inspect": context.get("inspectCommand") or "",
        "repair": context.get("repairCommand") or "",
        "pr": context.get("prCommand") if context.get("prProvider") == "command" else "",
        "deploy": context.get("deployCommand") or "",
    }
    command = command_by_phase.get(phase, "")
    if phase == "repair" and repair_attempts_used > 0:
        command = ""
    adapter = delivery_adapter_manifest(phase, context, workspace, command)
    preflight = delivery_action_preflight(record, phase, str(command or ""), context, workspace)
    repo_bound = bool(repo and repo.exists() and repo.is_dir() and workspace.get("ok") is True)
    git_state = delivery_git_state(repo) if repo_bound else {}
    status = "ready"
    ok = True
    blockers: list[str] = []
    if phase in {"modify", "build", "test", "inspect", "repair", "pr", "deploy"} and not repo_bound:
        status = "blocked"
        ok = False
        blockers.append(str(workspace.get("error") or "repositoryPath is not bound"))
    if phase in {"build", "test", "inspect"} and not command:
        status = "planned" if status != "blocked" else status
        ok = False
        blockers.append(f"{phase}Command is not configured")
    expected_branch = str(
        workspace.get("deliveryBranch") if context.get("worktreeMode") == "isolated" else context.get("branch") or ""
    ).strip()
    actual_branch = str(git_state.get("branch") or "").strip() if isinstance(git_state, dict) else ""
    if not mock and expected_branch and git_state and (git_state.get("ok") is not True or actual_branch != expected_branch):
        status = "blocked"
        ok = False
        blockers.append(f"repository branch is {actual_branch or 'unknown'}, expected {expected_branch}")
    pr_target_available = bool(context.get("prTarget") or context.get("githubRepository"))
    if phase == "deploy" and not context.get("deployTarget"):
        status = "planned" if status != "blocked" else status
    if phase == "pr" and not pr_target_available:
        status = "planned" if status != "blocked" else status
    if phase in {"modify", "build", "test", "inspect", "repair", "pr", "deploy"} and preflight.get("ok") is not True:
        status = "blocked"
        ok = False
        blockers.extend(str(item) for item in preflight.get("blockers", []) if item)
    commandResult: dict[str, Any] = {}
    if phase in {"modify", "build", "test", "inspect", "repair", "pr", "deploy"} and repo_bound and command and status != "blocked":
        if mock:
            status = "mocked"
            ok = True
            commandResult = {"ok": True, "status": "mocked", "command": command}
        else:
            commandResult = run_delivery_private_runner(command, repo, record, phase)
            status = str(commandResult.get("status") or "failed")
            ok = commandResult.get("ok") is True
    elif phase in {"modify", "repair", "pr", "deploy"} and repo_bound and status != "blocked":
        ok = git_state.get("ok") is True
        status = "ready" if ok else "blocked"
    if ok and phase == "deploy" and context.get("deployHealthCommand") and repo_bound and not mock:
        health = run_delivery_private_runner(str(context.get("deployHealthCommand") or ""), repo, record, "deploy")
        commandResult = {**commandResult, "deployHealth": health}
        ok = health.get("ok") is True
        status = "succeeded" if ok else "failed"
        if not ok:
            blockers.append("deploy health command failed")
    change_set = delivery_change_set(repo, context) if repo_bound else {"ok": False, "files": [], "patchHash": ""}
    pr_readiness = delivery_pr_readiness(record, workspace, change_set) if phase == "pr" and status != "blocked" else {}
    deploy_readiness = delivery_deploy_readiness(record, workspace, change_set) if phase == "deploy" else {}
    ci_readiness = github_actions_ci_adapter(record, workspace, repo, mock) if phase == "pr" and repo_bound and status != "blocked" else {}
    if phase == "pr" and repo_bound:
        github_result = pr_readiness.get("github") if isinstance(pr_readiness.get("github"), dict) else {}
        if context.get("prProvider") == "github":
            ok = github_result.get("ok") is True
            status = str(github_result.get("status") or status)
            blockers.extend(str(item) for item in github_result.get("blockers", []) if item)
        if ci_readiness and ci_readiness.get("ok") is not True and context.get("ciRequired"):
            ok = False
            status = "blocked"
            blockers.extend(str(item) for item in ci_readiness.get("blockers", []) if item)
            if not ci_readiness.get("blockers"):
                blockers.append("required CI is not passing")
    return {
        "phase": phase,
        "status": status,
        "ok": ok,
        "executionMode": "alpha-control-plane",
        "adapter": {**adapter, "executesCommand": bool(command and commandResult)},
        "preflight": preflight,
        "deliveryContext": context,
        "deliveryWorkspace": workspace,
        "command": command,
        "commandResult": commandResult,
        "gitState": git_state,
        "changeSet": change_set,
        "prReadiness": pr_readiness,
        "ciReadiness": ci_readiness,
        "deployReadiness": deploy_readiness,
        "blockers": blockers,
        "repairAttemptsUsed": repair_attempts_used,
        "missionUsers": record.get("missionUsers") if isinstance(record.get("missionUsers"), list) else [],
        "capabilityWorkSet": record.get("capabilityWorkSet") if isinstance(record.get("capabilityWorkSet"), list) else [],
        "notes": [
            "Configured lifecycle commands run only when the mission is approved and the repository or delivery worktree is valid.",
            "Blank lifecycle command slots record readiness, git state, change-set, and governance evidence without executing that phase.",
        ],
    }


def command_context_for_phase(phase: str, command: str) -> dict[str, str]:
    field = {
        "modify": "modifyCommand",
        "build": "buildCommand",
        "test": "testCommand",
        "inspect": "inspectCommand",
        "repair": "repairCommand",
        "pr": "prCommand",
        "deploy": "deployCommand",
    }.get(phase)
    return {field: command} if field else {}


def control_plane_execute_action(payload: dict[str, Any], actor: dict[str, str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    phase = clean_choice(payload.get("phase"), {"modify", "build", "test", "inspect", "repair", "pr", "deploy"}, "inspect")
    command = clean_optional_string(payload.get("command"), limit=1000)
    repository = clean_optional_string(payload.get("repositoryPath") or payload.get("cwd"), limit=1000)
    if not repository:
        raise ValueError("repositoryPath is required")
    profile = clean_optional_string(payload.get("profile"), limit=160) or active_runtime_profile()
    mission_id = clean_optional_string(payload.get("missionId"), limit=80)
    if not re.fullmatch(r"ms-[a-f0-9]{24}", mission_id or ""):
        mission_id = "ms-" + secrets.token_hex(12)
    job_id = clean_optional_string(payload.get("jobId"), limit=120) or "control-plane-" + secrets.token_urlsafe(8)
    task_id = clean_optional_string(payload.get("taskId"), limit=20)
    if not re.fullmatch(r"DEV-[0-9]{6}", task_id or ""):
        task_id = new_task_id()
    runtime = resolve_runtime_profile(profile)
    snapshot_policy = runtime.get("snapshotPolicy") if isinstance(runtime.get("snapshotPolicy"), dict) else {}
    context = normalize_delivery_context({
        **(payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}),
        "repositoryPath": repository,
        "branch": payload.get("branch"),
        "baseBranch": payload.get("baseBranch"),
        "deliveryBranch": payload.get("deliveryBranch"),
        "worktreeMode": payload.get("worktreeMode"),
        "worktreePath": payload.get("worktreePath"),
        "prProvider": payload.get("prProvider"),
        "prMode": payload.get("prMode"),
        "prTarget": payload.get("prTarget"),
        "githubRepository": payload.get("githubRepository"),
        "ciProvider": payload.get("ciProvider"),
        "ciRequired": payload.get("ciRequired"),
        "deployProvider": payload.get("deployProvider"),
        "deployTarget": payload.get("deployTarget"),
        "deployEnvironment": payload.get("deployEnvironment"),
        "repairBudget": payload.get("repairBudget"),
        **command_context_for_phase(phase, command),
    })
    approvals = payload.get("approvals") if isinstance(payload.get("approvals"), list) else []
    if bool_from_payload(payload.get("approved"), False):
        approvals = [
            *[item for item in approvals if isinstance(item, dict)],
            {
                "approvalId": "ap-" + secrets.token_hex(12),
                "nodeId": "implementation-approval",
                "decision": "approved",
                "approvedAt": utc_now(),
                "actorRole": corporate_role(actor.get("role")),
                "note": "Approved on the guarded control-plane execution request.",
            },
        ]
    record = update_mission(
        mission_id,
        jobId=job_id,
        taskId=task_id,
        state="running",
        operatorRole=corporate_role(actor.get("role")),
        actorUserId=actor.get("actorId") or actor.get("role") or "control-plane",
        organizationId=actor.get("organizationId") or str(organization_registry().get("activeOrganizationId") or ""),
        mock=bool(payload.get("mock")),
        controlPlaneExecution=True,
        controlPlaneEntrypoint="api-or-cli",
        missionKind="orchestrated",
        templateId="control-plane-exec",
        templateTitle="Guarded Control Plane Execution",
        objective=clean_optional_string(payload.get("objective"), limit=12000) or f"Guarded {phase} execution",
        question=clean_optional_string(payload.get("objective"), limit=12000) or f"Guarded {phase} execution",
        profile=profile,
        runtimeProfile=runtime.get("runtimeProfile") if isinstance(runtime.get("runtimeProfile"), dict) else {},
        modelPolicy=runtime.get("modelPolicy") if isinstance(runtime.get("modelPolicy"), dict) else {},
        snapshotPolicySummary=snapshot_policy_summary(snapshot_policy),
        deliveryContext=context,
        approvals=approvals,
        evidenceLedger=[] if not read_mission_record(mission_id) else None,
        nodes=[
            {
                "nodeId": f"control-plane-{phase}",
                "title": f"Control Plane {phase.title()}",
                "kind": "delivery-step",
                "phase": phase,
                "capability": f"delivery.{phase}",
                "state": "running",
                "attempts": 1,
                "evidenceRefs": [],
            }
        ],
        resumable=False,
        startedAt=utc_now(),
    ) or {}
    append_mission_audit(
        mission_id,
        "control-plane-execution-requested",
        task_id=task_id,
        job_id=job_id,
        actor=actor.get("actorId") or "control-plane",
        operator_role=corporate_role(actor.get("role")),
        summary=f"Guarded {phase} execution requested through the control-plane boundary.",
        details={
            "phase": phase,
            "repositoryPath": repository,
            "authPolicyHash": actor.get("authPolicyHash"),
            "sessionSubject": actor.get("actorId"),
        },
    )
    result = delivery_step_payload(record, {"nodeId": f"control-plane-{phase}", "phase": phase})
    ok = result.get("ok") is True
    ref = write_mission_evidence(
        mission_id,
        f"control-plane-{phase}",
        "control-plane-execution",
        {
            "auth": {
                "actorId": actor.get("actorId") or "",
                "role": corporate_role(actor.get("role")),
                "authPolicyHash": actor.get("authPolicyHash") or "",
                "sessionVerified": actor.get("sessionVerified") is True,
            },
            "request": {key: value for key, value in payload.items() if key not in {"session", "accessToken"}},
            "result": result,
        },
        task_id=task_id,
        job_id=job_id,
        operator_role=corporate_role(actor.get("role")),
        summary=f"Control-plane {phase} execution {'passed' if ok else 'blocked or failed'}.",
    )
    update_mission(
        mission_id,
        state="done" if ok else "error",
        ok=ok,
        completedAt=utc_now(),
        currentNodeId="",
        lastPhase=f"Control-plane {phase} execution {'completed' if ok else 'stopped'}.",
        deliveryClosure={"status": "completed" if ok else "blocked", "ok": ok, "blockers": result.get("blockers", [])},
    )
    append_mission_audit(
        mission_id,
        "control-plane-execution-completed" if ok else "control-plane-execution-blocked",
        task_id=task_id,
        job_id=job_id,
        actor=actor.get("actorId") or "control-plane",
        operator_role=corporate_role(actor.get("role")),
        summary=f"Control-plane {phase} execution {'completed' if ok else 'stopped before unsafe execution'}.",
        details={"phase": phase, "status": result.get("status"), "ok": ok, "blockers": result.get("blockers", [])},
        artifact_refs=[ref],
    )
    return {
        "ok": ok,
        "missionId": mission_id,
        "taskId": task_id,
        "jobId": job_id,
        "state": "done" if ok else "error",
        "phase": phase,
        "result": result,
        "evidenceRef": ref,
    }


def execute_mission_node(record: dict[str, Any], node: dict[str, Any]) -> tuple[bool, str, dict[str, Any], list[dict[str, Any]]]:
    mission_id = str(record.get("missionId") or "")
    task_id = str(record.get("taskId") or "")
    job_id = str(record.get("jobId") or "")
    operator = corporate_role(str(record.get("operatorRole") or "user"))
    profile = str(record.get("profile") or active_runtime_profile())
    mock = bool(record.get("mock"))
    node_id = str(node.get("nodeId") or "")
    kind = str(node.get("kind") or "")

    if kind == "approval":
        approval = next(
            (
                item for item in record.get("approvals", [])
                if isinstance(item, dict) and item.get("nodeId") == node_id and item.get("decision") == "approved"
            ),
            {},
        )
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "approval",
            {"approval": approval, "node": {key: node.get(key) for key in ("nodeId", "title", "capability")}},
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary=f"Approval recorded for {node.get('title') or node_id}.",
        )
        return True, "Approval recorded.", {"approvalId": approval.get("approvalId")}, [ref]

    if kind == "snapshot":
        scope = [] if mock else snapshot_scope(profile)
        update_mission(mission_id, snapshotCollections=scope)
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "snapshot-scope",
            {"profile": profile, "collections": scope, "collectionCount": len(scope)},
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary=f"Captured snapshot scope for profile {profile}.",
        )
        return True, f"Captured {len(scope)} snapshot collections.", {"collectionCount": len(scope)}, [ref]

    if kind == "knowledge-prepare":
        payload = prepare_knowledge_snapshot_payload(profile)
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "knowledge-snapshot-prepared",
            payload,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary=f"Prepared {payload['availableSourceCount']} of {payload['sourceCount']} organization knowledge sources.",
        )
        update_mission(mission_id, knowledgeSnapshot=payload, knowledgeSnapshotRef=ref, knowledgeQuality=payload.get("knowledgeQuality"))
        return True, "Knowledge snapshot prepared.", {
            "sourceCount": payload["sourceCount"],
            "missingSourceCount": payload["missingSourceCount"],
            "knowledgeQualityStatus": (payload.get("knowledgeQuality") or {}).get("status"),
            "contextSufficient": payload.get("contextSufficient") is True,
        }, [ref]

    if kind == "broker-overview":
        overview = broker_overview()
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "broker-overview",
            overview,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary="Captured broker-visible state.",
        )
        return True, "Captured broker-visible state.", {"workflowCount": len(overview.get("workflows", []))}, [ref]

    if kind == "schema-gate":
        payload = broker_command("schema-gate", timeout=60)
        ok = payload.get("ok") is True
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "schema-gate",
            payload,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary="Schema gate completed." if ok else "Schema gate did not pass.",
        )
        return ok, "Schema gate completed." if ok else "Schema gate did not pass.", payload, [ref]

    if kind == "coordination-report":
        objective = mission_objective_for_node(record, node)
        result = run_coordinator_report(task_id, objective, [], mock, job_id=job_id, revision=0, profile=profile)
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {"result": result}
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "coordination-report",
            {"ok": result.get("ok"), "exitCode": result.get("exitCode"), "payload": payload},
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary=f"Recorded Delivery Coordinator readout for {node.get('title') or node_id}.",
        )
        return result.get("ok") is True, "Delivery Coordinator readout recorded.", result, [ref]

    if kind == "delivery-plan":
        payload = delivery_plan_payload(record)
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "delivery-plan",
            payload,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary="Recorded delivery execution plan.",
        )
        return True, "Delivery execution plan recorded.", {"phaseCount": len(payload["lifecycle"])}, [ref]

    if kind == "delivery-step":
        payload = delivery_step_payload(record, node)
        phase = str(payload.get("phase") or "delivery")
        status = str(payload.get("status") or "planned")
        ok = payload.get("ok") is True
        artifact_refs: list[dict[str, Any]] = []
        if not ok and phase in {"build", "test", "inspect"}:
            context = delivery_context_summary(record)
            repair_budget = int(context.get("repairBudget") or 0)
            repair_attempts_used = int(record.get("repairAttemptsUsed") or 0)
            repair_command = str(context.get("repairCommand") or "").strip()
            workspace = ensure_delivery_workspace(record)
            repo = Path(str(workspace.get("path"))).expanduser().resolve() if workspace.get("path") else None
            if repair_command and repo and repo.exists() and repo.is_dir() and repair_attempts_used < repair_budget:
                repair_attempts_used += 1
                update_mission(mission_id, repairAttemptsUsed=repair_attempts_used)
                repair_preflight = delivery_action_preflight(record, "repair", repair_command, context, workspace)
                repair_result = (
                    {"ok": False, "status": "blocked", "error": "; ".join(repair_preflight.get("blockers", []))}
                    if repair_preflight.get("ok") is not True else
                    {"ok": True, "status": "mocked", "command": repair_command} if mock else run_delivery_private_runner(repair_command, repo, record, "repair")
                )
                repair_payload = {
                    "phase": "repair",
                    "status": repair_result.get("status"),
                    "ok": repair_result.get("ok") is True,
                    "targetPhase": phase,
                    "repairAttempt": repair_attempts_used,
                    "repairBudget": repair_budget,
                    "deliveryContext": context,
                    "deliveryWorkspace": workspace,
                    "command": repair_command,
                    "preflight": repair_preflight,
                    "commandResult": repair_result,
                    "gitState": delivery_git_state(repo),
                    "changeSet": delivery_change_set(repo, context),
                }
                repair_ref = write_mission_evidence(
                    mission_id,
                    f"{node_id}-repair-{repair_attempts_used}",
                    "delivery-repair-attempt",
                    repair_payload,
                    task_id=task_id,
                    job_id=job_id,
                    operator_role=operator,
                    summary=f"Repair attempt {repair_attempts_used} recorded for {phase}.",
                )
                artifact_refs.append(repair_ref)
                append_mission_audit(
                    mission_id,
                    "delivery-repair-attempted",
                    task_id=task_id,
                    job_id=job_id,
                    actor="mission-orchestrator",
                    operator_role=operator,
                    summary=f"Repair attempt {repair_attempts_used} ran for {phase}.",
                    details={"phase": phase, "repairAttempt": repair_attempts_used, "status": repair_result.get("status")},
                    artifact_refs=[repair_ref],
                )
                if repair_result.get("ok") is True:
                    retry_record = {**record, "repairAttemptsUsed": repair_attempts_used}
                    payload = delivery_step_payload(retry_record, node)
                    status = str(payload.get("status") or "planned")
                    ok = payload.get("ok") is True
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "delivery-step",
            payload,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary=f"Recorded {phase} stage as {status}.",
        )
        artifact_refs.append(ref)
        return ok, f"{phase.title()} stage recorded as {status}.", {"phase": phase, "status": status}, artifact_refs

    if kind == "summary":
        current = read_mission_record(mission_id, include_audit=True) or record
        evidence = current.get("evidenceLedger") if isinstance(current.get("evidenceLedger"), list) else []
        audit = current.get("audit") if isinstance(current.get("audit"), list) else []
        payload = {
            "missionId": mission_id,
            "templateId": current.get("templateId"),
            "state": current.get("state"),
            "evidenceCount": len(evidence),
            "auditCount": len(audit),
            "latestAudit": audit[-5:],
        }
        ref = write_mission_evidence(
            mission_id,
            node_id,
            "mission-summary",
            payload,
            task_id=task_id,
            job_id=job_id,
            operator_role=operator,
            summary="Recorded mission summary evidence.",
        )
        return True, "Mission summary recorded.", payload, [ref]

    ref = write_mission_evidence(
        mission_id,
        node_id,
        "mission-node",
        {"node": node, "message": "No executor is registered for this node kind."},
        task_id=task_id,
        job_id=job_id,
        operator_role=operator,
        summary=f"No executor is registered for {node_id}.",
    )
    return False, f"No executor is registered for node kind {kind}.", {"kind": kind}, [ref]


def set_orchestrated_job_state(mission_id: str, state: str, phase: str, **fields: Any) -> None:
    record = read_mission_record(mission_id)
    if not record:
        return
    job_id = record.get("jobId")
    if not isinstance(job_id, str):
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update({"state": state, "phase": phase, **fields})


def launch_mission_orchestrator(mission_id: str) -> bool:
    with MISSION_ORCHESTRATORS_LOCK:
        if mission_id in MISSION_ORCHESTRATORS:
            return False
        MISSION_ORCHESTRATORS.add(mission_id)

    def runner() -> None:
        try:
            run_mission_orchestrator(mission_id)
        finally:
            with MISSION_ORCHESTRATORS_LOCK:
                MISSION_ORCHESTRATORS.discard(mission_id)

    threading.Thread(target=runner, daemon=True).start()
    return True


def run_mission_orchestrator(mission_id: str) -> None:
    started = time.time()
    while True:
        record = read_mission_record(mission_id) or {}
        state = str(record.get("state") or "unknown")
        if state in {"paused", "cancelled", "done", "error"}:
            return
        nodes = [node for node in record.get("nodes", []) if isinstance(node, dict)]
        next_node = next((node for node in nodes if node.get("state") not in {"done", "skipped"}), None)
        if not next_node:
            proof_ref: dict[str, Any] | None = None
            closure: dict[str, Any] = {}
            if record.get("templateId") == "delivery-execution":
                proof_ref = write_delivery_proof_bundle(record)
                latest = read_mission_record(mission_id) or record
                closure = latest.get("deliveryClosure") if isinstance(latest.get("deliveryClosure"), dict) else {}
            completion_refs = write_connector_event_evidence(
                read_mission_record(mission_id) or record,
                "mission-summary",
                "mission-completed",
                {
                    "missionId": mission_id,
                    "templateId": record.get("templateId"),
                    "state": "done",
                    "closure": closure,
                    "proofRef": proof_ref or {},
                },
            )
            append_mission_audit(
                mission_id,
                "mission-completed",
                task_id=str(record.get("taskId") or "") or None,
                job_id=str(record.get("jobId") or "") or None,
                actor="mission-orchestrator",
                operator_role=corporate_role(str(record.get("operatorRole") or "user")),
                summary=f"Mission orchestration completed with closure status {closure.get('status')}." if closure else "Mission orchestration completed.",
                details={"templateId": record.get("templateId"), "nodeCount": len(nodes), "deliveryClosure": closure},
                artifact_refs=([proof_ref] if proof_ref else []) + completion_refs,
            )
            update_mission(
                mission_id,
                state="done",
                completedAt=utc_now(),
                durationSeconds=round(time.time() - started, 1),
                currentNodeId="",
                lastPhase=f"Mission completed: {closure.get('status')}." if closure else "Mission completed.",
                ok=closure.get("ok") if closure else True,
            )
            set_orchestrated_job_state(mission_id, "done", f"Mission completed: {closure.get('status')}." if closure else "Mission completed.", ok=closure.get("ok") if closure else True,
                                       durationSeconds=round(time.time() - started, 1),
                                       payload={"summary": "Mission completed.", "deliveryClosure": closure})
            return

        node_id = str(next_node.get("nodeId") or "")
        operator = corporate_role(str(record.get("operatorRole") or "user"))
        task_id = str(record.get("taskId") or "") or None
        job_id = str(record.get("jobId") or "") or None
        if next_node.get("requiresApproval") and not mission_node_approved(record, node_id):
            connector_refs = write_connector_event_evidence(
                record,
                node_id,
                "approval-requested",
                {
                    "missionId": mission_id,
                    "taskId": task_id,
                    "jobId": job_id,
                    "nodeId": node_id,
                    "title": next_node.get("title") or node_id,
                    "capability": next_node.get("capability"),
                    "operatorRole": operator,
                    "objective": record.get("objective") or record.get("question") or "",
                },
            )
            append_mission_audit(
                mission_id,
                "approval-requested",
                task_id=task_id,
                job_id=job_id,
                actor="mission-orchestrator",
                operator_role=operator,
                summary=f"Approval required for {next_node.get('title') or node_id}.",
                details={"nodeId": node_id, "capability": next_node.get("capability")},
                artifact_refs=connector_refs,
            )
            update_mission_node(mission_id, node_id, state="waiting_for_approval", waitingSince=utc_now())
            update_mission(mission_id, state="waiting_for_approval", currentNodeId=node_id,
                           lastPhase=f"Waiting for approval: {next_node.get('title') or node_id}.")
            set_orchestrated_job_state(
                mission_id,
                "waiting_for_approval",
                f"Waiting for approval: {next_node.get('title') or node_id}.",
            )
            return

        update_mission_node(
            mission_id,
            node_id,
            state="running",
            attempts=int(next_node.get("attempts") or 0) + 1,
            startedAt=utc_now(),
        )
        update_mission(mission_id, state="running", currentNodeId=node_id,
                       lastPhase=f"Running {next_node.get('title') or node_id}.")
        set_orchestrated_job_state(mission_id, "running", f"Running {next_node.get('title') or node_id}.")
        append_mission_audit(
            mission_id,
            "mission-node-started",
            task_id=task_id,
            job_id=job_id,
            actor="mission-orchestrator",
            operator_role=operator,
            summary=f"Started {next_node.get('title') or node_id}.",
            details={"nodeId": node_id, "kind": next_node.get("kind"), "capability": next_node.get("capability")},
        )
        try:
            latest = read_mission_record(mission_id) or record
            ok, summary, details, artifact_refs = execute_mission_node(latest, next_node)
        except Exception as exc:  # noqa: BLE001
            ok = False
            summary = str(exc)
            details = {"error": str(exc)}
            artifact_refs = []
        if not ok:
            failure_connector_refs = write_connector_event_evidence(
                read_mission_record(mission_id) or record,
                node_id,
                "status",
                {
                    "missionId": mission_id,
                    "nodeId": node_id,
                    "state": "error",
                    "summary": summary,
                    "details": details,
                },
            )
            update_mission_node(mission_id, node_id, state="error", completedAt=utc_now(), resultSummary=summary)
            update_mission(mission_id, state="error", completedAt=utc_now(), error=summary,
                           lastPhase=f"Mission stopped at {next_node.get('title') or node_id}.")
            set_orchestrated_job_state(mission_id, "error", summary, ok=False, error=summary)
            append_mission_audit(
                mission_id,
                "mission-node-failed",
                task_id=task_id,
                job_id=job_id,
                actor="mission-orchestrator",
                operator_role=operator,
                summary=summary,
                details={"nodeId": node_id, **details},
                artifact_refs=artifact_refs + failure_connector_refs,
            )
            return
        update_mission_node(mission_id, node_id, state="done", completedAt=utc_now(), resultSummary=summary)
        event_type = "control-decision" if next_node.get("kind") == "delivery-step" else "status"
        status_connector_refs = write_connector_event_evidence(
            read_mission_record(mission_id) or record,
            node_id,
            event_type,
            {
                "missionId": mission_id,
                "nodeId": node_id,
                "state": "done",
                "summary": summary,
                "details": details,
                "artifactRefs": artifact_refs,
            },
        )
        append_mission_audit(
            mission_id,
            "mission-node-completed",
            task_id=task_id,
            job_id=job_id,
            actor="mission-orchestrator",
            operator_role=operator,
            summary=summary,
            details={"nodeId": node_id, **details},
            artifact_refs=artifact_refs + status_connector_refs,
        )


def start_orchestrated_mission(
    template_id: str,
    objective: str,
    *,
    mock: bool = False,
    profile: str | None = None,
    operator_role: str | None = None,
    user_ids: Any = None,
    domain_capability_keys: Any = None,
    delivery_context: Any = None,
    actor_user_id: str | None = None,
    organization_id: str | None = None,
    workflow_origin: Any = None,
) -> dict[str, Any]:
    template = mission_template(template_id)
    if not template:
        raise ValueError("mission template is not available")
    operator = corporate_role(operator_role)
    actor_id = clean_optional_string(actor_user_id, limit=120) or operator
    selected_organization_id = clean_optional_string(organization_id, limit=120) or str(organization_registry().get("activeOrganizationId") or "")
    if operator not in set(template.get("allowedRoles", [])):
        raise ValueError("this endpoint cannot start that mission template")
    clean_objective = objective.strip()
    if not clean_objective:
        raise ValueError("objective is required")
    job_id = secrets.token_urlsafe(12)
    mission_id = "ms-" + secrets.token_hex(12)
    task_id = new_task_id()
    started = time.time()
    selected_profile = profile or active_runtime_profile()
    runtime = resolve_runtime_profile(selected_profile)
    runtime_profile = runtime.get("runtimeProfile") if isinstance(runtime.get("runtimeProfile"), dict) else {}
    model_policy = runtime.get("modelPolicy") if isinstance(runtime.get("modelPolicy"), dict) else {}
    snapshot_policy = runtime.get("snapshotPolicy") if isinstance(runtime.get("snapshotPolicy"), dict) else {}
    users = mission_user_bindings(user_ids)
    work_set = domain_capability_work_set(domain_capability_keys)
    delivery = normalize_delivery_context(delivery_context)
    origin = normalize_workflow_origin(workflow_origin)
    nodes = new_mission_nodes(template)
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "running",
            "createdAt": utc_now(),
            "startedEpoch": started,
            "taskId": task_id,
            "mock": mock,
            "question": clean_objective[:12000],
            "objective": clean_objective[:12000],
            "profile": selected_profile,
            "missionId": mission_id,
            "missionKind": "orchestrated",
            "templateId": template_id,
            "controlPlaneExecution": template_id == "delivery-execution",
            "controlPlaneEntrypoint": "mission-orchestrator" if template_id == "delivery-execution" else "",
            "operatorRole": operator,
            "actorUserId": actor_id,
            "organizationId": selected_organization_id,
            "missionUsers": users,
            "capabilityWorkSet": work_set,
            "deliveryContext": delivery,
            "workflowOrigin": origin,
            "repairAttemptsUsed": 0,
            "messages": [],
            "followUps": [],
            "steeringRevision": 0,
            "restartRequested": False,
            "phase": "Mission orchestration started.",
        }
    update_mission(
        mission_id,
        jobId=job_id,
        taskId=task_id,
        state="running",
        operatorRole=operator,
        actorUserId=actor_id,
        organizationId=selected_organization_id,
        mock=mock,
        missionKind="orchestrated",
        templateId=template_id,
        templateTitle=template.get("title"),
        controlPlaneExecution=template_id == "delivery-execution",
        controlPlaneEntrypoint="mission-orchestrator" if template_id == "delivery-execution" else "",
        objective=clean_objective[:12000],
        question=clean_objective[:12000],
        profile=selected_profile,
        runtimeProfile=runtime_profile,
        modelPolicy=model_policy,
        snapshotPolicySummary=snapshot_policy_summary(snapshot_policy),
        missionUsers=users,
        domainCapabilityKeys=[item.get("roleKey") for item in work_set if item.get("roleKey")],
        capabilityWorkSet=work_set,
        capabilityKeys=[item.get("capabilityKey") or item.get("roleKey") for item in work_set if item.get("capabilityKey") or item.get("roleKey")],
        deliveryContext=delivery,
        workflowOrigin=origin,
        repairAttemptsUsed=0,
        snapshotCollections=[],
        nodes=nodes,
        approvals=[],
        evidenceLedger=[],
        currentNodeId=nodes[0].get("nodeId") if nodes else "",
        resumable=True,
        steeringRevision=0,
        followUpCount=0,
        restartCount=0,
        startedAt=dt.datetime.fromtimestamp(started, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    append_mission_audit(
        mission_id,
        "mission-started",
        task_id=task_id,
        job_id=job_id,
        actor=actor_id,
        operator_role=operator,
        summary=f"Started {template.get('title')} mission for profile {selected_profile}.",
        details={
            "templateId": template_id,
            "objective": clean_objective[:500],
            "mock": mock,
            "userIds": [item.get("id") for item in users if item.get("id")],
            "domainCapabilityKeys": [item.get("roleKey") for item in work_set if item.get("roleKey")],
            "capabilityKeys": [item.get("capabilityKey") or item.get("roleKey") for item in work_set if item.get("capabilityKey") or item.get("roleKey")],
            "deliveryContext": delivery,
            "workflowOrigin": origin,
            "runtimeProfileId": runtime_profile.get("id"),
            "provider": model_policy.get("provider"),
            "model": model_policy.get("selectedModel"),
            "snapshotPolicyHash": snapshot_policy_summary(snapshot_policy).get("policyHash"),
        },
    )
    write_connector_event_evidence(
        read_mission_record(mission_id) or {},
        "mission-start",
        "status",
        {
            "missionId": mission_id,
            "templateId": template_id,
            "state": "running",
            "summary": f"Started {template.get('title')} mission.",
        },
    )
    launch_mission_orchestrator(mission_id)
    return {"ok": True, "missionId": mission_id, "jobId": job_id, "taskId": task_id, "state": "running"}


def approve_mission(mission_id: str, role: str, note: str = "", actor_user_id: str | None = None, actor_context: dict[str, Any] | None = None) -> dict[str, Any]:
    record = read_mission_record(mission_id)
    if not record:
        raise KeyError("mission not found")
    operator = corporate_role(role)
    actor_id = clean_optional_string(actor_user_id, limit=120) or operator
    if operator not in {"owner", "admin", "publisher"} or not mission_visible_to(record, operator, actor_context):
        raise PermissionError("this endpoint cannot approve the mission")
    authorization = auth_policy().get("authorization")
    prevent_self = not isinstance(authorization, dict) or authorization.get("preventSelfApproval") is not False
    if prevent_self and (actor_context is not None or identity_mode() == "oidc-required") and actor_id == str(record.get("actorUserId") or ""):
        raise PermissionError("separation of duties prevents a mission initiator from approving the same mission")
    nodes = [node for node in record.get("nodes", []) if isinstance(node, dict)]
    node = next((item for item in nodes if item.get("state") == "waiting_for_approval"), None)
    if not node:
        raise RuntimeError("mission is not waiting for approval")
    node_id = str(node.get("nodeId") or "")
    approval = {
        "approvalId": "ap-" + secrets.token_hex(12),
        "nodeId": node_id,
        "decision": "approved",
        "approvedAt": utc_now(),
        "actorRole": operator,
        "actorId": actor_id,
        "note": note.strip()[:2000],
    }
    with MISSION_LOCK:
        current = read_mission_record(mission_id) or record
        approvals = [item for item in current.get("approvals", []) if isinstance(item, dict)]
        approvals.append(approval)
        current_nodes = [dict(item) for item in current.get("nodes", []) if isinstance(item, dict)]
        for index, item in enumerate(current_nodes):
            if item.get("nodeId") == node_id:
                current_nodes[index] = {**item, "state": "approved", "approvedAt": approval["approvedAt"]}
                break
        write_mission_record({**current, "state": "running", "approvals": approvals, "nodes": current_nodes,
                              "currentNodeId": node_id, "lastPhase": "Approval recorded; mission is resuming."})
    set_orchestrated_job_state(mission_id, "running", "Approval recorded; mission is resuming.")
    append_mission_audit(
        mission_id,
        "mission-approved",
        task_id=str(record.get("taskId") or "") or None,
        job_id=str(record.get("jobId") or "") or None,
        actor=actor_id,
        operator_role=operator,
        summary=f"Approved {node.get('title') or node_id}.",
        details={"approvalId": approval["approvalId"], "nodeId": node_id, "note": approval["note"], "actorUserId": actor_id},
    )
    launch_mission_orchestrator(mission_id)
    return {"ok": True, "missionId": mission_id, "approval": approval, "state": "running"}


def pause_mission(mission_id: str, role: str, actor_user_id: str | None = None, actor_context: dict[str, Any] | None = None) -> dict[str, Any]:
    record = read_mission_record(mission_id)
    if not record:
        raise KeyError("mission not found")
    operator = corporate_role(role)
    actor_id = clean_optional_string(actor_user_id, limit=120) or operator
    if not mission_visible_to(record, operator, actor_context):
        raise PermissionError("mission is not visible from this endpoint")
    state = str(record.get("state") or "")
    if state not in {"running", "waiting_for_approval"}:
        raise RuntimeError("mission is not running")
    update_mission(mission_id, state="paused", lastPhase="Mission paused by operator.")
    set_orchestrated_job_state(mission_id, "paused", "Mission paused by operator.")
    append_mission_audit(
        mission_id,
        "mission-paused",
        task_id=str(record.get("taskId") or "") or None,
        job_id=str(record.get("jobId") or "") or None,
        actor=actor_id,
        operator_role=operator,
        summary="Mission paused by operator.",
    )
    return {"ok": True, "missionId": mission_id, "state": "paused"}


def resume_mission(mission_id: str, role: str, actor_user_id: str | None = None, actor_context: dict[str, Any] | None = None) -> dict[str, Any]:
    record = read_mission_record(mission_id)
    if not record:
        raise KeyError("mission not found")
    operator = corporate_role(role)
    actor_id = clean_optional_string(actor_user_id, limit=120) or operator
    if not mission_visible_to(record, operator, actor_context):
        raise PermissionError("mission is not visible from this endpoint")
    if str(record.get("state") or "") not in {"paused", "waiting_for_approval", "running"}:
        raise RuntimeError("mission cannot be resumed")
    update_mission(mission_id, state="running", lastPhase="Mission resumed by operator.")
    set_orchestrated_job_state(mission_id, "running", "Mission resumed by operator.")
    append_mission_audit(
        mission_id,
        "mission-resumed",
        task_id=str(record.get("taskId") or "") or None,
        job_id=str(record.get("jobId") or "") or None,
        actor=actor_id,
        operator_role=operator,
        summary="Mission resumed by operator.",
    )
    launch_mission_orchestrator(mission_id)
    return {"ok": True, "missionId": mission_id, "state": "running"}


def supervise_missions_on_startup() -> dict[str, Any]:
    scanned = 0
    orphaned = 0
    waiting = 0
    for path in sorted(MISSION_ROOT.glob("ms-*/mission.json")) if MISSION_ROOT.exists() else []:
        mission_id = path.parent.name
        record = read_mission_record(mission_id)
        if not record or record.get("missionKind") != "orchestrated":
            continue
        scanned += 1
        state = str(record.get("state") or "")
        if state == "running":
            orphaned += 1
            update_mission(
                mission_id,
                state="paused",
                lastPhase="Mission was interrupted by a server restart and is ready to resume.",
                orphanedAtStartup=True,
                orphanedAt=utc_now(),
            )
            append_mission_audit(
                mission_id,
                "mission-orphaned-at-startup",
                task_id=str(record.get("taskId") or "") or None,
                job_id=str(record.get("jobId") or "") or None,
                actor="mission-supervisor",
                operator_role=corporate_role(str(record.get("operatorRole") or "user")),
                summary="Mission was marked paused after server startup found no active orchestrator.",
                details={"previousState": state, "resumable": record.get("resumable") is True},
            )
        elif state == "waiting_for_approval":
            waiting += 1
            append_mission_audit(
                mission_id,
                "mission-supervisor-observed",
                task_id=str(record.get("taskId") or "") or None,
                job_id=str(record.get("jobId") or "") or None,
                actor="mission-supervisor",
                operator_role=corporate_role(str(record.get("operatorRole") or "user")),
                summary="Mission is still waiting for approval after startup.",
                details={"state": state, "currentNodeId": record.get("currentNodeId")},
            )
    return {"ok": True, "scanned": scanned, "orphaned": orphaned, "waitingForApproval": waiting}


class Handler(BaseHTTPRequestHandler):
    server_version = "SteelMission/0.1"

    def redirect(self, location: str, status: int = 303) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for name, value in getattr(self, "response_headers", []):
            self.send_header(str(name), str(value))
        self.response_headers = []
        self.end_headers()

    def authenticate(self, path: str, method: str) -> dict[str, Any] | None:
        try:
            actor = authenticate_http_request(self, path, method)
            self.auth_actor = actor
            return actor
        except PermissionError as exc:
            json_response(self, 401, unauthenticated_payload(str(exc)))
            return None

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if is_page_path(path):
            length = INDEX.stat().st_size
        elif path == "/plain":
            length = len(render_home().encode("utf-8"))
        elif is_legacy_page_path(path):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/auth/login":
            if development_login_available():
                # A literal placeholder, not %-formatting: the page embeds CSS,
                # and a stylesheet is full of characters that % treats as
                # conversion specifiers.
                body = DEVELOPMENT_LOGIN_PAGE.replace(
                    "__CONTAINER__", os.environ.get("HOSTNAME") or "steel-mission"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                location = begin_oidc_login(self)
                state = (parse_qs(urlparse(location).query).get("state") or [""])[0]
                secure = "; Secure" if oidc_redirect_uri(self, auth_policy().get("oidc", {})).startswith("https://") else ""
                self.response_headers = [("Set-Cookie", f"present_oidc_state={state}; Path=/auth/callback; HttpOnly; SameSite=Lax; Max-Age=600{secure}")]
                self.redirect(location)
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        if path == "/auth/callback":
            try:
                session, csrf = complete_oidc_login(self, parse_qs(urlparse(self.path).query))
                secure = "; Secure" if oidc_redirect_uri(self, auth_policy().get("oidc", {})).startswith("https://") else ""
                self.response_headers = [
                    ("Set-Cookie", f"present_session={session['accessToken']}; Path=/; HttpOnly; SameSite=Lax{secure}"),
                    ("Set-Cookie", f"present_csrf={csrf}; Path=/; SameSite=Lax{secure}"),
                    ("Set-Cookie", f"present_oidc_state=; Path=/auth/callback; HttpOnly; SameSite=Lax; Max-Age=0{secure}"),
                ]
                self.redirect("/")
            except Exception as exc:  # noqa: BLE001
                append_auth_audit("oidc-login-failed", ok=False, details={"error": str(exc)})
                json_response(self, 401, unauthenticated_payload(str(exc)))
            return
        actor: dict[str, Any] | None = None
        if path.startswith("/api/") and path != "/api/health":
            actor = self.authenticate(path, "GET")
            if actor is None:
                return
        elif path.startswith(("/job/", "/mission/")) and identity_mode() == "oidc-required":
            actor = self.authenticate(path, "GET")
            if actor is None:
                return
        if path in {"/api/owner/workspace", "/api/admin/workspace", "/api/publisher/workspace", "/api/user/workspace"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            json_response(self, 200, corporate_workspace(role))
            return
        if path in {"/api/owner/assignments", "/api/admin/assignments"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            if role not in {"owner", "admin"}:
                json_response(self, 403, {"ok": False, "error": "owner or admin role is required"})
                return
            payload = corporate_workspace(role)
            json_response(self, 200, {"ok": True, "role": payload["role"], "assignments": payload["assignments"]})
            return
        if path in {"/api/owner/organizations", "/api/admin/organizations", "/api/publisher/organizations", "/api/user/organizations"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            registry = organization_registry()
            actor_org_ids = set(clean_string_list(actor.get("organizationIds"), limit=50)) if actor else set()
            visible_organizations = [
                item for item in registry.get("organizations", [])
                if isinstance(item, dict) and (not actor_org_ids or item.get("id") in actor_org_ids)
            ]
            actor_active_id = str(actor.get("organizationId") if actor else registry.get("activeOrganizationId") or "")
            active = next((item for item in visible_organizations if item.get("id") == actor_active_id), visible_organizations[0] if visible_organizations else {})
            payload = {
                "schemaVersion": 1,
                "activeOrganizationId": active.get("id") or "",
                "organizations": visible_organizations,
            }
            if role in {"owner", "admin"}:
                payload = {**registry, **payload}
            json_response(self, 200, {
                "ok": True,
                "role": role,
                "canManageOrganizations": role in {"owner", "admin"},
                "activeOrganization": active,
                "payload": payload,
            })
            return
        if path in {"/api/owner/knowledge", "/api/admin/knowledge", "/api/publisher/knowledge", "/api/user/knowledge"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            payload = knowledge_registry()
            payload["role"] = role
            payload["canManageGeneralKnowledge"] = role in {"owner", "admin"}
            json_response(self, 200 if payload.get("ok") else 503, payload)
            return
        if path in {"/api/owner/knowledge/prepared", "/api/admin/knowledge/prepared"}:
            actor = actor or {"actorId": "user", "role": "user"}
            try:
                require_actor_role(actor, {"owner", "admin"})
                json_response(self, 200, {"ok": True, "payload": prepare_knowledge_snapshot_payload(active_runtime_profile())})
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
            return
        if path in {"/api/owner/users", "/api/admin/users", "/api/publisher/users", "/api/user/users"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            payload = user_registry()
            payload["role"] = role
            payload["canManageUsers"] = role in {"owner", "admin"}
            if role not in {"owner", "admin"}:
                payload["users"] = [
                    {key: item[key] for key in ("id", "name", "role", "status", "assignedCapabilities") if key in item}
                    for item in payload.get("users", [])
                    if isinstance(item, dict) and item.get("status") == "active" and corporate_role(str(item.get("role"))) == role
                ]
            json_response(self, 200, {"ok": True, **payload})
            return
        if path in {"/api/owner/missions", "/api/admin/missions", "/api/publisher/missions", "/api/user/missions"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            json_response(self, 200, mission_list(role, actor=actor))
            return
        if path in {"/api/owner/mutations", "/api/admin/mutations", "/api/publisher/mutations", "/api/user/mutations"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            payload = read_mutation_ledger(role)
            json_response(self, 200 if payload.get("ok") else 403, payload)
            return
        if path in {"/api/owner/integrations", "/api/admin/integrations", "/api/publisher/integrations", "/api/user/integrations"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            json_response(self, 200, integration_registry(role))
            return
        if path in {"/api/owner/control-policy", "/api/admin/control-policy", "/api/publisher/control-policy", "/api/user/control-policy"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            policy = control_policy()
            if role not in {"owner", "admin"}:
                policy = {
                    "policyId": policy.get("policyId"),
                    "modelIndependence": policy.get("modelIndependence"),
                    "customerBoundary": policy.get("customerBoundary"),
                    "autoApprovedPhases": policy.get("autoApprovedPhases"),
                }
            json_response(self, 200, {"ok": True, "role": role, "policy": policy, "canManagePolicy": role in {"owner", "admin"}})
            return
        if path in {"/api/owner/auth-policy", "/api/admin/auth-policy", "/api/publisher/auth-policy", "/api/user/auth-policy"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            policy = auth_policy()
            if role not in {"owner", "admin"}:
                policy = {
                    "policyId": policy.get("policyId"),
                    "enforcementMode": policy.get("enforcementMode"),
                    "oidc": {"enabled": policy.get("oidc", {}).get("enabled") if isinstance(policy.get("oidc"), dict) else False},
                    "kms": {"enabled": policy.get("kms", {}).get("enabled") if isinstance(policy.get("kms"), dict) else False},
                }
            json_response(self, 200, {"ok": True, "role": role, "policy": policy, "canManagePolicy": role in {"owner", "admin"}})
            return
        if path in {"/api/owner/mission-templates", "/api/admin/mission-templates", "/api/publisher/mission-templates", "/api/user/mission-templates"}:
            role = corporate_role(str(actor.get("role") if actor else "user"))
            json_response(self, 200, public_mission_templates(role))
            return
        if path.startswith("/api/missions/"):
            parts = path.strip("/").split("/")
            role = corporate_role(str(actor.get("role") if actor else "user"))
            visible_detail = mission_detail(parts[2], role, actor=actor) if len(parts) >= 3 else {"ok": False}
            if not visible_detail.get("ok"):
                json_response(self, 403, visible_detail)
                return
            if len(parts) == 4 and parts[3] == "proof":
                payload = mission_proof_bundle(parts[2], role)
                json_response(self, 200 if payload.get("ok") else 404, payload)
                return
            if len(parts) == 4 and parts[3] == "report":
                payload = mission_report_markdown(parts[2], role)
                if payload.get("ok"):
                    text_response(self, 200, str(payload.get("markdown") or ""), "text/markdown; charset=utf-8")
                else:
                    json_response(self, 404, payload)
                return
            if len(parts) == 4 and parts[3] == "siem":
                payload = mission_siem_jsonl(parts[2], role)
                if payload.get("ok"):
                    text_response(self, 200, str(payload.get("jsonl") or ""), "application/x-ndjson; charset=utf-8")
                else:
                    json_response(self, 403 if payload.get("status") == "locked" else 404, payload)
                return
            if len(parts) == 4 and parts[3] == "export":
                detail = visible_detail
                if not detail.get("ok"):
                    json_response(self, 404, detail)
                    return
                mission = detail.get("mission") if isinstance(detail.get("mission"), dict) else {}
                pack_ref = mission.get("deliveryProofPackRef") if isinstance(mission.get("deliveryProofPackRef"), dict) else {}
                pack_path = pack_ref.get("path")
                if not isinstance(pack_path, str) or not Path(pack_path).exists():
                    json_response(self, 404, {"ok": False, "error": "delivery proof pack is not available"})
                    return
                binary_response(
                    self,
                    200,
                    Path(pack_path).read_bytes(),
                    "application/zip",
                    filename=f"{parts[2]}-delivery-proof-pack.zip",
                )
                return
            if len(parts) == 3:
                payload = visible_detail
                json_response(self, 200 if payload.get("ok") else 404, payload)
                return
        if path == "/api/health":
            json_response(self, 200, {"ok": True, "service": "steel-mission-chat", **cos_provider_summary()})
            return
        if path == "/api/runtime-profiles":
            json_response(self, 200, {
                "ok": True,
                "activeProfile": active_runtime_profile(),
                "registry": runtime_profile_registry(),
                "modelRoles": model_role_registry(),
            })
            return
        if path == "/api/model-roles":
            json_response(self, 200, {"ok": True, "registry": model_role_registry()})
            return
        if path == "/api/integrations":
            json_response(self, 200, integration_registry(str(actor.get("role") if actor else "user")))
            return
        if path == "/api/control-policy":
            json_response(self, 200, {"ok": True, "policy": control_policy()})
            return
        if path == "/api/auth-policy":
            policy = auth_policy()
            if corporate_role(str(actor.get("role") if actor else "user")) not in {"owner", "admin"}:
                policy = {"policyId": policy.get("policyId"), "identityBoundary": policy.get("identityBoundary"), "oidc": {"enabled": policy.get("oidc", {}).get("enabled") is True}}
            json_response(self, 200, {"ok": True, "policy": policy})
            return
        if path == "/api/auth/whoami":
            public_actor = {key: actor.get(key) for key in ("actorId", "role", "organizationId", "organizationIds", "capabilities", "identitySource", "sessionVerified") if actor and key in actor}
            json_response(self, 200, {"ok": True, "actor": public_actor, "identityMode": identity_mode()})
            return
        if path == "/api/control-plane/readiness":
            role = corporate_role(str(actor.get("role") if actor else "user"))
            json_response(self, 200, control_plane_production_readiness(role))
            return
        if path == "/api/knowledge":
            payload = knowledge_registry()
            json_response(self, 200 if payload.get("ok") else 503, payload)
            return
        if path == "/api/broker/overview":
            json_response(self, 200, broker_overview())
            return
        if path.startswith("/api/chat/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                payload = JOBS.get(job_id)
            if payload is None:
                json_response(self, 404, {"ok": False, "error": "chat job not found"})
                return
            if not mission_visible_to_actor(payload, actor or {"actorId": "user", "role": "user"}):
                json_response(self, 403, {"ok": False, "error": "chat job is not visible to this actor"})
                return
            json_response(self, 200, job_api_payload(job_id, payload))
            return
        if path.startswith("/job/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                payload = JOBS.get(job_id)
            if payload is None:
                html_response(self, 404, render_shell('<section class="panel"><h2>Job not found</h2><p><a href="/">Ask a question</a></p></section>'))
                return
            if actor is not None and not mission_visible_to_actor(payload, actor):
                json_response(self, 403, {"ok": False, "error": "job is not visible to this actor"})
                return
            html_response(self, 200, render_job(job_id, payload))
            return
        if path.startswith("/mission/"):
            role = corporate_role(str(actor.get("role") if actor else parse_qs(urlparse(self.path).query).get("role", ["user"])[0]))
            if actor is not None and not mission_detail(path.rsplit("/", 1)[-1], role, actor=actor).get("ok"):
                json_response(self, 403, {"ok": False, "error": "mission is not visible to this actor"})
                return
            html_response(self, 200, render_mission_detail_page(path.rsplit("/", 1)[-1], role))
            return
        if is_page_path(path):
            html_response(self, 200, chat_index())
            return
        if is_legacy_page_path(path):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/plain":
            html_response(self, 200, render_home())
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/auth/login":
            # Completes the development sign-in the GET form starts. The posted
            # token is the credential: it is verified here exactly as a bearer
            # token is, so this route grants no authority of its own, and issuing
            # a token requires access to the machine the server runs on.
            if not development_login_available():
                json_response(self, 404, {"ok": False, "error": "not found"})
                return
            try:
                raw = read_request_bytes(self)
                token = (parse_qs(raw.decode("utf-8", "replace")).get("token") or [""])[0].strip()
            except Exception:  # noqa: BLE001
                token = ""
            verified = verify_control_plane_session(token) if token else {"ok": False, "error": "a session token is required"}
            if verified.get("ok") is not True:
                append_auth_audit("development-login-failed", "", ok=False,
                                  details={"error": str(verified.get("error") or "invalid session")})
                json_response(self, 401, unauthenticated_payload(str(verified.get("error") or "invalid session")))
                return
            csrf = secrets.token_urlsafe(32)
            append_auth_audit("development-login", str(verified.get("actorId") or ""), ok=True,
                              details={"origin": str(self.client_address[0] if self.client_address else "")})
            self.response_headers = [
                ("Set-Cookie", f"present_session={token}; Path=/; HttpOnly; SameSite=Lax"),
                ("Set-Cookie", f"present_csrf={csrf}; Path=/; SameSite=Lax"),
            ]
            self.redirect("/")
            return
        ingress_source = {
            "/api/integrations/github/webhook": "github",
            "/api/integrations/slack/events": "slack",
            "/api/integrations/jira/webhook": "jira",
        }.get(path)
        if ingress_source:
            try:
                raw_body = read_request_bytes(self)
                status, payload = process_workflow_ingress(
                    ingress_source,
                    self.headers,
                    raw_body,
                    str(self.headers.get("Content-Type") or "application/json"),
                )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, status, payload)
            return
        actor: dict[str, Any] | None = None
        if path.startswith("/api/") and path != "/api/auth/session":
            actor = self.authenticate(path, "POST")
            if actor is None:
                return
        elif path == "/ask" and identity_mode() == "oidc-required":
            actor = self.authenticate(path, "POST")
            if actor is None:
                return
        if path in {"/api/owner/assignments", "/api/admin/assignments"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                registry = save_domain_capability_registry(body, actor["role"])
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "payload": registry})
            return
        if path in {"/api/owner/knowledge", "/api/admin/knowledge"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                registry = save_general_knowledge_registry(body, actor["role"])
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "payload": registry})
            return
        if path in {"/api/owner/organizations", "/api/admin/organizations"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                registry = save_organization_registry(body, actor["role"])
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "payload": registry})
            return
        if path in {"/api/owner/knowledge/upload", "/api/admin/knowledge/upload"}:
            try:
                body = read_json(self, MAX_UPLOAD_REQUEST_BYTES)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                payload = upload_organization_knowledge(body, actor["role"])
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 202, {"ok": True, "payload": payload})
            return
        if path in {"/api/owner/users", "/api/admin/users"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                registry = save_user_registry(body, actor["role"])
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "payload": registry})
            return
        if path == "/api/missions/start":
            try:
                body = read_json(self)
                template_id = body.get("templateId")
                objective = body.get("objective")
                profile = body.get("profile")
                if not isinstance(template_id, str) or not template_id.strip():
                    raise ValueError("templateId is required")
                if not isinstance(objective, str) or not objective.strip():
                    raise ValueError("objective is required")
                if profile is not None and not isinstance(profile, str):
                    raise ValueError("profile must be a string")
                actor = actor or {"actorId": "user", "role": "user", "organizationId": str(organization_registry().get("activeOrganizationId") or "")}
                user_ids = clean_string_list(body.get("userIds"), limit=50)
                domain_capability_keys = clean_string_list(body.get("domainCapabilityKeys"), limit=50)
                authorize_mission_bindings(actor, user_ids, domain_capability_keys)
                delivery_context = normalize_delivery_context(body.get("delivery"))
                payload = start_orchestrated_mission(
                    template_id.strip(),
                    objective.strip()[:12000],
                    mock=bool(body.get("mock")),
                    profile=profile.strip() if isinstance(profile, str) and profile.strip() else None,
                    operator_role=actor["role"],
                    user_ids=user_ids,
                    domain_capability_keys=domain_capability_keys,
                    delivery_context=delivery_context,
                    actor_user_id=actor["actorId"],
                    organization_id=str(actor.get("organizationId") or ""),
                    workflow_origin=body.get("origin"),
                )
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 202, payload)
            return
        if path in {"/api/owner/control-policy", "/api/admin/control-policy"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                policy = save_control_policy(body, actor["role"])
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "policy": policy, "payload": policy})
            return
        if path in {"/api/owner/integrations", "/api/admin/integrations"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                registry = save_integration_registry(body, actor["role"])
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "registry": registry, "payload": registry})
            return
        if path in {"/api/owner/auth-policy", "/api/admin/auth-policy"}:
            try:
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                require_actor_role(actor, {"owner", "admin"})
                policy = save_auth_policy(body, actor["role"])
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "policy": policy, "payload": policy})
            return
        if path == "/api/auth/session":
            try:
                body = read_json(self)
                oidc_token = str(body.get("oidcToken") or body.get("idToken") or "")
                if oidc_token:
                    payload = issue_oidc_exchange_session(oidc_token)
                else:
                    if identity_mode() != "development-local" or not is_loopback_request(self):
                        raise PermissionError("OIDC token exchange is required")
                    local_actor = actor_from_payload(body, str(body.get("operatorRole") or "user"))
                    payload = issue_control_plane_session(local_actor["actorId"], local_actor["role"])
                append_auth_audit("session-issued", str(payload.get("claims", {}).get("sub") or ""), details={"authnMethod": payload.get("claims", {}).get("authn_method")})
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "session": payload, "payload": payload})
            return
        if path == "/api/auth/logout":
            try:
                payload = revoke_control_plane_session(str(actor.get("accessToken") or ""), str(actor.get("actorId") or "")) if actor else {"ok": True}
                self.response_headers = [
                    ("Set-Cookie", "present_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"),
                    ("Set-Cookie", "present_csrf=; Path=/; SameSite=Lax; Max-Age=0"),
                ]
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, payload)
            return
        if path == "/api/control-plane/execute":
            try:
                body = read_json(self)
                if not actor or actor.get("sessionVerified") is not True:
                    raise PermissionError("signed session is required")
                payload = control_plane_execute_action(body, actor)
            except PermissionError as exc:
                json_response(self, 401, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 202, payload)
            return
        if path.startswith("/api/missions/") and (
            path.endswith("/approve") or path.endswith("/pause") or path.endswith("/resume")
        ):
            try:
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "missions":
                    raise ValueError("invalid mission action path")
                body = read_json(self)
                actor = actor or {"actorId": "user", "role": "user"}
                if parts[3] == "approve":
                    note = body.get("note", "")
                    if not isinstance(note, str):
                        raise ValueError("note must be a string")
                    payload = approve_mission(parts[2], actor["role"], note, actor["actorId"], actor)
                elif parts[3] == "pause":
                    payload = pause_mission(parts[2], actor["role"], actor["actorId"], actor)
                else:
                    payload = resume_mission(parts[2], actor["role"], actor["actorId"], actor)
            except KeyError as exc:
                json_response(self, 404, {"ok": False, "error": str(exc)})
                return
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except RuntimeError as exc:
                json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 202, payload)
            return
        if path == "/api/runtime-profiles/validate":
            try:
                body = read_json(self)
                require_actor_role(actor or {}, {"owner", "admin"})
                profile = body.get("profile")
                if not isinstance(profile, dict):
                    raise ValueError("profile is required")
                status, payload = worker_json_command(["runtime-profile-validate"], profile)
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, status, payload)
            return
        if path == "/api/runtime-profiles/save":
            try:
                body = read_json(self)
                require_actor_role(actor or {}, {"owner", "admin"})
                operator = worker_operator_role(str(actor.get("role") if actor else "user"))
                profile = body.get("profile")
                if not isinstance(profile, dict):
                    raise ValueError("profile is required")
                before = read_json_file(RUNTIME_PROFILE_REGISTRY_PATH)
                status, payload = worker_json_command(["runtime-profile-save", "--operator-role", operator], profile)
                if status == 200:
                    record_mutation(
                        "runtime-profile-saved",
                        operator,
                        RUNTIME_PROFILE_REGISTRY_PATH,
                        before=before,
                        after=read_json_file(RUNTIME_PROFILE_REGISTRY_PATH),
                        details={"profileId": profile.get("id")},
                    )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, status, payload)
            return
        if path == "/api/runtime-profiles/clone":
            try:
                body = read_json(self)
                require_actor_role(actor or {}, {"owner", "admin"})
                operator = worker_operator_role(str(actor.get("role") if actor else "user"))
                source = body.get("sourceId")
                new_id = body.get("newId")
                label = body.get("label") or ""
                if not isinstance(source, str) or not source.strip():
                    raise ValueError("sourceId is required")
                if not isinstance(new_id, str) or not new_id.strip():
                    raise ValueError("newId is required")
                args = ["runtime-profile-clone", source.strip(), new_id.strip(), "--operator-role", operator]
                if isinstance(label, str) and label.strip():
                    args.extend(["--label", label.strip()])
                before = read_json_file(RUNTIME_PROFILE_REGISTRY_PATH)
                status, payload = worker_json_command(args)
                if status == 200:
                    record_mutation(
                        "runtime-profile-cloned",
                        operator,
                        RUNTIME_PROFILE_REGISTRY_PATH,
                        before=before,
                        after=read_json_file(RUNTIME_PROFILE_REGISTRY_PATH),
                        details={"sourceId": source.strip(), "newId": new_id.strip()},
                    )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, status, payload)
            return
        if path == "/api/model-roles/save":
            try:
                body = read_json(self)
                require_actor_role(actor or {}, {"owner", "admin"})
                operator = worker_operator_role(str(actor.get("role") if actor else "user"))
                role = body.get("role")
                if not isinstance(role, dict):
                    raise ValueError("role is required")
                before = read_json_file(MODEL_ROLE_REGISTRY_PATH)
                status, payload = worker_json_command(["model-role-save", "--operator-role", operator], role)
                if status == 200:
                    record_mutation(
                        "model-role-saved",
                        operator,
                        MODEL_ROLE_REGISTRY_PATH,
                        before=before,
                        after=read_json_file(MODEL_ROLE_REGISTRY_PATH),
                        details={"roleId": role.get("id")},
                    )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, status, payload)
            return
        if path == "/api/model-roles/delete":
            try:
                body = read_json(self)
                require_actor_role(actor or {}, {"owner", "admin"})
                operator = worker_operator_role(str(actor.get("role") if actor else "user"))
                role_id = body.get("roleId")
                if not isinstance(role_id, str) or not role_id.strip():
                    raise ValueError("roleId is required")
                before = read_json_file(MODEL_ROLE_REGISTRY_PATH)
                status, payload = worker_json_command(["model-role-delete", role_id.strip(), "--operator-role", operator])
                if status == 200:
                    record_mutation(
                        "model-role-deleted",
                        operator,
                        MODEL_ROLE_REGISTRY_PATH,
                        before=before,
                        after=read_json_file(MODEL_ROLE_REGISTRY_PATH),
                        details={"roleId": role_id.strip()},
                    )
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, status, payload)
            return
        if path == "/ask":
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("question body must be 1..128 KiB")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                question = (form.get("question") or [""])[0].strip()
                if not question:
                    raise ValueError("question is required")
                profile = (form.get("profile") or [""])[0].strip() or None
                job_id = start_job(
                    question[:12000], [], bool(form.get("mock")), profile,
                    str(actor.get("role") if actor else "user"), actor_user_id=str(actor.get("actorId") if actor else "user"),
                    organization_id=str(actor.get("organizationId") if actor else ""),
                )
            except Exception as exc:  # noqa: BLE001
                html_response(self, 400, render_shell(f'<section class="panel"><h2>Could not start DC13</h2><p>{escape_html(exc)}</p><p><a href="/">Try again</a></p></section>'))
                return
            self.send_response(303)
            self.send_header("Location", f"/job/{job_id}")
            self.end_headers()
            return
        if path.startswith("/api/chat/") and path.endswith("/follow-up"):
            try:
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "chat"] or parts[3] != "follow-up":
                    raise ValueError("invalid follow-up path")
                body = read_json(self)
                content = body.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("follow-up is required")
                with JOBS_LOCK:
                    existing_job = dict(JOBS.get(parts[2], {}))
                if not existing_job:
                    raise KeyError("chat job not found")
                if not mission_visible_to_actor(existing_job, actor or {"actorId": "user", "role": "user"}):
                    raise PermissionError("chat job is not visible to this actor")
                event = append_follow_up(parts[2], content)
                with JOBS_LOCK:
                    payload = dict(JOBS.get(parts[2], {}))
            except KeyError:
                json_response(self, 404, {"ok": False, "error": "chat job not found"})
                return
            except RuntimeError as exc:
                json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 202, {"ok": True, "jobId": parts[2], "event": event,
                                      "state": payload.get("state", "running"),
                                      "progress": job_api_payload(parts[2], payload).get("progress", {})})
            return
        if path.startswith("/api/chat/") and path.endswith("/decision"):
            try:
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "chat"] or parts[3] != "decision":
                    raise ValueError("invalid decision path")
                body = read_json(self)
                option_id = body.get("optionId")
                free_text = body.get("freeText", "")
                if not isinstance(option_id, str):
                    option_id = ""
                if not isinstance(free_text, str):
                    raise ValueError("decision free text must be a string")
                with JOBS_LOCK:
                    existing_job = dict(JOBS.get(parts[2], {}))
                if not existing_job:
                    raise KeyError("chat job not found")
                if not mission_visible_to_actor(existing_job, actor or {"actorId": "user", "role": "user"}):
                    raise PermissionError("chat job is not visible to this actor")
                event = append_decision_response(parts[2], option_id, free_text)
                with JOBS_LOCK:
                    payload = dict(JOBS.get(parts[2], {}))
            except KeyError:
                json_response(self, 404, {"ok": False, "error": "chat job not found"})
                return
            except RuntimeError as exc:
                json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            except PermissionError as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 202, {"ok": True, "jobId": parts[2], "event": event,
                                      "state": payload.get("state", "running"),
                                      "progress": job_api_payload(parts[2], payload).get("progress", {})})
            return
        if path != "/api/chat":
            self.send_error(404)
            return
        try:
            body = read_json(self, MAX_UPLOAD_REQUEST_BYTES)
            question = body.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("question is required")
            messages = clean_messages(body.get("messages"))
            uploads = body.get("uploads")
            profile = body.get("profile")
            if profile is not None and not isinstance(profile, str):
                raise ValueError("profile must be a string")
            actor = actor or {"actorId": "user", "role": "user"}
            work_mode = body.get("workMode")
            if work_mode is not None and not isinstance(work_mode, str):
                raise ValueError("workMode must be a string")
            job_id = start_job(question.strip()[:12000], messages, bool(body.get("mock")),
                               profile.strip() if isinstance(profile, str) and profile.strip() else None,
                               actor["role"],
                               uploads,
                               work_mode.strip() if isinstance(work_mode, str) and work_mode.strip() else None,
                               actor["actorId"], str(actor.get("organizationId") or ""))
        except Exception as exc:  # noqa: BLE001
            json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        # Tell the caller who the job was recorded against. The console polls with
        # this rather than re-deriving its identity, because the two only have to
        # disagree once -- a session that expires, a cookie discarded mid-run, an
        # edited actor field -- for the poll to be refused a job the same person
        # just created.
        json_response(self, 202, {"ok": True, "jobId": job_id, "state": "running",
                                  "actorUserId": actor["actorId"], "operatorRole": actor["role"]})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> int:
    global ACTIVE_COORDINATOR_PROVIDER, ACTIVE_COORDINATOR_ROLE, ACTIVE_RUNTIME_PROFILE
    parser = argparse.ArgumentParser(description="Run the local DC13 chat page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", choices=["claude", "glimmer"],
                        default=os.environ.get(COORDINATOR_PROVIDER_ENV))
    parser.add_argument("--role", default=os.environ.get(COORDINATOR_ROLE_ENV))
    parser.add_argument("--profile", default=os.environ.get(COORDINATOR_RUNTIME_PROFILE_ENV))
    args = parser.parse_args()
    ACTIVE_COORDINATOR_PROVIDER = args.provider
    ACTIVE_COORDINATOR_ROLE = args.role
    provider_profiles = {"claude": "dc13.claude", "glimmer": "dc13.local"}
    ACTIVE_RUNTIME_PROFILE = args.profile or provider_profiles.get(args.provider)
    if args.provider:
        os.environ[COORDINATOR_PROVIDER_ENV] = args.provider
    if ACTIVE_COORDINATOR_ROLE:
        os.environ[COORDINATOR_ROLE_ENV] = ACTIVE_COORDINATOR_ROLE
    os.environ[COORDINATOR_RUNTIME_PROFILE_ENV] = active_runtime_profile()
    supervisor = supervise_missions_on_startup()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"DC13 chat is available at http://{args.host}:{args.port} using {active_runtime_profile()} "
        f"(missions scanned {supervisor['scanned']}, orphaned {supervisor['orphaned']})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
