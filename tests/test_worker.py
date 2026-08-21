"""End-to-end tests for present-worker. Run with: pytest -q (from worker/).

These exercise the real CLI as a subprocess -- deliberately, since the
contract that matters is "what does automation see on stdout," not
internal function calls.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import re
import runpy
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

WORKER_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
BIN = WORKER_DIR / "bin" / "present-worker"
STEEL_MISSION = WORKER_DIR / "bin" / "steel-mission"
SSH_GUARD = WORKER_DIR / "bin" / "present-worker-ssh-guard"
BROKER = WORKER_DIR / "bin" / "present-lease-broker"
FILE_LOCK = WORKER_DIR / "bin" / "present-file-lock"
DOCKER_PROVISIONER = WORKER_DIR / "bin" / "present-docker-provisioner"
PRIVATE_RUNNER = WORKER_DIR / "bin" / "present-private-runner"

sys.path.insert(0, str(WORKER_DIR))
from adapters import claude_adapter, codex_adapter, common, glimmer_adapter, schema_check, verifier  # noqa: E402

sys.path.insert(0, str(TESTS_DIR))
from support import broker_state_document  # noqa: E402


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def run_worker_result(*args: str, input_text: str | None = None) -> tuple[int, dict, dict, str, str]:
    result = subprocess.run([str(BIN), *args], input=input_text, capture_output=True, text=True, timeout=60)
    return result.returncode, _parse_json(result.stdout), _parse_json(result.stderr), result.stdout, result.stderr


def run_worker(*args: str) -> tuple[int, dict]:
    code, stdout_payload, _stderr_payload, _stdout, _stderr = run_worker_result(*args)
    return code, stdout_payload


def run_steel_mission(*args: str) -> tuple[int, dict, dict, str, str]:
    result = subprocess.run([str(STEEL_MISSION), *args], capture_output=True, text=True, timeout=60)
    return result.returncode, _parse_json(result.stdout), _parse_json(result.stderr), result.stdout, result.stderr


def run_broker_result(*args: str, input_text: str | None = None) -> tuple[int, dict, dict, str, str]:
    result = subprocess.run([str(BROKER), *args], input=input_text, capture_output=True, text=True, timeout=120)
    return result.returncode, _parse_json(result.stdout), _parse_json(result.stderr), result.stdout, result.stderr


def run_ssh_guard_result(command: str) -> tuple[int, dict, dict, str, str]:
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": command}
    result = subprocess.run([str(SSH_GUARD)], capture_output=True, text=True, timeout=60, env=env)
    return result.returncode, _parse_json(result.stdout), _parse_json(result.stderr), result.stdout, result.stderr


def run_ssh_guard(command: str) -> tuple[int, dict]:
    code, stdout_payload, _stderr_payload, _stdout, _stderr = run_ssh_guard_result(command)
    return code, stdout_payload


def purge_task(task_id: str) -> None:
    """Remove every worktree role, the stage log, and any leftover task
    contract for task_id. Idempotent regardless of how a previous run
    ended -- a partially-failed prior run must never block a fresh one."""
    for role in ("glimmer", "build", "codex", "muse"):
        worktree = common.WORKTREES_DIR / f"{task_id}-{role}"
        subprocess.run(["git", "-C", str(common.DEFAULT_REPO), "worktree", "remove", "--force", str(worktree)], capture_output=True)
    (WORKER_DIR / "logs" / f"{task_id}.jsonl").unlink(missing_ok=True)
    (common.TASKS_DIR / f"{task_id}.json").unlink(missing_ok=True)
    shutil.rmtree(common.TASKS_DIR / task_id, ignore_errors=True)
    shutil.rmtree(WORKER_DIR / "jobs" / f"{task_id}-build", ignore_errors=True)
    (common.TEST_RESULTS_DIR / f"{task_id}-verify.json").unlink(missing_ok=True)


def current_git_branch(repo: Path = WORKER_DIR) -> str:
    result = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, timeout=10)
    branch = result.stdout.strip()
    return branch or "main"


def signed_private_runner_request(workspace: Path, key: str, **overrides: object) -> dict:
    request = {
        "schemaVersion": 1,
        "requestId": "pre-" + "1" * 24,
        "missionId": "ms-" + "2" * 24,
        "taskId": "DEV-123456",
        "phase": "inspect",
        "workspacePath": str(workspace),
        "argv": ["python3", "-c", "print('private-runner-ok')"],
        "timeoutSeconds": 30,
        "environment": {},
        "stdin": "",
        **overrides,
    }
    payload_hash = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request["attestation"] = {
        "algorithm": "hmac-sha256",
        "signerId": "steel-mission-control-plane",
        "payloadHash": payload_hash,
        "signature": hmac.new(key.encode(), payload_hash.encode(), hashlib.sha256).hexdigest(),
    }
    return request


def test_version():
    code, payload = run_worker("version")
    assert code == 0
    assert payload["name"] == "present-worker"
    assert payload["schemaRegistryHash"] == common.schema_registry_hash()
    assert schema_check.validate(payload, "canonical/worker-version-v1.json") == []


def test_steel_mission_wrapper_exposes_worker_version_and_doctor():
    code, payload, _stderr_payload, _stdout, _stderr = run_steel_mission("worker", "version")
    assert code == 0
    assert payload["name"] == "present-worker"
    assert payload["schemaRegistryHash"] == common.schema_registry_hash()

    code, payload, _stderr_payload, _stdout, _stderr = run_steel_mission("doctor")
    assert code == 0
    assert payload["ok"] is True
    assert payload["checks"]["steel_mission_chat"]["exists"] is True


def test_running_server_persists_configuration_outside_the_product_tree(tmp_path):
    """Exercise the real entrypoint and the normalizing users write end to end."""
    product = tmp_path / "product"
    product.mkdir()
    (product / "bin").mkdir()
    shutil.copy2(STEEL_MISSION, product / "bin" / "steel-mission")
    for directory in ("adapters", "config", "schemas", "starter-company", "steel-mission-chat"):
        shutil.copytree(WORKER_DIR / directory, product / directory)

    shipped_users = product / "config" / "users.json"
    shipped_before = shipped_users.read_bytes()
    state_home = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "STEEL_MISSION_HOST": "127.0.0.1",
        "STEEL_MISSION_PORT": str(port),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for variable in (
        "STEEL_MISSION_CONFIG_DIR",
        "PRESENT_CONFIG_DIR",
        "STEEL_MISSION_STATE_DIR",
        "STEEL_MISSION_MISSIONS_DIR",
        "PRESENT_MISSIONS_DIR",
    ):
        environment.pop(variable, None)
    base_url = f"http://127.0.0.1:{port}"

    def request_json(path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read())

    def start_server() -> subprocess.Popen:
        process = subprocess.Popen(
            [str(product / "bin" / "steel-mission"), "serve"],
            cwd=product,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"server exited during startup: {stdout}\n{stderr}")
            try:
                request_json("/api/health")
                return process
            except OSError:
                time.sleep(0.05)
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(f"server did not become healthy: {stdout}\n{stderr}")

    def stop_server(process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    process = start_server()
    try:
        users = request_json("/api/owner/users")["users"]
        users.append({
            "id": "runtime-owner",
            "name": "Runtime Owner",
            "email": "runtime@example.test",
            "role": "owner",
            "status": "active",
            "assignedCapabilities": [],
            "organizationIds": ["northstar-forge"],
            "identitySubjects": [],
            "externalIdentities": {"github": [], "slack": [], "jira": []},
        })
        saved = request_json("/api/owner/users", {"users": users})
        assert any(user["id"] == "runtime-owner" for user in saved["payload"]["users"])
    finally:
        stop_server(process)

    runtime_users = state_home / "steel-mission" / "config" / "users.json"
    assert shipped_users.read_bytes() == shipped_before
    assert any(
        user["id"] == "runtime-owner"
        for user in json.loads(runtime_users.read_text())["users"]
    )

    process = start_server()
    try:
        reloaded = request_json("/api/owner/users")
        assert any(user["id"] == "runtime-owner" for user in reloaded["users"])
    finally:
        stop_server(process)


def test_running_server_seeds_installation_identity_without_touching_the_product(
    tmp_path, daemon_process_manager
):
    product = tmp_path / "product"
    product.mkdir()
    (product / "bin").mkdir()
    shutil.copy2(STEEL_MISSION, product / "bin" / "steel-mission")
    for directory in ("adapters", "config", "schemas", "starter-company", "steel-mission-chat"):
        shutil.copytree(WORKER_DIR / directory, product / directory)

    shipped_before = {
        path.relative_to(product): path.read_bytes()
        for directory in ("config", "starter-company")
        for path in (product / directory).rglob("*")
        if path.is_file()
    }
    seed_dir = tmp_path / "installation-config-seed"
    seed_dir.mkdir()
    organization_registry = json.loads(
        (product / "config" / "organizations.json").read_text()
    )
    organization = organization_registry["organizations"][0]
    organization.update({
        "id": "steel-mission",
        "name": "Steel Mission",
        "slug": "steel-mission",
        "identifiers": {
            "legalName": "Steel Mission",
            "domain": "",
            "country": "CH",
            "environment": "local",
            "dataClassification": "installation-private",
        },
        "knowledgeSources": {"repositories": [], "documents": []},
        "notes": "Installation-owned configuration used only by this test.",
    })
    organization_registry["activeOrganizationId"] = "steel-mission"
    (seed_dir / "organizations.json").write_text(
        json.dumps(organization_registry, indent=2) + "\n"
    )
    (seed_dir / "users.json").write_text(json.dumps({
        "schemaVersion": 1,
        "producedAt": "2026-08-21T00:00:00Z",
        "producer": "installation-config-seed",
        "users": [{
            "id": "andrew-hermann",
            "name": "Andrew Hermann",
            "email": "",
            "role": "owner",
            "status": "active",
            "assignedCapabilities": [],
            "organizationIds": ["steel-mission"],
            "identitySubjects": [],
            "externalIdentities": {"github": [], "slack": [], "jira": []},
        }],
    }, indent=2) + "\n")

    state_home = tmp_path / "state"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        "XDG_STATE_HOME": str(state_home),
        "STEEL_MISSION_CONFIG_SEED_DIR": str(seed_dir),
        "STEEL_MISSION_HOST": "127.0.0.1",
        "STEEL_MISSION_PORT": str(port),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    base_url = f"http://127.0.0.1:{port}"

    def request_json(path: str) -> dict:
        with urlopen(base_url + path, timeout=2) as response:
            return json.loads(response.read())

    def start_server():
        process = daemon_process_manager.start(
            [str(product / "bin" / "steel-mission"), "serve"],
            name="installation-seed-server",
            cwd=product,
            env=environment,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if process.process.poll() is not None:
                raise AssertionError(
                    f"server exited during startup: {process.stdout()}\n{process.stderr()}"
                )
            try:
                request_json("/api/health")
                return process
            except OSError:
                time.sleep(0.05)
        raise AssertionError(
            f"server did not become healthy: {process.stdout()}\n{process.stderr()}"
        )

    process = start_server()
    whoami = request_json("/api/auth/whoami")["actor"]
    assert whoami["actorId"] == "andrew-hermann"
    assert whoami["role"] == "owner"
    assert whoami["organizationId"] == "steel-mission"
    users = request_json("/api/owner/users")["users"]
    assert [(user["name"], user["role"]) for user in users] == [
        ("Andrew Hermann", "owner")
    ]
    organizations = request_json("/api/owner/organizations")
    assert organizations["payload"]["activeOrganizationId"] == "steel-mission"
    assert [item["name"] for item in organizations["payload"]["organizations"]] == [
        "Steel Mission"
    ]

    runtime_config = state_home / "steel-mission" / "config"
    first_seed = {
        name: (runtime_config / name).read_bytes()
        for name in ("organizations.json", "users.json")
    }
    process.stop()

    restarted = start_server()
    assert request_json("/api/auth/whoami")["actor"]["actorId"] == "andrew-hermann"
    assert {
        name: (runtime_config / name).read_bytes()
        for name in ("organizations.json", "users.json")
    } == first_seed
    restarted.stop()

    assert {
        path.relative_to(product): path.read_bytes()
        for directory in ("config", "starter-company")
        for path in (product / directory).rglob("*")
        if path.is_file()
    } == shipped_before


def test_installation_config_seed_refuses_symlinks(tmp_path, monkeypatch):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    shipped = tmp_path / "shipped" / "users.json"
    shipped.parent.mkdir()
    shipped.write_text("{}\n")
    seed_dir = tmp_path / "config-seed"
    seed_dir.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("{}\n")
    (seed_dir / "users.json").symlink_to(target)
    monkeypatch.setenv("STEEL_MISSION_CONFIG_SEED_DIR", str(seed_dir))

    with pytest.raises(ValueError, match="must not be a symlink"):
        chat["installation_config_seed_source"](shipped, tmp_path / "state")


def test_status_schema_valid():
    code, payload = run_worker("status")
    assert code == 0
    errors = schema_check.validate(payload, "worker-status-v1.schema.json")
    assert errors == [], errors


def test_status_advertises_protocol_210_capability_registry():
    code, payload = run_worker("status")
    assert code == 0
    assert payload["schemaRegistryHash"] == common.schema_registry_hash()
    assert payload["detail"]["protocolVersion"] == "2.10"
    assert "coordination-report" in payload["capabilities"]
    assert "workflow-dag" in payload["capabilities"]
    assert payload["detail"]["workerIdentity"]["kind"] == "macbook-local"
    assert payload["detail"]["workerIdentity"]["resourceLimits"]["maxWorkflowParallel"] == 8
    assert payload["detail"]["schemaRegistry"]["schema"] == "schema-registry-v1"
    assert payload["detail"]["schemaRegistry"]["hash"] == payload["schemaRegistryHash"]
    assert payload["detail"]["policyPackRegistry"]["schema"] == "policy-pack-registry-v1"
    registry = payload["detail"]["capabilityRegistry"]
    assert registry["workflow-dag"]["available"] is True
    assert registry["workflow-dag"]["constraints"]["maxParallel"] == 8
    assert "evidence-manifest-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "handoff-package-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "imported-artifact-index-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "redaction-report-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "task-state-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "worker-lease-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "context-checkpoint-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "workflow-input-context-v1" in registry["workflow-dag"]["outputSchemas"]
    assert "context-checkpoint-v1" in registry["coordination-report"]["outputSchemas"]
    assert registry["deterministic-verify"]["authority"] == "acceptance"
    assert registry["deterministic-verify"]["constraints"]["onlyPassAuthority"] is True
    assert registry["candidate"]["verb"] == "fix"
    assert registry["candidate"]["targets"] == ["present-repository"]


def test_worker_tool_contracts_describe_agent_tool_authority():
    code, payload = run_worker("tool-contracts")
    assert code == 0
    assert schema_check.validate(payload, "canonical/agent-tool-contract-registry-v1.json") == []

    contracts = {contract["capabilityId"]: contract for contract in payload["contracts"]}
    assert contracts["deterministic-verify"]["deterministicAcceptanceAuthority"] is True
    assert contracts["deterministic-verify"]["permissions"]["canGateAcceptance"] is True
    assert contracts["deterministic-verify"]["authority"] == "acceptance"
    assert "verification-v1" in contracts["deterministic-verify"]["outputSchemas"]

    assert contracts["coordination-report"]["deterministicAcceptanceAuthority"] is False
    assert contracts["coordination-report"]["permissions"]["canGateAcceptance"] is False
    assert contracts["coordination-report"]["authority"] == "advisory-status"


def test_status_distinguishes_installed_from_authenticated():
    _, payload = run_worker("status")
    caps = payload["detail"]["providers"]
    for provider in ("claude", "codex"):
        cap = caps[provider]
        assert set(("installed", "authenticated", "ready")) <= set(cap.keys())


def test_status_does_not_require_optional_glimmer():
    code, payload = run_worker("status")
    assert code == 0
    assert "glimmer" in payload["detail"]["optionalProviders"]
    assert "glimmer" not in payload["missing"]
    if "claude" not in payload["missing"] and "codex" not in payload["missing"] and "git" not in payload["missing"]:
        assert payload["classification"] == "READY"


def test_claude_live_plan_uses_canonical_envelope():
    captured = {}
    original_auth = claude_adapter.authenticated
    original_invoke = claude_adapter._invoke
    try:
        claude_adapter.authenticated = lambda: (True, {})
        def fake_invoke(_prompt, _schema, **kwargs):
            captured.update(kwargs)
            return ({
                "summary": "bounded plan",
                "steps": [{"id": "s1", "description": "implement", "dependsOn": []}],
                "openQuestions": [],
            }, None)
        claude_adapter._invoke = fake_invoke
        payload = claude_adapter.plan(
            "DEV-900006", "live", "requirement", model="claude-opus-5", effort="high",
        )
        assert payload["mock"] is False
        assert payload["provenance"]["source"] == "worker"
        assert captured == {"model": "claude-opus-5", "effort": "high"}
        assert schema_check.validate(payload, "canonical/plan-v1.json") == []
    finally:
        claude_adapter.authenticated = original_auth
        claude_adapter._invoke = original_invoke


def test_codex_authentication_contract_uses_exit_status_not_wording():
    original_which = codex_adapter.common.which
    original_run = codex_adapter.common.run
    try:
        codex_adapter.common.which = lambda binary: "/opt/homebrew/bin/codex"
        codex_adapter.common.run = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="Authentication mode: ChatGPT", stderr=""
        )
        authenticated, _ = codex_adapter.authenticated()
        assert authenticated is True
    finally:
        codex_adapter.common.which = original_which
        codex_adapter.common.run = original_run


def test_codex_live_fix_uses_supported_noninteractive_approval_config(tmp_path):
    captured = {}
    original_auth = codex_adapter.authenticated
    original_run = codex_adapter.common.run
    try:
        codex_adapter.authenticated = lambda: (True, {})

        def fake_run(command, *args, **kwargs):
            if command[:4] == ["codex", "--ask-for-approval", "never", "exec"]:
                captured["command"] = command
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps({"summary": "ok", "addressedFindings": []}))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        codex_adapter.common.run = fake_run
        payload = codex_adapter.fix(
            "DEV-900007", "live", "requirement", "plan", "abc123", [], [], tmp_path,
        )
        assert captured["command"][:4] == ["codex", "--ask-for-approval", "never", "exec"]
        assert captured["command"].count("--ask-for-approval") == 1
        assert payload["schemaVersion"] == 1
        assert payload["mock"] is False
    finally:
        codex_adapter.authenticated = original_auth
        codex_adapter.common.run = original_run


def test_codex_coordinator_report_is_read_only_schema_constrained_and_canonical():
    captured = {}
    original_auth = codex_adapter.authenticated
    original_run = codex_adapter.common.run
    try:
        codex_adapter.authenticated = lambda: (True, {})

        def fake_run(command, *args, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input")
            schema_path = Path(command[command.index("--output-schema") + 1])
            captured["schema"] = json.loads(schema_path.read_text())
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps({
                "summary": "One live record needs attention.",
                "items": [{
                    "subject": "DEV-900188",
                    "status": "ACTIVE",
                    "stateClass": "canonical",
                    "source": "worker snapshot",
                    "freshness": "current",
                    "note": None,
                }],
                "notChecked": [],
                "contradictions": [],
                "advisoryNote": claude_adapter.COORDINATOR_ADVISORY_NOTE,
            }))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        codex_adapter.common.run = fake_run
        payload = codex_adapter.coordinator_report(
            "DEV-900188",
            "live",
            "What needs attention?",
            {"tasks": [{"taskId": "DEV-900188", "state": "active"}]},
            {"probe": "ok", "packageId": "DC13", "corpusGeneration": 1, "currentThrough": "today"},
            model="gpt-5.6-terra",
            effort="high",
        )

        command = captured["command"]
        assert command[:4] == ["codex", "--ask-for-approval", "never", "exec"]
        assert command[command.index("--model") + 1] == "gpt-5.6-terra"
        assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--output-schema" in command
        item_schema = captured["schema"]["properties"]["items"]["items"]
        assert item_schema["required"] == ["subject", "status", "stateClass", "source", "freshness", "note"]
        assert item_schema["properties"]["note"]["type"] == ["string", "null"]
        assert "STATE" in captured["input"]
        assert payload["producer"] == "steel-mission coordination-report (codex)"
        assert payload["mock"] is False
        assert payload["packIdentity"]["probe"] == "ok"
        assert "note" not in payload["items"][0]
        assert schema_check.validate(payload, "canonical/coordination-report-v1.json") == []
    finally:
        codex_adapter.authenticated = original_auth
        codex_adapter.common.run = original_run


def test_codex_diff_stat_counts_untracked_files(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)

    (tmp_path / "new.txt").write_text("one\ntwo\n")
    assert codex_adapter._diff_stat(tmp_path) == {"filesChanged": 1, "insertions": 2, "deletions": 0}


def test_protocol_22_missing_contract_and_deprecated_specialist_are_structured():
    task_id = "DEV-123456"
    purge_task(task_id)
    try:
        code, _, payload, stdout, stderr = run_worker_result("plan", task_id)
        assert code == 30 and stdout == "" and stderr
        assert payload["status"] == "BLOCKED"
        code, _, payload, stdout, stderr = run_worker_result("specialist", task_id)
        assert code == 30 and stdout == "" and stderr
        assert payload["status"] == "DEPRECATED"
        assert payload["task_id"] == task_id
        assert payload["verb"] == "specialist"
    finally:
        purge_task(task_id)


def test_ssh_guard_allows_protocol_210_status():
    code, payload = run_ssh_guard("present-worker status")
    assert code == 0
    assert payload["schemaVersion"] == 1
    assert payload["producer"] == "present-worker status"
    assert payload["detail"]["protocolVersion"] == "2.10"
    assert payload["detail"]["advisoryVerbs"] == ["coordination-report"]
    assert payload["detail"]["workflowVerbs"] == ["workflow", "workflow-cancel"]
    assert "worker-lease" in payload["detail"]["lifecycleVerbs"]
    assert "capabilityRegistry" in payload["detail"]


def test_ssh_guard_allows_task_state():
    task_id = "DEV-900058"
    purge_task(task_id)
    code, payload = run_ssh_guard(f"present-worker task-state {task_id}")
    assert code == 0
    assert payload["status"] == "EMPTY"
    assert schema_check.validate(payload, "canonical/task-state-v1.json") == []


def test_ssh_guard_allows_workflow_cancel_and_cleanup_dry_run():
    task_id = "DEV-900059"
    purge_task(task_id)
    try:
        code, payload = run_ssh_guard(f"present-worker workflow-cancel {task_id}")
        assert code == 0
        assert payload["status"] == "NO_ACTIVE_WORK"
        assert schema_check.validate(payload, "canonical/workflow-cancel-v1.json") == []

        code, payload = run_ssh_guard(f"present-worker task-cleanup {task_id} --dry-run")
        assert code == 0
        assert payload["status"] == "DRY_RUN"
        assert schema_check.validate(payload, "canonical/task-cleanup-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_worker_lease_acquire_renew_release_and_guard():
    task_id = "DEV-900061"
    purge_task(task_id)
    contract_hash = "1" * 64
    try:
        code, lease, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire",
            "--contract-hash", contract_hash, "--ttl-seconds", "60")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert lease["status"] == "ACTIVE"
        assert lease["contractHash"] == contract_hash
        assert lease["worker"]["kind"] == "macbook-local"
        assert schema_check.validate(lease, "canonical/worker-lease-v1.json") == []

        code, renewed, stderr_payload, stdout, stderr = run_ssh_guard_result(
            f"present-worker worker-lease {task_id} --renew --lease-id {lease['leaseId']} --ttl-seconds 120")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert renewed["leaseId"] == lease["leaseId"]
        assert renewed["renewalCount"] == 1
        assert renewed["ttlSeconds"] == 120
        assert schema_check.validate(renewed, "canonical/worker-lease-v1.json") == []

        code, released, stderr_payload, stdout, stderr = run_ssh_guard_result(
            f"present-worker worker-lease {task_id} --release --lease-id {lease['leaseId']}")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert released["status"] == "RELEASED"
        assert schema_check.validate(released, "canonical/worker-lease-v1.json") == []
    finally:
        purge_task(task_id)


def _worker_pool(tmp_path, workers: list[dict]) -> Path:
    path = tmp_path / "worker-pool.json"
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "producedAt": common.utc_now(),
        "producer": "test-control-plain",
        "workers": workers,
    }))
    assert schema_check.validate(json.loads(path.read_text()), "canonical/worker-pool-registry-v1.json") == []
    return path


def _broker_state_payload(path: Path) -> dict:
    payload = json.loads(path.read_text())
    assert schema_check.validate(payload, "canonical/broker-state-v1.json") == []
    return payload


def _write_broker_state(tmp_path: Path, tasks: dict, events: list[dict] | None = None) -> Path:
    path = tmp_path / "broker-state.json"
    path.write_text(json.dumps(broker_state_document(tasks, events)))
    assert schema_check.validate(json.loads(path.read_text()), "canonical/broker-state-v1.json") == []
    return path


def _status_for_pool(worker_id: str, *, protocol: str = "2.10", max_parallel: int = 8,
                     trust_level: str = "local-user-trusted",
                     unavailable: list[str] | None = None,
                     produced_at: str | None = None) -> dict:
    code, status = run_worker("status")
    assert code == 0
    status = json.loads(json.dumps(status))
    status["producedAt"] = produced_at or common.utc_now()
    identity = status["detail"]["workerIdentity"]
    identity["id"] = worker_id
    identity["kind"] = worker_id.split(":", 1)[0]
    identity["trustLevel"] = trust_level
    identity["protocolVersion"] = protocol
    identity["resourceLimits"]["maxWorkflowParallel"] = max_parallel
    status["detail"]["protocolVersion"] = protocol
    registry = status["detail"]["capabilityRegistry"]
    for capability in ("plan", "code-review", "review", "security-review", "adversarial", "candidate", "fix"):
        if capability in registry:
            registry[capability]["available"] = True
            registry[capability]["missing"] = []
    for capability in unavailable or []:
        if capability in registry:
            registry[capability]["available"] = False
            registry[capability]["missing"] = ["test"]
    return status


def _isolated_worker_env(tmp_path: Path, worker_id: str) -> dict[str, str]:
    root = tmp_path / worker_id.replace(":", "_")
    return {
        "PRESENT_WORKER_ID": worker_id,
        "PRESENT_WORKER_KIND": worker_id.split(":", 1)[0],
        "PRESENT_WORKER_SURFACE": worker_id.split(":", 1)[0],
        "PRESENT_TASKS_DIR": str(root / "tasks"),
        "PRESENT_LOGS_DIR": str(root / "logs"),
        "PRESENT_TEST_RESULTS_DIR": str(root / "test-results"),
        "PRESENT_JOBS_DIR": str(root / "jobs"),
        "PRESENT_WORKTREES_DIR": str(root / "worktrees"),
    }


def _isolated_worker_entry(tmp_path: Path, worker_id: str, *, command: list[str] | None = None,
                           unavailable: list[str] | None = None,
                           transport_kind: str = "local") -> dict:
    command = command or [str(BIN)]
    return {
        "id": worker_id,
        "command": command,
        "transport": {
            "kind": transport_kind,
            "command": command,
            "timeoutSeconds": 30,
            "env": _isolated_worker_env(tmp_path, worker_id),
        },
        "status": _status_for_pool(worker_id, unavailable=unavailable),
        "health": {"score": 100},
    }


def test_lease_broker_selects_eligible_worker_from_pool(tmp_path):
    task_id = "DEV-900064"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        stale = "2000-01-01T00:00:00Z"
        pool = _worker_pool(tmp_path, [
            {"id": "macbook-local:stale", "command": [str(BIN)],
             "status": _status_for_pool("macbook-local:stale", produced_at=stale), "lastSeenAt": stale},
            {"id": "linux-container:no-verify", "command": [str(BIN)],
             "status": _status_for_pool("linux-container:no-verify", unavailable=["deterministic-verify"])},
            {"id": "macbook-local:selected", "command": [str(BIN)],
             "status": _status_for_pool("macbook-local:selected")},
        ])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "SELECTED"
        assert payload["schemaRegistryHash"] == common.schema_registry_hash()
        assert payload["selectedWorker"] == "macbook-local:selected"
        assert [node["workerId"] for node in payload["placementPlan"]] == [
            "linux-container:no-verify", "macbook-local:selected"]
        assert schema_check.validate(
            payload["artifactTransferManifest"], "canonical/artifact-transfer-manifest-v1.json") == []
        reasons = {candidate["workerId"]: candidate["reasons"] for candidate in payload["candidates"]}
        assert "worker status is stale" in reasons["macbook-local:stale"]
        assert "capability 'deterministic-verify' is unavailable" in reasons["linux-container:no-verify"]
    finally:
        purge_task(task_id)


def test_lease_broker_rejects_worker_when_tool_contract_does_not_satisfy_node(tmp_path):
    task_id = "DEV-900145"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        registry = {
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "present-worker tool contracts",
            "workerId": "macbook-local:bad-contract",
            "contracts": [{
                "capabilityId": "plan",
                "agentId": "claude:plan",
                "toolId": "plan",
                "provider": "claude",
                "authority": "advisory",
                "available": True,
                "permissions": {
                    "filesystem": "read",
                    "network": "provider",
                    "worktreeMutation": False,
                    "canGateAcceptance": False,
                },
                "inputKinds": ["requirement"],
                "outputSchemas": ["wrong-plan-v1"],
                "evidenceRoles": ["advisory-plan"],
                "deterministicAcceptanceAuthority": False,
            }],
        }
        script = tmp_path / "bad_contract_worker.py"
        script.write_text(
            "import json, sys\n"
            f"registry = {registry!r}\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'tool-contracts':\n"
            "    print(json.dumps(registry)); raise SystemExit(0)\n"
            "print(json.dumps({'status': 'BROKER_BLOCKED', 'reason': 'unexpected command'})); raise SystemExit(1)\n"
        )
        pool = _worker_pool(tmp_path, [{
            "id": "macbook-local:bad-contract",
            "command": [sys.executable, str(script)],
            "status": _status_for_pool("macbook-local:bad-contract"),
        }])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), input_text=_workflow_bundle(task_id, nodes))
        assert code != 0
        payload = payload or stderr_payload
        assert payload["status"] == "NO_ELIGIBLE_WORKER"
        reasons = payload["candidates"][0]["reasons"]
        assert "tool contract does not produce output schema 'plan-v1'" in reasons
        assert payload["placementPlan"][0]["workerId"] is None
    finally:
        purge_task(task_id)


def test_lease_broker_rejects_old_protocol_worker(tmp_path):
    task_id = "DEV-900130"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        pool = _worker_pool(tmp_path, [
            {"id": "macbook-local:old-protocol", "command": [str(BIN)],
             "status": _status_for_pool("macbook-local:old-protocol", protocol="2.09"),
             "health": {"score": 100}},
        ])
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), input_text=_workflow_bundle(task_id, nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "NO_ELIGIBLE_WORKER"
        assert "protocol 2.09 is below 2.10" in payload["candidates"][0]["reasons"]
    finally:
        purge_task(task_id)


def test_lease_broker_quarantine_and_attestation_block_worker_selection(tmp_path):
    task_id = "DEV-900131"
    purge_task(task_id)
    try:
        drifted = _isolated_worker_entry(tmp_path, "macbook-local:drifted")
        drifted["statusAttestation"] = {
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "algorithm": "sha256",
            "hash": "0" * 64,
        }
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(tmp_path, "macbook-local:quarantined"),
            drifted,
        ])
        state_path = tmp_path / "broker-state.json"
        code, quarantined, stderr_payload, stdout, stderr = run_broker_result(
            "quarantine-worker", task_id, "--pool", str(pool), "--state", str(state_path),
            "--worker-id", "macbook-local:quarantined", "--reason", "network faults",
            "--operator-role", "admin")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert quarantined["status"] == "WORKER_QUARANTINED"

        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), "--state", str(state_path),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "NO_ELIGIBLE_WORKER"
        reasons = {candidate["workerId"]: candidate["reasons"] for candidate in payload["candidates"]}
        assert any("quarantined" in reason for reason in reasons["macbook-local:quarantined"])
        assert "worker capability attestation does not match current status" in reasons["macbook-local:drifted"]
        state = _broker_state_payload(state_path)
        assert state["operatorAuditLog"][0]["action"] == "quarantine-worker"
        assert any(event["type"] == "worker-quarantined" for event in state["events"])
    finally:
        purge_task(task_id)


def test_broker_and_worker_argparse_errors_include_schema_registry_hash(tmp_path):
    code, stdout_payload, broker_error, stdout, stderr = run_broker_result("select")
    assert code == 30
    assert stdout_payload == {}
    assert stdout == ""
    assert stderr
    assert broker_error["status"] == "BROKER_BLOCKED"
    assert broker_error["schemaRegistryHash"] == common.schema_registry_hash()

    task_id = "DEV-900090"
    purge_task(task_id)
    try:
        code, stdout_payload, worker_error, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--ttl-seconds", "120")
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert stderr
        assert worker_error["status"] == "PROTOCOL_ERROR"
        assert worker_error["schemaRegistryHash"] == common.schema_registry_hash()
    finally:
        purge_task(task_id)


def test_broker_rejects_malformed_pool_status_and_state_at_admission(tmp_path):
    task_id = "DEV-900091"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bad_pool = tmp_path / "bad-pool.json"
        bad_pool.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "test-control-plain",
            "workers": [{"id": "macbook-local:no-command"}],
        }))
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(bad_pool), input_text=_workflow_bundle(task_id, nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "BROKER_BLOCKED"
        assert payload["schemaValidation"]["schemaId"] == "worker-pool-registry-v1"
        assert "$.workers[0].command" in payload["schemaValidation"]["failingJsonPaths"]

        bad_status = _status_for_pool("macbook-local:bad-status")
        bad_status["classification"] = "BANANA"
        bad_status_pool = tmp_path / "bad-status-pool.json"
        bad_status_pool.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "test-control-plain",
            "workers": [{"id": "macbook-local:bad-status", "command": [str(BIN)], "status": bad_status}],
        }))
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(bad_status_pool), input_text=_workflow_bundle(task_id, nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["schemaValidation"]["schemaId"] == "worker-status-v1"
        assert "$.classification" in payload["schemaValidation"]["failingJsonPaths"]

        legacy_state = tmp_path / "legacy-state.json"
        legacy_state.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "present-lease-broker",
            "tasks": {},
            "events": [],
        }))
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "gc", "--state", str(legacy_state), "--older-than-seconds", "1")
        assert code == 30
        assert stdout_payload == {}
        assert payload["schemaValidation"]["schemaId"] == "broker-state-v1"
        assert "$.updatedAt" in payload["schemaValidation"]["failingJsonPaths"]
    finally:
        purge_task(task_id)


def test_lease_broker_runs_distributed_and_renews_explicit_leases(tmp_path):
    task_id = "DEV-900065"
    purge_task(task_id)
    try:
        code, status = run_worker("status")
        assert code == 0
        worker_id = status["detail"]["workerIdentity"]["id"]
        pool = _worker_pool(tmp_path, [{"id": worker_id, "command": [str(BIN)], "status": status}])
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--mock", "--ttl-seconds", "120",
            input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "DISTRIBUTED_WORKFLOW_DISPATCHED"
        assert payload["selectedWorker"] == worker_id
        assert payload["nodeDispatches"][0]["workerId"] == worker_id
        assert payload["nodeDispatches"][0]["startedAt"]
        assert payload["nodeDispatches"][0]["finishedAt"]
        assert payload["nodeDispatches"][0]["timingsMs"]["total"] >= 0
        assert payload["placementPlan"][0]["selectionReason"].startswith("selected highest scored")
        assert payload["nodeDispatches"][0]["releasedLease"]["status"] == "RELEASED"
        assert payload["workflow"]["status"] == "SUCCEEDED"

        contract_hash = common.canonical_hash(json.loads(bundle_text)["contract"])
        code, lease, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire",
            "--contract-hash", contract_hash, "--ttl-seconds", "60")
        assert code == 0, stderr
        code, renewed, stderr_payload, stdout, stderr = run_broker_result(
            "renew", task_id, "--pool", str(pool), "--worker-id", worker_id,
            "--lease-id", lease["leaseId"], "--ttl-seconds", "180")
        assert code == 0, stderr
        assert renewed["status"] == "LEASE_RENEWED"
        assert renewed["lease"]["ttlSeconds"] == 180
        code, released, stderr_payload, stdout, stderr = run_broker_result(
            "release", task_id, "--pool", str(pool), "--worker-id", worker_id,
            "--lease-id", lease["leaseId"])
        assert code == 0, stderr
        assert released["status"] == "LEASE_RELEASED"
    finally:
        purge_task(task_id)


def test_lease_broker_rejects_removed_whole_workflow_command():
    code, stdout_payload, stderr_payload, stdout, stderr = run_broker_result("run-workflow", "DEV-900073")
    assert code == 30
    assert stdout_payload == {}
    assert stdout == ""
    assert stderr
    assert stderr_payload["status"] == "BROKER_BLOCKED"
    assert "invalid choice" in stderr_payload["reason"]


def test_worker_artifact_export_import_round_trips_by_hash():
    task_id = "DEV-900072"
    purge_task(task_id)
    try:
        code, plan, stderr_payload, stdout, stderr = run_worker_result(
            "plan", task_id, "--mock", input_text=_bundle(task_id, ["/usr/bin/true"]))
        assert code == 0, stderr
        assert stderr_payload == {}
        path = common.TASKS_DIR / task_id / "plan" / "plan.json"
        digest = common.sha256_file(path)
        code, exported, stderr_payload, stdout, stderr = run_worker_result(
            "artifact-export", task_id, "plan", "--sha256", digest)
        assert code == 0, stderr
        assert exported["status"] == "EXPORTED"
        assert exported["sha256"] == digest
        assert schema_check.validate(exported, "canonical/worker-artifact-export-v1.json") == []

        code, imported, stderr_payload, stdout, stderr = run_worker_result(
            "artifact-import", task_id, input_text=json.dumps(exported))
        assert code == 0, stderr
        assert imported["status"] == "IMPORTED"
        assert imported["sha256"] == digest
        assert schema_check.validate(imported, "canonical/worker-artifact-import-v1.json") == []
        assert common.sha256_file(Path(imported["path"])) == digest
    finally:
        purge_task(task_id)


def test_workflow_node_resolves_imported_artifacts_as_local_inputs(tmp_path):
    task_id = "DEV-900083"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        contract = json.loads(bundle_text)["contract"]
        contract_hash = common.canonical_hash(contract)
        code, admission, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=bundle_text)
        assert code == 0, stderr
        assert admission["status"] == "ADMITTED"

        code, lease, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire", "--contract-hash", contract_hash, "--ttl-seconds", "60")
        assert code == 0, stderr
        content = json.dumps({"schemaVersion": 1, "taskId": task_id, "status": "SEEDED_PLAN"}).encode()
        digest = hashlib.sha256(content).hexdigest()
        code, imported, stderr_payload, stdout, stderr = run_worker_result(
            "artifact-import", task_id,
            input_text=json.dumps({
                "taskId": task_id,
                "sourceNodeId": "plan",
                "sourcePath": "/broker/artifacts/plan.json",
                "kind": "plan",
                "sha256": digest,
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }))
        assert code == 0, stderr
        assert imported["status"] == "IMPORTED"
        assert imported["sourceNodeId"] == "plan"

        code, record, stderr_payload, stdout, stderr = run_worker_result(
            "workflow-node", task_id, "verify", "--mock", "--lease-id", lease["leaseId"])
        assert code == 0, stderr
        assert stderr_payload == {}
        resolved = record["resolvedInputs"]["artifacts"][0]
        assert resolved["nodeId"] == "plan"
        assert resolved["path"] == "/broker/artifacts/plan.json"
        assert resolved["localPath"] == imported["path"]
        assert common.sha256_file(Path(resolved["localPath"])) == digest
        accepted = record["payload"]["acceptedEvidence"][0]
        assert accepted["nodeId"] == "plan"
        assert accepted["path"] == "/broker/artifacts/plan.json"
        assert accepted["localPath"] == imported["path"]
        assert schema_check.validate(record["payload"], "canonical/verification-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_node_verifier_command_consumes_local_imported_inputs(tmp_path):
    task_id = "DEV-900084"
    purge_task(task_id)
    checker = tmp_path / "check_workflow_input.py"
    checker.write_text(
        "import hashlib, json, os, pathlib, sys\n"
        "context_path = os.environ.get('PRESENT_WORKFLOW_INPUTS_PATH')\n"
        "if not context_path:\n"
        "    raise SystemExit(10)\n"
        "context = json.loads(pathlib.Path(context_path).read_text())\n"
        "artifact = context['resolvedInputs']['artifacts'][0]\n"
        "local_path = pathlib.Path(artifact['localPath'])\n"
        "if artifact['nodeId'] != 'plan' or artifact['path'] != '/broker/artifacts/plan.json':\n"
        "    raise SystemExit(11)\n"
        "if hashlib.sha256(local_path.read_bytes()).hexdigest() != sys.argv[1]:\n"
        "    raise SystemExit(12)\n"
    )
    try:
        content = b"verifier command consumed this imported artifact\n"
        digest = hashlib.sha256(content).hexdigest()
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(
            task_id, nodes, verification_command=[sys.executable, str(checker), digest])
        contract = json.loads(bundle_text)["contract"]
        contract_hash = common.canonical_hash(contract)
        code, admission, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=bundle_text)
        assert code == 0, stderr
        assert admission["status"] == "ADMITTED"
        code, lease, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire", "--contract-hash", contract_hash, "--ttl-seconds", "60")
        assert code == 0, stderr
        code, imported, stderr_payload, stdout, stderr = run_worker_result(
            "artifact-import", task_id,
            input_text=json.dumps({
                "taskId": task_id,
                "sourceNodeId": "plan",
                "sourcePath": "/broker/artifacts/plan.json",
                "kind": "plan",
                "sha256": digest,
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }))
        assert code == 0, stderr
        code, record, stderr_payload, stdout, stderr = run_worker_result(
            "workflow-node", task_id, "verify", "--lease-id", lease["leaseId"])
        assert code == 0, stderr
        assert stderr_payload == {}
        assert record["status"] == "SUCCEEDED"
        assert record["payload"]["result"] == "PASS"
        detail = json.loads(record["payload"]["checks"][0]["detail"])
        assert detail["workflowInputs"][0]["localPath"] == imported["path"]
        assert record["payload"]["acceptedEvidence"][0]["localPath"] == imported["path"]
        assert schema_check.validate(record["payload"], "canonical/verification-v1.json") == []
        index_path = common.TASKS_DIR / task_id / "imported-artifacts" / "index.json"
        context_path = common.TASKS_DIR / task_id / "workflow-inputs" / "verify.json"
        import_index = json.loads(index_path.read_text())
        input_context = json.loads(context_path.read_text())
        assert schema_check.validate(import_index, "canonical/imported-artifact-index-v1.json") == []
        assert schema_check.validate(input_context, "canonical/workflow-input-context-v1.json") == []
        code, state, stderr_payload, stdout, stderr = run_worker_result("task-state", task_id)
        assert code == 0, stderr
        artifact_kinds = {artifact["kind"] for artifact in state["artifacts"]}
        assert {"imported-artifacts-index", "workflow-input-context"} <= artifact_kinds
        assert schema_check.validate(state, "canonical/task-state-v1.json") == []
        code, cleanup, stderr_payload, stdout, stderr = run_worker_result("task-cleanup", task_id, "--dry-run")
        assert code == 0, stderr
        retained_kinds = {artifact["kind"] for artifact in cleanup["retained"]}
        assert {"imported-artifacts-index", "workflow-inputs", "workflow-input-context"} <= retained_kinds
        assert schema_check.validate(cleanup, "canonical/task-cleanup-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_context_checkpoint_is_schema_gated_and_retained():
    task_id = "DEV-900034"
    purge_task(task_id)
    try:
        task_dir = common.TASKS_DIR / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "requirement.md").write_text("Report on long-running worker context compaction.\n")
        code, checkpoint, stderr_payload, stdout, stderr = run_worker_result(
            "context-checkpoint", task_id, "--mock", "--reason", "manual",
            "--phase", "Reconciling long-running state", "--next-action", "Resume from retained facts")
        assert code == 0, stderr
        assert checkpoint["checkpointId"].startswith("cc-")
        assert checkpoint["currentState"]["phase"] == "Reconciling long-running state"
        assert checkpoint["retainedFacts"][0]["kind"] == "question"
        assert schema_check.validate(checkpoint, "canonical/context-checkpoint-v1.json") == []
        checkpoint_path = common.task_artifact_path(task_id, "context-checkpoint")
        assert checkpoint_path.exists()

        code, state, stderr_payload, stdout, stderr = run_worker_result("task-state", task_id)
        assert code == 0, stderr
        artifact_kinds = {artifact["kind"] for artifact in state["artifacts"]}
        assert "context-checkpoint" in artifact_kinds
        assert schema_check.validate(state, "canonical/task-state-v1.json") == []

        code, cleanup, stderr_payload, stdout, stderr = run_worker_result("task-cleanup", task_id, "--dry-run")
        assert code == 0, stderr
        retained_kinds = {artifact["kind"] for artifact in cleanup["retained"]}
        assert "context-checkpoint" in retained_kinds
        assert schema_check.validate(cleanup, "canonical/task-cleanup-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_redaction_scans_import_index_and_input_contexts():
    task_id = "DEV-900085"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        code, admission, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=bundle_text)
        assert code == 0, stderr
        assert admission["status"] == "ADMITTED"
        content = b"seeded imported artifact for redaction coverage\n"
        digest = hashlib.sha256(content).hexdigest()
        code, imported, stderr_payload, stdout, stderr = run_worker_result(
            "artifact-import", task_id,
            input_text=json.dumps({
                "taskId": task_id,
                "sourceNodeId": "plan",
                "sourcePath": "/broker/artifacts/preexisting-plan.json",
                "kind": "plan",
                "sha256": digest,
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }))
        assert code == 0, stderr
        code, workflow, stderr_payload, stdout, stderr = run_worker_result("workflow", task_id, "--mock")
        assert code == 0, stderr
        redaction = json.loads(
            (common.TASKS_DIR / task_id / "redaction-report" / "redaction-report.json").read_text())
        scanned = {item["path"] for item in redaction["scannedArtifacts"]}
        assert str(common.TASKS_DIR / task_id / "imported-artifacts" / "index.json") in scanned
        assert any("/workflow-inputs/" in path for path in scanned)
        assert schema_check.validate(redaction, "canonical/redaction-report-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_state_idempotency_reconcile_and_gc(tmp_path):
    task_id = "DEV-900067"
    purge_task(task_id)
    try:
        code, status = run_worker("status")
        assert code == 0
        worker_id = status["detail"]["workerIdentity"]["id"]
        pool = _worker_pool(tmp_path, [{"id": worker_id, "command": [str(BIN)], "status": status}])
        state_path = tmp_path / "broker-state.json"
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        code, dispatched, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--mock", "--ttl-seconds", "120", input_text=bundle_text)
        assert code == 0, stderr
        assert dispatched["status"] == "DISTRIBUTED_WORKFLOW_DISPATCHED"
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_COMPLETE"
        assert state["tasks"][task_id]["workflowRunId"] == dispatched["workflow"]["workflowRunId"]
        assert {event["type"] for event in state["events"]} >= {
            "distributed-start", "distributed-node-dispatch", "distributed-node-release",
            "artifact-materialized", "distributed-complete"}

        code, replay, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert replay["status"] == "IDEMPOTENT_REPLAY"
        assert replay["task"]["workflowRunId"] == dispatched["workflow"]["workflowRunId"]
        state = _broker_state_payload(state_path)
        state["tasks"][task_id]["terminalAt"] = "2000-01-01T00:00:00Z"
        for event in state["events"]:
            event["producedAt"] = "2000-01-01T00:00:00Z"
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True))
        code, gc, stderr_payload, stdout, stderr = run_broker_result(
            "gc", "--state", str(state_path), "--older-than-seconds", "1")
        assert code == 0, stderr
        assert gc["status"] == "GC_COMPLETE"
        assert task_id in gc["removedTasks"]
        state = _broker_state_payload(state_path)
        assert task_id not in state["tasks"]
    finally:
        purge_task(task_id)


def test_lease_broker_compacts_terminal_state_and_unreferenced_artifacts(tmp_path):
    task_id = "DEV-900112"
    store = tmp_path / "artifact-store"
    orphan_dir = store / task_id
    orphan_dir.mkdir(parents=True)
    orphan = orphan_dir / "orphan.json"
    orphan.write_text("{}")
    old_epoch = 946684800
    os.utime(orphan, (old_epoch, old_epoch))
    state_path = _write_broker_state(tmp_path, {
        task_id: {
            "taskId": task_id,
            "status": "DISTRIBUTED_COMPLETE",
            "contractHash": "5" * 64,
            "selectedWorker": "distributed",
            "terminalAt": "2000-01-01T00:00:00Z",
            "attempts": [],
            "updatedAt": "2000-01-01T00:00:00Z",
        }
    }, events=[{
        "eventId": "be-compact-test",
        "taskId": task_id,
        "type": "distributed-complete",
        "producedAt": "2000-01-01T00:00:00Z",
    }])

    code, dry_run, stderr_payload, stdout, stderr = run_broker_result(
        "compact", "--state", str(state_path), "--artifact-store", str(store),
        "--older-than-seconds", "1", "--dry-run")
    assert code == 0, stderr
    assert stderr_payload == {}
    assert dry_run["status"] == "COMPACTED"
    assert dry_run["dryRun"] is True
    assert task_id in dry_run["candidateTasks"]
    assert str(orphan) in dry_run["artifactCandidates"]
    assert orphan.exists()
    assert schema_check.validate(dry_run, "canonical/broker-response-compact-v1.json") == []

    code, compacted, stderr_payload, stdout, stderr = run_broker_result(
        "compact", "--state", str(state_path), "--artifact-store", str(store),
        "--older-than-seconds", "1")
    assert code == 0, stderr
    assert stderr_payload == {}
    assert compacted["removedTasks"] == [task_id]
    assert compacted["removedArtifacts"] == [str(orphan)]
    assert not orphan.exists()
    state = _broker_state_payload(state_path)
    assert task_id not in state["tasks"]


def test_lease_broker_normalize_state_rewrites_current_shape(tmp_path):
    task_id = "DEV-900123"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "schemaVersion": 1,
        "producedAt": common.utc_now(),
        "producer": "present-lease-broker",
        "updatedAt": common.utc_now(),
        "legacyTop": True,
        "tasks": {
            task_id: {
                "taskId": task_id,
                "status": "DISTRIBUTED_COMPLETE",
                "contractHash": "9" * 64,
                "selectedWorker": "distributed",
                "attempts": [],
                "updatedAt": common.utc_now(),
                "terminalAt": common.utc_now(),
                "legacyField": "remove-me",
            }
        },
        "events": [],
    }))
    code, payload, stderr_payload, stdout, stderr = run_broker_result(
        "normalize-state", "--state", str(state_path))
    assert code == 0, stderr
    assert payload["status"] == "STATE_NORMALIZED"
    assert any("legacyTop" in item for item in payload["changes"])
    state = _broker_state_payload(state_path)
    assert "legacyTop" not in state
    assert "legacyField" not in state["tasks"][task_id]
    assert state["recoveryLedger"] == []


def test_lease_broker_normalize_state_strict_rejects_unknown_fields(tmp_path):
    task_id = "DEV-900124"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "schemaVersion": 1,
        "producedAt": common.utc_now(),
        "producer": "present-lease-broker",
        "updatedAt": common.utc_now(),
        "legacyTop": True,
        "tasks": {
            task_id: {
                "taskId": task_id,
                "status": "DISTRIBUTED_COMPLETE",
                "contractHash": "9" * 64,
                "selectedWorker": "distributed",
                "attempts": [],
                "updatedAt": common.utc_now(),
            }
        },
        "events": [],
    }))
    code, stdout_payload, payload, stdout, stderr = run_broker_result(
        "normalize-state", "--state", str(state_path), "--strict")
    assert code == 30
    assert stdout_payload == {}
    assert payload["status"] == "BROKER_BLOCKED"
    assert "unknown top-level fields" in payload["reason"]


def test_lease_broker_node_placement_and_artifact_transfer_manifest(tmp_path):
    task_id = "DEV-900068"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        pool = _worker_pool(tmp_path, [
            {"id": "macbook-local:plan", "command": [str(BIN)],
             "transport": {"kind": "local", "command": [str(BIN)], "timeoutSeconds": 30},
             "status": _status_for_pool("macbook-local:plan", unavailable=["deterministic-verify"]),
             "health": {"score": 100}},
            {"id": "linux-container:verify", "command": [str(BIN)],
             "transport": {"kind": "container", "command": [str(BIN)], "timeoutSeconds": 30},
             "status": _status_for_pool("linux-container:verify", unavailable=["plan"]),
             "health": {"score": 100}},
            {"id": "macbook-local:whole", "command": [str(BIN)],
             "status": _status_for_pool("macbook-local:whole"),
             "health": {"score": 10}},
        ])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert payload["selectedWorker"] == "macbook-local:whole"
        placement = {node["nodeId"]: node["workerId"] for node in payload["placementPlan"]}
        assert placement == {"plan": "macbook-local:plan", "verify": "linux-container:verify"}
        assert payload["admissionReport"]["status"] == "ADMITTED"
        assert payload["admissionReport"]["candidateCount"] == len(payload["candidates"])
        assert schema_check.validate(payload["admissionReport"], "canonical/admission-report-v1.json") == []
        transfers = payload["artifactTransferManifest"]["transfers"]
        assert transfers == [{
            "artifactRole": "deterministic-acceptance",
            "fromNodeId": "plan",
            "fromWorkerId": "macbook-local:plan",
            "required": True,
            "toNodeId": "verify",
            "toWorkerId": "linux-container:verify",
        }]
        assert schema_check.validate(
            payload["artifactTransferManifest"], "canonical/artifact-transfer-manifest-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_runs_distributed_nodes_and_materializes_artifacts(tmp_path):
    task_id = "DEV-900071"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(
                tmp_path, "macbook-local:plan", unavailable=["deterministic-verify"]),
            _isolated_worker_entry(
                tmp_path, "linux-container:verify", unavailable=["plan"], transport_kind="container"),
        ])
        state_path = tmp_path / "broker-state.json"
        store_path = tmp_path / "artifact-store"
        bundle_text = _workflow_bundle(task_id, nodes)
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", "--ttl-seconds", "120",
            input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "DISTRIBUTED_WORKFLOW_DISPATCHED"
        assert schema_check.validate(payload["workflow"], "canonical/workflow-result-v1.json") == []
        assert payload["workflow"]["status"] == "SUCCEEDED"
        distributed_artifacts = payload["distributedArtifacts"]
        assert schema_check.validate(payload["integrityLedger"], "canonical/artifact-integrity-ledger-v1.json") == []
        assert {
            "materialize",
            "import",
            "redaction",
            "handoff",
            "workflow-dag",
            "attempt-replay",
            "artifact-provenance",
            "evidence-graph",
            "acceptance",
        } <= {
            entry["operation"] for entry in payload["integrityLedger"]["entries"]}
        assert schema_check.validate(
            distributed_artifacts, "canonical/broker-distributed-artifacts-v1.json") == []
        assert schema_check.validate(payload["admissionReport"], "canonical/admission-report-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["admission"], "canonical/broker-workflow-admission-compact-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["handoffPackage"], "canonical/handoff-package-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["evidenceManifest"], "canonical/evidence-manifest-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["redactionReport"], "canonical/redaction-report-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["workflowDag"], "canonical/workflow-dag-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["replayReport"], "canonical/distributed-replay-report-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["artifactProvenance"], "canonical/artifact-provenance-chain-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["evidenceGraph"], "canonical/evidence-graph-v1.json") == []
        assert schema_check.validate(
            distributed_artifacts["acceptanceManifest"], "canonical/acceptance-manifest-v1.json") == []
        handoff_path = Path(distributed_artifacts["handoffPath"])
        assert handoff_path.exists()
        for path_key in (
            "workflowDagPath",
            "replayReportPath",
            "artifactProvenancePath",
            "evidenceGraphPath",
            "acceptanceManifestPath",
        ):
            assert Path(distributed_artifacts[path_key]).exists()
        assert common.sha256_file(handoff_path) == distributed_artifacts["handoffSha256"]
        assert distributed_artifacts["handoffPackage"]["workflowRunId"] == payload["workflow"]["workflowRunId"]
        assert distributed_artifacts["redactionReport"]["status"] == "CLEAN"
        assert distributed_artifacts["retentionPolicy"]["class"] == "standard"
        assert distributed_artifacts["workflowDag"]["workflowRunId"] == payload["workflow"]["workflowRunId"]
        assert distributed_artifacts["replayReport"]["freshNodes"] == ["plan", "verify"]
        assert distributed_artifacts["artifactProvenance"]["artifacts"][0]["nodeId"] == "plan"
        assert distributed_artifacts["evidenceGraph"]["gateDecision"] == "INCONCLUSIVE"
        assert distributed_artifacts["acceptanceManifest"]["decision"] == "INCONCLUSIVE"
        assert [dispatch["nodeId"] for dispatch in payload["nodeDispatches"]] == ["plan", "verify"]
        assert [dispatch["workerId"] for dispatch in payload["nodeDispatches"]] == [
            "macbook-local:plan", "linux-container:verify"]
        assert all(dispatch["status"] == "SUCCEEDED" for dispatch in payload["nodeDispatches"])
        assert all(dispatch["timingsMs"]["execution"] >= 0 for dispatch in payload["nodeDispatches"])
        assert payload["nodeDispatches"][1]["timingsMs"]["artifactImport"] >= 0
        assert payload["placementPlan"][0]["selectionReason"].startswith("selected highest scored")
        assert payload["artifactTransferManifest"]["transfers"][0]["fromWorkerId"] == "macbook-local:plan"
        assert payload["artifactTransferManifest"]["transfers"][0]["toWorkerId"] == "linux-container:verify"
        assert payload["contextCheckpoints"]
        assert {item["kind"] for item in payload["contextCheckpoints"]} == {"context-checkpoint"}
        assert {item["nodeId"] for item in payload["contextCheckpoints"]} == {"plan", "verify"}
        materialized = payload["artifactStore"]["artifacts"]
        assert payload["artifactStore"]["descriptor"]["kind"] == "local-filesystem"
        assert {item["nodeId"] for item in materialized} == {"plan", "verify"}
        assert all(item["status"] == "MATERIALIZED" for item in materialized)
        for item in materialized:
            target = Path(item["contentAddressedPath"])
            assert target.exists()
            assert target.name == item["sha256"]
            assert common.sha256_file(target) == item["sha256"]
        verify_dispatch = payload["nodeDispatches"][1]
        assert verify_dispatch["artifactImports"][0]["status"] == "IMPORTED"
        plan_digest = next(item["sha256"] for item in materialized if item["nodeId"] == "plan")
        imported_path = tmp_path / "linux-container_verify" / "tasks" / task_id / "imported-artifacts" / plan_digest
        assert imported_path.exists()
        assert common.sha256_file(imported_path) == plan_digest
        verify_node = payload["workflow"]["nodes"][1]
        assert verify_node["resolvedInputs"]["artifacts"][0]["localPath"] == str(imported_path)
        assert verify_node["payload"]["acceptedEvidence"][0]["localPath"] == str(imported_path)
        evidence_by_node = {item["nodeId"]: item for item in payload["workflow"]["evidence"]}
        assert evidence_by_node["plan"]["acceptedBy"] == ["verify"]
        assert payload["workflow"]["nodes"][1]["payload"]["acceptedEvidence"][0]["id"] == evidence_by_node["plan"]["id"]
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_COMPLETE"
        assert state["tasks"][task_id]["distributed"]["workflowDag"]["workflowRunId"] == payload["workflow"]["workflowRunId"]
        assert state["tasks"][task_id]["distributed"]["replayReport"]["freshNodes"] == ["plan", "verify"]
        assert state["tasks"][task_id]["distributed"]["acceptanceManifest"]["decision"] == "INCONCLUSIVE"
        assert schema_check.validate(
            state["tasks"][task_id]["distributed"], "canonical/broker-distributed-state-v1.json") == []
        assert {event["type"] for event in state["events"]} >= {
            "distributed-start", "distributed-node-dispatch", "distributed-node-release",
            "artifact-materialized", "distributed-complete", "distributed-handoff"}
        for event in state["events"]:
            assert schema_check.validate(event, "canonical/broker-event-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_hardening_slice_queries_acceptance_compaction_and_release(tmp_path):
    task_id = "DEV-900141"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(tmp_path, "macbook-local:query-plan", unavailable=["deterministic-verify"]),
            _isolated_worker_entry(
                tmp_path, "linux-container:query-verify", unavailable=["plan"], transport_kind="container"),
        ])
        state_path = tmp_path / "broker-state.json"
        store_path = tmp_path / "artifact-store"
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--artifact-store-kind", "remote-stub",
            "--mock", input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert payload["artifactStore"]["descriptor"]["kind"] == "remote-stub"

        code, status, stderr_payload, stdout, stderr = run_broker_result(
            "workflow-status", task_id, "--state", str(state_path))
        assert code == 0, stderr
        assert status["status"] == "WORKFLOW_STATUS"
        assert status["workflows"][0]["counts"]["nodes"] == 2
        assert status["workflows"][0]["artifactStoreAdapter"]["kind"] == "remote-stub"

        code, history, stderr_payload, stdout, stderr = run_broker_result(
            "workflow-history", task_id, "--state", str(state_path), "--limit", "20")
        assert code == 0, stderr
        assert history["status"] == "WORKFLOW_HISTORY"
        assert any(event["type"] == "distributed-complete" for event in history["events"])
        assert history["nodeDispatches"][0]["nodeId"] == "plan"

        code, acceptance, stderr_payload, stdout, stderr = run_broker_result(
            "acceptance-status", task_id, "--state", str(state_path), "--artifact-store", str(store_path))
        assert code == 0, stderr
        assert acceptance["status"] == "ACCEPTANCE_STATUS"
        assert acceptance["ready"] is True
        assert acceptance["summary"]["errors"] == 0

        code, worker_status, stderr_payload, stdout, stderr = run_broker_result(
            "worker-status", "--pool", str(pool), "--state", str(state_path))
        assert code == 0, stderr
        assert worker_status["status"] == "WORKER_STATUS"
        assert {worker["workerId"] for worker in worker_status["workers"]} == {
            "macbook-local:query-plan", "linux-container:query-verify"}

        code, quarantined, stderr_payload, stdout, stderr = run_broker_result(
            "quarantine-worker", task_id, "--pool", str(pool), "--state", str(state_path),
            "--worker-id", "linux-container:query-verify", "--reason", "test hardening release")
        assert code == 0, stderr
        assert quarantined["status"] == "WORKER_QUARANTINED"

        code, released, stderr_payload, stdout, stderr = run_broker_result(
            "unquarantine-worker", task_id, "--pool", str(pool), "--state", str(state_path),
            "--worker-id", "linux-container:query-verify", "--reason", "healthy after manual check")
        assert code == 0, stderr
        assert released["status"] == "WORKER_UNQUARANTINED"
        pool_payload = json.loads(pool.read_text())
        released_worker = next(worker for worker in pool_payload["workers"] if worker["id"] == "linux-container:query-verify")
        assert released_worker["quarantine"]["active"] is False
        assert released_worker["health"]["recentFailures"] == 0

        code, audit, stderr_payload, stdout, stderr = run_broker_result(
            "audit-log", "--state", str(state_path), "--task-id", task_id)
        assert code == 0, stderr
        assert {entry["action"] for entry in audit["entries"]} >= {"quarantine-worker", "unquarantine-worker"}

        code, refresh, stderr_payload, stdout, stderr = run_broker_result(
            "refresh-scheduler", task_id, "--pool", str(pool), "--state", str(state_path),
            "--iterations", "1", "--interval-seconds", "0")
        assert code == 0, stderr
        assert refresh["status"] == "REFRESH_SCHEDULER_COMPLETE"
        assert refresh["iterations"][0]["workers"]

        code, compacted, stderr_payload, stdout, stderr = run_broker_result(
            "compact", "--state", str(state_path), "--artifact-store", str(store_path),
            "--older-than-seconds", "0", "--v2")
        assert code == 0, stderr
        assert compacted["mode"] == "broker-state-compaction-v2"
        assert compacted["compactedTasks"] == [task_id]
        state = _broker_state_payload(state_path)
        distributed = state["tasks"][task_id]["distributed"]
        assert distributed["compactionSummary"]["beforeCounts"]["nodes"] == 2
        assert distributed["nodes"] == {}
        assert distributed["artifactStoreAdapter"]["kind"] == "remote-stub"
        assert schema_check.validate(distributed, "canonical/broker-distributed-state-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_demo_uses_provisioner_registry_and_query_state(tmp_path):
    task_id = "DEV-900142"
    purge_task(task_id)
    provisioner_log = tmp_path / "registry-provisioner.jsonl"
    provisioner = tmp_path / "registry_provisioner.py"
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "with pathlib.Path(sys.argv[1]).open('a') as handle:\n"
        "    handle.write(json.dumps(request, sort_keys=True) + '\\n')\n"
        "print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': False, 'workerKinds': [], 'trustLevels': [], 'supportedProviders': ['test'], 'reason': 'not needed for demo'}))\n"
    )
    registry = tmp_path / "provisioners.json"
    registry.write_text(json.dumps({
        "schemaVersion": 1,
        "producedAt": common.utc_now(),
        "producer": "pytest provisioner registry",
        "provisioners": [{
            "id": "registry-skip",
            "kind": "container",
            "command": [sys.executable, str(provisioner), str(provisioner_log)],
            "timeoutSeconds": 30,
        }],
    }))
    assert schema_check.validate(json.loads(registry.read_text()), "canonical/provisioner-registry-v1.json") == []
    try:
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:demo")])
        state_path = tmp_path / "broker-state.json"
        store_path = tmp_path / "artifact-store"
        code, demo, stderr_payload, stdout, stderr = run_broker_result(
            "demo-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--provisioner-registry", str(registry), "--mock")
        assert code == 0, stderr
        assert demo["status"] == "DEMO_DISTRIBUTED_COMPLETE"
        assert demo["run"]["provisioning"][0]["id"] == "registry-skip"
        assert demo["workflowStatus"]["counts"]["nodes"] == 2
        assert demo["acceptanceManifest"]["decision"] == "INCONCLUSIVE"
        assert json.loads(provisioner_log.read_text().splitlines()[0])["action"] == "discover"

        code, location, stderr_payload, stdout, stderr = run_broker_result(
            "state-location", "--state", str(state_path))
        assert code == 0, stderr
        assert location["status"] == "STATE_LOCATION"
        assert location["exists"] is True
        assert location["taskCount"] == 1
    finally:
        purge_task(task_id)


def test_lease_broker_operational_surfaces_export_replay_runbook_and_window(tmp_path):
    task_id = "DEV-900143"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:ops")])
        state_path = tmp_path / "broker-state.json"
        store_path = tmp_path / "artifact-store"
        code, run, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr

        code, dag, stderr_payload, stdout, stderr = run_broker_result(
            "workflow-dag", task_id, "--state", str(state_path))
        assert code == 0, stderr
        assert dag["status"] == "WORKFLOW_DAG"
        assert [node["nodeId"] for node in dag["dag"]["nodes"]] == ["plan", "verify"]

        code, timeline, stderr_payload, stdout, stderr = run_broker_result(
            "lease-timeline", task_id, "--state", str(state_path))
        assert code == 0, stderr
        assert timeline["status"] == "LEASE_TIMELINE"
        assert any(event["type"] == "distributed-node-dispatch" for event in timeline["events"])

        code, gate, stderr_payload, stdout, stderr = run_broker_result(
            "acceptance-gate", task_id, "--state", str(state_path), "--artifact-store", str(store_path))
        assert code == 0, stderr
        assert gate["status"] == "ACCEPTANCE_GATE"
        assert {check["id"] for check in gate["checks"]} >= {
            "artifact-integrity", "redaction-clean", "mock-evidence",
            "required-evidence-roles", "deterministic-acceptance"}
        state = _broker_state_payload(state_path)
        distributed = state["tasks"][task_id]["distributed"]
        assert distributed["contractChecks"]
        assert distributed["contractChecks"][0]["status"] == "PASS"
        assert distributed["toolContracts"]
        assert any(event["action"] == "dispatch-node" for event in distributed["narrativeEvents"])
        assert any(event["action"] == "run-node" for event in distributed["narrativeEvents"])

        code, narrative, stderr_payload, stdout, stderr = run_broker_result(
            "job-narrative", task_id, "--state", str(state_path))
        assert code == 0, stderr
        assert narrative["status"] == "JOB_NARRATIVE"
        assert schema_check.validate(narrative["narrative"], "canonical/job-narrative-v1.json") == []
        assert "control plane" in narrative["plainText"]
        assert "dispatched node" in narrative["plainText"]
        assert "Worker" in narrative["plainText"]
        assert "Deterministic acceptance" in narrative["plainText"]

        code, evidence, stderr_payload, stdout, stderr = run_broker_result(
            "evidence-query", task_id, "--state", str(state_path), "--artifact-store", str(store_path))
        assert code == 0, stderr
        assert evidence["status"] == "EVIDENCE_QUERY"
        assert evidence["evidenceSummary"]["evidenceCount"] >= 1
        assert evidence["evidenceSummary"]["filteredEvidenceCount"] == evidence["evidenceSummary"]["evidenceCount"]
        assert evidence["policyChecks"]
        assert "acceptance-manifest" in evidence["artifacts"]
        code, filtered, stderr_payload, stdout, stderr = run_broker_result(
            "evidence-query", task_id, "--state", str(state_path), "--artifact-store", str(store_path),
            "--evidence-role", "deterministic-acceptance", "--not-gate-eligible")
        assert code == 0, stderr
        assert filtered["evidenceSummary"]["filteredEvidenceCount"] >= 1
        assert all(item["evidenceRole"] == "deterministic-acceptance" for item in filtered["filteredEvidence"])

        export_path = tmp_path / "state-export.json"
        code, exported, stderr_payload, stdout, stderr = run_broker_result(
            "state-export", "--state", str(state_path), "--pool", str(pool), "--output", str(export_path))
        assert code == 0, stderr
        assert exported["status"] == "STATE_EXPORTED"
        assert export_path.exists()
        code, imported, stderr_payload, stdout, stderr = run_broker_result(
            "state-import-validate", str(export_path))
        assert code == 0, stderr
        assert imported["status"] == "STATE_IMPORT_VALIDATED"
        assert imported["ok"] is True

        code, compacted, stderr_payload, stdout, stderr = run_broker_result(
            "compact", "--state", str(state_path), "--artifact-store", str(store_path),
            "--older-than-seconds", "0", "--v2")
        assert code == 0, stderr
        assert compacted["compactedTasks"] == [task_id]
        code, replay, stderr_payload, stdout, stderr = run_broker_result(
            "compaction-replay", task_id, "--state", str(state_path), "--artifact-store", str(store_path))
        assert code == 0, stderr
        assert replay["status"] == "COMPACTION_REPLAY"
        assert {"workflow-dag", "acceptance-manifest"} <= set(replay["restoredArtifacts"])

        code, chaos, stderr_payload, stdout, stderr = run_broker_result("chaos-matrix")
        assert code == 0, stderr
        assert {case["id"] for case in chaos["cases"]} >= {"lock-timeout", "worker-timeout", "provision-failure"}

        runbook_path = tmp_path / "runbook.md"
        code, runbook, stderr_payload, stdout, stderr = run_broker_result(
            "runbook-generate", "--output", str(runbook_path))
        assert code == 0, stderr
        assert runbook["status"] == "RUNBOOK_GENERATED"
        assert "Multiple Broker Windows" in runbook_path.read_text()

        code, window, stderr_payload, stdout, stderr = run_broker_result(
            "create-broker-window", "--name", "ops", "--state", str(state_path), "--pool", str(pool),
            "--artifact-store", str(store_path), "--port", "8877", "--window-dir", str(tmp_path / "windows"))
        assert code == 0, stderr
        assert window["status"] == "BROKER_WINDOW_CREATED"
        assert Path(window["path"]).exists()
        assert window["window"]["url"] == "http://127.0.0.1:8877/"
    finally:
        purge_task(task_id)


def test_lease_broker_acceptance_policy_reports_missing_live_evidence_and_remediation(tmp_path):
    task_id = "DEV-900146"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "code-review", "capability": "code-review", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["code-review"],
             "inputs": {"artifactsFrom": ["code-review"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:policy")])
        state_path = tmp_path / "broker-state.json"
        store_path = tmp_path / "artifact-store"
        policy_pack = _registered_policy_pack("present-standard-change")
        code, run, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock",
            input_text=_workflow_bundle(task_id, nodes, policy_pack=policy_pack))
        assert code == 0, stderr
        acceptance = run["distributedArtifacts"]["acceptanceManifest"]
        assert acceptance["decision"] == "INCONCLUSIVE"
        assert acceptance["acceptancePolicy"]["mode"] == "single-deterministic-verifier"
        checks = {check["id"]: check for check in acceptance["policyChecks"]}
        assert checks["mock-evidence"]["status"] == "INCONCLUSIVE"
        assert checks["required-evidence-roles"]["status"] == "INCONCLUSIVE"
        assert checks["deterministic-acceptance"]["status"] == "INCONCLUSIVE"
        assert any("without --mock" in item["action"] for item in acceptance["remediation"])

        code, gate, stderr_payload, stdout, stderr = run_broker_result(
            "acceptance-gate", task_id, "--state", str(state_path), "--artifact-store", str(store_path),
            "--require-pass")
        assert code != 0
        gate = gate or stderr_payload
        assert gate["decision"] == "INCONCLUSIVE"
        assert any(item["checkId"] == "mock-evidence" for item in gate["remediation"])

        code, evidence, stderr_payload, stdout, stderr = run_broker_result(
            "evidence-query", task_id, "--state", str(state_path), "--artifact-store", str(store_path),
            "--evidence-role", "advisory-review")
        assert code == 0, stderr
        assert evidence["evidenceSummary"]["filteredEvidenceCount"] == 1
        assert evidence["filteredEvidence"][0]["nodeId"] == "code-review"
        assert evidence["policyChecks"] == acceptance["policyChecks"]
    finally:
        purge_task(task_id)


def test_lease_broker_provisioner_replay_stress_fixture_and_broker_server(tmp_path, monkeypatch):
    task_id = "DEV-900144"
    purge_task(task_id)
    try:
        fixture_root = tmp_path / "fixture-root"
        provisioner = [
            sys.executable, str(WORKER_DIR / "bin" / "present-fixture-provisioner"),
            "--worker-id", "cloud-ephemeral:fixture",
            "--root", str(fixture_root),
            "--command", str(BIN),
        ]
        registry = tmp_path / "provisioners.json"
        registry.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "pytest provisioner registry",
            "provisioners": [{
                "id": "fixture",
                "kind": "cloud",
                "command": provisioner,
                "timeoutSeconds": 30,
            }],
        }))
        state_path = tmp_path / "broker-state.json"
        store_path = tmp_path / "artifact-store"
        pool = _worker_pool(tmp_path, [])
        code, stress, stderr_payload, stdout, stderr = run_broker_result(
            "stress-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--provisioner-registry", str(registry),
            "--width", "2", "--max-parallel", "2", "--timeout-seconds", "120")
        assert code == 0, stderr
        assert stress["status"] == "STRESS_DISTRIBUTED_COMPLETE"
        assert stress["run"]["provisioning"][0]["id"] == "fixture"
        evidence_paths = [
            item["path"]
            for item in stress["run"]["distributedArtifacts"]["evidenceManifest"]["artifacts"]
        ]
        assert evidence_paths
        assert all(str(store_path) in path for path in evidence_paths)

        code, replay, stderr_payload, stdout, stderr = run_broker_result(
            "provisioner-replay", "--state", str(state_path), "--task-id", task_id)
        assert code == 0, stderr
        assert replay["status"] == "PROVISIONER_REPLAY"
        assert {"discover", "reserve", "provision"} <= {entry["action"] for entry in replay["entries"]}

        code, drift, stderr_payload, stdout, stderr = run_broker_result(
            "capability-drift", "--pool", str(pool))
        assert code == 0, stderr
        assert drift["status"] == "CAPABILITY_DRIFT"
        assert drift["workers"][0]["workerId"] == "cloud-ephemeral:fixture"

        broker_server = runpy.run_path(str(WORKER_DIR / "bin" / "present-broker-server"))
        app = broker_server["BrokerApp"](
            state=state_path, pool=pool, artifact_store=store_path, provisioner_registry=registry)
        overview = app.overview()
        assert overview["ok"] is True
        assert overview["workflows"][0]["taskId"] == task_id
        code, created = app.window_create({"name": "from-api", "port": 8878}, 8878)
        assert code == 0
        assert created["window"]["url"] == "http://127.0.0.1:8878/"

        monkeypatch.setenv("PRESENT_BROKER_STATE", str(state_path))
        monkeypatch.setenv("PRESENT_WORKER_POOL", str(pool))
        monkeypatch.setenv("PRESENT_PROVISIONER_REGISTRY", str(registry))
        steel_mission_server = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
        steel_mission_overview = steel_mission_server["broker_overview"]()
        assert steel_mission_overview["workflows"][0]["taskId"] == task_id
        assert steel_mission_overview["workers"][0]["workerId"] == "cloud-ephemeral:fixture"
        narratives = steel_mission_server["broker_narratives"]()
        assert narratives[0]["taskId"] == task_id
        assert "control plane" in narratives[0]["plainText"]
    finally:
        purge_task(task_id)


def test_lease_broker_applies_retention_policy_from_policy_pack_registry(tmp_path):
    task_id = "DEV-900135"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:retained")])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--mock",
            input_text=_workflow_bundle(
                task_id, nodes, policy_pack=_registered_policy_pack("present-retained-change")))
        assert code == 0, stderr
        assert stderr_payload == {}
        retention = payload["distributedArtifacts"]["retentionPolicy"]
        assert retention == {
            "class": "audit",
            "ttlSeconds": 2592000,
            "redactionRequired": True,
            "purgeOnCompletion": False,
        }
    finally:
        purge_task(task_id)


def test_broker_artifact_writer_rejects_malformed_payload_before_disk(tmp_path):
    task_id = "DEV-900086"
    broker = runpy.run_path(str(BROKER))
    writer = broker["_write_broker_artifact"]
    bad_admission = {
        **common.canonical_envelope(task_id, "present-lease-broker workflow admission", mocked=True),
        "status": "ADMITTED",
        "workflowId": task_id,
        "contractHash": "not-a-sha",
        "policyPack": {
            "id": "present-default-change",
            "version": 1,
            "registryHash": "0" * 64,
            "riskClass": "normal",
            "status": "SATISFIED",
        },
    }
    try:
        writer(
            tmp_path, task_id, "workflow-admission", bad_admission,
            "canonical/broker-workflow-admission-compact-v1.json")
        raise AssertionError("malformed broker artifact must be rejected before write")
    except common.TaskBundleError as exc:
        assert "broker artifact workflow-admission failed schema validation" in str(exc)
        assert "contractHash" in str(exc)
        assert isinstance(exc, common.SchemaValidationError)
        assert schema_check.validate(exc.report, "canonical/schema-validation-error-v1.json") == []
        assert exc.report["schemaId"] == "broker-workflow-admission-compact-v1"
        assert "$.contractHash" in exc.report["failingJsonPaths"]
    assert not (tmp_path / "_broker" / task_id / "workflow-admission.json").exists()
    assert not (tmp_path / "_broker" / task_id / "workflow-admission.tmp").exists()


def test_worker_artifact_writer_rejects_malformed_registered_artifact(tmp_path, monkeypatch):
    task_id = "DEV-900087"
    monkeypatch.setattr(common, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(common, "LOGS_DIR", tmp_path / "logs")
    bad_plan = {
        **common.canonical_envelope(task_id, "present-worker plan", mocked=True),
        "summary": "missing required plan status",
        "steps": [],
        "risks": [],
    }
    try:
        common.write_task_artifact(task_id, "plan", bad_plan)
        raise AssertionError("registered task artifacts must be schema-gated before write")
    except common.TaskBundleError as exc:
        assert "task artifact plan failed schema validation" in str(exc)
        assert isinstance(exc, common.SchemaValidationError)
        assert schema_check.validate(exc.report, "canonical/schema-validation-error-v1.json") == []
        assert exc.report["validationPoint"] == "worker-write"
        assert exc.report["artifactStage"] == "plan"
    assert not common.task_artifact_path(task_id, "plan").exists()
    log_lines = (common.LOGS_DIR / f"{task_id}.jsonl").read_text().splitlines()
    assert any(json.loads(line)["stage"] == "schema-validation" for line in log_lines)


def test_broker_schema_validation_failure_is_structured_in_error_response(tmp_path):
    task_id = "DEV-900088"
    now = common.utc_now()
    state_path = tmp_path / "broker-state.json"
    state_path.write_text(json.dumps({
        "schemaVersion": 1,
        "producedAt": now,
        "producer": "present-lease-broker",
        "updatedAt": now,
        "tasks": {
            task_id: {
                "taskId": task_id,
                "status": "DISTRIBUTED_RUNNING",
                "contractHash": "a" * 64,
                "selectedWorker": "distributed",
                "attempts": [],
                "updatedAt": now,
                "distributed": {
                    "schemaVersion": 1,
                    "taskId": task_id,
                    "producedAt": now,
                    "producer": "present-lease-broker distributed state",
                    "workflowId": f"{task_id}-workflow",
                    "contractHash": "a" * 64,
                    "storeRoot": str(tmp_path / "store"),
                    "placements": [],
                    "artifactTransferManifest": {},
                    "nodes": {},
                    "nodeDispatches": [],
                    "artifactStore": [],
                    "importedArtifacts": [],
                },
            }
        },
        "events": [],
    }))
    code, stdout_payload, stderr_payload, stdout, stderr = run_broker_result(
        "gc", "--state", str(state_path), "--older-than-seconds", "1")
    assert code == 30
    assert stdout_payload == {}
    assert stderr_payload["status"] == "BROKER_BLOCKED"
    assert stderr_payload["schemaRegistryHash"] == common.schema_registry_hash()
    report = stderr_payload["schemaValidation"]
    assert schema_check.validate(report, "canonical/schema-validation-error-v1.json") == []
    assert report["taskId"] == task_id
    assert report["schemaId"] == "broker-distributed-state-v1"
    assert report["schemaRegistryHash"] == common.schema_registry_hash()
    assert "$.updatedAt" in report["failingJsonPaths"]


def test_worker_schema_validation_failure_is_structured_in_error_response():
    task_id = "DEV-900089"
    purge_task(task_id)
    try:
        index = common.TASKS_DIR / task_id / "imported-artifacts" / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({
            "schemaVersion": 1,
            "taskId": task_id,
            "producedAt": common.utc_now(),
            "producer": "present-worker artifact import index",
            "artifacts": [{"bad": True}],
        }))
        content = b"artifact import payload"
        digest = hashlib.sha256(content).hexdigest()
        code, stdout_payload, stderr_payload, stdout, stderr = run_worker_result(
            "artifact-import", task_id,
            input_text=json.dumps({
                "taskId": task_id,
                "sourceNodeId": "plan",
                "sourcePath": "/broker/plan.json",
                "kind": "plan",
                "sha256": digest,
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }))
        assert code == 30
        assert stdout_payload == {}
        assert stderr_payload["status"] == "PROTOCOL_ERROR"
        assert stderr_payload["schemaRegistryHash"] == common.schema_registry_hash()
        report = stderr_payload["schemaValidation"]
        assert schema_check.validate(report, "canonical/schema-validation-error-v1.json") == []
        assert report["taskId"] == task_id
        assert report["schemaId"] == "imported-artifact-index-v1"
        assert report["schemaRegistryHash"] == common.schema_registry_hash()
        assert report["artifactKind"] == "imported-artifact-index"
        log_lines = (WORKER_DIR / "logs" / f"{task_id}.jsonl").read_text().splitlines()
        assert any(json.loads(line)["stage"] == "schema-validation" for line in log_lines)
    finally:
        purge_task(task_id)


def _test_node_policy(node: dict) -> dict:
    policy = node.get("policy") if isinstance(node.get("policy"), dict) else {}
    return {
        "required": policy.get("required", True),
        "blocksOn": policy.get("blocksOn", ["FAILED", "BLOCKED"]),
        "timeoutSeconds": policy.get("timeoutSeconds", 900),
    }


def _test_node_contract_hash(node: dict) -> str:
    normalized = {
        "id": node["id"],
        "capability": node["capability"],
        "dependsOn": node["dependsOn"],
        "policy": _test_node_policy(node),
    }
    if isinstance(node.get("inputs"), dict):
        normalized["inputs"] = node["inputs"]
    if isinstance(node.get("outputs"), dict):
        normalized["outputs"] = node["outputs"]
    return common.canonical_hash(normalized)


def test_lease_broker_resumes_durable_distributed_nodes_and_imports_artifacts(tmp_path):
    task_id = "DEV-900082"
    purge_task(task_id)
    plan_marker = tmp_path / "plan-reran"
    plan_guard = tmp_path / "plan_guard.py"
    plan_guard.write_text(
        "import os, pathlib, sys\n"
        "args = sys.argv[3:]\n"
        "if len(args) >= 3 and args[0] == 'workflow-node' and args[2] == 'plan':\n"
        "    pathlib.Path(sys.argv[2]).write_text('plan reran')\n"
        "    raise SystemExit(30)\n"
        "os.execv(sys.argv[1], [sys.argv[1], *args])\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        contract = json.loads(bundle_text)["contract"]
        contract_hash = common.canonical_hash(contract)
        store_path = tmp_path / "artifact-store"
        seed_path = tmp_path / "seed-plan.json"
        seed_path.write_text(json.dumps({"taskId": task_id, "status": "SEEDED_PLAN"}))
        digest = common.sha256_file(seed_path)
        materialized_path = store_path / digest[:2] / digest
        materialized_path.parent.mkdir(parents=True)
        materialized_path.write_bytes(seed_path.read_bytes())
        now = common.utc_now()
        plan_record = {
            "id": "plan",
            "capability": "plan",
            "verb": "plan",
            "dependsOn": [],
            "nodeContractHash": _test_node_contract_hash(nodes[0]),
            "policy": _test_node_policy(nodes[0]),
            "outputs": nodes[0]["outputs"],
            "status": "SUCCEEDED",
            "exitCode": 0,
            "startedAt": now,
            "finishedAt": now,
            "payload": {"status": "SEEDED_PLAN"},
            "artifacts": [{"kind": "plan", "path": str(seed_path), "sha256": digest}],
        }
        plan_dispatch = {"nodeId": "plan", "workerId": "macbook-local:plan", "status": "SUCCEEDED", "attempts": 1}
        materialized = {
            "nodeId": "plan",
            "workerId": "macbook-local:plan",
            "kind": "plan",
            "sha256": digest,
            "sourcePath": str(seed_path),
            "contentAddressedPath": str(materialized_path),
            "status": "MATERIALIZED",
        }
        state_path = tmp_path / "broker-state.json"
        state_path.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": now,
            "producer": "present-lease-broker",
            "updatedAt": now,
            "tasks": {
                task_id: {
                    "taskId": task_id,
                    "status": "DISTRIBUTED_RUNNING",
                    "contractHash": contract_hash,
                    "selectedWorker": "distributed",
                    "attempts": [{
                        "attemptId": "attempt-1",
                        "workerId": "distributed",
                        "status": "DISTRIBUTED_RUNNING",
                        "startedAt": now,
                    }],
                    "updatedAt": now,
                    "distributed": {
                        "schemaVersion": 1,
                        "taskId": task_id,
                        "producedAt": now,
                        "producer": "present-lease-broker distributed state",
                        "updatedAt": now,
                        "workflowId": f"{task_id}-workflow",
                        "contractHash": contract_hash,
                        "storeRoot": str(store_path),
                        "placements": [
                            {
                                "nodeId": "plan",
                                "capability": "plan",
                                "workerId": "macbook-local:plan",
                                "selectionReason": "seeded resume fixture",
                                "eligibleWorkers": [{"workerId": "macbook-local:plan", "score": 100}],
                            },
                            {
                                "nodeId": "verify",
                                "capability": "deterministic-verify",
                                "workerId": "linux-container:verify",
                                "selectionReason": "seeded resume fixture",
                                "eligibleWorkers": [{"workerId": "linux-container:verify", "score": 100}],
                            },
                        ],
                        "artifactTransferManifest": {
                            "schemaVersion": 1,
                            "taskId": task_id,
                            "producedAt": now,
                            "producer": "present-lease-broker artifact transfer planner",
                            "contractHash": contract_hash,
                            "transfers": [],
                        },
                        "nodes": {
                            "plan": {
                                "nodeContractHash": plan_record["nodeContractHash"],
                                "completed": True,
                                "record": plan_record,
                                "dispatch": plan_dispatch,
                                "materialized": [materialized],
                                "imports": [],
                                "updatedAt": now,
                            }
                        },
                        "nodeDispatches": [plan_dispatch],
                        "artifactStore": [materialized],
                        "importedArtifacts": [],
                    },
                }
            },
            "events": [],
        }))
        plan_worker = _isolated_worker_entry(
            tmp_path,
            "macbook-local:plan",
            command=[sys.executable, str(plan_guard), str(BIN), str(plan_marker)],
            unavailable=["deterministic-verify"],
        )
        verify_worker = _isolated_worker_entry(
            tmp_path, "linux-container:verify", unavailable=["plan"], transport_kind="container")
        pool = _worker_pool(tmp_path, [plan_worker, verify_worker])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert not plan_marker.exists()
        assert payload["workflow"]["reusedNodes"] == ["plan"]
        assert payload["workflow"]["freshNodes"] == ["verify"]
        assert [dispatch["nodeId"] for dispatch in payload["nodeDispatches"]] == ["plan", "verify"]
        assert payload["nodeDispatches"][1]["artifactImports"][0]["status"] == "IMPORTED"
        imported_path = tmp_path / "linux-container_verify" / "tasks" / task_id / "imported-artifacts" / digest
        assert imported_path.exists()
        assert common.sha256_file(imported_path) == digest
        verify_node = payload["workflow"]["nodes"][1]
        assert verify_node["resolvedInputs"]["artifacts"][0]["localPath"] == str(imported_path)
        state = _broker_state_payload(state_path)
        assert set(state["tasks"][task_id]["distributed"]["nodes"]) == {"plan", "verify"}
        assert "distributed-resume" in {event["type"] for event in state["events"]}
    finally:
        purge_task(task_id)


def test_lease_broker_runs_ready_nodes_in_parallel(tmp_path):
    task_id = "DEV-900074"
    purge_task(task_id)
    wrapper = tmp_path / "sleepy_worker.py"
    wrapper.write_text(
        "import os, sys, time\n"
        "if len(sys.argv) > 2 and sys.argv[2] == 'workflow-node':\n"
        "    time.sleep(float(os.environ.get('PRESENT_TEST_NODE_SLEEP', '0')))\n"
        "os.execv(sys.argv[1], [sys.argv[1], *sys.argv[2:]])\n"
    )
    command = [sys.executable, str(wrapper), str(BIN)]
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "code-review", "capability": "code-review", "dependsOn": [],
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
        ]
        plan_worker = _isolated_worker_entry(
            tmp_path, "macbook-local:parallel-plan", command=command, unavailable=["code-review"])
        review_worker = _isolated_worker_entry(
            tmp_path, "linux-container:parallel-review", command=command, unavailable=["plan"])
        for worker in (plan_worker, review_worker):
            worker["transport"]["env"]["PRESENT_TEST_NODE_SLEEP"] = "1"
        pool = _worker_pool(tmp_path, [plan_worker, review_worker])
        started = time.monotonic()
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--mock", "--ttl-seconds", "120",
            input_text=_workflow_bundle(task_id, nodes, max_parallel=2))
        elapsed = time.monotonic() - started
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["workflow"]["status"] == "SUCCEEDED"
        assert [dispatch["nodeId"] for dispatch in payload["nodeDispatches"]] == ["plan", "code-review"]
        assert elapsed < 3.5
        events = _broker_state_payload(tmp_path / "state.json")["events"]
        batches = [event for event in events if event["type"] == "distributed-batch-dispatch"]
        assert batches[0]["nodeIds"] == ["plan", "code-review"]
    finally:
        purge_task(task_id)


def test_lease_broker_uses_external_lock_service(tmp_path):
    task_id = "DEV-900075"
    purge_task(task_id)
    lock_log = tmp_path / "locks.jsonl"
    lock_service = tmp_path / "lock_service.py"
    lock_service.write_text(
        "import json, pathlib, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with path.open('a') as f:\n"
        "    f.write(json.dumps(payload, sort_keys=True) + '\\n')\n"
        "print(json.dumps({'status': 'ACQUIRED' if payload['action'] == 'acquire' else 'RELEASED'}))\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        worker = _isolated_worker_entry(tmp_path, "macbook-local:locked")
        pool = _worker_pool(tmp_path, [worker])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--lock-command",
            sys.executable, str(lock_service), str(lock_log), "--mock",
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["nodeDispatches"][0]["lock"]["status"] == "ACQUIRED"
        assert payload["nodeDispatches"][0]["lockRelease"]["status"] == "RELEASED"
        calls = [json.loads(line) for line in lock_log.read_text().splitlines()]
        assert schema_check.validate(calls[0], "canonical/broker-lock-request-v1.json") == []
        assert schema_check.validate(payload["nodeDispatches"][0]["lock"], "canonical/broker-lock-response-v1.json") == []
        assert [call["action"] for call in calls] == ["acquire", "release"]
        assert calls[0]["nodeId"] == "plan"
    finally:
        purge_task(task_id)


def test_lease_broker_treats_lock_service_timeout_as_network_partition(tmp_path):
    task_id = "DEV-900133"
    purge_task(task_id)
    lock_service = tmp_path / "partitioned_lock_service.py"
    lock_service.write_text(
        "import time\n"
        "time.sleep(5)\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:partitioned-lock")])
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--lock-command",
            sys.executable, str(lock_service), "--lock-timeout-seconds", "1", "--mock",
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "DISTRIBUTED_WORKFLOW_FAILED"
        assert payload["nodeDispatches"][0]["status"] == "LOCK_REJECTED"
        assert payload["nodeDispatches"][0]["lock"]["status"] == "FAILED"
        assert payload["nodeDispatches"][0]["lock"]["retryable"] is True
    finally:
        purge_task(task_id)


def test_present_file_lock_adapter_acquires_denies_and_releases(tmp_path):
    request = {
        "schemaVersion": 1,
        "action": "acquire",
        "taskId": "DEV-900078",
        "contractHash": "a" * 64,
        "nodeId": "plan",
        "workerId": "macbook-local:file-lock",
        "ttlSeconds": 60,
        "producedAt": common.utc_now(),
    }
    first = subprocess.run(
        [sys.executable, str(FILE_LOCK), "--dir", str(tmp_path / "locks")],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 0, first.stderr
    acquired = json.loads(first.stdout)
    assert acquired["status"] == "ACQUIRED"
    assert schema_check.validate(acquired, "canonical/broker-lock-response-v1.json") == []

    second = subprocess.run(
        [sys.executable, str(FILE_LOCK), "--dir", str(tmp_path / "locks")],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 30
    denied = json.loads(second.stderr)
    assert denied["status"] == "DENIED"
    assert schema_check.validate(denied, "canonical/broker-lock-response-v1.json") == []

    release = subprocess.run(
        [sys.executable, str(FILE_LOCK), "--dir", str(tmp_path / "locks")],
        input=json.dumps({**request, "action": "release", "lockId": acquired["lockId"]}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert release.returncode == 0, release.stderr
    released = json.loads(release.stdout)
    assert released["status"] == "RELEASED"
    assert schema_check.validate(released, "canonical/broker-lock-response-v1.json") == []


def test_lease_broker_fault_matrix_lock_and_lease_rejections(tmp_path):
    cases = []
    denied_lock = tmp_path / "denied_lock.py"
    denied_lock.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'status': 'DENIED', 'reason': 'chaos lock denial', 'retryable': True}), file=sys.stderr)\n"
        "raise SystemExit(30)\n"
    )
    cases.append({
        "task_id": "DEV-900125",
        "pool_worker": _isolated_worker_entry(tmp_path, "macbook-local:lock-denied"),
        "extra_args": ["--lock-command", sys.executable, str(denied_lock)],
        "expected_dispatch": "LOCK_REJECTED",
    })
    lease_reject_worker = tmp_path / "lease_reject_worker.py"
    lease_reject_worker.write_text(
        "import json, os, sys\n"
        "args = sys.argv[2:]\n"
        "if args and args[0] == 'worker-lease' and '--acquire' in args:\n"
        "    print(json.dumps({'status': 'BLOCKED', 'reason': 'chaos lease rejection', 'retryable': True}), file=sys.stderr)\n"
        "    raise SystemExit(30)\n"
        "os.execv(sys.argv[1], [sys.argv[1], *args])\n"
    )
    cases.append({
        "task_id": "DEV-900126",
        "pool_worker": _isolated_worker_entry(
            tmp_path, "macbook-local:lease-rejected",
            command=[sys.executable, str(lease_reject_worker), str(BIN)]),
        "extra_args": [],
        "expected_dispatch": "LEASE_REJECTED",
    })
    for case in cases:
        purge_task(case["task_id"])
        pool = _worker_pool(tmp_path, [case["pool_worker"]])
        nodes = [{"id": "plan", "capability": "plan", "dependsOn": [],
                  "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}}]
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "run-distributed", case["task_id"], "--pool", str(pool), "--state", str(tmp_path / f"{case['task_id']}.json"),
            "--artifact-store", str(tmp_path / f"{case['task_id']}-store"),
            *case["extra_args"], "--mock", input_text=_workflow_bundle(case["task_id"], nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "DISTRIBUTED_WORKFLOW_FAILED"
        assert payload["nodeDispatches"][0]["status"] == case["expected_dispatch"]
        purge_task(case["task_id"])


def test_lease_broker_provisions_container_worker_before_dispatch(tmp_path):
    task_id = "DEV-900076"
    purge_task(task_id)
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status_for_pool("linux-container:provisioned")))
    provisioner = tmp_path / "provisioner.py"
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('action') == 'discover':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': True, 'workerKinds': ['linux-container'], 'trustLevels': ['local-user-trusted'], 'supportedProviders': ['test'], 'estimatedStartupSeconds': 1, 'costClass': 'test'}))\n"
        "    raise SystemExit(0)\n"
        "if request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RESERVED', 'reservationId': 'test-reservation', 'ttlSeconds': 300}))\n"
        "    raise SystemExit(0)\n"
        "status = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "worker_id = 'linux-container:provisioned'\n"
        "root = pathlib.Path(sys.argv[2])\n"
        "bin_path = sys.argv[3]\n"
        "env = {\n"
        "  'PRESENT_WORKER_ID': worker_id,\n"
        "  'PRESENT_WORKER_KIND': 'linux-container',\n"
        "  'PRESENT_WORKER_SURFACE': 'container',\n"
        "  'PRESENT_TASKS_DIR': str(root / 'tasks'),\n"
        "  'PRESENT_LOGS_DIR': str(root / 'logs'),\n"
        "  'PRESENT_TEST_RESULTS_DIR': str(root / 'test-results'),\n"
        "  'PRESENT_JOBS_DIR': str(root / 'jobs'),\n"
        "  'PRESENT_WORKTREES_DIR': str(root / 'worktrees')\n"
        "}\n"
        "worker = {'id': worker_id, 'command': [bin_path], 'transport': {'kind': 'container', 'command': [bin_path], 'timeoutSeconds': 30, 'env': env}, 'status': status, 'health': {'score': 100}}\n"
        "print(json.dumps({'schemaVersion': 1, 'status': 'PROVISIONED', 'requestTaskId': request['taskId'], 'workers': [worker]}))\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--provision-command",
            sys.executable, str(provisioner), str(status_path), str(tmp_path / "provisioned-root"), str(BIN),
            "--mock", input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["provisioning"][0]["workers"] == ["linux-container:provisioned"]
        assert payload["nodeDispatches"][0]["workerId"] == "linux-container:provisioned"
        persisted = json.loads(pool.read_text())
        assert persisted["workers"][0]["id"] == "linux-container:provisioned"
    finally:
        purge_task(task_id)


def test_lease_broker_validates_provisioner_contracts(tmp_path):
    task_id = "DEV-900096"
    purge_task(task_id)
    seen_request = tmp_path / "request.json"
    provisioner = tmp_path / "contract_provisioner.py"
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status_for_pool("linux-container:contracted")))
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('action') == 'discover':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': True, 'workerKinds': ['linux-container'], 'trustLevels': ['local-user-trusted'], 'supportedProviders': ['test'], 'estimatedStartupSeconds': 1, 'costClass': 'test'}))\n"
        "    raise SystemExit(0)\n"
        "if request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RESERVED', 'reservationId': 'contract-reservation', 'ttlSeconds': 300}))\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(request, sort_keys=True))\n"
        "status = json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
        "worker = {'id': 'linux-container:contracted', 'command': [sys.argv[3]], 'status': status}\n"
        "print(json.dumps({'schemaVersion': 1, 'status': 'PROVISIONED', 'workers': [worker]}))\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "provision", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--provision-command", sys.executable, str(provisioner), str(seen_request), str(status_path), str(BIN),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        request = json.loads(seen_request.read_text())
        assert schema_check.validate(request, "canonical/broker-provision-request-v1.json") == []
        assert payload["provisioning"][0]["status"] == "PROVISIONED"
        assert schema_check.validate({
            "status": payload["provisioning"][0]["status"],
            "workers": json.loads(pool.read_text())["workers"],
        }, "canonical/broker-provision-response-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_selects_best_discovered_provisioner(tmp_path):
    task_id = "DEV-900119"
    purge_task(task_id)
    log_path = tmp_path / "provisioner-log.jsonl"
    provisioner = tmp_path / "ranked_provisioner.py"
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status_for_pool("linux-container:selected")))
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "label = sys.argv[1]\n"
        "log = pathlib.Path(sys.argv[2])\n"
        "status = json.loads(pathlib.Path(sys.argv[3]).read_text())\n"
        "bin_path = sys.argv[4]\n"
        "request = json.loads(sys.stdin.read())\n"
        "with log.open('a') as handle:\n"
        "    handle.write(json.dumps({'label': label, 'action': request.get('action')}) + '\\n')\n"
        "if request.get('action') == 'discover':\n"
        "    slow = label == 'slow'\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': True, 'workerKinds': ['linux-container'], 'trustLevels': ['local-user-trusted'], 'supportedProviders': ['test'], 'estimatedStartupSeconds': 90 if slow else 1, 'costClass': 'high' if slow else 'local'}))\n"
        "    raise SystemExit(0)\n"
        "if request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RESERVED', 'reservationId': label + '-reservation', 'ttlSeconds': 300}))\n"
        "    raise SystemExit(0)\n"
        "worker = {'id': 'linux-container:selected', 'command': [bin_path], 'status': status}\n"
        "print(json.dumps({'schemaVersion': 1, 'status': 'PROVISIONED', 'workers': [worker]}))\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "provision", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--provision-command", sys.executable, str(provisioner), "slow", str(log_path), str(status_path), str(BIN),
            "--provision-command", sys.executable, str(provisioner), "fast", str(log_path), str(status_path), str(BIN),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        by_id = {item["id"]: item for item in payload["provisioning"]}
        assert by_id["cli-1"]["status"] == "SKIPPED"
        assert by_id["cli-1"]["selection"]["selected"] is False
        assert by_id["cli-2"]["status"] == "PROVISIONED"
        assert by_id["cli-2"]["selection"]["selected"] is True
        log = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert [item for item in log if item["action"] == "provision"] == [{"label": "fast", "action": "provision"}]
    finally:
        purge_task(task_id)


def test_lease_broker_reservation_failure_blocks_provision(tmp_path):
    task_id = "DEV-900120"
    purge_task(task_id)
    log_path = tmp_path / "reservation-log.jsonl"
    provisioner = tmp_path / "reservation_provisioner.py"
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "log = pathlib.Path(sys.argv[1])\n"
        "request = json.loads(sys.stdin.read())\n"
        "with log.open('a') as handle:\n"
        "    handle.write(json.dumps({'action': request.get('action')}) + '\\n')\n"
        "if request.get('action') == 'discover':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': True, 'workerKinds': ['linux-container'], 'trustLevels': ['local-user-trusted'], 'supportedProviders': ['test'], 'estimatedStartupSeconds': 1, 'costClass': 'local'}))\n"
        "elif request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DENIED', 'reason': 'slot already reserved', 'retryable': True}))\n"
        "else:\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'PROVISIONED', 'workers': []}))\n"
        "raise SystemExit(0)\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [{"id": "plan", "capability": "plan", "dependsOn": [],
                  "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}}]
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "provision", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--provision-command", sys.executable, str(provisioner), str(log_path),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert payload["status"] == "NO_WORKERS_PROVISIONED"
        assert payload["provisioning"][0]["status"] == "FAILED"
        assert payload["provisioning"][0]["reservation"]["status"] == "DENIED"
        assert [json.loads(line)["action"] for line in log_path.read_text().splitlines()] == ["discover", "reserve"]
    finally:
        purge_task(task_id)


def test_lease_broker_releases_reservation_after_provision_failure(tmp_path):
    task_id = "DEV-900125"
    purge_task(task_id)
    log_path = tmp_path / "reservation-release-log.jsonl"
    provisioner = tmp_path / "reservation_release_provisioner.py"
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "log = pathlib.Path(sys.argv[1])\n"
        "request = json.loads(sys.stdin.read())\n"
        "with log.open('a') as handle:\n"
        "    handle.write(json.dumps(request, sort_keys=True) + '\\n')\n"
        "if request.get('action') == 'discover':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': True, 'workerKinds': ['linux-container'], 'trustLevels': ['local-user-trusted'], 'supportedProviders': ['test'], 'estimatedStartupSeconds': 1, 'costClass': 'local'}))\n"
        "elif request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RESERVED', 'reservationId': 'release-me', 'ttlSeconds': 300}))\n"
        "elif request.get('action') == 'release-reservation':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RELEASED', 'reservationId': request['reservationId']}))\n"
        "else:\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'FAILED', 'reason': 'boot failed', 'retryable': True}))\n"
        "raise SystemExit(0)\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [{"id": "plan", "capability": "plan", "dependsOn": [],
                  "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}}]
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "provision", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--provision-command", sys.executable, str(provisioner), str(log_path),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "NO_WORKERS_PROVISIONED"
        assert payload["provisioning"][0]["reservationRelease"]["status"] == "RELEASED"
        calls = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert [call["action"] for call in calls] == ["discover", "reserve", "provision", "release-reservation"]
        assert calls[-1]["reservationId"] == "release-me"
        assert schema_check.validate(calls[-1], "canonical/broker-provision-reservation-request-v1.json") == []
        assert schema_check.validate(
            payload["provisioning"][0]["reservationRelease"],
            "canonical/broker-provision-reservation-response-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_releases_reservation_when_provisioned_worker_fails_healthcheck(tmp_path):
    task_id = "DEV-900132"
    purge_task(task_id)
    log_path = tmp_path / "healthcheck-release-log.jsonl"
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status_for_pool("linux-container:unhealthy")))
    provisioner = tmp_path / "unhealthy_provisioner.py"
    provisioner.write_text(
        "import json, pathlib, sys\n"
        "log = pathlib.Path(sys.argv[1])\n"
        "status = json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
        "request = json.loads(sys.stdin.read())\n"
        "with log.open('a') as handle:\n"
        "    handle.write(json.dumps(request, sort_keys=True) + '\\n')\n"
        "if request.get('action') == 'discover':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'DISCOVERED', 'canProvision': True, 'workerKinds': ['linux-container'], 'trustLevels': ['local-user-trusted'], 'supportedProviders': ['test'], 'estimatedStartupSeconds': 1, 'costClass': 'local'}))\n"
        "elif request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RESERVED', 'reservationId': 'health-reservation', 'ttlSeconds': 300}))\n"
        "elif request.get('action') == 'release-reservation':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'RELEASED', 'reservationId': request['reservationId']}))\n"
        "else:\n"
        "    worker = {'id': 'linux-container:unhealthy', 'command': [sys.executable, '-c', 'import sys; sys.exit(30)'], 'status': status, 'health': {'score': 100}}\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'PROVISIONED', 'workers': [worker]}))\n"
        "raise SystemExit(0)\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [{"id": "plan", "capability": "plan", "dependsOn": [],
                  "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}}]
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "provision", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--provision-command", sys.executable, str(provisioner), str(log_path), str(status_path),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "NO_WORKERS_PROVISIONED"
        report = payload["provisioning"][0]
        assert report["status"] == "FAILED"
        assert report["failureClass"] == "healthcheck-failed"
        assert report["healthChecks"][0]["workerId"] == "linux-container:unhealthy"
        assert report["healthChecks"][0]["status"] == "FAILED"
        assert report["reservationRelease"]["status"] == "RELEASED"
        assert json.loads(pool.read_text())["workers"] == []
        calls = [json.loads(line)["action"] for line in log_path.read_text().splitlines()]
        assert calls == ["discover", "reserve", "provision", "release-reservation"]
    finally:
        purge_task(task_id)


def test_lease_broker_reports_provisioning_failure_before_placement(tmp_path):
    task_id = "DEV-900092"
    purge_task(task_id)
    provisioner = tmp_path / "failing_provisioner.py"
    provisioner.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "if request.get('action') == 'discover':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'FAILED', 'canProvision': False, 'workerKinds': [], 'trustLevels': [], 'supportedProviders': [], 'reason': 'capacity exhausted', 'retryable': True}))\n"
        "elif request.get('action') == 'reserve':\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'FAILED', 'reason': 'capacity exhausted', 'retryable': True}))\n"
        "else:\n"
        "    print(json.dumps({'schemaVersion': 1, 'status': 'FAILED', 'reason': 'capacity exhausted', 'retryable': True}))\n"
        "raise SystemExit(30)\n"
    )
    try:
        pool = _worker_pool(tmp_path, [])
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--provision-command",
            sys.executable, str(provisioner), "--mock", input_text=_workflow_bundle(task_id, nodes))
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "NO_NODE_PLACEMENT"
        assert payload["schemaRegistryHash"] == common.schema_registry_hash()
        assert payload["provisioning"][0]["status"] == "FAILED"
        assert payload["provisioning"][0]["reason"] == "capacity exhausted"
    finally:
        purge_task(task_id)


def test_worker_pool_trust_policy_is_versioned_and_enforced(tmp_path, monkeypatch):
    policy = {
        "schemaVersion": 1,
        "producedAt": common.utc_now(),
        "producer": "present-worker-pool-trust-policy",
        "riskTrust": {
            "low": ["sandboxed"],
            "normal": ["sandboxed"],
            "high": ["secure-enterprise"],
            "critical": ["secure-enterprise"],
        },
    }
    policy_path = tmp_path / "trust-policy.json"
    policy_path.write_text(json.dumps(policy))
    monkeypatch.setattr(common, "WORKER_POOL_TRUST_POLICY_PATH", policy_path)
    broker = runpy.run_path(str(BROKER))
    assert common.load_worker_pool_trust_policy()["riskTrust"]["high"] == ["secure-enterprise"]
    assert broker["_trust_satisfies"]({"riskClass": "high"}, "local-user-trusted") is False
    assert broker["_trust_satisfies"]({"riskClass": "high"}, "secure-enterprise") is True


def test_lease_broker_renews_lease_during_long_node_execution(tmp_path):
    task_id = "DEV-900079"
    purge_task(task_id)
    slow_worker = tmp_path / "slow_worker.py"
    slow_worker.write_text(
        "import os, sys, time\n"
        "args = sys.argv[2:]\n"
        "if args and args[0] == 'workflow-node':\n"
        "    time.sleep(1.2)\n"
        "os.execv(sys.argv[1], [sys.argv[1], *args])\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        command = [sys.executable, str(slow_worker), str(BIN)]
        worker = _isolated_worker_entry(tmp_path, "macbook-local:heartbeat", command=command)
        pool = _worker_pool(tmp_path, [worker])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--heartbeat-interval-seconds", "1",
            "--ttl-seconds", "30", "--mock", input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        renewals = payload["nodeDispatches"][0]["leaseRenewals"]
        assert renewals
        assert renewals[0]["status"] == "RENEWED"
    finally:
        purge_task(task_id)


def test_broker_event_type_registry_rejects_unknown_events():
    broker = runpy.run_path(str(BROKER))
    state = {"events": []}
    try:
        broker["_append_event"](state, "DEV-900110", "unknown-event")
        assert False, "unknown broker event type should be rejected"
    except common.TaskBundleError as exc:
        assert "not registered" in str(exc)
    broker["_append_event"](state, "DEV-900110", "select", workerId="macbook-local:test")
    assert state["events"][0]["type"] == "select"


def test_integrity_ledger_enforcement_rejects_missing_materialize_entry():
    broker = runpy.run_path(str(BROKER))
    ledger = {
        "schemaVersion": 1,
        "taskId": "DEV-900111",
        "producedAt": common.utc_now(),
        "producer": "present-lease-broker artifact integrity",
        "contractHash": "1" * 64,
        "workflowRunId": "wr-" + "2" * 24,
        "entries": [
            {
                "id": "il-redaction",
                "operation": "redaction",
                "status": "CLEAN",
                "producedAt": common.utc_now(),
                "sha256": "3" * 64,
            },
            {
                "id": "il-handoff",
                "operation": "handoff",
                "status": "READY",
                "producedAt": common.utc_now(),
                "sha256": "3" * 64,
            },
        ],
    }
    try:
        broker["_validate_integrity_ledger"](
            [{"nodeId": "plan", "status": "MATERIALIZED", "sha256": "4" * 64}],
            [],
            {"status": "READY", "handoffSha256": "3" * 64, "redactionReport": {"status": "CLEAN"}},
            ledger,
        )
        assert False, "ledger without a materialize entry should be rejected"
    except common.TaskBundleError as exc:
        assert "missing a materialize entry" in str(exc)


def test_lease_broker_recover_plans_and_applies_stale_state(tmp_path):
    task_id = "DEV-900097"
    purge_task(task_id)
    try:
        pool = _worker_pool(tmp_path, [{
            "id": "macbook-local:recover",
            "command": [str(BIN)],
            "status": _status_for_pool("macbook-local:recover"),
        }])
        state_path = _write_broker_state(tmp_path, {
            task_id: {
                "taskId": task_id,
                "status": "RUNNING",
                "contractHash": "7" * 64,
                "selectedWorker": "macbook-local:recover",
                "lastHeartbeatAt": "2000-01-01T00:00:00Z",
                "attempts": [],
                "updatedAt": common.utc_now(),
            }
        })
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path), "--stale-seconds", "1")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "RECOVERY_PLAN"
        assert payload["actions"][0]["action"] == "failover-lease"
        assert schema_check.validate(payload, "canonical/broker-response-recover-v1.json") == []

        code, applied, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path), "--stale-seconds", "1",
            "--apply")
        assert code == 0, stderr
        assert applied["status"] == "RECOVERY_APPLIED"
        assert applied["appliedActions"][0]["applyStatus"] == "APPLIED"
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "LEASE_EXPIRED"
        assert any(event["type"] == "recover-applied" for event in state["events"])
    finally:
        purge_task(task_id)


def test_lease_broker_recovery_retry_node_redispatches_downstream_nodes(tmp_path):
    task_id = "DEV-900113"
    purge_task(task_id)
    counter_worker = tmp_path / "counter_worker.py"
    counter_worker.write_text(
        "import os, pathlib, sys\n"
        "counter = pathlib.Path(sys.argv[2])\n"
        "watched = sys.argv[3]\n"
        "args = sys.argv[4:]\n"
        "if len(args) >= 3 and args[0] == 'workflow-node' and args[2] == watched:\n"
        "    count = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(count + 1))\n"
        "os.execv(sys.argv[1], [sys.argv[1], *args])\n"
    )
    plan_count = tmp_path / "plan-count"
    verify_count = tmp_path / "verify-count"
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(
                tmp_path, "macbook-local:plan",
                command=[sys.executable, str(counter_worker), str(BIN), str(plan_count), "plan"],
                unavailable=["deterministic-verify"]),
            _isolated_worker_entry(
                tmp_path, "linux-container:verify",
                command=[sys.executable, str(counter_worker), str(BIN), str(verify_count), "verify"],
                unavailable=["plan"], transport_kind="container"),
        ])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert first["workflow"]["freshNodes"] == ["plan", "verify"]
        assert plan_count.read_text() == "1"
        assert verify_count.read_text() == "1"

        code, recovery, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path),
            "--apply", "--action", "retry-node", "--node-id", "plan", "--reason", "force retry")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert recovery["status"] == "RECOVERY_APPLIED"
        assert recovery["appliedActions"][0]["invalidatedNodes"] == ["plan", "verify"]
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_RUNNING"
        assert "terminalAt" not in state["tasks"][task_id]
        assert state["tasks"][task_id]["distributed"]["nodes"] == {}

        code, resumed, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert resumed["status"] == "DISTRIBUTED_WORKFLOW_DISPATCHED"
        assert resumed["workflow"]["freshNodes"] == ["plan", "verify"]
        assert resumed["workflow"]["reusedNodes"] == []
        assert plan_count.read_text() == "2"
        assert verify_count.read_text() == "2"
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_COMPLETE"
        ledger = state["tasks"][task_id]["distributed"]["attemptLedger"]
        assert [entry["status"] for entry in ledger] == ["DISTRIBUTED_COMPLETE", "DISTRIBUTED_COMPLETE"]
        assert ledger[-1]["freshNodes"] == ["plan", "verify"]
    finally:
        purge_task(task_id)


def test_lease_broker_recovers_stale_node_lease_and_cancels_subgraph(tmp_path):
    task_id = "DEV-900134"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(tmp_path, "macbook-local:plan", unavailable=["deterministic-verify"]),
            _isolated_worker_entry(
                tmp_path, "linux-container:verify", unavailable=["plan"], transport_kind="container"),
        ])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}

        state = _broker_state_payload(state_path)
        task = state["tasks"][task_id]
        completed_nodes = json.loads(json.dumps(task["distributed"]["nodes"]))
        task["status"] = "DISTRIBUTED_RUNNING"
        task.pop("terminalAt", None)
        task["distributed"]["nodeDispatches"][0]["status"] = "RUNNING"
        task["distributed"]["nodeDispatches"][0]["startedAt"] = "2000-01-01T00:00:00Z"
        task["distributed"]["nodeDispatches"][0]["leaseId"] = "wl-" + "4" * 24
        state_path.write_text(json.dumps(state))

        code, plan, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path), "--stale-seconds", "1")
        assert code == 0, stderr
        assert plan["status"] == "RECOVERY_PLAN"
        lease_action = next(action for action in plan["actions"] if action["action"] == "recover-node-lease")
        assert lease_action["leaseId"] == "wl-" + "4" * 24
        assert schema_check.validate(plan, "canonical/broker-response-recover-v1.json") == []

        code, recovered, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path),
            "--apply", "--action", "recover-node-lease", "--node-id", "plan",
            "--idempotency-key", "recover-node-lease-once")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert recovered["status"] == "RECOVERY_APPLIED"
        assert recovered["appliedActions"][0]["invalidatedNodes"] == ["plan", "verify"]
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_RUNNING"
        assert state["tasks"][task_id]["distributed"]["nodes"] == {}
        assert state["operatorAuditLog"][-1]["action"] == "recover"
        state["tasks"][task_id]["distributed"]["nodes"] = completed_nodes
        state_path.write_text(json.dumps(state))

        code, cancelled, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path),
            "--apply", "--action", "cancel-subgraph", "--node-id", "plan",
            "--idempotency-key", "cancel-plan-subgraph")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert cancelled["status"] == "RECOVERY_APPLIED"
        assert cancelled["appliedActions"][0]["cancelledNodes"] == ["plan", "verify"]
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["distributed"]["cancelledNodes"] == ["plan", "verify"]
    finally:
        purge_task(task_id)


def test_lease_broker_recover_run_applies_and_executes_retry(tmp_path):
    task_id = "DEV-900115"
    purge_task(task_id)
    count_path = tmp_path / "plan-count"
    counter_worker = tmp_path / "counter_worker.py"
    counter_worker.write_text(
        "import os, pathlib, sys\n"
        "counter = pathlib.Path(sys.argv[2])\n"
        "args = sys.argv[3:]\n"
        "if len(args) >= 3 and args[0] == 'workflow-node' and args[2] == 'plan':\n"
        "    count = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(count + 1))\n"
        "os.execv(sys.argv[1], [sys.argv[1], *args])\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(
                tmp_path, "macbook-local:plan",
                command=[sys.executable, str(counter_worker), str(BIN), str(count_path)]),
        ])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert first["status"] == "DISTRIBUTED_WORKFLOW_DISPATCHED"
        assert count_path.read_text() == "1"

        code, recovered, stderr_payload, stdout, stderr = run_broker_result(
            "recover-run", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--action", "retry-node", "--node-id", "plan",
            "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert recovered["status"] == "RECOVERY_RUN_COMPLETE"
        assert recovered["recovery"]["status"] == "RECOVERY_APPLIED"
        assert recovered["run"]["status"] == "DISTRIBUTED_WORKFLOW_DISPATCHED"
        assert count_path.read_text() == "2"
        ledger = _broker_state_payload(state_path)["tasks"][task_id]["distributed"]["attemptLedger"]
        assert ledger[-1]["recoveryIdempotencyKey"] == recovered["recovery"]["idempotencyKey"]
        assert schema_check.validate(recovered, "canonical/broker-response-recover-run-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_recovery_idempotency_key_replays_without_duplicate_apply(tmp_path):
    task_id = "DEV-900121"
    purge_task(task_id)
    try:
        nodes = [{"id": "plan", "capability": "plan", "dependsOn": [],
                  "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}}]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:plan")])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr

        key = "retry-plan-once"
        code, applied, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path),
            "--apply", "--action", "retry-node", "--node-id", "plan", "--idempotency-key", key)
        assert code == 0, stderr
        assert applied["idempotencyKey"] == key
        assert applied["idempotentReplay"] is False
        state_after_first = _broker_state_payload(state_path)
        events_after_first = len(state_after_first["events"])
        assert state_after_first["recoveryLedger"][0]["idempotencyKey"] == key
        assert "recoveryApplications" not in state_after_first["tasks"][task_id]

        code, replay, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path),
            "--apply", "--action", "retry-node", "--node-id", "plan", "--idempotency-key", key)
        assert code == 0, stderr
        assert replay["idempotentReplay"] is True
        assert len(_broker_state_payload(state_path)["events"]) == events_after_first
    finally:
        purge_task(task_id)


def test_lease_broker_recovery_rematerializes_missing_artifact(tmp_path):
    task_id = "DEV-900114"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(tmp_path, "macbook-local:plan"),
        ])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        materialized_path = Path(first["artifactStore"]["artifacts"][0]["contentAddressedPath"])
        digest = first["artifactStore"]["artifacts"][0]["sha256"]
        materialized_path.unlink()

        code, recovery, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(pool), "--state", str(state_path),
            "--apply", "--stale-seconds", "1")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert recovery["status"] == "RECOVERY_APPLIED"
        assert recovery["appliedActions"][0]["action"] == "rematerialize-artifact"
        assert recovery["appliedActions"][0]["resumeRequired"] is False
        assert recovery["appliedActions"][0]["artifactsMaterialized"] == 1
        assert materialized_path.exists()
        assert common.sha256_file(materialized_path) == digest
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_COMPLETE"

        code, replay, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert replay["status"] == "IDEMPOTENT_REPLAY"
    finally:
        purge_task(task_id)


def test_lease_broker_artifact_audit_reports_missing_materialized_artifact(tmp_path):
    task_id = "DEV-900116"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:plan")])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        Path(first["artifactStore"]["artifacts"][0]["contentAddressedPath"]).unlink()

        code, audit, stderr_payload, stdout, stderr = run_broker_result(
            "artifact-audit", task_id, "--state", str(state_path), "--artifact-store", str(store_path), "--repair")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert audit["status"] == "ARTIFACT_AUDIT_COMPLETE"
        assert audit["summary"]["errors"] == 1
        assert any(item["kind"] == "materialized-artifact" for item in audit["findings"])
        assert audit["repairActions"][0]["action"] == "rematerialize-artifact"
        assert schema_check.validate(audit, "canonical/broker-response-artifact-audit-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_artifact_audit_reports_corrupt_handoff_artifact(tmp_path):
    task_id = "DEV-900126"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:plan")])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        handoff_path = Path(first["distributedArtifacts"]["handoffPath"])
        handoff_path.write_text("{\"schemaVersion\": 1, \"status\": \"CORRUPT\"}\n")

        code, audit, stderr_payload, stdout, stderr = run_broker_result(
            "artifact-audit", task_id, "--state", str(state_path), "--artifact-store", str(store_path))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert audit["summary"]["errors"] >= 1
        assert any(
            item["severity"] == "error"
            and item["kind"] == "broker-artifact"
            and item["path"] == str(handoff_path)
            for item in audit["findings"]
        )
        assert schema_check.validate(audit, "canonical/broker-response-artifact-audit-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_recovery_does_not_reopen_cancelled_without_override(tmp_path):
    task_id = "DEV-900117"
    now = common.utc_now()
    state_path = _write_broker_state(tmp_path, {
        task_id: {
            "taskId": task_id,
            "status": "CANCELLED",
            "contractHash": "6" * 64,
            "selectedWorker": "distributed",
            "terminalAt": now,
            "attempts": [],
            "updatedAt": now,
            "distributed": {
                "schemaVersion": 1,
                "taskId": task_id,
                "producedAt": now,
                "producer": "present-lease-broker distributed state",
                "updatedAt": now,
                "workflowId": f"{task_id}-workflow",
                "contractHash": "6" * 64,
                "storeRoot": str(tmp_path / "store"),
                "placements": [{"nodeId": "plan", "capability": "plan", "workerId": "macbook-local:plan", "eligibleWorkers": []}],
                "artifactTransferManifest": {"schemaVersion": 1, "taskId": task_id, "producedAt": now, "producer": "test", "contractHash": "6" * 64, "transfers": []},
                "nodes": {},
                "nodeDispatches": [],
                "artifactStore": [],
                "importedArtifacts": [],
            },
        }
    })
    pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:plan")])
    code, stdout_payload, blocked, stdout, stderr = run_broker_result(
        "recover", task_id, "--pool", str(pool), "--state", str(state_path),
        "--apply", "--action", "retry-node", "--node-id", "plan")
    assert code == 30
    assert stdout_payload == {}
    assert blocked["status"] == "RECOVERY_BLOCKED"
    assert "cancelled workflow" in blocked["appliedActions"][0]["reason"]

    code, applied, stderr_payload, stdout, stderr = run_broker_result(
        "recover", task_id, "--pool", str(pool), "--state", str(state_path),
        "--apply", "--action", "retry-node", "--node-id", "plan", "--force-cancelled")
    assert code == 0, stderr
    assert applied["status"] == "RECOVERY_APPLIED"
    state = _broker_state_payload(state_path)
    assert state["tasks"][task_id]["status"] == "DISTRIBUTED_RUNNING"


def test_lease_broker_recovery_authorizes_admin_only_actions(tmp_path):
    task_id = "DEV-900122"
    state_path = _write_broker_state(tmp_path, {
        task_id: {
            "taskId": task_id,
            "status": "RUNNING",
            "contractHash": "8" * 64,
            "selectedWorker": "macbook-local:plan",
            "attempts": [],
            "updatedAt": common.utc_now(),
        }
    })
    pool = _worker_pool(tmp_path, [_isolated_worker_entry(tmp_path, "macbook-local:plan")])
    code, stdout_payload, blocked, stdout, stderr = run_broker_result(
        "recover", task_id, "--pool", str(pool), "--state", str(state_path),
        "--apply", "--action", "mark-inconsistent", "--reason", "admin only")
    assert code == 30
    assert blocked["status"] == "RECOVERY_BLOCKED"
    assert "requires admin" in blocked["appliedActions"][0]["reason"]

    code, applied, stderr_payload, stdout, stderr = run_broker_result(
        "recover", task_id, "--pool", str(pool), "--state", str(state_path),
        "--apply", "--action", "mark-inconsistent", "--reason", "admin approved", "--operator-role", "admin")
    assert code == 0, stderr
    assert applied["status"] == "RECOVERY_APPLIED"
    assert _broker_state_payload(state_path)["tasks"][task_id]["status"] == "INCONSISTENT"


def test_lease_broker_rematerialize_falls_back_to_resume_when_source_worker_missing(tmp_path):
    task_id = "DEV-900118"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        full_pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(tmp_path, "macbook-local:plan", unavailable=["deterministic-verify"]),
            _isolated_worker_entry(tmp_path, "linux-container:verify", unavailable=["plan"], transport_kind="container"),
        ])
        state_path = tmp_path / "state.json"
        store_path = tmp_path / "store"
        code, first, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(full_pool), "--state", str(state_path),
            "--artifact-store", str(store_path), "--mock", input_text=bundle_text)
        assert code == 0, stderr
        Path(first["artifactStore"]["artifacts"][0]["contentAddressedPath"]).unlink()
        verify_only_pool = _worker_pool(tmp_path, [
            _isolated_worker_entry(tmp_path, "linux-container:verify", unavailable=["plan"], transport_kind="container"),
        ])
        code, recovery, stderr_payload, stdout, stderr = run_broker_result(
            "recover", task_id, "--pool", str(verify_only_pool), "--state", str(state_path), "--apply")
        assert code == 0, stderr
        assert recovery["appliedActions"][0]["resumeRequired"] is True
        assert recovery["appliedActions"][0]["invalidatedNodes"] == ["plan", "verify"]
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "DISTRIBUTED_RUNNING"
    finally:
        purge_task(task_id)


def test_worker_duplicate_lease_acquire_is_idempotent():
    task_id = "DEV-900093"
    purge_task(task_id)
    try:
        contract_hash = "9" * 64
        code, first, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire", "--contract-hash", contract_hash, "--ttl-seconds", "120")
        assert code == 0, stderr
        assert stderr_payload == {}
        code, duplicate, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire", "--contract-hash", contract_hash, "--ttl-seconds", "120")
        assert code == 0, stderr
        assert stderr_payload == {}
        assert duplicate["leaseId"] == first["leaseId"]
        assert duplicate["renewalCount"] == 0
        assert schema_check.validate(duplicate, "canonical/worker-lease-v1.json") == []
    finally:
        purge_task(task_id)


def test_lease_broker_reconcile_observes_worker_cancelled_workflow(tmp_path):
    task_id = "DEV-900094"
    purge_task(task_id)
    try:
        code, status = run_worker("status")
        assert code == 0
        worker_id = status["detail"]["workerIdentity"]["id"]
        pool = _worker_pool(tmp_path, [{"id": worker_id, "command": [str(BIN)], "status": status}])
        state_path = tmp_path / "broker-state.json"
        state_path.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "present-lease-broker",
            "updatedAt": common.utc_now(),
            "tasks": {
                task_id: {
                    "taskId": task_id,
                    "status": "RUNNING",
                    "contractHash": "8" * 64,
                    "selectedWorker": worker_id,
                    "attempts": [],
                    "updatedAt": common.utc_now(),
                }
            },
            "events": [],
        }))
        code, cancel, stderr_payload, stdout, stderr = run_worker_result("workflow-cancel", task_id)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert cancel["status"] in {"CANCELLED", "NO_ACTIVE_WORK"}

        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "reconcile", task_id, "--pool", str(pool), "--state", str(state_path))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "CANCELLED"
        state = _broker_state_payload(state_path)
        assert state["tasks"][task_id]["status"] == "CANCELLED"
        assert any(event["type"] == "reconcile" and event["status"] == "CANCELLED" for event in state["events"])
    finally:
        purge_task(task_id)


def test_lease_broker_deprovisions_retire_after_task_worker(tmp_path):
    task_id = "DEV-900080"
    purge_task(task_id)
    deprovision_log = tmp_path / "deprovision.jsonl"
    deprovisioner = tmp_path / "deprovisioner.py"
    deprovisioner.write_text(
        "import json, pathlib, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "with pathlib.Path(sys.argv[1]).open('a') as handle:\n"
        "    handle.write(json.dumps(request, sort_keys=True) + '\\n')\n"
        "print(json.dumps({'status': 'DEPROVISIONED'}))\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        worker = _isolated_worker_entry(tmp_path, "cloud-ephemeral:retire")
        worker["lifecycle"] = {
            "retireAfterTask": True,
            "deprovisionCommand": [sys.executable, str(deprovisioner), str(deprovision_log)],
        }
        pool = _worker_pool(tmp_path, [worker])
        state_path = tmp_path / "state.json"
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(state_path),
            "--artifact-store", str(tmp_path / "store"), "--mock",
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["deprovisioning"][0]["status"] == "DEPROVISIONED"
        assert payload["deprovisioning"][0]["removedFromPool"] is True
        assert json.loads(pool.read_text())["workers"] == []
        logged = [json.loads(line) for line in deprovision_log.read_text().splitlines()]
        assert logged[0]["action"] == "deprovision"
        assert logged[0]["workerId"] == "cloud-ephemeral:retire"
        events = _broker_state_payload(state_path)["events"]
        assert "deprovision" in {event["type"] for event in events}
    finally:
        purge_task(task_id)


def test_lease_broker_reconciles_stranded_provisioned_workers(tmp_path):
    task_id = "DEV-900124"
    deprovision_log = tmp_path / "cleanup-deprovision.jsonl"
    deprovisioner = tmp_path / "cleanup_deprovisioner.py"
    deprovisioner.write_text(
        "import json, pathlib, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "with pathlib.Path(sys.argv[1]).open('a') as handle:\n"
        "    handle.write(json.dumps(request, sort_keys=True) + '\\n')\n"
        "print(json.dumps({'status': 'DEPROVISIONED'}))\n"
    )
    worker = _isolated_worker_entry(tmp_path, "cloud-ephemeral:stranded")
    worker["lifecycle"] = {
        "retireAfterTask": True,
        "deprovisionCommand": [sys.executable, str(deprovisioner), str(deprovision_log)],
    }
    pool = _worker_pool(tmp_path, [worker])
    state_path = _write_broker_state(tmp_path, {
        task_id: {
            "taskId": task_id,
            "status": "DISTRIBUTED_COMPLETE",
            "contractHash": "a" * 64,
            "selectedWorker": "distributed",
            "attempts": [],
            "terminalAt": common.utc_now(),
            "updatedAt": common.utc_now(),
        }
    })
    code, payload, stderr_payload, stdout, stderr = run_broker_result(
        "provision-cleanup", task_id, "--pool", str(pool), "--state", str(state_path))
    assert code == 0, stderr
    assert payload["status"] == "PROVISION_CLEANUP_COMPLETE"
    assert payload["workers"] == ["cloud-ephemeral:stranded"]
    assert payload["provisioning"][0]["removedFromPool"] is True
    assert json.loads(pool.read_text())["workers"] == []
    assert json.loads(deprovision_log.read_text())["action"] == "deprovision"


def test_lease_broker_detects_corrupted_materialized_artifact_on_import(tmp_path):
    task_id = "DEV-900095"
    purge_task(task_id)
    try:
        broker = runpy.run_path(str(BROKER))
        import_upstream = broker["_import_upstream_artifacts"]
        worker = _isolated_worker_entry(tmp_path, "linux-container:corrupt", unavailable=["plan"])
        digest = "a" * 64
        corrupted = tmp_path / "store" / digest[:2] / digest
        corrupted.parent.mkdir(parents=True)
        corrupted.write_text("not the bytes named by the digest")
        reports = import_upstream(
            worker,
            "linux-container:corrupt",
            task_id,
            {"id": "verify", "inputs": {"artifactsFrom": ["plan"]}},
            {
                "plan": {
                    "id": "plan",
                    "artifacts": [{
                        "kind": "plan",
                        "sha256": digest,
                        "path": "/broker/plan.json",
                    }],
                }
            },
            [{
                "nodeId": "plan",
                "workerId": "macbook-local:plan",
                "kind": "plan",
                "sha256": digest,
                "contentAddressedPath": str(corrupted),
                "status": "MATERIALIZED",
            }],
        )
        assert reports[0]["status"] == "FAILED"
        assert "content hash" in reports[0]["reason"]
    finally:
        purge_task(task_id)


def test_docker_provisioner_dry_run_emits_container_worker(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(_status_for_pool("linux-container:dry-run")))
    request = {"schemaVersion": 1, "action": "provision", "taskId": "DEV-900081", "contractHash": "b" * 64}
    result = subprocess.run(
        [
            sys.executable, str(DOCKER_PROVISIONER),
            "--worker-id", "linux-container:dry-run",
            "--root", str(tmp_path / "docker-root"),
            "--command", str(BIN),
            "--status-json", str(status_path),
            "--retire-after-task",
            "--dry-run",
        ],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PROVISIONED"
    worker = payload["workers"][0]
    assert worker["id"] == "linux-container:dry-run"
    assert worker["transport"]["kind"] == "container"
    assert worker["lifecycle"]["retireAfterTask"] is True
    assert schema_check.validate({
        "schemaVersion": 1,
        "producedAt": common.utc_now(),
        "producer": "test-control-plain",
        "workers": payload["workers"],
    }, "canonical/worker-pool-registry-v1.json") == []


def test_lease_broker_recovers_from_retryable_network_drop(tmp_path):
    task_id = "DEV-900077"
    purge_task(task_id)
    marker = tmp_path / "drop-once"
    chaos_worker = tmp_path / "chaos_worker.py"
    chaos_worker.write_text(
        "import json, os, pathlib, sys\n"
        "marker = pathlib.Path(sys.argv[2])\n"
        "args = sys.argv[3:]\n"
        "if args and args[0] == 'workflow-node' and not marker.exists():\n"
        "    marker.write_text('dropped')\n"
        "    print(json.dumps({'status': 'PROTOCOL_ERROR', 'reason': 'simulated network drop', 'retryable': True}), file=sys.stderr)\n"
        "    raise SystemExit(30)\n"
        "os.execv(sys.argv[1], [sys.argv[1], *args])\n"
    )
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        command = [sys.executable, str(chaos_worker), str(BIN), str(marker)]
        worker = _isolated_worker_entry(tmp_path, "ssh:chaos", command=command, transport_kind="ssh")
        pool = _worker_pool(tmp_path, [worker])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "run-distributed", task_id, "--pool", str(pool), "--state", str(tmp_path / "state.json"),
            "--artifact-store", str(tmp_path / "store"), "--node-retries", "2", "--mock",
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert stderr_payload == {}
        assert marker.exists()
        assert payload["workflow"]["status"] == "SUCCEEDED"
        assert payload["nodeDispatches"][0]["attempts"] == 2
    finally:
        purge_task(task_id)


def test_lease_broker_health_capacity_and_policy_placement_rules(tmp_path):
    task_id = "DEV-900069"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        state_path = tmp_path / "broker-state.json"
        state = {
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "present-lease-broker",
            "updatedAt": common.utc_now(),
            "tasks": {
                "DEV-900001": {
                    "taskId": "DEV-900001",
                    "status": "RUNNING",
                    "contractHash": "1" * 64,
                    "selectedWorker": "macbook-local:busy",
                    "leaseId": "wl-" + "4" * 24,
                    "attempts": [],
                    "updatedAt": common.utc_now(),
                }
            },
            "events": [],
        }
        state_path.write_text(json.dumps(state))
        pool = _worker_pool(tmp_path, [
            {"id": "macbook-local:busy", "command": [str(BIN)],
             "status": _status_for_pool("macbook-local:busy"),
             "placement": {"maxActiveLeases": 1}, "health": {"score": 100}},
            {"id": "cloud-ephemeral:blocked", "command": [str(BIN)],
             "status": _status_for_pool("cloud-ephemeral:blocked"),
             "placement": {"allowedRiskClasses": ["high"]}, "health": {"score": 100}},
            {"id": "linux-container:healthy", "command": [str(BIN)],
             "status": _status_for_pool("linux-container:healthy"),
             "health": {"score": 90, "latencyMs": 20}},
            {"id": "macbook-local:slow", "command": [str(BIN)],
             "status": _status_for_pool("macbook-local:slow"),
             "health": {"score": 70, "latencyMs": 1000}},
        ])
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), "--state", str(state_path),
            input_text=_workflow_bundle(task_id, nodes))
        assert code == 0, stderr
        assert payload["selectedWorker"] == "linux-container:healthy"
        reasons = {candidate["workerId"]: candidate["reasons"] for candidate in payload["candidates"]}
        assert any("no free lease capacity" in reason for reason in reasons["macbook-local:busy"])
        assert any("risk class normal is not allowed" in reason for reason in reasons["cloud-ephemeral:blocked"])
    finally:
        purge_task(task_id)


def test_lease_broker_blocks_active_foreign_lease_but_allows_expired_failover(tmp_path):
    task_id = "DEV-900066"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        contract_hash = common.canonical_hash(json.loads(bundle_text)["contract"])
        code, status = run_worker("status")
        assert code == 0
        worker_id = status["detail"]["workerIdentity"]["id"]
        pool = _worker_pool(tmp_path, [{"id": worker_id, "command": [str(BIN)], "status": status}])
        foreign_lease = {
            **common.canonical_envelope(task_id, "present-worker worker lease", mocked=False),
            "status": "ACTIVE",
            "leaseId": "wl-" + "3" * 24,
            "contractHash": contract_hash,
            "worker": {
                "id": "cloud-ephemeral:runner-7",
                "kind": "cloud-ephemeral",
                "executionSurface": "cloud",
                "host": "runner-7",
                "trustLevel": "sandboxed",
                "protocolVersion": "2.10",
                "workerVersion": "1.1.0",
                "resourceLimits": {"maxWorkflowParallel": 8},
                "labels": ["cloud"],
            },
            "acquiredAt": common.utc_now(),
            "expiresAt": "2999-01-01T00:00:00Z",
            "ttlSeconds": 3600,
            "reason": "test active foreign lease",
            "renewalCount": 0,
        }
        common.write_task_artifact(task_id, "worker-lease", foreign_lease)
        code, stdout_payload, payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), input_text=bundle_text)
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "NO_ELIGIBLE_WORKER"
        assert "cloud-ephemeral:runner-7" in payload["candidates"][0]["reasons"][0]

        foreign_lease["expiresAt"] = "2000-01-01T00:00:00Z"
        common.write_task_artifact(task_id, "worker-lease", foreign_lease)
        code, selected, stderr_payload, stdout, stderr = run_broker_result(
            "select", task_id, "--pool", str(pool), input_text=bundle_text)
        assert code == 0, stderr
        assert selected["status"] == "SELECTED"
        assert selected["selectedWorker"] == worker_id
    finally:
        purge_task(task_id)


def test_lease_broker_reconcile_failover_acquires_new_lease(tmp_path):
    task_id = "DEV-900070"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        contract_hash = common.canonical_hash(json.loads(bundle_text)["contract"])
        code, status = run_worker("status")
        assert code == 0
        worker_id = status["detail"]["workerIdentity"]["id"]
        pool = _worker_pool(tmp_path, [{"id": worker_id, "command": [str(BIN)], "status": status}])
        state_path = tmp_path / "broker-state.json"
        state_path.write_text(json.dumps({
            "schemaVersion": 1,
            "producedAt": common.utc_now(),
            "producer": "present-lease-broker",
            "updatedAt": common.utc_now(),
            "tasks": {
                task_id: {
                    "taskId": task_id,
                    "status": "WAITING",
                    "contractHash": contract_hash,
                    "selectedWorker": worker_id,
                    "leaseId": "wl-" + "5" * 24,
                    "attempts": [],
                    "updatedAt": common.utc_now(),
                }
            },
            "events": [],
        }))
        foreign_lease = {
            **common.canonical_envelope(task_id, "present-worker worker lease", mocked=False),
            "status": "ACTIVE",
            "leaseId": "wl-" + "5" * 24,
            "contractHash": contract_hash,
            "worker": {
                "id": "cloud-ephemeral:expired",
                "kind": "cloud-ephemeral",
                "executionSurface": "cloud",
                "host": "expired",
                "trustLevel": "sandboxed",
                "protocolVersion": "2.10",
                "workerVersion": "1.1.0",
                "resourceLimits": {"maxWorkflowParallel": 8},
                "labels": ["cloud"],
            },
            "acquiredAt": "2000-01-01T00:00:00Z",
            "expiresAt": "2000-01-01T00:00:00Z",
            "ttlSeconds": 60,
            "reason": "expired test lease",
            "renewalCount": 0,
        }
        common.write_task_artifact(task_id, "worker-lease", foreign_lease)
        code, payload, stderr_payload, stdout, stderr = run_broker_result(
            "reconcile", task_id, "--pool", str(pool), "--state", str(state_path),
            "--failover", "--ttl-seconds", "120", input_text=bundle_text)
        assert code == 0, stderr
        assert payload["status"] == "FAILOVER_LEASED"
        assert payload["task"]["selectedWorker"] == worker_id
        assert payload["task"]["leaseId"].startswith("wl-")
        state = _broker_state_payload(state_path)
        assert any(event["type"] == "failover-leased" for event in state["events"])
    finally:
        purge_task(task_id)


def test_ssh_guard_allows_workflow_admit_only_flag():
    task_id = "DEV-900047"
    purge_task(task_id)
    try:
        code, stdout_payload, payload, stdout, stderr = run_ssh_guard_result(
            f"present-worker workflow {task_id} --admit-only")
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert payload["status"] == "BLOCKED"
        assert "workflow contract" in payload["reason"]
    finally:
        purge_task(task_id)


def test_ssh_guard_denies_doctor_and_bad_task_id():
    code, stdout_payload, payload, stdout, stderr = run_ssh_guard_result("present-worker doctor")
    assert code == 30
    assert stdout == ""
    assert stderr
    assert payload["status"] == "DENIED"

    code, stdout_payload, payload, stdout, stderr = run_ssh_guard_result("present-worker build DEV-ABCDEF")
    assert code == 30
    assert stdout == ""
    assert stderr
    assert payload["status"] == "DENIED"


def test_doctor_exits_zero():
    code, payload = run_worker("doctor")
    assert code == 0
    assert payload["ok"] is True
    assert payload["schemaRegistryHash"] == common.schema_registry_hash()
    assert schema_check.validate(payload, "canonical/worker-doctor-v1.json") == []


def test_mock_pipeline_never_claims_production_pass():
    task_id = "DEV-900001"
    purge_task(task_id)  # clean slate regardless of how a previous run ended
    try:
        for stage in ("plan", "build", "review", "fix", "adversarial"):
            code, payload = run_worker(stage, task_id, "--mock")
            assert code == 0, (stage, payload)
            assert payload["mock"] is True, (stage, payload)

        assert schema_check.validate(json.loads((common.TASKS_DIR / task_id / "plan" / "plan.json").read_text()), "canonical/plan-v1.json") == []
        assert schema_check.validate(json.loads((common.TASKS_DIR / task_id / "build" / "build.json").read_text()), "canonical/build-result-v1.json") == []
        assert schema_check.validate(json.loads((common.TASKS_DIR / task_id / "review" / "review.json").read_text()), "canonical/review-v1.json") == []
        assert schema_check.validate(json.loads((common.TASKS_DIR / task_id / "fix" / "fix.json").read_text()), "canonical/fix-result-v1.json") == []
        assert schema_check.validate(json.loads((common.TASKS_DIR / task_id / "adversarial" / "adversarial.json").read_text()), "canonical/adversarial-result-v1.json") == []

        code, payload = run_worker("verify", task_id, "--mock")
        assert code == 0
        assert payload["mock"] is True
        assert payload["result"] == "INCONCLUSIVE"
        errors = schema_check.validate(payload, "verification-v1.schema.json")
        assert errors == [], errors
    finally:
        purge_task(task_id)


def test_duplicate_fix_worktree_is_blocked_not_shared():
    task_id = "DEV-900002"
    purge_task(task_id)
    try:
        code1, _ = run_worker("fix", task_id, "--mock")
        assert code1 == 0
        code2, stdout_payload, payload2, stdout, stderr = run_worker_result("fix", task_id, "--mock")
        assert code2 == 30
        assert stdout == ""
        assert stderr
        assert payload2["status"] == "BLOCKED"
    finally:
        purge_task(task_id)


def test_verify_without_contract_is_inconclusive_stderr_only():
    task_id = "DEV-000013"
    purge_task(task_id)
    code, stdout_payload, payload, stdout, stderr = run_worker_result("verify", task_id)
    assert code == 20
    assert stdout == ""
    assert stderr
    assert payload["schemaVersion"] == 1
    assert payload["result"] == "INCONCLUSIVE"
    assert payload["mock"] is False
    assert "missing task contract" in payload["checks"][0]["detail"]
    purge_task(task_id)


def _bundle(task_id: str, command: list[str], expected: int = 0) -> str:
    now = "2026-08-16T20:00:00Z"
    return json.dumps({
        "schemaVersion": 1,
        "taskId": task_id,
        "task": {
            "schemaVersion": 1,
            "taskId": task_id,
            "title": "worker contract test",
            "state": "VERIFYING",
            "createdAt": now,
        },
        "requirement": "Run the declared deterministic command.\n",
        "contract": {
            "schemaVersion": 1,
            "taskId": task_id,
            "producedAt": now,
            "producer": "pytest",
            "provenance": {"source": "control-plain"},
            "verification": {
                "target": "worker",
                "commands": [{
                    "name": "declared-command",
                    "argv": command,
                    "expectedExitCode": expected,
                    "timeoutSeconds": 10,
                }],
            },
        },
    })


def _build_bundle(task_id: str, build_command: list[str]) -> str:
    bundle = json.loads(_bundle(task_id, ["/usr/bin/true"]))
    bundle["contract"] = {
        **bundle["contract"],
        "schemaVersion": 2,
        "build": {
            "target": "worker",
            "commands": [{
                "name": "detached-build",
                "argv": build_command,
                "expectedExitCode": 0,
                "timeoutSeconds": 30,
            }],
        },
    }
    return json.dumps(bundle)


def _registered_policy_pack(pack_id: str, version: int = 1) -> dict:
    entry = common.policy_pack_registry_entry(pack_id, version)
    assert entry is not None
    return {**entry, "registryHash": common.policy_pack_registry_hash(pack_id, version)}


DEFAULT_WORKFLOW_POLICY_PACK = _registered_policy_pack("present-default-change")


def _workflow_bundle(task_id: str, nodes: list[dict], *, max_parallel: int = 2,
                     policy_pack: dict | None = None,
                     verification_command: list[str] | None = None) -> str:
    bundle = json.loads(_bundle(task_id, verification_command or ["/usr/bin/true"]))
    bundle["contract"] = {
        **bundle["contract"],
        "schemaVersion": 3,
        "workflow": {
            "workflowId": f"{task_id}-workflow",
            "maxParallel": max_parallel,
            "nodes": nodes,
        },
        "policyPack": policy_pack or DEFAULT_WORKFLOW_POLICY_PACK,
    }
    return json.dumps(bundle)


def test_workflow_dag_runs_capability_constrained_mock_nodes():
    task_id = "DEV-900040"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "code-review", "capability": "code-review", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "policy": {"required": True, "blocksOn": ["FAILED", "BLOCKED"], "timeoutSeconds": 60},
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
            {"id": "adversarial", "capability": "adversarial", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "adversarial-result-v1", "evidenceRole": "advisory-security"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["code-review", "adversarial"],
             "inputs": {"artifactsFrom": ["code-review", "adversarial"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        contract = json.loads(_workflow_bundle(task_id, nodes))["contract"]
        assert schema_check.validate(contract, "canonical/task-contract-v3.json") == []
        code, payload, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "SUCCEEDED"
        assert schema_check.validate(payload, "canonical/workflow-result-v1.json") == []
        assert payload["workflowId"] == f"{task_id}-workflow"
        assert payload["workflowRunId"].startswith("wr-")
        assert len(payload["contractHash"]) == 64
        assert payload["executionLease"]["leaseId"].startswith("wl-")
        assert payload["executionLease"]["status"] == "ACTIVE"
        assert payload["executionLease"]["contractHash"] == payload["contractHash"]
        assert set(payload["freshNodes"]) == {"plan", "code-review", "adversarial", "verify"}
        assert payload["reusedNodes"] == []
        assert [node["id"] for node in payload["nodes"]] == ["plan", "code-review", "adversarial", "verify"]
        assert {node["status"] for node in payload["nodes"]} == {"SUCCEEDED"}
        assert all(len(node["nodeContractHash"]) == 64 for node in payload["nodes"])
        assert payload["nodes"][1]["verb"] == "review"
        assert payload["nodes"][1]["resolvedInputs"]["artifacts"][0]["nodeId"] == "plan"
        assert payload["nodes"][1]["policy"]["timeoutSeconds"] == 60
        assert payload["nodes"][2]["verb"] == "adversarial"
        assert payload["nodes"][3]["payload"]["result"] == "INCONCLUSIVE"
        assert payload["nodes"][3]["resolvedInputs"]["artifacts"]
        assert payload["nodes"][3]["payload"]["acceptedEvidence"]
        assert schema_check.validate(payload["nodes"][3]["payload"], "canonical/verification-v1.json") == []
        assert "deterministic verify" in payload["advisoryNote"]
        evidence_by_node = {item["nodeId"]: item for item in payload["evidence"]}
        assert evidence_by_node["verify"]["id"].startswith("ev-")
        assert evidence_by_node["verify"]["artifactKind"] == "verification"
        assert evidence_by_node["verify"]["evidenceRole"] == "deterministic-acceptance"
        assert set(evidence_by_node["verify"]["upstreamNodeIds"]) == {"code-review", "adversarial"}
        assert evidence_by_node["verify"]["gateEligible"] is False
        accepted_ids = {item["id"] for item in payload["nodes"][3]["payload"]["acceptedEvidence"]}
        assert accepted_ids == {evidence_by_node["code-review"]["id"], evidence_by_node["adversarial"]["id"]}
        assert evidence_by_node["code-review"]["acceptedBy"] == ["verify"]
        assert evidence_by_node["adversarial"]["acceptedBy"] == ["verify"]
        artifact = json.loads((common.TASKS_DIR / task_id / "workflow" / "workflow.json").read_text())
        assert artifact["status"] == "SUCCEEDED"
        manifest = json.loads(
            (common.TASKS_DIR / task_id / "evidence-manifest" / "evidence-manifest.json").read_text())
        assert schema_check.validate(manifest, "canonical/evidence-manifest-v1.json") == []
        assert manifest["workflowId"] == f"{task_id}-workflow"
        assert manifest["workflowRunId"] == payload["workflowRunId"]
        assert manifest["contractHash"] == payload["contractHash"]
        assert manifest["containsMock"] is True
        assert manifest["gateEligible"] is False
        assert {item["id"] for item in manifest["artifacts"]} == {item["id"] for item in payload["evidence"]}
        handoff = json.loads(
            (common.TASKS_DIR / task_id / "handoff-package" / "handoff-package.json").read_text())
        redaction = json.loads(
            (common.TASKS_DIR / task_id / "redaction-report" / "redaction-report.json").read_text())
        assert schema_check.validate(redaction, "canonical/redaction-report-v1.json") == []
        assert redaction["status"] == "CLEAN"
        assert redaction["exportable"] is True
        assert schema_check.validate(handoff, "canonical/handoff-package-v1.json") == []
        assert handoff["workflowId"] == f"{task_id}-workflow"
        assert handoff["workflowRunId"] == payload["workflowRunId"]
        assert handoff["contractHash"] == payload["contractHash"]
        assert handoff["policyPack"]["id"] == "present-default-change"
        assert handoff["policyPack"]["version"] == 1
        assert handoff["policyPack"]["registryHash"] == DEFAULT_WORKFLOW_POLICY_PACK["registryHash"]
        assert handoff["admissionArtifact"]["status"] == "ADMITTED"
        assert handoff["workflowArtifact"]["status"] == "SUCCEEDED"
        assert handoff["evidenceManifest"]["gateEligible"] is False
        assert handoff["redactionReport"]["status"] == "CLEAN"
        assert handoff["exportable"] is True
        assert handoff["executionLease"]["leaseId"] == payload["executionLease"]["leaseId"]
        assert handoff["gateDecision"] == "INCONCLUSIVE"
        assert "inconclusive" in handoff["gateReason"].lower()
        assert {item["id"] for item in handoff["verificationEvidence"]} == {evidence_by_node["verify"]["id"]}
        assert handoff["workflowArtifact"]["sha256"] == common.sha256_file(
            common.TASKS_DIR / task_id / "workflow" / "workflow.json")
        assert handoff["evidenceManifest"]["sha256"] == common.sha256_file(
            common.TASKS_DIR / task_id / "evidence-manifest" / "evidence-manifest.json")
        log_lines = (WORKER_DIR / "logs" / f"{task_id}.jsonl").read_text().splitlines()
        assert any(json.loads(line)["stage"] == "workflow" for line in log_lines)
        assert any(json.loads(line)["stage"] == "evidence-manifest" for line in log_lines)
        assert any(json.loads(line)["stage"] == "redaction-report" for line in log_lines)
        assert any(json.loads(line)["stage"] == "handoff-package" for line in log_lines)
        lease = json.loads(
            (common.TASKS_DIR / task_id / "worker-lease" / "worker-lease.json").read_text())
        assert lease["leaseId"] == payload["executionLease"]["leaseId"]
        assert lease["status"] == "RELEASED"
        assert schema_check.validate(lease, "canonical/worker-lease-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_resume_reuses_nodes_and_rebuilds_handoff_package():
    task_id = "DEV-900055"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        code, first, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=bundle_text
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert first["freshNodes"] == ["plan"]
        assert first["reusedNodes"] == []
        (common.TASKS_DIR / task_id / "evidence-manifest" / "evidence-manifest.json").unlink()
        (common.TASKS_DIR / task_id / "handoff-package" / "handoff-package.json").unlink()

        code, second, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=bundle_text
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert second["workflowRunId"] == first["workflowRunId"]
        assert second["contractHash"] == first["contractHash"]
        assert second["freshNodes"] == []
        assert second["reusedNodes"] == ["plan"]
        assert second["resumedFromRunId"] == first["workflowRunId"]
        handoff = json.loads(
            (common.TASKS_DIR / task_id / "handoff-package" / "handoff-package.json").read_text())
        assert handoff["workflowRunId"] == first["workflowRunId"]
        assert schema_check.validate(handoff, "canonical/handoff-package-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_resume_refuses_changed_contract_hash_for_same_task():
    task_id = "DEV-900056"
    purge_task(task_id)
    try:
        first_nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        second_nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "review", "capability": "code-review", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=_workflow_bundle(task_id, first_nodes)
        )
        assert code == 0, stderr
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=_workflow_bundle(task_id, second_nodes)
        )
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "PROTOCOL_ERROR"
        assert "contract hash differs" in payload["reason"]
    finally:
        purge_task(task_id)


def test_workflow_uses_explicit_worker_lease_and_blocks_foreign_active_lease():
    task_id = "DEV-900062"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(task_id, nodes)
        contract_hash = common.canonical_hash(json.loads(bundle_text)["contract"])
        code, lease, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--acquire",
            "--contract-hash", contract_hash, "--ttl-seconds", "300")
        assert code == 0, stderr
        assert stderr_payload == {}

        code, workflow, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--lease-id", lease["leaseId"], input_text=bundle_text)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert workflow["executionLease"]["leaseId"] == lease["leaseId"]
        current_lease = json.loads(
            (common.TASKS_DIR / task_id / "worker-lease" / "worker-lease.json").read_text())
        assert current_lease["status"] == "ACTIVE"

        code, released, stderr_payload, stdout, stderr = run_worker_result(
            "worker-lease", task_id, "--release", "--lease-id", lease["leaseId"])
        assert code == 0, stderr
        assert released["status"] == "RELEASED"
    finally:
        purge_task(task_id)

    foreign_task = "DEV-900063"
    purge_task(foreign_task)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        bundle_text = _workflow_bundle(foreign_task, nodes)
        contract_hash = common.canonical_hash(json.loads(bundle_text)["contract"])
        foreign_lease = {
            **common.canonical_envelope(foreign_task, "present-worker worker lease", mocked=False),
            "status": "ACTIVE",
            "leaseId": "wl-" + "2" * 24,
            "contractHash": contract_hash,
            "worker": {
                "id": "linux-container:runner-1",
                "kind": "linux-container",
                "executionSurface": "linux-container",
                "host": "runner-1",
                "trustLevel": "sandboxed",
                "protocolVersion": "2.10",
                "workerVersion": "1.1.0",
                "resourceLimits": {"maxWorkflowParallel": 8},
                "labels": ["container"],
            },
            "acquiredAt": common.utc_now(),
            "expiresAt": "2999-01-01T00:00:00Z",
            "ttlSeconds": 3600,
            "reason": "test foreign lease",
            "renewalCount": 0,
        }
        common.write_task_artifact(foreign_task, "worker-lease", foreign_lease)
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", foreign_task, "--mock", input_text=bundle_text)
        assert code == 30
        assert stdout_payload == {}
        assert payload["status"] == "PROTOCOL_ERROR"
        assert "active lease" in payload["reason"]
        assert "linux-container:runner-1" in payload["reason"]
    finally:
        purge_task(foreign_task)


def test_task_state_reports_reconciled_handoff_and_reusable_nodes():
    task_id = "DEV-900057"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, workflow, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 0, stderr
        assert stderr_payload == {}

        code, state, stderr_payload, stdout, stderr = run_worker_result("task-state", task_id)
        assert code == 0, stderr
        assert stderr_payload == {}
        assert state["status"] == "COMPLETE"
        assert state["workflowRunId"] == workflow["workflowRunId"]
        assert state["contractHash"] == workflow["contractHash"]
        assert state["gateDecision"] == "INCONCLUSIVE"
        assert state["warnings"] == []
        assert {artifact["name"] for artifact in state["artifacts"]} == {
            "workflow-admission", "workflow", "evidence-manifest", "redaction-report", "handoff-package",
            "worker-lease", "context-checkpoint"}
        assert state["workerLease"]["status"] == "RELEASED"
        assert state["nodes"] == [{
            "id": "plan",
            "capability": "plan",
            "status": "SUCCEEDED",
            "nodeContractHash": workflow["nodes"][0]["nodeContractHash"],
            "reusable": True,
        }]
        assert schema_check.validate(state, "canonical/task-state-v1.json") == []
    finally:
        purge_task(task_id)


def test_task_cleanup_requires_handoff_proof_and_applies_retention_policy():
    task_id = "DEV-900060"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
        ]
        code, workflow, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 0, stderr
        assert stderr_payload == {}

        code, preview, stderr_payload, stdout, stderr = run_worker_result("task-cleanup", task_id, "--dry-run")
        assert code == 0, stderr
        assert preview["status"] == "DRY_RUN"
        assert schema_check.validate(preview, "canonical/task-cleanup-result-v1.json") == []
        assert any(candidate["kind"] == "stage-log" for candidate in preview["candidates"])

        code, stdout_payload, blocked, stdout, stderr = run_worker_result(
            "task-cleanup", task_id, "--force",
            "--contract-hash", "0" * 64,
            "--handoff-sha256", preview["handoffSha256"],
        )
        assert code == 30
        assert stdout_payload == {}
        assert blocked["status"] == "BLOCKED"
        assert "contract hash" in blocked["reason"]

        code, cleaned, stderr_payload, stdout, stderr = run_worker_result(
            "task-cleanup", task_id, "--force",
            "--contract-hash", workflow["contractHash"],
            "--handoff-sha256", preview["handoffSha256"],
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert cleaned["status"] == "CLEANED"
        assert cleaned["retentionPolicy"]["retain"] == [
            "canonical-task-artifacts",
            "handoff-package",
            "evidence-manifest",
            "redaction-report",
            "context-checkpoint",
            "imported-artifacts-index",
            "workflow-inputs",
            "workflow-input-context",
        ]
        assert schema_check.validate(cleaned, "canonical/task-cleanup-result-v1.json") == []
        assert (common.TASKS_DIR / task_id / "handoff-package" / "handoff-package.json").exists()
        assert (common.TASKS_DIR / task_id / "redaction-report" / "redaction-report.json").exists()
    finally:
        purge_task(task_id)


def test_workflow_admit_only_schedules_without_executing_nodes():
    task_id = "DEV-900044"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "code-review", "capability": "code-review", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
            {"id": "adversarial", "capability": "adversarial", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "adversarial-result-v1", "evidenceRole": "advisory-security"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["code-review", "adversarial"],
             "inputs": {"artifactsFrom": ["code-review", "adversarial"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "ADMITTED"
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
        assert [batch["nodeIds"] for batch in payload["schedule"]] == [
            ["plan"], ["code-review", "adversarial"], ["verify"]]
        assert {node["decision"] for node in payload["nodes"]} == {"ADMITTED"}
        assert payload["nodes"][0]["registry"]["authority"] == "advisory"
        assert "does not execute nodes" in payload["advisoryNote"]
        artifact = json.loads(
            (common.TASKS_DIR / task_id / "workflow-admission" / "workflow-admission.json").read_text())
        assert artifact["status"] == "ADMITTED"
        assert not (common.TASKS_DIR / task_id / "plan" / "plan.json").exists()
        log_lines = (WORKER_DIR / "logs" / f"{task_id}.jsonl").read_text().splitlines()
        assert any(json.loads(line)["stage"] == "workflow-admission" for line in log_lines)
    finally:
        purge_task(task_id)


def test_workflow_admission_rejects_required_output_schema_mismatch():
    task_id = "DEV-900045"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "verification-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert payload["status"] == "REJECTED"
        assert payload["nodes"][0]["decision"] == "REJECTED"
        assert "output schema" in payload["nodes"][0]["reasons"][0]
        assert payload["nodes"][1]["decision"] == "REJECTED"
        assert "depends on non-admitted nodes" in payload["nodes"][1]["reasons"][0]
        assert payload["schedule"] == []
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_admission_partial_for_optional_invalid_lane():
    task_id = "DEV-900046"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "optional-check", "capability": "deterministic-verify", "dependsOn": [],
             "policy": {"required": False, "blocksOn": [], "timeoutSeconds": 30},
             "outputs": {"schema": "verification-v1", "evidenceRole": "optional-diagnostic"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "PARTIAL"
        decisions = {node["id"]: node for node in payload["nodes"]}
        assert decisions["plan"]["decision"] == "ADMITTED"
        assert decisions["optional-check"]["decision"] == "REJECTED"
        assert "evidence role" in decisions["optional-check"]["reasons"][0]
        assert payload["schedule"] == [{"batch": 1, "nodeIds": ["plan"]}]
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_admission_rejects_required_verifier_without_upstream_evidence_inputs():
    task_id = "DEV-900048"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "code-review", "capability": "code-review", "dependsOn": [],
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["code-review"],
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=_workflow_bundle(task_id, nodes)
        )
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert payload["status"] == "REJECTED"
        verify = {node["id"]: node for node in payload["nodes"]}["verify"]
        assert verify["decision"] == "REJECTED"
        assert "does not consume artifacts" in verify["reasons"][0]
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_admission_satisfies_policy_pack():
    task_id = "DEV-900049"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "code-review", "capability": "code-review", "dependsOn": ["plan"],
             "inputs": {"artifactsFrom": ["plan"]},
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": ["code-review"],
             "inputs": {"artifactsFrom": ["code-review"]},
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        policy_pack = _registered_policy_pack("present-standard-change")
        bundle_text = _workflow_bundle(task_id, nodes, policy_pack=policy_pack)
        contract = json.loads(bundle_text)["contract"]
        assert schema_check.validate(contract, "canonical/task-contract-v3.json") == []
        code, payload, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only", input_text=bundle_text
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "ADMITTED"
        assert payload["policyPack"]["id"] == "present-standard-change"
        assert payload["policyPack"]["status"] == "SATISFIED"
        assert payload["policyPack"]["violations"] == []
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_admission_rejects_high_risk_policy_without_security_review():
    task_id = "DEV-900050"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": [],
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        policy_pack = _registered_policy_pack("present-high-risk-change")
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only",
            input_text=_workflow_bundle(task_id, nodes, policy_pack=policy_pack)
        )
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert payload["status"] == "REJECTED"
        assert payload["policyPack"]["status"] == "REJECTED"
        assert any("high risk requires" in violation for violation in payload["policyPack"]["violations"])
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_admission_rejects_forbidden_policy_capability():
    task_id = "DEV-900051"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "plan", "capability": "plan", "dependsOn": [],
             "outputs": {"schema": "plan-v1", "evidenceRole": "advisory-plan"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": [],
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        policy_pack = _registered_policy_pack("present-no-planning")
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only",
            input_text=_workflow_bundle(task_id, nodes, policy_pack=policy_pack)
        )
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert payload["policyPack"]["status"] == "REJECTED"
        assert "forbidden capability 'plan' is present" in payload["policyPack"]["violations"]
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_admission_rejects_policy_required_evidence_not_consumed_by_verifier():
    task_id = "DEV-900052"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "code-review", "capability": "code-review", "dependsOn": [],
             "outputs": {"schema": "review-v1", "evidenceRole": "advisory-review"}},
            {"id": "verify", "capability": "deterministic-verify", "dependsOn": [],
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
        ]
        policy_pack = _registered_policy_pack("present-evidence-gated")
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id, "--mock", "--admit-only",
            input_text=_workflow_bundle(task_id, nodes, policy_pack=policy_pack)
        )
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert payload["status"] == "REJECTED"
        assert any("required evidence is not consumed" in violation for violation in payload["policyPack"]["violations"])
        assert schema_check.validate(payload, "canonical/workflow-admission-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_policy_pack_rejects_registry_tampering():
    task_id = "DEV-900053"
    tampered = _registered_policy_pack("present-standard-change")
    tampered["minVerifierCount"] = 0
    conflict = json.loads(_workflow_bundle(task_id, [
        {"id": "plan", "capability": "plan", "dependsOn": []},
    ], policy_pack=tampered))["contract"]
    try:
        common.validate_workflow_contract(conflict, task_id)
        raise AssertionError("policy pack payload must match the registry")
    except common.TaskBundleError as exc:
        assert "differs from the registered policy" in str(exc)


def test_workflow_contract_requires_policy_pack():
    task_id = "DEV-900054"
    contract = json.loads(_workflow_bundle(task_id, [
        {"id": "plan", "capability": "plan", "dependsOn": []},
    ]))["contract"]
    del contract["policyPack"]
    try:
        common.validate_workflow_contract(contract, task_id)
        raise AssertionError("task-contract-v3 must require a policy pack before launch")
    except common.TaskBundleError as exc:
        assert "policyPack" in str(exc)


def test_workflow_contract_rejects_self_orchestration_and_cycles():
    task_id = "DEV-900041"
    unsupported = json.loads(_workflow_bundle(task_id, [
        {"id": "agent", "capability": "self-orchestrate", "dependsOn": []},
    ]))["contract"]
    try:
        common.validate_workflow_contract(unsupported, task_id)
        raise AssertionError("unsupported workflow capabilities must be rejected")
    except common.TaskBundleError as exc:
        assert "unsupported capability" in str(exc)

    cyclic = json.loads(_workflow_bundle(task_id, [
        {"id": "a", "capability": "plan", "dependsOn": ["b"]},
        {"id": "b", "capability": "review", "dependsOn": ["a"]},
    ]))["contract"]
    try:
        common.validate_workflow_contract(cyclic, task_id)
        raise AssertionError("workflow cycles must be rejected")
    except common.TaskBundleError as exc:
        assert "acyclic" in str(exc)

    bad_input = json.loads(_workflow_bundle(task_id, [
        {"id": "plan", "capability": "plan", "dependsOn": []},
        {"id": "review", "capability": "review", "dependsOn": [],
         "inputs": {"artifactsFrom": ["plan"]}},
    ]))["contract"]
    try:
        common.validate_workflow_contract(bad_input, task_id)
        raise AssertionError("workflow inputs must be direct dependency references")
    except common.TaskBundleError as exc:
        assert "direct dependencies" in str(exc)


def test_workflow_optional_node_failure_is_evidence_not_a_gate():
    task_id = "DEV-900042"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "optional-verify", "capability": "deterministic-verify", "dependsOn": [],
             "policy": {"required": False, "blocksOn": [], "timeoutSeconds": 30},
             "outputs": {"schema": "verification-v1", "evidenceRole": "optional-diagnostic"}},
        ]
        code, payload, stderr_payload, stdout, stderr = run_worker_result(
            "workflow", task_id,
            input_text=_workflow_bundle(task_id, nodes, verification_command=["/usr/bin/false"])
        )
        assert code == 0, stderr
        assert stderr_payload == {}
        assert payload["status"] == "SUCCEEDED"
        assert payload["nodes"][0]["status"] == "FAILED"
        assert payload["nodes"][0]["policy"]["required"] is False
        assert payload["evidence"][0]["evidenceRole"] == "optional-diagnostic"
        assert schema_check.validate(payload, "canonical/workflow-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_workflow_required_node_failure_blocks_downstream_nodes():
    task_id = "DEV-900043"
    purge_task(task_id)
    try:
        nodes = [
            {"id": "required-verify", "capability": "deterministic-verify", "dependsOn": [],
             "outputs": {"schema": "verification-v1", "evidenceRole": "deterministic-acceptance"}},
            {"id": "after", "capability": "plan", "dependsOn": ["required-verify"]},
        ]
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "workflow", task_id,
            input_text=_workflow_bundle(task_id, nodes, verification_command=["/usr/bin/false"])
        )
        assert code == 20
        assert stdout == ""
        assert stderr and stdout_payload == {}
        assert payload["status"] == "FAILED"
        assert [node["id"] for node in payload["nodes"]] == ["required-verify"]
        assert payload["nodes"][0]["status"] == "FAILED"
        assert payload["nodes"][0]["policy"]["blocksOn"] == ["FAILED", "BLOCKED"]
        assert schema_check.validate(payload, "canonical/workflow-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_detached_build_returns_receipt_then_canonical_result():
    task_id = "DEV-900005"
    purge_task(task_id)
    try:
        bundle_text = _build_bundle(task_id, [sys.executable, "-c", "import time; time.sleep(0.25)"])
        contract = json.loads(bundle_text)["contract"]
        assert schema_check.validate(contract, "task-contract-v2.schema.json") == []
        code, receipt, _, _, _ = run_worker_result("build", task_id, input_text=bundle_text)
        assert code == 0
        assert receipt["state"] in {"QUEUED", "RUNNING"}
        assert schema_check.validate(receipt, "build-job-v1.schema.json") == []

        result = None
        for _ in range(50):
            code, stdout_payload, stderr_payload, _, _ = run_worker_result("build", task_id)
            if stdout_payload.get("outcome"):
                result = stdout_payload
                break
            assert code == 0
            assert stdout_payload.get("state") in {"QUEUED", "RUNNING"}
            time.sleep(0.1)
        assert result is not None
        assert result["outcome"] == "SUCCEEDED"
        assert result["mock"] is False
        assert schema_check.validate(result, "canonical/build-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_detached_build_failure_is_canonical_stderr():
    task_id = "DEV-900007"
    purge_task(task_id)
    try:
        code, receipt, _, _, _ = run_worker_result(
            "build", task_id, input_text=_build_bundle(task_id, ["/usr/bin/false"])
        )
        assert code == 0 and receipt["state"] in {"QUEUED", "RUNNING"}
        failure = None
        for _ in range(50):
            code, stdout_payload, stderr_payload, stdout, stderr = run_worker_result("build", task_id)
            if code == 20:
                assert stdout == "" and stderr
                failure = stderr_payload
                break
            assert code == 0
            time.sleep(0.1)
        assert failure is not None
        assert failure["outcome"] == "BUILD_FAILED"
        assert failure["mock"] is False
        assert schema_check.validate(failure, "canonical/build-result-v1.json") == []
    finally:
        purge_task(task_id)


def test_task_contract_v2_rejects_shell_string_command():
    task_id = "DEV-900008"
    purge_task(task_id)
    try:
        bundle = json.loads(_build_bundle(task_id, ["/usr/bin/true"]))
        bundle["contract"]["build"]["commands"] = ["/usr/bin/true"]
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "build", task_id, input_text=json.dumps(bundle)
        )
        assert code == 30
        assert stdout == "" and stderr and stdout_payload == {}
        assert payload["status"] == "PROTOCOL_ERROR"
    finally:
        purge_task(task_id)


def test_stdin_bundle_produces_canonical_real_pass():
    task_id = "DEV-900003"
    purge_task(task_id)
    try:
        bundle_text = _bundle(task_id, ["/usr/bin/true"])
        contract = json.loads(bundle_text)["contract"]
        assert schema_check.validate(contract, "task-contract-v1.schema.json") == []
        code, payload, _, _, _ = run_worker_result("verify", task_id, input_text=bundle_text)
        assert code == 0
        assert payload["result"] == "PASS"
        assert payload["mock"] is False
        assert schema_check.validate(payload, "verification-v1.schema.json") == []
    finally:
        purge_task(task_id)


def test_stdin_bundle_real_failure_is_stderr_only():
    task_id = "DEV-900004"
    purge_task(task_id)
    try:
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "verify", task_id, input_text=_bundle(task_id, ["/usr/bin/false"])
        )
        assert code == 20
        assert stdout == ""
        assert stderr
        assert stdout_payload == {}
        assert payload["result"] == "FAIL"
        assert payload["mock"] is False
        assert schema_check.validate(payload, "verification-v1.schema.json") == []
    finally:
        purge_task(task_id)


def test_glimmer_status_never_crashes_when_stopped():
    code, payload = run_worker("glimmer", "status")
    assert code == 0
    assert "ready" in payload
    assert schema_check.validate(payload, "canonical/worker-glimmer-response-v1.json") == []


def test_glimmer_ready_admin_status_is_success():
    cli = _load_cli_module()
    assert cli._payload_exit_code({"status": "READY", "provider": "glimmer"}) == 0
    assert cli._payload_exit_code({"status": "STOPPED", "provider": "glimmer"}) == 0


def test_glimmer_coordinator_rewarms_an_unloaded_model_before_chat(monkeypatch):
    progress: list[dict] = []
    posts: list[tuple[str, dict, float]] = []

    monkeypatch.setattr(glimmer_adapter, "status", lambda _model: {
        "provider": "glimmer",
        "installed": True,
        "model": "qwen2.5-coder:14b",
        "model_available": True,
        "service_running": True,
        "model_loaded": False,
        "ready": False,
    })
    monkeypatch.setattr(glimmer_adapter, "model_loaded", lambda _model: True)

    def fake_post(path, payload, timeout):
        posts.append((path, payload, timeout))
        return {"done": True}

    def fake_generate(_payload, timeout, progress=None):
        assert timeout <= 120
        if progress:
            progress({"type": "system", "subtype": "glimmer_request_started"})
        return ({
            "summary": "The local model re-warmed and answered.",
            "items": [],
            "notChecked": [],
            "contradictions": [],
        }, None)

    monkeypatch.setattr(glimmer_adapter, "_http_post", fake_post)
    monkeypatch.setattr(glimmer_adapter, "_generate_streaming_json", fake_generate)

    result = glimmer_adapter.coordinator_report(
        "DEV-900190", "live", "Where are we?", {}, {"probe": "ok"},
        timeout=120, progress=progress.append,
    )

    assert result["summary"] == "The local model re-warmed and answered."
    assert posts == [(
        "/api/generate",
        {"model": "qwen2.5-coder:14b", "prompt": "", "stream": False, "keep_alive": "30m"},
        90.0,
    )]
    assert [event["subtype"] for event in progress] == [
        "glimmer_rewarm_started",
        "glimmer_rewarm_completed",
        "glimmer_request_started",
    ]


def test_glimmer_coordinator_reports_a_failed_rewarm_without_starting_chat(monkeypatch):
    progress: list[dict] = []
    generation_called = False

    monkeypatch.setattr(glimmer_adapter, "status", lambda _model: {
        "provider": "glimmer",
        "installed": True,
        "model": "qwen2.5-coder:14b",
        "model_available": True,
        "service_running": True,
        "model_loaded": False,
        "ready": False,
    })
    monkeypatch.setattr(glimmer_adapter, "_http_post", lambda *_args, **_kwargs: None)

    def fake_generate(*_args, **_kwargs):
        nonlocal generation_called
        generation_called = True
        return {}, None

    monkeypatch.setattr(glimmer_adapter, "_generate_streaming_json", fake_generate)

    result = glimmer_adapter.coordinator_report(
        "DEV-900190", "live", "Where are we?", {}, {"probe": "ok"},
        timeout=120, progress=progress.append,
    )

    assert result["status"] == "GLIMMER_NOT_READY"
    assert "re-warm failed" in result["reason"]
    assert result["retryable"] is True
    assert generation_called is False
    assert [event["subtype"] for event in progress] == [
        "glimmer_rewarm_started",
        "glimmer_rewarm_failed",
    ]


def test_glimmer_coordinator_stops_when_rewarm_exhausts_the_caller_budget(monkeypatch):
    progress: list[dict] = []
    generation_called = False

    monkeypatch.setattr(glimmer_adapter, "status", lambda _model: {
        "provider": "glimmer",
        "installed": True,
        "model": "qwen2.5-coder:14b",
        "model_available": True,
        "service_running": True,
        "model_loaded": False,
        "ready": False,
    })
    monkeypatch.setattr(glimmer_adapter, "rewarm", lambda *_args, **_kwargs: {
        "status": "READY",
        "provider": "glimmer",
        "model": "qwen2.5-coder:14b",
        "rewarmed": True,
    })
    clock = iter((10.0, 12.0))
    monkeypatch.setattr(glimmer_adapter.time, "monotonic", lambda: next(clock))

    def fake_generate(*_args, **_kwargs):
        nonlocal generation_called
        generation_called = True
        return {}, None

    monkeypatch.setattr(glimmer_adapter, "_generate_streaming_json", fake_generate)

    result = glimmer_adapter.coordinator_report(
        "DEV-900190", "live", "Where are we?", {}, {"probe": "ok"},
        timeout=2, progress=progress.append,
    )

    assert result["status"] == "PROVIDER_ERROR"
    assert "re-warm exhausted the 2s caller timeout" in result["reason"]
    assert result["retryable"] is True
    assert generation_called is False
    assert progress[-1]["subtype"] == "glimmer_request_budget_exhausted"


def test_glimmer_coordinator_still_refuses_to_cold_start_the_server(monkeypatch):
    post_called = False
    monkeypatch.setattr(glimmer_adapter, "status", lambda _model: {
        "provider": "glimmer",
        "installed": True,
        "model": "qwen2.5-coder:14b",
        "model_available": True,
        "service_running": False,
        "model_loaded": False,
        "ready": False,
    })

    def fake_post(*_args, **_kwargs):
        nonlocal post_called
        post_called = True
        return {"done": True}

    monkeypatch.setattr(glimmer_adapter, "_http_post", fake_post)

    result = glimmer_adapter.coordinator_report(
        "DEV-900190", "live", "Where are we?", {}, {"probe": "ok"}, timeout=120,
    )

    assert result["status"] == "GLIMMER_NOT_READY"
    assert "server is not running" in result["reason"]
    assert "cold-start" in result["reason"]
    assert post_called is False


def test_glimmer_server_start_still_enforces_the_memory_floor(monkeypatch):
    monkeypatch.setattr(glimmer_adapter, "installed", lambda: True)
    monkeypatch.setattr(glimmer_adapter, "server_running", lambda: False)
    monkeypatch.setattr(glimmer_adapter, "memory_check", lambda: {
        "ok": True,
        "free_gb": 3.0,
        "min_required_gb": glimmer_adapter.MIN_FREE_GB_TO_START,
    })

    result = glimmer_adapter.start()

    assert result["status"] == "GLIMMER_NOT_READY"
    assert "insufficient free memory" in result["reason"]


def test_model_binding_registry_validates_and_resolves_local_dc13_instance():
    code, registry = run_worker("model-roles")
    assert code == 0
    assert schema_check.validate(registry, "canonical/model-role-registry-v1.json") == []
    assert {role["id"] for role in registry["roles"]} == {
        "dc13.coordination-report",
        "delivery.planner",
        "delivery.coder",
        "delivery.reviewer",
        "delivery.acceptance",
    }

    code, policy = run_worker("model-role-resolve", "dc13.coordination-report", "--provider", "glimmer", "--ignore-readiness")

    assert code == 0
    assert policy["role"] == "dc13.coordination-report"
    assert policy["provider"] == "glimmer"
    assert policy["selectedModel"] == "qwen2.5-coder:14b"
    assert policy["transport"] == "ollama"
    assert policy["snapshotProfile"] == "steel-mission-starter"
    assert policy["fallbackReason"] == "provider-override"


def test_model_role_auto_falls_back_to_ready_glimmer():
    cli = _load_cli_module()
    policy = cli._resolve_model_policy(
        "dc13.coordination-report",
        provider_capabilities={
            "claude": {"ready": False},
            "glimmer": {"ready": True},
        },
    )

    assert policy["provider"] == "glimmer"
    assert policy["selectedModel"] == "qwen2.5-coder:14b"
    assert policy["fallbackReason"] == "primary-unavailable"


def test_coordinator_role_prefers_codex_and_names_registry_fallback_when_unavailable():
    cli = _load_cli_module()

    preferred = cli._resolve_model_policy(
        "dc13.coordination-report",
        provider_capabilities={
            "codex": {"ready": True},
            "claude": {"ready": True},
            "glimmer": {"ready": True},
        },
    )
    fallback = cli._resolve_model_policy(
        "dc13.coordination-report",
        provider_capabilities={
            "codex": {"ready": False},
            "claude": {"ready": True},
            "glimmer": {"ready": True},
        },
    )

    assert preferred["provider"] == "codex"
    assert preferred["selectedModel"] == "gpt-5.6-sol"
    assert "reasoning-effort:xhigh" in preferred["nativeCapabilities"]
    assert "fallbackReason" not in preferred
    assert fallback["provider"] == "claude"
    assert fallback["selectedModel"] == "claude-sonnet-5"
    assert "reasoning-effort:medium" in fallback["nativeCapabilities"]
    assert fallback["fallbackReason"] == "primary-unavailable"


def test_delivery_roles_pin_opus_five_and_codex_maximum_settings():
    registry = json.loads((WORKER_DIR / "config" / "model-role-registry.json").read_text())
    models = {model["id"]: model for model in registry["models"]}
    roles = {role["id"]: role for role in registry["roles"]}

    assert roles["delivery.planner"]["primaryModel"] == "claude-opus-5"
    assert roles["delivery.acceptance"]["primaryModel"] == "claude-opus-5"
    assert "reasoning-effort:high" in models["claude-opus-5"]["nativeCapabilities"]
    assert roles["delivery.reviewer"]["primaryModel"] == "gpt-5.6-sol"
    assert "reasoning-effort:xhigh" in models["gpt-5.6-sol"]["nativeCapabilities"]


def test_model_role_resolution_refuses_a_model_unknown_to_its_provider(tmp_path, monkeypatch):
    cli = _load_cli_module()
    registry = json.loads((WORKER_DIR / "config" / "model-role-registry.json").read_text())
    codex = next(model for model in registry["models"] if model["provider"] == "codex")
    old_id = codex["id"]
    codex["id"] = "codex-invented-model"
    for role in registry["roles"]:
        if role["primaryModel"] == old_id:
            role["primaryModel"] = codex["id"]
        role["fallbackModels"] = [codex["id"] if model == old_id else model for model in role["fallbackModels"]]
    path = tmp_path / "model-role-registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv(cli.MODEL_ROLE_REGISTRY_ENV, str(path))

    with pytest.raises(
        common.TaskBundleError,
        match="provider 'codex' does not recognize model 'codex-invented-model'",
    ):
        cli._resolve_model_policy("dc13.coordination-report", require_ready=False)


def test_previous_v1_registry_migrates_the_legacy_codex_binding(tmp_path, monkeypatch):
    cli = _load_cli_module()
    registry = json.loads((WORKER_DIR / "config" / "model-role-registry.json").read_text())
    codex = next(model for model in registry["models"] if model["provider"] == "codex")
    current_id = codex["id"]
    codex["id"] = "codex-cli-default"
    codex["nativeCapabilities"] = [
        capability for capability in codex["nativeCapabilities"]
        if not capability.startswith("reasoning-effort:")
    ]
    for role in registry["roles"]:
        if role["primaryModel"] == current_id:
            role["primaryModel"] = codex["id"]
        role["fallbackModels"] = [
            codex["id"] if model == current_id else model for model in role["fallbackModels"]
        ]
    path = tmp_path / "model-role-registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv(cli.MODEL_ROLE_REGISTRY_ENV, str(path))

    policy = cli._resolve_model_policy("dc13.coordination-report", require_ready=False)

    assert policy["selectedModel"] == "gpt-5.6-sol"
    assert "reasoning-effort:xhigh" in policy["nativeCapabilities"]
    assert cli._validated_model_invocation(policy) == ("gpt-5.6-sol", "xhigh")


def test_model_role_refuses_provider_that_lacks_required_native_capability():
    cli = _load_cli_module()

    with pytest.raises(common.TaskBundleError, match="no candidate with required provider-native capabilities"):
        cli._resolve_model_policy(
            "dc13.coordination-report",
            provider_override="glimmer",
            require_ready=False,
            required_native_capabilities=["model-effort-controls"],
        )


def test_runtime_profile_registry_validates_and_resolves_local_profile():
    code, registry = run_worker("runtime-profiles")
    assert code == 0
    assert schema_check.validate(registry, "canonical/runtime-profile-registry-v1.json") == []
    assert {profile["id"] for profile in registry["profiles"]} >= {
        "dc13.auto", "dc13.codex", "dc13.local", "dc13.claude",
    }

    code, resolution = run_worker("runtime-profile-resolve", "dc13.local", "--ignore-readiness")

    assert code == 0
    assert schema_check.validate(resolution, "canonical/runtime-profile-resolution-v1.json") == []
    assert resolution["runtimeProfile"]["id"] == "dc13.local"
    assert resolution["runtimeProfile"]["modelRole"] == "dc13.coordination-report"
    assert resolution["runtimeProfile"]["modelProvider"] == "glimmer"
    assert "DC13" in resolution["runtimeProfile"]["visibilityRoleKeys"]
    assert "DC11" in resolution["runtimeProfile"]["visibilityRoleKeys"]
    assert "DC12" in resolution["runtimeProfile"]["visibilityRoleKeys"]
    assert resolution["modelPolicy"]["provider"] == "glimmer"
    assert resolution["modelPolicy"]["selectedModel"] == "qwen2.5-coder:14b"
    assert resolution["modelPolicy"]["capabilityMode"] == "provider-native-with-governance-envelope"
    assert set(resolution["modelPolicy"]["requiredProviderCapabilities"]) == {
        "local-inference",
        "persistent-model-residency",
        "structured-json-output",
    }
    assert set(resolution["modelPolicy"]["requiredProviderCapabilities"]) <= set(
        resolution["modelPolicy"]["nativeCapabilities"]
    )
    assert set(resolution["modelPolicy"]["governanceCapabilities"]) >= {
        "audit-evidence",
        "bounded-snapshot",
        "guarded-execution",
        "role-binding",
    }
    assert resolution["snapshotPolicy"]["sourceProfile"] == "worker-local-glimmer-fallback"
    assert "${" not in json.dumps(resolution["snapshotPolicy"])


def test_codex_runtime_profile_is_selectable_and_contract_valid():
    code, resolution = run_worker("runtime-profile-resolve", "dc13.codex", "--ignore-readiness")

    assert code == 0
    assert schema_check.validate(resolution, "canonical/runtime-profile-resolution-v1.json") == []
    assert resolution["runtimeProfile"]["modelProvider"] == "codex"
    assert resolution["modelPolicy"]["provider"] == "codex"
    assert resolution["modelPolicy"]["transport"] == "codex-cli"


def test_mock_coordinator_report_routes_through_codex():
    task_id = "DEV-900188"
    purge_task(task_id)
    try:
        code, payload = run_worker("coordination-report", task_id, "--mock", "--provider", "codex")

        assert code == 0
        assert payload["producer"] == "steel-mission coordination-report (codex)"
        assert payload["mock"] is True
        assert schema_check.validate(payload, "canonical/coordination-report-v1.json") == []
    finally:
        purge_task(task_id)


def test_changing_registry_model_changes_coordinator_invocation_and_record(monkeypatch, tmp_path):
    cli = _load_cli_module()
    captured: dict = {}
    registry = json.loads((WORKER_DIR / "config" / "model-role-registry.json").read_text())
    codex = next(model for model in registry["models"] if model["provider"] == "codex")
    old_id = codex["id"]
    codex["id"] = "gpt-5.6-terra"
    codex["nativeCapabilities"] = ["provider-native-cli", "reasoning-effort:high"]
    for role in registry["roles"]:
        if role["primaryModel"] == old_id:
            role["primaryModel"] = codex["id"]
        role["fallbackModels"] = [
            codex["id"] if model == old_id else model for model in role["fallbackModels"]
        ]
    path = tmp_path / "model-role-registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv(cli.MODEL_ROLE_REGISTRY_ENV, str(path))

    class Args:
        task_id = "DEV-900189"
        mock = True
        timeout_seconds = 42
        profile = None
        role = None
        provider = None

    def fake_report(*_args, model=None, effort=None, **_kwargs):
        captured["adapter"] = {"model": model, "effort": effort}
        return {"status": "OK"}

    def fake_record(_task_id, _stage, _fields=None, **extra):
        captured["record"] = extra
        return extra

    monkeypatch.setattr(cli, "_load_or_synthesize_contract", lambda *_args: {})
    monkeypatch.setattr(cli, "_effective_coordinator_runtime_profile", lambda *_args: None)
    monkeypatch.setattr(cli, "_effective_coordinator_snapshot_policy", lambda *_args: {})
    monkeypatch.setattr(cli, "_coordinator_state_snapshot", lambda *_args: {})
    monkeypatch.setattr(cli, "_coordinator_pack_identity", lambda: {"probe": "ok"})
    monkeypatch.setattr(cli, "_requirement_gate", lambda *_args: None)
    monkeypatch.setattr(cli, "_coordinator_progress_writer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.common, "load_requirement", lambda _task_id: "Where are we?")
    monkeypatch.setattr(cli.common, "record_stage", fake_record)
    monkeypatch.setattr(cli.codex_adapter, "coordinator_report", fake_report)
    monkeypatch.setattr(cli, "emit", lambda _payload, **_kwargs: 0)

    assert cli.cmd_coordinator_report(Args()) == 0
    assert captured["adapter"] == {"model": "gpt-5.6-terra", "effort": "high"}
    assert captured["record"] == {
        "role": "codex",
        "provider": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
    }


def test_plan_command_invokes_the_registry_declared_opus_model(monkeypatch):
    cli = _load_cli_module()
    captured: dict = {}
    policy = {
        "selectedModel": "claude-opus-5",
        "provider": "claude",
        "nativeCapabilities": ["reasoning-effort:high"],
    }

    class Args:
        task_id = "DEV-900189"
        mock = False

    def fake_plan(*_args, model=None, effort=None, **_kwargs):
        captured.update(model=model, effort=effort)
        return {"status": "OK"}

    monkeypatch.setattr(cli, "_load_or_synthesize_contract", lambda *_args: {})
    monkeypatch.setattr(cli, "_requirement_gate", lambda *_args: None)
    monkeypatch.setattr(cli, "_resolve_model_policy", lambda *_args, **_kwargs: policy)
    monkeypatch.setattr(cli.claude_adapter, "plan", fake_plan)
    monkeypatch.setattr(cli.common, "load_requirement", lambda _task_id: "Plan this")
    monkeypatch.setattr(cli.common, "record_stage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "emit", lambda _payload, **_kwargs: 0)

    assert cli.cmd_plan(Args()) == 0
    assert captured == {"model": "claude-opus-5", "effort": "high"}


def test_review_command_invokes_the_registry_declared_codex_model(monkeypatch, tmp_path):
    cli = _load_cli_module()
    captured: dict = {}
    policy = {
        "selectedModel": "gpt-5.6-sol",
        "provider": "codex",
        "nativeCapabilities": ["reasoning-effort:xhigh"],
    }

    class Args:
        task_id = "DEV-900189"
        mock = False

    def fake_review(*_args, model=None, effort=None, **_kwargs):
        captured.update(model=model, effort=effort)
        return {"status": "OK"}

    monkeypatch.setattr(cli, "_load_or_synthesize_contract", lambda *_args: {})
    monkeypatch.setattr(cli, "_requirement_gate", lambda *_args: None)
    monkeypatch.setattr(cli, "_resolve_model_policy", lambda *_args, **_kwargs: policy)
    monkeypatch.setattr(cli, "_latest_stage_output_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(cli, "_worktree_path", lambda *_args: tmp_path / "absent")
    monkeypatch.setattr(cli, "_artifact_plan_text", lambda _task_id: "plan")
    monkeypatch.setattr(cli, "_workflow_input_context_text", lambda: "context")
    monkeypatch.setattr(cli.codex_adapter, "review", fake_review)
    monkeypatch.setattr(cli.common, "load_requirement", lambda _task_id: "Review this")
    monkeypatch.setattr(cli.common, "record_stage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "emit", lambda _payload, **_kwargs: 0)

    assert cli.cmd_review(Args()) == 0
    assert captured == {"model": "gpt-5.6-sol", "effort": "xhigh"}


def test_acceptance_command_invokes_the_registry_declared_opus_model(monkeypatch, tmp_path):
    cli = _load_cli_module()
    captured: dict = {}
    policy = {
        "selectedModel": "claude-opus-5",
        "provider": "claude",
        "nativeCapabilities": ["reasoning-effort:high"],
    }

    class Args:
        task_id = "DEV-900189"
        mock = False

    def fake_adversarial(*_args, model=None, effort=None, **_kwargs):
        captured.update(model=model, effort=effort)
        return {"status": "OK"}

    monkeypatch.setattr(cli, "_load_or_synthesize_contract", lambda *_args: {})
    monkeypatch.setattr(cli, "_requirement_gate", lambda *_args: None)
    monkeypatch.setattr(cli, "_resolve_model_policy", lambda *_args, **_kwargs: policy)
    monkeypatch.setattr(cli, "_latest_stage_output_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(cli, "_worktree_path", lambda *_args: tmp_path / "absent")
    monkeypatch.setattr(cli, "_artifact_plan_text", lambda _task_id: "plan")
    monkeypatch.setattr(cli, "_workflow_input_context_text", lambda: "context")
    monkeypatch.setattr(cli.claude_adapter, "adversarial", fake_adversarial)
    monkeypatch.setattr(cli.common, "load_requirement", lambda _task_id: "Accept this")
    monkeypatch.setattr(cli.common, "record_stage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "emit", lambda _payload, **_kwargs: 0)

    assert cli.cmd_adversarial(Args()) == 0
    assert captured == {"model": "claude-opus-5", "effort": "high"}


def test_steel_mission_refuses_unknown_runtime_profile_before_starting_mission(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["start_orchestrated_mission"].__globals__
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["TASKS_DIR"] = tmp_path / "tasks"
    globals_["launch_mission_orchestrator"] = lambda _mission_id: None
    jobs_before = set(chat["JOBS"])

    try:
        with pytest.raises(ValueError, match="unknown runtime profile.*dc13.never-existed"):
            chat["start_orchestrated_mission"](
                "investigate",
                "Reject a profile that was never registered.",
                mock=True,
                profile="dc13.never-existed",
                operator_role="owner",
            )
        assert set(chat["JOBS"]) == jobs_before
        assert not globals_["MISSION_ROOT"].exists()
    finally:
        for job_id in set(chat["JOBS"]) - jobs_before:
            chat["JOBS"].pop(job_id, None)


def test_steel_mission_known_runtime_profile_keeps_offline_fallback(monkeypatch):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["resolve_runtime_profile"].__globals__
    globals_["RUNTIME_PROFILE_REGISTRY_PATH"] = WORKER_DIR / "config" / "runtime-profiles.json"

    def offline_worker(*_args, **_kwargs):
        raise OSError("worker is offline")

    monkeypatch.setattr(globals_["subprocess"], "run", offline_worker)
    resolution = chat["resolve_runtime_profile"]("dc13.local")

    assert resolution["runtimeProfile"]["id"] == "dc13.local"
    assert resolution["runtimeProfile"]["status"] == "active"
    assert resolution["runtimeProfile"]["modelProvider"] == "glimmer"
    assert resolution["modelPolicy"]["provider"] == "glimmer"


def test_runtime_profile_manager_publisher_can_save_and_clone(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime-profiles.json"
    model_path = tmp_path / "model-role-registry.json"
    shutil.copy(WORKER_DIR / "config" / "runtime-profiles.json", runtime_path)
    shutil.copy(WORKER_DIR / "config" / "model-role-registry.json", model_path)
    monkeypatch.setenv("PRESENT_RUNTIME_PROFILE_REGISTRY", str(runtime_path))
    monkeypatch.setenv("PRESENT_MODEL_ROLE_REGISTRY", str(model_path))

    registry = json.loads(runtime_path.read_text())
    source = next(profile for profile in registry["profiles"] if profile["id"] == "dc13.local")
    profile = json.loads(json.dumps(source))
    profile["id"] = "dc13.publisher"
    profile["label"] = "DC13 Publisher"
    profile["editableBy"] = ["owner", "admin", "publisher"]
    profile["sources"]["taskRoots"] = ["${PRESENT_TASKS_DIR}", "/tmp/project/tasks"]

    code, payload, stderr, _stdout, _stderr = run_worker_result(
        "runtime-profile-save", "--operator-role", "publisher", input_text=json.dumps(profile)
    )

    assert code == 0, stderr
    assert schema_check.validate(payload, "canonical/runtime-profile-registry-v1.json") == []
    assert any(item["id"] == "dc13.publisher" for item in payload["profiles"])

    code, cloned = run_worker("runtime-profile-clone", "dc13.publisher", "dc13.publisher.copy",
                              "--operator-role", "publisher")

    assert code == 0
    copy = next(profile for profile in cloned["profiles"] if profile["id"] == "dc13.publisher.copy")
    assert copy["label"] == "DC13 Publisher Copy"
    assert copy["defaultFor"] == []

    profile["label"] = "Blocked Local Edit"
    code, _payload, stderr, _stdout, _stderr = run_worker_result(
        "runtime-profile-save", "--operator-role", "local-user", input_text=json.dumps(profile)
    )

    assert code == 30
    assert "cannot edit runtime profile" in stderr.get("reason", "")


def test_model_binding_manager_is_admin_only_and_updates_dc13_binding(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime-profiles.json"
    model_path = tmp_path / "model-role-registry.json"
    shutil.copy(WORKER_DIR / "config" / "runtime-profiles.json", runtime_path)
    shutil.copy(WORKER_DIR / "config" / "model-role-registry.json", model_path)
    monkeypatch.setenv("PRESENT_RUNTIME_PROFILE_REGISTRY", str(runtime_path))
    monkeypatch.setenv("PRESENT_MODEL_ROLE_REGISTRY", str(model_path))
    role = {
        "id": "dc13.coordination-report",
        "title": "Delivery Coordinator Test Binding",
        "primaryModel": "qwen2.5-coder:14b",
        "fallbackModels": ["claude-sonnet-5"],
        "snapshotProfile": "worker-local-glimmer-fallback",
    }

    code, _payload, stderr, _stdout, _stderr = run_worker_result(
        "model-role-save", "--operator-role", "publisher", input_text=json.dumps(role)
    )

    assert code == 30
    assert "only owner/admin" in stderr.get("reason", "")

    code, payload, stderr, _stdout, _stderr = run_worker_result(
        "model-role-save", "--operator-role", "org-admin", input_text=json.dumps(role)
    )

    assert code == 0, stderr
    assert schema_check.validate(payload, "canonical/model-role-registry-v1.json") == []
    qwen = next(model for model in payload["models"] if model["id"] == "qwen2.5-coder:14b")
    claude = next(model for model in payload["models"] if model["id"] == "claude-sonnet-5")
    assert "dc13.coordination-report" in qwen["roles"]
    assert "dc13.coordination-report" in claude["roles"]
    updated = next(item for item in payload["roles"] if item["id"] == "dc13.coordination-report")
    assert updated["primaryModel"] == "qwen2.5-coder:14b"


# --- Guards added after the 2026-08-16 acceptance failure ---------------------
# See records/2026-08-17-acceptance-failure-analysis.md. Each test below pins one
# behaviour that failure depended on.

PLACEHOLDER_REQUIREMENT = (
    "# DEV-900010 - worker credential degradation acceptance\n\n"
    "_Infrastructure task. State the requirement here._\n\n"
    "This file is authored by a human. The control plane never invents requirements.\n"
)


def _bundle_with(task_id: str, *, requirement: str, command: list[str] | None = None,
                 target: str = "worker") -> str:
    bundle = json.loads(_bundle(task_id, command or ["/usr/bin/true"]))
    bundle["requirement"] = requirement
    bundle["contract"]["verification"]["target"] = target
    return json.dumps(bundle)


def test_generative_verb_refuses_an_unstated_requirement():
    """A pd-task placeholder must never reach a live model.

    On 2026-08-16 it did, and Codex authored 325 lines from the task title.
    """
    task_id = "DEV-900010"
    purge_task(task_id)
    try:
        code, _stdout_payload, payload, stdout, stderr = run_worker_result(
            "fix", task_id, input_text=_bundle_with(task_id, requirement=PLACEHOLDER_REQUIREMENT))
        assert code == 30
        assert stdout == ""
        assert stderr
        assert payload["status"] == "REQUIREMENT_MISSING"
        assert not (common.WORKTREES_DIR / f"{task_id}-codex").exists()
    finally:
        purge_task(task_id)


def test_argv_driven_verbs_are_not_gated_on_requirement_text():
    """Infrastructure acceptance legitimately carries a placeholder: its
    assertions live in the contract, and build/verify never read the text."""
    task_id = "DEV-900011"
    purge_task(task_id)
    try:
        code, payload, _stderr_payload, _stdout, _stderr = run_worker_result(
            "verify", task_id, input_text=_bundle_with(task_id, requirement=PLACEHOLDER_REQUIREMENT))
        assert code == 0
        assert payload["result"] == "PASS"
    finally:
        purge_task(task_id)


def test_live_fix_refuses_a_worker_target_rather_than_writing_to_the_corpus():
    """The fix stage can only produce worktrees of repos/Present. When the
    contract names the worker checkout, the honest answer is that the
    capability does not exist -- not a silent write to the document corpus."""
    task_id = "DEV-900013"
    purge_task(task_id)
    try:
        code, _stdout_payload, payload, stdout, stderr = run_worker_result(
            "fix", task_id, input_text=_bundle_with(
                task_id, requirement="Remove the obsolete readiness dependency from the worker.\n"))
        assert code == 30
        assert stdout == ""
        assert stderr
        assert payload["status"] == "BLOCKED"
        assert "not an implemented capability" in payload["reason"]
        assert not (common.WORKTREES_DIR / f"{task_id}-codex").exists()
    finally:
        purge_task(task_id)


def test_verification_refuses_when_no_worktree_holds_the_produced_commit():
    """Verifying the base tree when a fix produced a commit elsewhere reports
    on work that is not under test. That must be INCONCLUSIVE, never a quiet
    fallback -- the fallback is the original defect with better logging."""
    task_id = "DEV-900012"
    purge_task(task_id)
    try:
        bundle = _bundle_with(task_id, requirement="Deterministic check.\n")
        code, _payload, _stderr_payload, _stdout, _stderr = run_worker_result(
            "verify", task_id, input_text=bundle)
        assert code == 0
        log = WORKER_DIR / "logs" / f"{task_id}.jsonl"
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stage": "fix", "output_commit": "0" * 40}) + "\n")
        code, _stdout_payload, payload, stdout, stderr = run_worker_result(
            "verify", task_id, input_text=bundle)
        assert code == 20
        assert stdout == ""
        assert stderr
        assert payload["result"] == "INCONCLUSIVE"
        assert "no worktree holds it" in payload["checks"][0]["detail"]
    finally:
        purge_task(task_id)


def test_verification_records_the_tree_it_ran_in():
    task_id = "DEV-900014"
    purge_task(task_id)
    try:
        code, payload, _stderr_payload, _stdout, _stderr = run_worker_result(
            "verify", task_id, input_text=_bundle_with(task_id, requirement="Deterministic check.\n"))
        assert code == 0
        assert json.loads(payload["checks"][0]["detail"])["tree"] == str(WORKER_DIR)
    finally:
        purge_task(task_id)


def test_worker_target_build_records_the_worker_commit_not_the_corpus_commit():
    task_id = "DEV-900015"
    purge_task(task_id)
    try:
        bundle = _build_bundle(task_id, ["/bin/sleep", "1"])
        code, _payload, _stderr_payload, _stdout, _stderr = run_worker_result(
            "build", task_id, input_text=bundle)
        assert code == 0
        result = None
        for _ in range(30):
            time.sleep(1)
            code, payload, _stderr_payload, _stdout, _stderr = run_worker_result(
                "build", task_id, input_text=bundle)
            if code == 0 and "outcome" in payload:
                result = payload
                break
        assert result is not None
        worker_commit = subprocess.run(
            ["git", "-C", str(WORKER_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        assert result["provenance"]["commit"] == worker_commit
        assert result["provenance"]["commit"] == common.git_rev_parse(common.DEFAULT_REPO)
    finally:
        purge_task(task_id)


def test_credential_probe_failure_is_not_reported_as_a_missing_credential():
    """Claude Code reads the macOS login keychain, which a BatchMode SSH
    session cannot unlock. Reporting that as CREDENTIAL_MISSING states a fact
    about the credential the worker never established."""
    failed = claude_adapter._credential_refusal({"probe": "failed", "probeError": "keychain locked"})
    assert failed["status"] == "CREDENTIAL_PROBE_FAILED"
    assert failed["retryable"] is True
    assert claude_adapter._credential_refusal({"probe": "ok"})["status"] == "CREDENTIAL_MISSING"
    assert codex_adapter.credential_refusal({"probe": "ok"})["status"] == "CREDENTIAL_MISSING"
    for provider in (claude_adapter, codex_adapter):
        assert provider.status()["probe"] in {"ok", "failed"}


def test_claude_token_file_gives_every_transport_the_same_credential_answer(tmp_path):
    """A 0600 token file is injected as CLAUDE_CODE_OAUTH_TOKEN so the probe
    answers identically over BatchMode SSH and a desktop session. A caller's
    own env wins; absence falls through to the keychain; an unusable file is
    a loud probe failure, never a silent fallback; and the token value never
    leaks into a defect message."""
    saved = {key: os.environ.get(key) for key in
             ("CLAUDE_CODE_OAUTH_TOKEN", "PRESENT_WORKER_CLAUDE_TOKEN_FILE")}
    token_path = tmp_path / "present-worker-token"
    try:
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ["PRESENT_WORKER_CLAUDE_TOKEN_FILE"] = str(token_path)

        assert claude_adapter._credential_env() == (None, None)

        token_path.write_text("sk-ant-oat01-test-value\n")
        token_path.chmod(0o600)
        env, defect = claude_adapter._credential_env()
        assert defect is None
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test-value"

        token_path.chmod(0o644)
        env, defect = claude_adapter._credential_env()
        assert env is None and "group/other" in defect
        assert "sk-ant-oat01" not in defect
        auth, meta = claude_adapter.authenticated()
        assert auth is False and meta["probe"] == "failed"
        assert claude_adapter._credential_refusal(meta)["status"] == "CREDENTIAL_PROBE_FAILED"

        token_path.chmod(0o600)
        token_path.write_text("")
        assert claude_adapter._credential_env()[1].endswith("is empty")

        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "caller-owned"
        assert claude_adapter._credential_env() == (None, None)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_cli_module():
    """Import bin/present-worker as a module; it has no .py suffix."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("present_worker_cli", str(BIN))
    spec = importlib.util.spec_from_file_location("present_worker_cli", BIN, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fix_target_refusal_is_keyed_on_the_worker_target_only():
    """The refusal must not widen: the corpus fix path is the one that works."""
    cli = _load_cli_module()
    blocked = cli._fix_target_refusal("DEV-900016", {"verification": {"target": "worker"}})
    assert blocked["status"] == "BLOCKED"
    assert "not an implemented capability" in blocked["reason"]
    assert cli._fix_target_refusal("DEV-900016", {"verification": {"target": "present-repository"}}) is None


def test_ssh_guard_admits_coordinator_report_and_worker_blocks_without_contract():
    """The guard token exists; the worker still refuses a task with no contract
    rather than improvising a report subject."""
    task_id = "DEV-000871"
    purge_task(task_id)
    try:
        code, _stdout_payload, stderr_payload, _stdout, _stderr = run_ssh_guard_result(
            f"present-worker coordination-report {task_id}")
        assert code == 30
        assert stderr_payload.get("status") == "BLOCKED"
        assert "contract" in stderr_payload.get("reason", "")
    finally:
        purge_task(task_id)


def test_ssh_guard_denies_remote_whole_workflow_execution():
    """Remote orchestration must enter through admit-only or workflow-node."""
    task_id = "DEV-000872"
    purge_task(task_id)
    try:
        code, stdout_payload, stderr_payload, stdout, stderr = run_ssh_guard_result(
            f"present-worker workflow {task_id}")
        assert code == 30
        assert stdout_payload == {}
        assert stdout == ""
        assert stderr
        assert stderr_payload.get("status") == "DENIED"
        assert "allowlist" in stderr_payload.get("reason", "")
    finally:
        purge_task(task_id)


def test_mock_coordinator_report_is_canonical_advisory_and_claims_no_authority():
    task_id = "DEV-000872"
    purge_task(task_id)
    try:
        code, payload = run_worker("coordination-report", task_id, "--mock")
        assert code == 0
        assert payload["mock"] is True
        assert payload["taskId"] == task_id
        assert payload["producer"] == "steel-mission coordination-report (codex)"
        # The invariant is worker-authored, never model prose: no authority,
        # no PASS, advisory only.
        assert "no authority" in payload["advisoryNote"]
        assert "PASS" in payload["advisoryNote"]
        # A mock run retrieved nothing and must say so.
        assert payload["items"] == []
        assert payload["notChecked"]
        assert payload["packIdentity"]["probe"] == "ok"
        assert payload["packIdentity"]["packageId"]
        assert isinstance(payload["packIdentity"]["corpusGeneration"], int)
        assert payload["packIdentity"]["currentThrough"]
        # manifestSha256 is deliberately absent: the authority's packIdentity
        # is additionalProperties:false and does not permit it.
        assert "manifestSha256" not in payload["packIdentity"]
        assert schema_check.validate(payload, "canonical/coordination-report-v1.json") == []
        artifact = json.loads(
            (common.TASKS_DIR / task_id / "coordination-report" / "coordination-report.json").read_text())
        assert artifact["mock"] is True
        log_lines = (WORKER_DIR / "logs" / f"{task_id}.jsonl").read_text().splitlines()
        assert any(json.loads(line)["stage"] == "coordination-report" for line in log_lines)
    finally:
        purge_task(task_id)


def test_coordinator_state_snapshot_is_bounded_and_declares_omissions():
    cli = _load_cli_module()
    snapshot = cli._coordinator_state_snapshot(cli.DEFAULT_COORDINATOR_SNAPSHOT_POLICY)
    assert len(snapshot["tasks"]) <= cli.COORDINATOR_SNAPSHOT_MAX_TASKS
    assert len(snapshot["buildJobs"]) <= cli.COORDINATOR_SNAPSHOT_MAX_BUILD_JOBS
    assert len(snapshot["verifyResults"]) <= cli.COORDINATOR_SNAPSHOT_MAX_VERIFY_RESULTS
    assert len(snapshot["distributedWorkflows"]) <= cli.COORDINATOR_SNAPSHOT_MAX_DISTRIBUTED_WORKFLOWS
    assert len(snapshot["advisoryTasks"]) <= cli.COORDINATOR_SNAPSHOT_MAX_ADVISORY_TASKS
    assert len(snapshot["missions"]) <= cli.COORDINATOR_SNAPSHOT_MAX_MISSIONS
    assert snapshot["tasksOmittedFromSnapshot"] >= 0
    assert snapshot["buildJobsOmittedFromSnapshot"] >= 0
    assert snapshot["verifyResultsOmittedFromSnapshot"] >= 0
    assert snapshot["distributedWorkflowsOmittedFromSnapshot"] >= 0
    assert snapshot["advisoryTasksOmittedFromSnapshot"] >= 0
    assert snapshot["generalDocumentsOmittedFromSnapshot"] >= 0
    assert snapshot["missionsOmittedFromSnapshot"] >= 0
    collections = {item["name"]: item for item in snapshot["snapshotCollections"]}
    assert set(collections) == {
        "tasks", "advisoryTasks", "buildJobs", "verifyResults", "distributedWorkflows", "generalDocuments", "missions"}
    for item in collections.values():
        assert item["totalAvailable"] >= item["returned"]
        assert item["omittedFromSnapshot"] == item["totalAvailable"] - item["returned"]
    assert "generatedAt" in snapshot and "repositoryCommits" in snapshot


def test_coordinator_pending_decisions_come_from_live_mission_and_session_records(tmp_path):
    cli = _load_cli_module()
    mission_root = tmp_path / "missions"
    escalated_dir = mission_root / "ms-aaaaaaaaaaaaaaaaaaaaaaaa"
    escalated_dir.mkdir(parents=True)
    escalated_path = escalated_dir / "mission.json"
    escalated = {
        "missionId": "ms-aaaaaaaaaaaaaaaaaaaaaaaa",
        "taskId": "DEV-900187",
        "state": "waiting_for_decision",
        "objective": "Keep the redirected mission within its grant",
        "updatedAt": "2026-08-21T15:00:00Z",
        "decisionRequest": {
            "id": "decision-scope-187",
            "question": "Should mission 187 stay within the granted paths?",
            "requestedAt": "2026-08-21T15:00:00Z",
        },
    }
    escalated_path.write_text(json.dumps(escalated))

    approval_dir = mission_root / "ms-bbbbbbbbbbbbbbbbbbbbbbbb"
    approval_dir.mkdir()
    approval_path = approval_dir / "mission.json"
    approval = {
        "missionId": "ms-bbbbbbbbbbbbbbbbbbbbbbbb",
        "taskId": "DEV-900188",
        "state": "waiting_for_approval",
        "objective": "Publish the accepted delivery",
        "currentNodeId": "publish-approval",
        "updatedAt": "2026-08-21T15:01:00Z",
        "nodes": [{
            "nodeId": "publish-approval",
            "title": "Publish approval",
            "kind": "approval",
            "capability": "delivery.approve",
            "requiresApproval": True,
            "state": "waiting_for_approval",
        }],
    }
    approval_path.write_text(json.dumps(approval))

    session = json.loads(
        (WORKER_DIR / "schemas" / "fixtures" / "valid" / "agent-session-status-v1.working.json").read_text()
    )
    session["eventId"] = "ase-cccccccccccccccccccccccc"
    session["lastEvent"] = {
        "eventId": session["eventId"],
        "sequence": session["sequence"],
        "at": "2026-08-21T15:02:00Z",
        "kind": "decision-requested",
        "summary": "The review is blocked on the Person.",
    }
    session["state"] = "waiting-on-person"
    session["stage"] = "review-loop"
    session["pendingDecision"] = {
        "decisionId": "decision-blocked-session",
        "kind": "blocked",
        "question": "Who can provide the missing acceptance account?",
        "requestedAt": "2026-08-21T15:02:00Z",
    }
    feed_path = mission_root / "agent-session-status.jsonl"
    feed_path.write_text(json.dumps(session) + "\n")

    policy = {
        "schemaVersion": 1,
        "sourceProfile": "pending-decision-fixture",
        "includeCollections": ["missions"],
        "limits": {"missions": 10, "operatorAudit": 0},
        "taskSelector": {"mode": "latest"},
        "sources": {
            "taskRoots": [],
            "logRoots": [],
            "buildJobRoots": [],
            "verifyResultRoots": [],
            "brokerStatePaths": [],
            "missionRoots": [str(mission_root)],
            "repositoryRoots": [],
        },
    }

    snapshot = cli._coordinator_state_snapshot(policy)

    assert {item["kind"] for item in snapshot["pendingDecisions"]} == {
        "blocked-session", "escalated-mission", "mission-approval",
    }
    assert "Should mission 187 stay within the granted paths?" in snapshot["pendingDecisionSummary"]
    assert "Who can provide the missing acceptance account?" in snapshot["pendingDecisionSummary"]
    assert snapshot["pendingDecisionsOmittedFromSnapshot"] == 0
    escalated_summary = next(item for item in snapshot["missions"] if item["missionId"] == escalated["missionId"])
    assert escalated_summary["decisionRequest"] == escalated["decisionRequest"]

    escalated_path.write_text(json.dumps({**escalated, "state": "done", "decisionRequest": None}))
    approval_path.write_text(json.dumps({
        **approval,
        "state": "done",
        "nodes": [{**approval["nodes"][0], "state": "done"}],
    }))
    finished = {
        **session,
        "eventId": "ase-dddddddddddddddddddddddd",
        "sequence": 3,
        "state": "succeeded",
        "lastEvent": {
            "eventId": "ase-dddddddddddddddddddddddd",
            "sequence": 3,
            "at": "2026-08-21T15:03:00Z",
            "kind": "finished",
            "summary": "The session finished.",
        },
        "outcome": {"status": "succeeded", "summary": "The session finished."},
    }
    finished.pop("pendingDecision")
    feed_path.write_text(json.dumps(session) + "\n" + json.dumps(finished) + "\n")

    empty = cli._coordinator_state_snapshot(policy)

    assert empty["pendingDecisions"] == []
    assert empty["pendingDecisionSummary"] == "Nothing currently needs the Person."


def test_server_snapshot_policy_binds_the_configured_status_feed_and_attention_rule(tmp_path, monkeypatch):
    feed_path = tmp_path / "live-status.jsonl"
    monkeypatch.setenv("STEEL_MISSION_AGENT_SESSION_STATUS_FEED", str(feed_path))
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))

    policy = chat["default_snapshot_policy"]()
    resolved = chat["resolve_runtime_profile"]("dc13.claude")["snapshotPolicy"]
    requirement = chat["build_requirement"]("What needs me?", [])

    assert str(feed_path) in policy["sources"]["missionRoots"]
    assert str(feed_path) in resolved["sources"]["missionRoots"]
    assert "pendingDecisions" in requirement
    assert "pendingDecisionSummary" in requirement


def test_coordinator_snapshot_policy_attached_to_job_controls_snapshot_roots(tmp_path):
    cli = _load_cli_module()
    task_id = "DEV-900201"
    other_task_id = "DEV-900202"
    tasks_dir = tmp_path / "tasks"
    logs_dir = tmp_path / "logs"
    test_results_dir = tmp_path / "test-results"
    jobs_dir = tmp_path / "jobs"
    for current in (task_id, other_task_id):
        task_dir = tasks_dir / current
        task_dir.mkdir(parents=True)
        (task_dir / "contract.json").write_text(json.dumps({
            "schemaVersion": 1,
            "taskId": current,
            "producedAt": common.utc_now(),
            "producer": "control-plain",
            "provenance": {"source": "control-plain"},
            "verification": {"target": "worker", "commands": [{
                "name": "true",
                "argv": ["/usr/bin/true"],
                "expectedExitCode": 0,
                "timeoutSeconds": 10,
            }]},
        }))
        common.append_jsonl(logs_dir / f"{current}.jsonl", {
            "stage": "plan",
            "status": "OK",
            "timestamp": common.utc_now(),
        })
    jobs_dir.joinpath(f"{task_id}-build").mkdir(parents=True)
    jobs_dir.joinpath(f"{task_id}-build", "status.json").write_text(json.dumps({
        "jobId": f"{task_id}-build",
        "taskId": task_id,
        "state": "finished",
        "outcome": "SUCCEEDED",
    }))
    test_results_dir.mkdir()
    (test_results_dir / f"{task_id}-verify.json").write_text(json.dumps({
        "schemaVersion": 1,
        "taskId": task_id,
        "result": "PASS",
        "mock": False,
        "producedAt": common.utc_now(),
        "checks": [{"name": "true", "passed": True}],
    }))
    contract = {
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": common.utc_now(),
        "producer": "steel-mission-chat-local",
        "provenance": {"source": "worker-local-advisory-client"},
        "verb": "coordination-report",
        "advisory": True,
        "verificationAuthority": False,
        "snapshotPolicy": {
            "schemaVersion": 1,
            "sourceProfile": "other-project-verify-only",
            "includeCollections": ["verifyResults"],
            "limits": {
                "tasks": 0,
                "advisoryTasks": 0,
                "buildJobs": 0,
                "verifyResults": 1,
                "distributedWorkflows": 0,
                "brokerStateSources": 0,
                "brokerArtifacts": 0,
                "operatorAudit": 0,
            },
            "taskSelector": {"mode": "explicit", "taskIds": [task_id]},
            "sources": {
                "taskRoots": [str(tasks_dir)],
                "logRoots": [str(logs_dir)],
                "buildJobRoots": [str(jobs_dir)],
                "verifyResultRoots": [str(test_results_dir)],
                "brokerStatePaths": [],
                "repositoryRoots": [{"name": "other-project", "path": str(tmp_path)}],
            },
        },
    }
    policy = cli._effective_coordinator_snapshot_policy(task_id, contract)
    snapshot = cli._coordinator_state_snapshot(policy)

    assert snapshot["snapshotPolicy"]["sourceProfile"] == "other-project-verify-only"
    assert snapshot["tasks"] == []
    assert snapshot["buildJobs"] == []
    assert [item["taskId"] for item in snapshot["verifyResults"]] == [task_id]
    assert {item["collection"] for item in snapshot["sourceExclusions"]} == {
        "tasks", "advisoryTasks", "buildJobs", "distributedWorkflows", "generalDocuments", "missions"}
    collections = {item["name"]: item for item in snapshot["snapshotCollections"]}
    assert collections["tasks"]["enabled"] is False
    assert collections["verifyResults"]["enabled"] is True
    assert collections["verifyResults"]["totalAvailable"] == 1
    assert snapshot["repositoryCommits"][0]["name"] == "other-project"


def test_coordinator_snapshot_policy_must_be_attached_to_job():
    cli = _load_cli_module()
    try:
        cli._effective_coordinator_snapshot_policy("DEV-900201", {
            "schemaVersion": 1,
            "taskId": "DEV-900201",
            "verb": "coordination-report",
        })
    except common.TaskBundleError as exc:
        assert "snapshotPolicy" in str(exc)
    else:
        raise AssertionError("coordination-report accepted a job without snapshotPolicy")


def test_coordinator_snapshot_policy_config_file_is_not_a_truth_source(tmp_path, monkeypatch):
    cli = _load_cli_module()
    policy_path = tmp_path / "ignored-coordination-snapshot-policy.json"
    policy_path.write_text(json.dumps({
        "schemaVersion": 1,
        "defaults": {
            "includeCollections": ["tasks"],
        }
    }))
    monkeypatch.setenv("PRESENT_COORDINATOR_SNAPSHOT_POLICY", str(policy_path))

    try:
        cli._effective_coordinator_snapshot_policy("DEV-900203", None)
    except common.TaskBundleError as exc:
        assert "snapshotPolicy" in str(exc)
    else:
        raise AssertionError("ambient cos snapshot policy config was accepted")


def test_coordinator_state_snapshot_includes_broker_distributed_workflow_summary(tmp_path):
    cli = _load_cli_module()
    task_id = "DEV-900136"
    store = tmp_path / "store"
    broker_dir = store / "_broker" / task_id
    broker_dir.mkdir(parents=True)
    (broker_dir / "acceptance-manifest.json").write_text(json.dumps({
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": common.utc_now(),
        "producer": "present-lease-broker deterministic acceptance",
        "workflowId": f"{task_id}-workflow",
        "workflowRunId": "wr-" + "5" * 24,
        "contractHash": "a" * 64,
        "decision": "INCONCLUSIVE",
        "reason": "mock verification is advisory only",
        "acceptedEvidence": [],
    }))
    (broker_dir / "evidence-graph.json").write_text(json.dumps({
        "schemaVersion": 1,
        "taskId": task_id,
        "producedAt": common.utc_now(),
        "producer": "present-lease-broker evidence graph",
        "workflowId": f"{task_id}-workflow",
        "workflowRunId": "wr-" + "5" * 24,
        "contractHash": "a" * 64,
        "nodes": [{"nodeId": "verify", "role": "deterministic-acceptance"}],
        "redactionStatus": "CLEAN",
        "gateDecision": "INCONCLUSIVE",
    }))
    state_path = tmp_path / "broker-state.json"
    state = broker_state_document({
        task_id: {
            "taskId": task_id,
            "status": "DISTRIBUTED_COMPLETE",
            "contractHash": "a" * 64,
            "workflowRunId": "wr-" + "5" * 24,
            "selectedWorker": "distributed",
            "terminalAt": common.utc_now(),
            "updatedAt": common.utc_now(),
            "attempts": [],
            "distributed": {
                "schemaVersion": 1,
                "taskId": task_id,
                "producedAt": common.utc_now(),
                "producer": "present-lease-broker distributed state",
                "updatedAt": common.utc_now(),
                "workflowId": f"{task_id}-workflow",
                "contractHash": "a" * 64,
                "storeRoot": str(store),
                "placements": [],
                "artifactTransferManifest": {},
                "nodes": {
                    "plan": {"record": {"status": "SUCCEEDED"}},
                    "verify": {"record": {"status": "SUCCEEDED"}},
                },
                "nodeDispatches": [
                    {"nodeId": "plan", "workerId": "macbook-local:plan", "status": "SUCCEEDED"},
                    {"nodeId": "verify", "workerId": "linux-container:verify", "status": "SUCCEEDED"},
                ],
                "artifactStore": [
                    {"nodeId": "plan", "status": "MATERIALIZED"},
                    {"nodeId": "verify", "status": "MATERIALIZED"},
                ],
                "importedArtifacts": [],
                "replayReport": {
                    "reusedNodes": ["plan"],
                    "freshNodes": ["verify"],
                    "invalidatedNodes": [],
                    "skippedNodes": [],
                },
                "acceptanceManifest": {
                    "decision": "INCONCLUSIVE",
                    "reason": "mock verification is advisory only",
                },
                "cancelledNodes": ["adversarial"],
            },
        },
    }, events=[
        {"eventId": "ev-1", "taskId": task_id, "type": "worker-quarantined",
         "workerId": "linux-container:verify", "producedAt": common.utc_now()},
    ])
    state["operatorAuditLog"] = [
        {"auditId": "oa-" + "1" * 24, "taskId": task_id, "action": "quarantine-worker",
         "operatorRole": "admin", "workerId": "linux-container:verify",
         "reason": "network faults", "producedAt": common.utc_now()},
    ]
    state_path.write_text(json.dumps(state))
    policy = {
        "schemaVersion": 1,
        "sourceProfile": "broker-state-fixture",
        "includeCollections": ["distributedWorkflows"],
        "limits": {
            "tasks": 0,
            "advisoryTasks": 0,
            "buildJobs": 0,
            "verifyResults": 0,
            "distributedWorkflows": 12,
            "brokerStateSources": 1,
            "brokerArtifacts": 10,
            "operatorAudit": 8,
        },
        "sources": {
            "taskRoots": [],
            "logRoots": [],
            "buildJobRoots": [],
            "verifyResultRoots": [],
            "brokerStatePaths": [str(state_path)],
            "repositoryRoots": [],
        },
    }

    snapshot = cli._coordinator_state_snapshot(policy)
    collections = {item["name"]: item for item in snapshot["snapshotCollections"]}
    assert collections["distributedWorkflows"]["totalAvailable"] >= 1
    workflow = next(item for item in snapshot["distributedWorkflows"] if item["taskId"] == task_id)
    assert workflow["status"] == "DISTRIBUTED_COMPLETE"
    assert workflow["acceptanceDecision"] == "INCONCLUSIVE"
    assert workflow["replay"]["freshNodes"] == ["verify"]
    assert workflow["nodeStatusCounts"] == {"SUCCEEDED": 2}
    assert workflow["dispatchStatusCounts"] == {"SUCCEEDED": 2}
    assert workflow["artifactStatusCounts"] == {"MATERIALIZED": 2}
    assert workflow["cancelledNodes"] == ["adversarial"]
    assert workflow["quarantinedWorkers"] == ["linux-container:verify"]
    assert workflow["brokerArtifacts"]["acceptanceManifest"]["decision"] == "INCONCLUSIVE"
    assert workflow["brokerArtifacts"]["evidenceGraph"]["evidenceNodes"] == 1
    assert "handoff-package.json" in workflow["brokerArtifacts"]["missing"]


def test_advisory_chat_tasks_do_not_consume_the_pipeline_task_window():
    """Chat by-product must not crowd out the state the report is about.

    The chat mints one task per question and those ids sort above the pipeline
    range, so an unpartitioned tail handed 16 of 30 slots to advisory tasks on
    2026-08-17 and got worse with every question. Identity is the contract's
    producer, never the id range: DEV-999996/999997 are real pipeline tasks in
    the DEV-9 range carrying verify PASS evidence.
    """
    cli = _load_cli_module()
    snapshot = cli._coordinator_state_snapshot(cli.DEFAULT_COORDINATOR_SNAPSHOT_POLICY)
    producer = common.ADVISORY_TASK_PRODUCER

    def producer_of(task_id):
        return cli._task_producer(common.TASKS_DIR / task_id)

    # Assert the partition exists before asserting over it, so this cannot
    # pass merely because a key is missing.
    assert "advisoryTasks" in snapshot and "tasks" in snapshot
    names = {item["name"] for item in snapshot["snapshotCollections"]}
    assert {"tasks", "advisoryTasks"} <= names
    for task in snapshot["tasks"]:
        assert producer_of(task["taskId"]) != producer, (
            f"{task['taskId']} is advisory by-product and must not hold a pipeline slot")
    for task in snapshot["advisoryTasks"]:
        assert producer_of(task["taskId"]) == producer

    # A task whose producer cannot be established stays in the pipeline
    # population -- demotion requires positive evidence.
    assert cli._task_producer(common.TASKS_DIR / "DEV-000000-does-not-exist") is None


def test_coordinator_progress_messages_name_the_question_and_snapshot_context():
    cli = _load_cli_module()
    task_id = "DEV-900021"
    progress_dir = common.TASKS_DIR / task_id
    purge_task(task_id)
    try:
        progress_dir.mkdir(parents=True)
        writer = cli._coordinator_progress_writer(
            task_id,
            "report on DEV-999996 and explain the stale verification records",
            {
                "snapshotCollections": [
                    {"name": "tasks", "returned": 30, "totalAvailable": 47},
                    {"name": "advisoryTasks", "returned": 5, "totalAvailable": 15},
                    {"name": "verifyResults", "returned": 8, "totalAvailable": 9},
                ],
            },
        )

        initial = json.loads((progress_dir / "progress.json").read_text())
        assert initial["events"] == 0
        assert initial["elapsedSeconds"] >= 0
        assert initial["phase"].startswith("Starting Claude")
        assert initial["provider"] == "claude"
        assert initial["model"] == cli.claude_adapter.COORDINATOR_MODEL
        assert initial["timeline"][0]["label"] == "Snapshot ready"
        assert "30 of 47 tasks" in initial["timeline"][0]["detail"]
        assert initial["timeline"][1]["label"] == "Context checkpoint"
        assert initial["timeline"][1]["checkpointId"].startswith("cc-")
        checkpoint_path = common.task_artifact_path(task_id, "context-checkpoint")
        assert checkpoint_path.exists()
        checkpoint = json.loads(checkpoint_path.read_text())
        assert checkpoint["reason"] == "snapshot-prepared"
        assert schema_check.validate(checkpoint, "canonical/context-checkpoint-v1.json") == []

        writer({"type": "system", "subtype": "process_started", "pid": 1234, "pgid": 1234})
        process_started = json.loads((progress_dir / "progress.json").read_text())
        assert process_started["modelPid"] == 1234
        assert process_started["modelPgid"] == 1234
        assert process_started["phase"].startswith("Model process started")
        assert process_started["timeline"][-1]["label"] == "Model process started"
        assert "Claude process is running" in process_started["timeline"][-1]["detail"]

        writer({"type": "system", "subtype": "init"})
        started = json.loads((progress_dir / "progress.json").read_text())
        assert started["phase"].startswith("Model stream opened")
        assert "report on DEV-999996" in started["phase"]
        assert started["events"] == 2
        assert started["firstEventSeconds"] >= 0
        assert started["latestEventSubtype"] == "init"
        assert [event["label"] for event in started["timeline"]][-1] == "Model stream opened"

        writer({"type": "system", "subtype": "api_retry"})
        retrying = json.loads((progress_dir / "progress.json").read_text())
        assert retrying["phase"].startswith("Provider retrying")
        assert retrying["timeline"][-1]["label"] == "Provider retry"
        assert retrying["timeline"][-1]["detail"].startswith("Claude reported a retry")

        writer({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 123})
        reconciling = json.loads((progress_dir / "progress.json").read_text())
        assert reconciling["phase"].startswith("Reconciling 30 of 47 tasks")
        assert "5 of 15 advisory tasks" in reconciling["phase"]
        assert "8 of 9 verify results" in reconciling["phase"]
        assert "thinking" not in reconciling["phase"].lower()
        assert reconciling["thinkingTokens"] == 123
        assert reconciling["firstThinkingTokenSeconds"] >= started["firstEventSeconds"]
        assert reconciling["timeline"][-2]["label"] == "Thinking started"
        assert reconciling["timeline"][-2]["tokens"] == 123
        assert reconciling["timeline"][-1]["label"] == "Context checkpoint"
        checkpoint = json.loads(checkpoint_path.read_text())
        assert checkpoint["reason"] == "stream-progress"
        assert checkpoint["currentState"]["thinkingTokens"] == 123

        writer({"type": "assistant"})
        writing = json.loads((progress_dir / "progress.json").read_text())
        assert writing["phase"].startswith("Writing findings from 30 of 47 tasks")
        assert writing["firstAssistantSeconds"] >= started["firstEventSeconds"]
        assert writing["timeline"][-2]["label"] == "Writing findings"
        assert writing["timeline"][-1]["label"] == "Context checkpoint"

        writer({"type": "result"})
        finishing = json.loads((progress_dir / "progress.json").read_text())
        assert finishing["phase"].startswith("Finalizing the reconciled report")
        assert finishing["events"] == 6
        assert finishing["resultSeconds"] >= started["firstEventSeconds"]
        assert finishing["timeline"][-1]["label"] == "Result received"
    finally:
        purge_task(task_id)


def test_coordinator_progress_messages_show_glimmer_provider_and_model():
    cli = _load_cli_module()
    task_id = "DEV-900145"
    progress_dir = common.TASKS_DIR / task_id
    purge_task(task_id)
    try:
        progress_dir.mkdir(parents=True)
        writer = cli._coordinator_progress_writer(
            task_id,
            "status",
            {"snapshotCollections": [{"name": "tasks", "returned": 1, "totalAvailable": 2}]},
            provider="glimmer",
            model="qwen2.5-coder:14b",
        )

        initial = json.loads((progress_dir / "progress.json").read_text())
        assert initial["provider"] == "glimmer"
        assert initial["providerLabel"] == "Glimmer"
        assert initial["model"] == "qwen2.5-coder:14b"
        assert initial["phase"].startswith("Starting Glimmer")

        writer({"type": "system", "subtype": "glimmer_rewarm_started"})
        warming = json.loads((progress_dir / "progress.json").read_text())
        assert warming["phase"].startswith("Re-warming Glimmer model qwen2.5-coder:14b")
        assert warming["timeline"][-1]["label"] == "Re-warming local model"

        writer({"type": "system", "subtype": "glimmer_rewarm_completed"})
        warmed = json.loads((progress_dir / "progress.json").read_text())
        assert warmed["phase"].startswith("Glimmer model qwen2.5-coder:14b re-warmed")
        assert warmed["timeline"][-1]["label"] == "Local model ready"

        writer({
            "type": "system",
            "subtype": "glimmer_rewarm_failed",
            "reason": "Glimmer model re-warm failed: timed out",
        })
        failed = json.loads((progress_dir / "progress.json").read_text())
        assert failed["phase"].startswith("Glimmer model qwen2.5-coder:14b re-warm failed")
        assert failed["timeline"][-1]["label"] == "Local model re-warm failed"
        assert failed["timeline"][-1]["detail"] == "Glimmer model re-warm failed: timed out"

        writer({
            "type": "system",
            "subtype": "glimmer_request_budget_exhausted",
            "reason": "Glimmer re-warm exhausted the 90s caller timeout before advisory generation",
        })
        exhausted = json.loads((progress_dir / "progress.json").read_text())
        assert exhausted["phase"].startswith("Glimmer request budget exhausted after re-warm")
        assert exhausted["timeline"][-1]["label"] == "Local model request timed out"

        writer({"type": "system", "subtype": "glimmer_request_started"})
        started = json.loads((progress_dir / "progress.json").read_text())
        assert started["phase"].startswith("Glimmer request started")
        assert "Glimmer model qwen2.5-coder:14b is loaded" in started["timeline"][-1]["detail"]

        writer({"type": "system", "subtype": "api_retry"})
        retrying = json.loads((progress_dir / "progress.json").read_text())
        assert retrying["timeline"][-1]["detail"].startswith("Glimmer reported a retry")
        # Only the wording-bearing fields must be free of Claude branding:
        # checkpointPath embeds the absolute checkout path, which can itself
        # contain "Claude" (e.g. a worktree under "Steel-Mission Claude").
        worded = [retrying["phase"], retrying["providerLabel"], retrying["model"]] + [
            f"{event.get('label', '')} {event.get('detail', '')}" for event in retrying["timeline"]
        ]
        assert "Claude" not in json.dumps(worded), worded
    finally:
        purge_task(task_id)


def test_coordinator_progress_messages_show_codex_provider_and_model():
    cli = _load_cli_module()
    task_id = "DEV-900188"
    progress_dir = common.TASKS_DIR / task_id
    purge_task(task_id)
    try:
        progress_dir.mkdir(parents=True)
        cli._coordinator_progress_writer(
            task_id,
            "status",
            {"snapshotCollections": [{"name": "tasks", "returned": 1, "totalAvailable": 1}]},
            provider="codex",
            model="codex-cli-default",
        )

        initial = json.loads((progress_dir / "progress.json").read_text())
        assert initial["provider"] == "codex"
        assert initial["providerLabel"] == "Codex"
        assert initial["model"] == "codex-cli-default"
        assert initial["phase"].startswith("Starting Codex")
    finally:
        purge_task(task_id)


def test_event_array_reply_is_parsed_not_crashed():
    """`claude -p --output-format json` may answer with an event array.

    On 2026-08-17 every live coordination-report that survived its timeout died on
    `'list' object has no attribute 'get'`: the reply was a 20-element array
    whose terminal `result` element carried the structured output. A shape the
    adapter does not expect must degrade to a provider defect, never raise.
    """
    reply = json.dumps([
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
        {"type": "result", "subtype": "success", "is_error": False,
         "structured_output": {"summary": "reconciled", "items": []}},
    ])
    outer, defect = claude_adapter._terminal_result(reply)
    assert defect is None
    assert outer["structured_output"]["summary"] == "reconciled"

    # A single result object stays supported.
    outer, defect = claude_adapter._terminal_result(json.dumps({"structured_output": {"a": 1}}))
    assert defect is None and outer["structured_output"] == {"a": 1}

    # Unusable shapes are reported, never raised.
    for bad in (json.dumps([{"type": "system"}]), json.dumps("scalar"), "not json"):
        outer, defect = claude_adapter._terminal_result(bad)
        assert outer is None and defect


def test_turn_limit_leaves_room_for_the_structured_output_round_trip():
    """Structured output costs at least two turns, so 1 is not a viable limit.

    Measured 2026-08-17: at --max-turns 1 every failing coordination-report ended
    `error_max_turns`, and the successful ones reported num_turns=2 -- passing
    on the exact boundary.
    """
    assert claude_adapter.MAX_TURNS >= 2


def test_cli_failure_is_reported_from_the_result_event_not_the_raw_stream():
    """A stopped run must say why, not emit the tail of its own stdout.

    `error_max_turns` used to surface as
    `"cacheCreationInputTokens":0,"webSearchRequests":0,...` -- the last 1000
    chars of stdout -- which cost three experiments to diagnose.
    """
    # The exact shape captured from a real max-turns failure.
    outer = {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "stop_reason": "tool_use", "num_turns": 2, "result": None,
             "usage": {"cacheCreationInputTokens": 0, "costUSD": 0.009348}}
    reason = claude_adapter._failure_reason(outer)
    assert reason and "error_max_turns" in reason
    assert "max-turns" in reason  # names the actual cause
    assert "cacheCreationInputTokens" not in reason  # never the raw stream

    # A plain error still reports its text.
    assert "boom" in claude_adapter._failure_reason(
        {"type": "result", "subtype": "success", "is_error": True, "result": "boom"})
    # A clean success is not a failure.
    assert claude_adapter._failure_reason(
        {"type": "result", "subtype": "success", "is_error": False,
         "structured_output": {"a": 1}}) is None


def test_invoke_reports_a_nonzero_exit_from_its_result_event():
    """The whole path: non-zero exit + event array -> a reason, not a stream.

    Exercised with the shape a real `error_max_turns` run produced, because
    that failure only reproduces at low effort and must not need a live model
    to stay covered.
    """
    import subprocess as sp

    events = [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "error_max_turns", "is_error": True,
         "stop_reason": "tool_use", "num_turns": 2, "result": None,
         "usage": {"cacheCreationInputTokens": 0, "costUSD": 0.009348}},
    ]
    original = claude_adapter._stream_events
    try:
        claude_adapter._stream_events = lambda cmd, **kw: (events, 1, None)
        output, error = claude_adapter._invoke("prompt", claude_adapter.COORDINATOR_REPORT_SCHEMA)
    finally:
        claude_adapter._stream_events = original
    assert output is None
    assert "error_max_turns" in error and "max-turns" in error
    assert "cacheCreationInputTokens" not in error


def test_schema_conformance_failure_is_retried_within_the_caller_budget():
    """A schema retry-exhaustion is per-attempt; a credential defect is not.

    Measured 2026-08-17: `error_max_structured_output_retries` is what every
    270.2s "timeout" actually was. The same prompt often succeeds next time,
    so it is retried -- but inside the caller's deadline, never beyond it.
    """
    def reply(subtype, structured=None):
        event = {"type": "result", "subtype": subtype, "is_error": subtype != "success"}
        if structured is not None:
            event["structured_output"] = structured
        return [event]

    calls = {"n": 0}
    original = claude_adapter._stream_events
    try:
        # Fails once on schema retries, then succeeds.
        def flaky(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return reply("error_max_structured_output_retries"), 1, None
            return reply("success", {"summary": "ok"}), 0, None
        claude_adapter._stream_events = flaky
        output, error = claude_adapter._invoke("p", claude_adapter.COORDINATOR_REPORT_SCHEMA, timeout=600)
        assert error is None and output == {"summary": "ok"}
        assert calls["n"] == 2, "a schema-conformance failure must be retried"

        def always(subtype):
            def stream(cmd, **kw):
                calls["n"] += 1
                return reply(subtype), 1, None
            return stream

        # A non-retryable failure is returned immediately, never repeated.
        calls["n"] = 0
        claude_adapter._stream_events = always("error_during_execution")
        output, error = claude_adapter._invoke("p", claude_adapter.COORDINATOR_REPORT_SCHEMA, timeout=600)
        assert output is None and "error_during_execution" in error
        assert calls["n"] == 1, "only schema/turn failures are retried"

        # With no budget left for a second attempt, it reports instead of retrying.
        calls["n"] = 0
        claude_adapter._stream_events = always("error_max_structured_output_retries")
        output, error = claude_adapter._invoke("p", claude_adapter.COORDINATOR_REPORT_SCHEMA, timeout=1)
        assert output is None and "not retried" in error
        assert calls["n"] == 1
    finally:
        claude_adapter._stream_events = original


def test_streaming_kills_a_stalled_run_but_not_a_slow_one():
    """Silence, not duration, is what marks a run as stalled.

    This is what lets the total budget be generous: a run still emitting
    events is never killed for taking a while. Exercised against the real
    `_stream_events` using a stand-in process, so the queue/idle machinery is
    under test rather than mocked away.
    """
    slow_but_alive = [
        sys.executable, "-c",
        "import time,sys\n"
        "for i in range(6):\n"
        "    print('{\"type\":\"system\",\"subtype\":\"thinking_tokens\"}', flush=True)\n"
        "    time.sleep(0.2)\n"
        "print('{\"type\":\"result\",\"subtype\":\"success\",\"structured_output\":{\"a\":1}}', flush=True)\n",
    ]
    seen: list = []
    events, rc, defect = claude_adapter._stream_events(
        slow_but_alive, timeout=30, idle_timeout=2, env=None, progress=seen.append)
    assert defect is None, "a run that keeps emitting must not be killed"
    assert rc == 0
    assert events[-1]["structured_output"] == {"a": 1}
    assert seen[0]["subtype"] == "process_started"
    assert isinstance(seen[0]["pgid"], int)
    assert len(seen) == len(events) + 1, "every event reaches the progress callback"

    stalled = [sys.executable, "-c", "import time; time.sleep(30)"]
    start = time.monotonic()
    events, rc, defect = claude_adapter._stream_events(
        stalled, timeout=30, idle_timeout=1, env=None, progress=None)
    elapsed = time.monotonic() - start
    assert defect and "no output" in defect
    assert elapsed < 15, "a stalled run must stop on silence, not wait out the budget"


def test_thin_report_is_re_asked_and_then_declared_under_surveyed():
    """A survey that did not happen must never pass as a complete one."""
    snapshot = {"snapshotCollections": [
        {"name": "tasks", "totalAvailable": 48, "returned": 30, "omittedFromSnapshot": 18},
        {"name": "buildJobs", "totalAvailable": 30, "returned": 25, "omittedFromSnapshot": 5},
    ]}
    thin = {"summary": "s", "items": [{"subject": "one"}], "notChecked": [], "contradictions": []}
    full = {"summary": "s", "items": [{"subject": str(i)} for i in range(12)],
            "notChecked": [{"subject": "omitted", "reason": "truncated"}], "contradictions": []}

    # Detection is relative to the snapshot, so a narrow question is not punished.
    assert claude_adapter._survey_shortfall(thin, snapshot)
    assert claude_adapter._survey_shortfall(full, snapshot) is None
    assert claude_adapter._survey_shortfall(thin, {"snapshotCollections": [
        {"name": "tasks", "totalAvailable": 3, "returned": 3, "omittedFromSnapshot": 0}]}) is None
    # Rule 10's other half: truncation unacknowledged is also under-surveyed.
    assert claude_adapter._survey_shortfall(
        {**full, "notChecked": []}, snapshot) is not None

    calls = {"n": 0}
    original_invoke, original_auth = claude_adapter._invoke, claude_adapter.authenticated
    try:
        claude_adapter.authenticated = lambda: (True, {})

        # Thin, then adequate: the re-ask is what fixes it, and no marker remains.
        def flaky(prompt, schema, **kw):
            calls["n"] += 1
            return (thin if calls["n"] == 1 else full), None
        claude_adapter._invoke = flaky
        report = claude_adapter.coordinator_report("DEV-900021", "live", "req", snapshot, {"probe": "ok"})
        assert calls["n"] == 2, "a thin report must be re-asked"
        assert len(report["items"]) == 12
        assert not any("UNDER-SURVEYED" in n.get("reason", "") for n in report["notChecked"])

        # Thin twice: the report ships, but says so about itself.
        calls["n"] = 0
        claude_adapter._invoke = lambda prompt, schema, **kw: (thin, None)
        report = claude_adapter.coordinator_report("DEV-900022", "live", "req", snapshot, {"probe": "ok"})
        assert calls["n"] == 0  # replaced wholesale; count via notChecked instead
        flagged = [n for n in report["notChecked"] if "UNDER-SURVEYED" in n.get("reason", "")]
        assert flagged, "a persistently thin report must declare its own shortfall"
        assert "establishes nothing" in flagged[0]["reason"]
    finally:
        claude_adapter._invoke, claude_adapter.authenticated = original_invoke, original_auth


def test_model_env_is_independent_of_the_invoking_session():
    """The worker must not behave differently depending on who launched it.

    `CLAUDE_EFFORT` could contradict the effort this adapter passes
    explicitly, and session/messaging ids couple the call to a parent Claude
    Code session. The credential is the sole survivor, and the output-token
    budget is declared rather than inherited: a worker invoked over plain SSH
    carries no such variable, and a truncated structured output fails the
    schema instead of degrading visibly.
    """
    polluted = {
        "CLAUDE_CODE_SESSION_ID": "parent-session",
        "CLAUDE_EFFORT": "high",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000",
        "PATH": "/usr/bin",
        "HOME": "/tmp",
    }
    original = claude_adapter.common.execution_env
    original_cred = claude_adapter._credential_env
    try:
        claude_adapter.common.execution_env = lambda: dict(polluted)
        claude_adapter._credential_env = lambda: (None, None)
        env, defect = claude_adapter._model_env()
        assert defect is None
        assert "CLAUDE_CODE_SESSION_ID" not in env
        assert "CLAUDE_EFFORT" not in env, "an inherited effort could contradict --effort"
        assert "CLAUDECODE" not in env
        assert env["HOME"] == "/tmp", "ordinary variables are untouched"
        # Declared, not inherited: the caller's 1000 must not win.
        assert env[claude_adapter.OUTPUT_TOKEN_ENV] == claude_adapter.MODEL_OUTPUT_TOKEN_BUDGET

        # The credential survives, because it is how the call authenticates.
        claude_adapter._credential_env = lambda: ({**polluted, claude_adapter.TOKEN_ENV: "tok"}, None)
        env, _ = claude_adapter._model_env()
        assert env[claude_adapter.TOKEN_ENV] == "tok"
        assert "CLAUDE_CODE_SESSION_ID" not in env

        # A credential defect still short-circuits.
        claude_adapter._credential_env = lambda: (None, "token file unreadable")
        env, defect = claude_adapter._model_env()
        assert env is None and defect == "token file unreadable"
    finally:
        claude_adapter.common.execution_env = original
        claude_adapter._credential_env = original_cred


# Fixtures where the worker deliberately disagrees with the schema authority.
# Empty, and it should stay that way: the coordination-report-v1 divergences recorded on
# 2026-08-17 were resolved by the authority's ruling of the same day, which made
# probe='failed' a valid honest report rather than a protocol error. An entry
# here must state what the Mini permits, what the worker refuses, and why.
SCHEMA_AUTHORITY_DIVERGENCES: dict[str, str] = {}


def test_agent_session_status_feed_contract_accepts_and_rejects_authority_fixtures():
    fixtures = WORKER_DIR / "schemas" / "fixtures"
    valid = json.loads((fixtures / "valid" / "agent-session-status-v1.working.json").read_text())
    invalid = json.loads(
        (fixtures / "invalid" / "agent-session-status-v1.waiting-without-decision.json").read_text()
    )

    schema_name = "canonical/agent-session-status-v1.json"
    assert schema_check.validate(valid, schema_name) == []
    assert schema_check.validate(invalid, schema_name)
    assert valid["lastEvent"]["sequence"] == valid["sequence"]
    assert valid["budgetSpent"].keys() == {"elapsedSeconds", "turns"}


def test_schema_registry_admission_rejects_malformed_registry(tmp_path, monkeypatch):
    bad_registry = json.loads(common.SCHEMA_REGISTRY_PATH.read_text())
    del bad_registry["producer"]
    registry_path = tmp_path / "schema-registry.json"
    registry_path.write_text(json.dumps(bad_registry))
    monkeypatch.setattr(common, "SCHEMA_REGISTRY_PATH", registry_path)

    try:
        common.load_schema_registry()
        raise AssertionError("malformed schema registry must not be admitted")
    except common.SchemaValidationError as exc:
        assert exc.report["schemaId"] == "schema-registry-v1"
        assert exc.report["validationPoint"] == "registry-admission"
        assert exc.report["artifactKind"] == "schema-registry"
        assert "$.producer" in exc.report["failingJsonPaths"]
        assert re.fullmatch(r"[a-f0-9]{64}", exc.report["schemaRegistryHash"])
        assert schema_check.validate(exc.report, "canonical/schema-validation-error-v1.json") == []


@pytest.mark.parametrize(
    ("schema_file", "config_file", "defect"),
    [
        ("user-registry-v1.json", "users.json", "empty-users"),
        ("organization-registry-v1.json", "organizations.json", "capabilities-not-an-array"),
        ("auth-policy-v1.json", "auth-policy.json", "unknown-identity-boundary"),
    ],
)
def test_configuration_schemas_accept_shipped_config_and_reject_known_defects(
    schema_file, config_file, defect
):
    payload = json.loads((WORKER_DIR / "config" / config_file).read_text())
    invalid = json.loads(json.dumps(payload))

    if defect == "empty-users":
        invalid["users"] = []
    elif defect == "capabilities-not-an-array":
        invalid["organizations"][0]["domainCapabilityKeys"] = "DC13"
    else:
        invalid["identityBoundary"]["mode"] = "unverified-remote"

    schema_name = f"canonical/{schema_file}"
    assert schema_check.validate(payload, schema_name) == []
    assert schema_check.validate(invalid, schema_name)


@pytest.mark.parametrize(
    ("writer_name", "normalizer_name", "config_file", "path_name", "schema_file", "defect"),
    [
        ("save_user_registry", "normalize_user_registry", "users.json", "USER_REGISTRY_PATH", "user-registry-v1.json", "empty-users"),
        ("save_organization_registry", "normalize_organization_registry", "organizations.json", "ORGANIZATION_REGISTRY_PATH", "organization-registry-v1.json", "empty-organizations"),
        ("save_auth_policy", "normalize_auth_policy", "auth-policy.json", "AUTH_POLICY_PATH", "auth-policy-v1.json", "unknown-boundary"),
    ],
)
def test_configuration_writers_schema_gate_before_replacing_files(
    tmp_path, writer_name, normalizer_name, config_file, path_name, schema_file, defect
):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    submitted = json.loads((WORKER_DIR / "config" / config_file).read_text())
    invalid = json.loads(json.dumps(submitted))
    if defect == "empty-users":
        invalid["users"] = []
    elif defect == "empty-organizations":
        invalid["organizations"] = []
    else:
        invalid["identityBoundary"]["mode"] = "unverified-remote"

    target = tmp_path / config_file
    target.write_text(json.dumps(submitted, sort_keys=True))
    before = target.read_bytes()
    ledger = tmp_path / f"{defect}-mutations.jsonl"
    globals_ = chat[writer_name].__globals__
    globals_[path_name] = target
    globals_["MUTATION_LEDGER_PATH"] = ledger
    globals_[normalizer_name] = lambda _payload: invalid

    with pytest.raises(ValueError, match="schema"):
        chat[writer_name](submitted, "owner")

    assert target.read_bytes() == before
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["status"] == "rejected"
    assert events[0]["changed"] is False
    assert events[0]["details"]["schemaFile"] == schema_file
    assert events[0]["details"]["errors"]


def test_schema_registry_is_canonical_and_covers_registered_artifact_writes():
    registry = common.load_schema_registry()
    assert schema_check.validate(registry, "canonical/schema-registry-v1.json") == []
    schema_files = {path.name for path in (WORKER_DIR / "schemas" / "canonical").glob("*.json")}
    registered_files = {entry["schemaFile"] for entry in registry["schemas"]}
    assert registered_files == schema_files
    assert "schema-registry-v1.json" in registered_files

    referenced = set()
    for path in [
        WORKER_DIR / "adapters" / "common.py",
        WORKER_DIR / "bin" / "present-worker",
        WORKER_DIR / "bin" / "present-lease-broker",
        WORKER_DIR / "tests" / "test_worker.py",
    ]:
        text = path.read_text()
        referenced.update(re.findall(r"canonical/([a-z0-9-]+-v[0-9]+\\.json)", text))
    assert referenced <= registered_files
    by_id = {entry["id"]: entry for entry in registry["schemas"]}
    assert len(by_id) == len(registry["schemas"])
    assert "model-role-registry-v1" in by_id
    assert "runtime-profile-registry-v1" in by_id
    assert "runtime-profile-resolution-v1" in by_id

    configuration_schemas = {
        "user-registry-v1": "user-registry-v1.json",
        "organization-registry-v1": "organization-registry-v1.json",
        "auth-policy-v1": "auth-policy-v1.json",
    }
    for schema_id, schema_file in configuration_schemas.items():
        assert by_id[schema_id]["schemaFile"] == schema_file
        assert by_id[schema_id]["owner"] == "schema-authority"
    assert by_id["domain-capability-registry-v1"]["validationPoints"] == [
        "api-projection-validation"
    ]

    by_family = {}
    for entry in registry["schemas"]:
        assert entry["lifecycle"] in {"active", "deprecated", "retired"}
        assert entry["compatibility"] in {"initial", "patch", "minor", "breaking"}
        assert entry["introducedAt"]
        if entry["lifecycle"] == "active":
            assert "deprecatedAt" not in entry
            assert "removalNotBefore" not in entry
            assert "replacedBy" not in entry
        else:
            assert entry["replacedBy"] in by_id
            assert entry["deprecatedAt"]
        by_family.setdefault((entry["artifactKind"], entry["owner"], entry["producer"]), []).append(entry)

    for family in by_family.values():
        family.sort(key=lambda item: item["version"])
        for index, entry in enumerate(family):
            if index == 0:
                continue
            assert entry["compatibility"] != "initial"
            assert entry.get("replacementFor") in {item["id"] for item in family[:index]}
            assert entry.get("changeSummary")

    deprecated_without_replacement = json.loads(json.dumps(registry))
    deprecated_without_replacement["schemas"][0]["lifecycle"] = "deprecated"
    deprecated_without_replacement["schemas"][0]["deprecatedAt"] = "2026-08-18"
    assert schema_check.validate(deprecated_without_replacement, "canonical/schema-registry-v1.json")

    stage_to_schema = {}
    for entry in registry["schemas"]:
        for stage in entry.get("artifactStages", []):
            assert stage not in stage_to_schema
            stage_to_schema[stage] = entry["schemaFile"]
    for stage in [
        "plan",
        "build",
        "review",
        "fix",
        "adversarial",
        "coordination-report",
        "workflow-admission",
        "workflow",
        "evidence-manifest",
        "redaction-report",
        "handoff-package",
        "worker-lease",
        "workflow-cancel",
        "context-checkpoint",
        "task-cleanup",
    ]:
        assert common.schema_name_for_task_artifact(stage) == f"canonical/{stage_to_schema[stage]}"


def test_worker_validator_agrees_with_the_schema_authority():
    """Every Mini fixture must be judged as the Mini judges it.

    The fixtures are the authority's own conformance bar, copied verbatim.
    Valid ones must be accepted and invalid ones rejected -- the invalid set
    encodes the invariants that matter most, including a mock claiming PASS
    and mock evidence marked gate-eligible. A subset validator that quietly
    accepts those makes the worker's conformance illusory.
    """
    fixtures = WORKER_DIR / "schemas" / "fixtures"
    checked = 0
    for kind in ("valid", "invalid"):
        for path in sorted((fixtures / kind).glob("*.json")):
            key = f"{kind}/{path.name}"
            stem = path.name.split(".")[0]
            errors = schema_check.validate(json.loads(path.read_text()), f"canonical/{stem}.json")
            accepted = not errors
            if key in SCHEMA_AUTHORITY_DIVERGENCES:
                assert kind == "valid" and not accepted, (
                    f"{key} is recorded as a divergence but now agrees with the authority; "
                    f"remove it from SCHEMA_AUTHORITY_DIVERGENCES")
                continue
            checked += 1
            assert accepted == (kind == "valid"), (
                f"{key}: authority says {kind}, worker "
                f"{'accepted' if accepted else f'rejected ({errors[0]})'}")
    assert checked >= 24, f"only {checked} fixtures checked; did the fixture set go missing?"


def test_lease_broker_schema_gate_covers_registry_and_golden_fixtures():
    code, payload, stderr_payload, stdout, stderr = run_broker_result("schema-gate")
    assert code == 0, stderr
    assert stderr_payload == {}
    assert payload["status"] == "SCHEMA_GATE_COMPLETE"
    assert payload["ok"] is True
    assert payload["missingRegistryEntries"] == []
    assert payload["fixtureFailures"] == []
    assert payload["schemaRegistryHash"] == common.schema_registry_hash()
    assert schema_check.validate(payload, "canonical/broker-response-refresh-v1.json") == []


def test_ref_backed_schema_sections_are_actually_validated():
    """A `$ref` must not mean 'validate nothing'.

    task-contract-v2 declares `build` as `{"$ref": "#/$defs/buildCommands"}`.
    Before $ref resolution the whole build section went unchecked, so a shell
    string in place of an argv array passed validation -- for commands that run
    with an 86400s timeout.
    """
    shell_string = json.loads(
        (WORKER_DIR / "schemas" / "fixtures" / "invalid" / "task-contract-v2.shell-string.json").read_text())
    errors = schema_check.validate(shell_string, "canonical/task-contract-v2.json")
    assert errors, "a shell string in place of an argv array must be rejected"
    assert any("build" in e for e in errors), f"the fault is in build, reported: {errors}"

    # An unresolvable ref is reported, never silently treated as valid.
    assert schema_check._resolve({"$ref": "#/$defs/nope"}, {"$defs": {}}, "$", errs := []) is None
    assert errs and "does not resolve" in errs[0]
    assert schema_check._resolve({"$ref": "https://example/x"}, {}, "$", errs2 := []) is None
    assert errs2 and "non-local" in errs2[0]


def test_coordinator_report_budget_is_caller_supplied():
    """The caller's deadline must govern the model call.

    The chat server killed jobs at 180s while the adapter believed it had 600s,
    so the outer kill always won and the worker was SIGTERMed with nothing
    recorded. One budget, threaded from the caller, prevents that inversion.
    """
    captured: dict = {}

    original = claude_adapter._invoke
    original_auth = claude_adapter.authenticated
    try:
        claude_adapter.authenticated = lambda: (True, {})

        def fake_invoke(prompt, schema, **kwargs):
            captured.update(kwargs)
            return {"summary": "s", "items": [], "notChecked": [],
                    "contradictions": []}, None

        claude_adapter._invoke = fake_invoke
        claude_adapter.coordinator_report(
            "DEV-900007", "live", "requirement", {}, {"probe": "ok"},
            timeout=42, model="claude-opus-5", effort="high",
        )
        # A deadline, not a fresh grant per attempt: the budget is shared with
        # the survey re-ask, so an attempt gets what remains of it and can
        # never exceed it.
        assert 40 <= captured["timeout"] <= 42
        # Effort is pinned: `low` returned an empty "placeholder" stub claiming
        # nothing was unchecked, which the role canon forbids.
        assert captured["effort"] == "high"
        assert captured["model"] == "claude-opus-5"
    finally:
        claude_adapter._invoke = original
        claude_adapter.authenticated = original_auth


def test_waiting_page_shows_progress_and_survives_a_bare_job():
    """The wait must be legible, and never break on a missing nicety."""
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    assert chat["format_elapsed"](7) == "7s"
    assert chat["format_elapsed"](165) == "2m 45s"

    full = chat["render_running"]("JOB1", {
        "startedEpoch": time.time() - 42, "taskId": "DEV-900019",
        "question": "report on DEV-999996",
        "scope": [{"name": "tasks", "returned": 30, "totalAvailable": 47}],
    })
    assert "42s" in full and "DEV-900019" in full
    assert "report on DEV-999996" in full
    assert "30 of 47 tasks" in full
    assert 'http-equiv="refresh"' in full  # still self-updating
    globals_ = chat["render_running"].__globals__
    original = globals_["read_progress"]
    try:
        globals_["read_progress"] = lambda task_id: {
            "phase": "Reconciling 30 of 47 tasks",
            "thinkingTokens": 100,
            "timeline": [
                {"elapsedSeconds": 0.1, "label": "Snapshot ready", "detail": "Prepared 30 of 47 tasks"},
                {"elapsedSeconds": 0.2, "label": "Context checkpoint", "checkpointId": "cc-abc123abc123abc123abc123",
                 "checkpointPath": "/tmp/context-checkpoint.json"},
                {"elapsedSeconds": 1.2, "label": "Thinking started", "detail": "Model began reconciling"},
            ],
        }
        rich = chat["render_running"]("JOB3", {
            "startedEpoch": time.time() - 9, "taskId": "DEV-900019", "question": "report",
        })
        assert "progress-timeline" in rich
        assert "Snapshot ready" in rich
        assert "cc-abc123abc123abc123abc123" in rich
        assert "Thinking started" in rich
    finally:
        globals_["read_progress"] = original

    # A job dict carrying none of the optional display fields still renders.
    bare = chat["render_running"]("JOB2", {"state": "running"})
    assert "JOB2" in bare and "elapsed" in bare

    # The scope preview is a preview: its failure must not break asking.
    assert chat["snapshot_scope"]() is not None


def test_main_chat_index_script_is_parseable(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    html = chat["chat_index"]()
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    script = tmp_path / "steel-mission-index-script.js"
    script.write_text(scripts[0])
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_chat_index_keeps_its_patch_seam_and_wraps_the_application_renderer():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["chat_index"].__globals__
    globals_["application_chat_index"] = lambda: "application-page"

    assert chat["chat_index"]() == "application-page"


def test_steel_mission_page_routes_cover_work_settings_and_missions():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    assert chat["is_page_path"]("/")
    assert chat["is_page_path"]("/index.html")
    assert not chat["is_page_path"]("/owner")
    assert chat["is_legacy_page_path"]("/owner")
    assert chat["is_legacy_page_path"]("/admin/settings")
    assert chat["is_legacy_page_path"]("/publisher/missions")
    assert chat["is_legacy_page_path"]("/user/missions/ms-" + "5" * 24)
    assert not chat["is_page_path"]("/api/admin/settings")


def test_steel_mission_knowledge_registry_loads_foundations_and_project_roles():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    payload = chat["knowledge_registry"]()

    assert payload["ok"] is True
    assert {item["role_key"] for item in payload["foundations"]} >= {"KD01", "KD02", "KD03"}
    assert {item["domainKey"] for item in payload["knowledgeDomains"]} >= {"KD01", "KD02", "KD03"}
    roles = {item["roleKey"]: item for item in payload["roles"]}
    capabilities = {item["capabilityKey"]: item for item in payload["capabilities"]}
    expected_capabilities = {f"DC{i:02d}" for i in range(1, 14)}
    assert set(roles) >= expected_capabilities
    assert set(capabilities) >= expected_capabilities
    assert all(roles[key]["sourceCount"] >= 1 for key in expected_capabilities)
    assert roles["DC01"]["displayName"] == "Counterweight"
    assert roles["DC12"]["displayName"] == "Synthesis"
    assert roles["DC13"]["currentFNumber"] == "DC13"
    assert roles["DC13"]["displayName"] == "Delivery Coordinator"
    assert capabilities["DC13"]["fNumber"] == "DC13"
    assert capabilities["DC13"]["displayName"] == "Delivery Coordinator"
    assert payload["activeOrganization"]["id"] == "northstar-forge"
    assert set(payload["activeOrganization"]["knowledgeDomainKeys"]) >= {"KD01", "KD02", "KD03"}
    assert set(payload["activeOrganization"]["domainCapabilityKeys"]) >= expected_capabilities


def test_every_shipped_domain_capability_has_an_active_assignee_or_recorded_reason():
    """An ownership gap must be a visible product decision, never an omission."""
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    capability_keys = {
        item["capabilityKey"] for item in chat["knowledge_registry"]()["capabilities"]
    }
    active_assignees = {
        capability
        for user in chat["user_registry"]()["users"]
        if user["status"] == "active"
        for capability in user["assignedCapabilities"]
    }

    # Keep any deliberate exceptions here with a non-empty rationale. The
    # shipped starter organization currently chooses to assign every capability.
    deliberately_unowned: dict[str, str] = {}
    assert all(reason.strip() for reason in deliberately_unowned.values())
    unresolved = capability_keys - active_assignees - deliberately_unowned.keys()
    assert not unresolved, (
        "domain capabilities have neither an active assignee nor a recorded "
        f"reason for remaining unowned: {sorted(unresolved)}"
    )


def test_steel_mission_vocabulary_endpoint_matches_knowledge_registry():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    payload = chat["ui_vocabulary"]()

    assert schema_check.validate(payload, "canonical/ui-vocabulary-v1.json") == []
    assert [term["label"] for term in payload["terms"]] == [
        "Domain Capability",
        "Knowledge Domain",
        "Access level",
        "Coordinator model",
        "Snapshot policy",
        "Work mode",
    ]
    assert {
        term["conceptKey"]: term["wireNames"] for term in payload["terms"]
    } == {
        "domain-capability": ["roleKey", "capabilityKey", "assignedCapabilities"],
        "knowledge-domain": ["knowledgeDomainKeys"],
        "access-level": ["role", "operatorRole"],
        "coordinator-model": ["runtimeProfile", "STEEL_MISSION_RUNTIME_PROFILE"],
        "snapshot-policy": ["snapshotProfile"],
        "work-mode": ["workMode"],
    }
    expected_capabilities = {
        item["capabilityKey"]: item["displayName"]
        for item in chat["knowledge_registry"]()["capabilities"]
    }
    served_capabilities = {
        item["capabilityKey"]: item["displayName"]
        for item in payload["capabilities"]
    }
    assert served_capabilities == expected_capabilities
    assert len(payload["capabilities"]) == len(served_capabilities)
    assert set(served_capabilities) == {f"DC{i:02d}" for i in range(1, 14)}

    responses = []
    globals_ = chat["Handler"].do_GET.__globals__
    globals_["json_response"] = lambda _handler, status, response: responses.append(
        (status, response)
    )
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/vocabulary"
    handler.authenticate = lambda _path, _method: {"actorId": "user", "role": "user"}
    handler.do_GET()

    assert responses == [(200, payload)]


def test_steel_mission_assignment_projection_writes_through_user_registry(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    user_registry = tmp_path / "users.json"
    legacy_assignments = tmp_path / "domain-capabilities.json"
    user_registry.write_text((WORKER_DIR / "config" / "users.json").read_text())
    globals_ = chat["save_domain_capability_registry"].__globals__
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["DOMAIN_CAPABILITIES_PATH"] = legacy_assignments
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    submitted = chat["domain_capability_registry"]()
    next(item for item in submitted["assignments"] if item["roleKey"] == "DC03")[
        "publishers"
    ] = []

    saved = chat["save_domain_capability_registry"](submitted, "owner")

    persisted_users = json.loads(user_registry.read_text())["users"]
    publisher = next(item for item in persisted_users if item["id"] == "avery-stone")
    assert "DC03" not in publisher["assignedCapabilities"]
    assert "DC04" in publisher["assignedCapabilities"]
    assert not legacy_assignments.exists()
    by_key = {item["roleKey"]: item for item in saved["assignments"]}
    assert by_key["DC03"]["publishers"] == []
    assert by_key["DC04"]["publishers"] == ["avery-stone"]
    assert by_key["DC04"]["users"] == ["jordan-lee"]
    assert schema_check.validate(saved, "canonical/domain-capability-registry-v1.json") == []


def test_steel_mission_capability_registry_endpoint_authorization_and_round_trip(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    legacy_assignments = tmp_path / "domain-capabilities.json"
    user_registry = tmp_path / "users.json"
    user_registry.write_text((WORKER_DIR / "config" / "users.json").read_text())
    globals_ = chat["Handler"].do_POST.__globals__
    globals_["DOMAIN_CAPABILITIES_PATH"] = legacy_assignments
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    shipped = chat["domain_capability_registry"]()

    def post(payload, actor):
        responses = []
        globals_["read_json"] = lambda _handler: payload
        globals_["json_response"] = lambda _handler, status, response: responses.append(
            (status, response)
        )
        handler = object.__new__(chat["Handler"])
        handler.path = "/api/owner/assignments"
        handler.authenticate = lambda _path, _method: actor
        handler.do_POST()
        return responses[0]

    before = user_registry.read_bytes()
    status, refusal = post(shipped, {"actorId": "avery-stone", "role": "publisher"})
    assert status == 403
    assert refusal["ok"] is False
    assert user_registry.read_bytes() == before

    submitted = json.loads(json.dumps(shipped))
    next(item for item in submitted["assignments"] if item["roleKey"] == "DC01")[
        "publishers"
    ].append("avery-stone")
    status, saved = post(submitted, {"actorId": "morgan-vale", "role": "owner"})
    assert status == 200
    assert saved["ok"] is True

    responses = []
    globals_["json_response"] = lambda _handler, status, response: responses.append(
        (status, response)
    )
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/owner/assignments"
    handler.authenticate = lambda _path, _method: {"actorId": "morgan-vale", "role": "owner"}
    handler.do_GET()
    status, reloaded = responses[0]
    by_key = {item["roleKey"]: item for item in reloaded["assignments"]}
    assert status == 200
    assert by_key["DC01"]["publishers"] == ["avery-stone"]

    accepted = user_registry.read_bytes()
    invalid = json.loads(json.dumps(submitted))
    next(item for item in invalid["assignments"] if item["roleKey"] == "DC02")["users"].append(
        "unknown-user"
    )
    status, refusal = post(invalid, {"actorId": "morgan-vale", "role": "owner"})
    assert status == 400
    assert "unknown-user" in refusal["error"]
    assert user_registry.read_bytes() == accepted
    assert not legacy_assignments.exists()


def test_steel_mission_legacy_capability_registry_is_not_an_assignment_authority(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    legacy_assignments = tmp_path / "domain-capabilities.json"
    user_registry = tmp_path / "users.json"
    mutation_ledger = tmp_path / "mutation-ledger.jsonl"
    legacy_assignments.write_text('{"schemaVersion": 1, "assignments": [')
    user_registry.write_text((WORKER_DIR / "config" / "users.json").read_text())

    globals_ = chat["domain_capability_registry"].__globals__
    globals_["DOMAIN_CAPABILITIES_PATH"] = legacy_assignments
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["MUTATION_LEDGER_PATH"] = mutation_ledger

    projection = chat["domain_capability_registry"]()

    by_key = {item["roleKey"]: item for item in projection["assignments"]}
    assert by_key["DC13"]["publishers"] == ["avery-stone"]
    assert by_key["DC13"]["users"] == ["jordan-lee"]
    assert legacy_assignments.read_text() == '{"schemaVersion": 1, "assignments": ['
    assert not mutation_ledger.exists()


def test_steel_mission_capability_assignments_refuse_unknown_users_without_writing(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    legacy_assignments = tmp_path / "domain-capabilities.json"
    user_registry = tmp_path / "users.json"
    user_registry.write_text((WORKER_DIR / "config" / "users.json").read_text())
    globals_ = chat["Handler"].do_POST.__globals__
    globals_["DOMAIN_CAPABILITIES_PATH"] = legacy_assignments
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    shipped = chat["domain_capability_registry"]()
    submitted = json.loads(json.dumps(shipped))
    next(item for item in submitted["assignments"] if item["roleKey"] == "DC03")["publishers"].append(
        "unknown-publisher"
    )
    next(item for item in submitted["assignments"] if item["roleKey"] == "DC04")["users"].append(
        "unknown-user"
    )
    before = user_registry.read_bytes()

    responses = []
    globals_["read_json"] = lambda _handler: submitted
    globals_["json_response"] = lambda _handler, status, payload: responses.append((status, payload))
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/owner/assignments"
    handler.authenticate = lambda _path, _method: {"actorId": "owner", "role": "owner"}
    handler.do_POST()

    assert responses[0][0] == 400
    assert responses[0][1]["ok"] is False
    assert "unknown-publisher" in responses[0][1]["error"]
    assert "unknown-user" in responses[0][1]["error"]
    assert user_registry.read_bytes() == before
    assert not legacy_assignments.exists()


def test_steel_mission_capability_assignments_refuse_an_empty_post_without_writing(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    legacy_assignments = tmp_path / "domain-capabilities.json"
    user_registry = tmp_path / "users.json"
    user_registry.write_text((WORKER_DIR / "config" / "users.json").read_text())
    before = user_registry.read_bytes()

    globals_ = chat["Handler"].do_POST.__globals__
    globals_["DOMAIN_CAPABILITIES_PATH"] = legacy_assignments
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    responses = []
    globals_["read_json"] = lambda _handler: {}
    globals_["json_response"] = lambda _handler, status, payload: responses.append((status, payload))
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/owner/assignments"
    handler.authenticate = lambda _path, _method: {"actorId": "owner", "role": "owner"}
    handler.do_POST()

    assert responses[0][0] == 400
    assert responses[0][1]["ok"] is False
    assert "assignments" in responses[0][1]["error"]
    assert user_registry.read_bytes() == before
    assert not legacy_assignments.exists()


@pytest.mark.parametrize("identity_override", [None, "oidc-required"])
def test_steel_mission_capability_binding_authorization_is_identity_mode_independent(
    monkeypatch, identity_override
):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["authorize_mission_bindings"].__globals__
    globals_["AUTH_POLICY_PATH"] = WORKER_DIR / "config" / "auth-policy.json"
    if identity_override is None:
        monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
        assert chat["identity_mode"]() == "development-local"
    else:
        monkeypatch.setenv("PRESENT_IDENTITY_MODE", identity_override)
        assert chat["identity_mode"]() == identity_override

    publisher = {
        "actorId": "avery-stone",
        "role": "publisher",
        "capabilities": ["DC13"],
    }
    with pytest.raises(PermissionError, match="not assigned requested capabilities: DC03"):
        chat["authorize_mission_bindings"](publisher, [], ["DC03", "DC13"])

    chat["authorize_mission_bindings"](publisher, [], ["DC13"])
    chat["authorize_mission_bindings"]({"role": "admin", "capabilities": []}, [], ["DC03"])


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [("permission", 403), ("validation", 400)],
)
def test_steel_mission_admin_endpoints_share_failure_status_mapping(failure, expected_status):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["Handler"].do_POST.__globals__
    endpoints = [
        "/api/owner/assignments",
        "/api/owner/knowledge",
        "/api/owner/organizations",
        "/api/owner/knowledge/upload",
        "/api/owner/users",
        "/api/owner/control-policy",
        "/api/owner/integrations",
        "/api/owner/auth-policy",
        "/api/runtime-profiles/validate",
        "/api/runtime-profiles/save",
        "/api/runtime-profiles/clone",
        "/api/model-roles/save",
        "/api/model-roles/delete",
    ]

    for endpoint in endpoints:
        responses = []
        globals_["json_response"] = lambda _handler, status, payload: responses.append(
            (status, payload)
        )
        if failure == "validation":
            def invalid_payload(*_args, **_kwargs):
                raise ValueError("same invalid payload")

            globals_["read_json"] = invalid_payload
            actor = {"actorId": "owner", "role": "owner"}
        else:
            globals_["read_json"] = lambda *_args, **_kwargs: {}
            actor = {"actorId": "publisher", "role": "publisher"}
        handler = object.__new__(chat["Handler"])
        handler.path = endpoint
        handler.authenticate = lambda _path, _method: actor

        handler.do_POST()

        assert responses == [
            (
                expected_status,
                {
                    "ok": False,
                    "error": "same invalid payload"
                    if failure == "validation"
                    else "actor is not allowed to perform this action",
                },
            )
        ], endpoint


def _post_user_registry_payload(tmp_path, submitted):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    user_registry = tmp_path / "users.json"
    user_registry.write_text((WORKER_DIR / "config" / "users.json").read_text())
    before = user_registry.read_bytes()

    globals_ = chat["Handler"].do_POST.__globals__
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    responses = []
    globals_["read_json"] = lambda _handler: submitted
    globals_["json_response"] = lambda _handler, status, payload: responses.append((status, payload))
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/owner/users"
    handler.authenticate = lambda _path, _method: {"actorId": "owner", "role": "owner"}
    handler.do_POST()
    return responses[0], before, user_registry.read_bytes()


def test_steel_mission_user_registry_refuses_an_empty_user_list(tmp_path):
    (status, response), before, after = _post_user_registry_payload(tmp_path, {"users": []})

    assert status == 400
    assert response["ok"] is False
    assert "users" in response["error"]
    assert before == after


def test_steel_mission_user_registry_refuses_an_unsupported_status(tmp_path):
    submitted = json.loads((WORKER_DIR / "config" / "users.json").read_text())
    submitted["users"][0]["status"] = "suspended"
    (status, response), before, after = _post_user_registry_payload(tmp_path, submitted)

    assert status == 400
    assert response["ok"] is False
    assert "suspended" in response["error"]
    assert before == after


def test_steel_mission_user_registry_names_an_unknown_capability_key(tmp_path):
    submitted = json.loads((WORKER_DIR / "config" / "users.json").read_text())
    submitted["users"][0]["assignedCapabilities"].append("DC99")
    (status, response), before, after = _post_user_registry_payload(tmp_path, submitted)

    assert status == 400
    assert response["ok"] is False
    assert "DC99" in response["error"]
    assert before == after


def test_steel_mission_organization_registry_preserves_explicit_empty_scopes(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    organization_registry = tmp_path / "organizations.json"
    submitted = json.loads((WORKER_DIR / "config" / "organizations.json").read_text())
    submitted["organizations"][0]["knowledgeDomainKeys"] = []
    submitted["organizations"][0]["domainCapabilityKeys"] = []
    organization_registry.write_text((WORKER_DIR / "config" / "organizations.json").read_text())

    globals_ = chat["Handler"].do_POST.__globals__
    globals_["ORGANIZATION_REGISTRY_PATH"] = organization_registry
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    responses = []
    globals_["read_json"] = lambda _handler: submitted
    globals_["json_response"] = lambda _handler, status, payload: responses.append((status, payload))
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/owner/organizations"
    handler.authenticate = lambda _path, _method: {"actorId": "owner", "role": "owner"}
    handler.do_POST()

    assert responses[0][0] == 200
    organization = responses[0][1]["payload"]["organizations"][0]
    assert organization["domainCapabilityKeys"] == []
    assert organization["knowledgeDomainKeys"] == []
    persisted = json.loads(organization_registry.read_text())["organizations"][0]
    assert persisted["domainCapabilityKeys"] == []
    assert persisted["knowledgeDomainKeys"] == []


def test_steel_mission_refuses_unknown_worktree_mode_before_starting_delivery(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    repository = tmp_path / "repo"
    repository.mkdir()
    marker = repository / "unchanged.txt"
    marker.write_text("source checkout")
    started = []
    responses = []
    request = {
        "templateId": "delivery-execution",
        "objective": "Keep the source checkout isolated.",
        "delivery": {
            "repositoryPath": str(repository),
            "worktreeMode": "sandboxed",
        },
    }

    globals_ = chat["Handler"].do_POST.__globals__
    globals_["read_json"] = lambda _handler: request
    globals_["json_response"] = lambda _handler, status, payload: responses.append((status, payload))
    globals_["start_orchestrated_mission"] = (
        lambda *args, **kwargs: started.append((args, kwargs)) or {"ok": True}
    )
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/missions/start"
    handler.authenticate = lambda _path, _method: {
        "actorId": "owner",
        "role": "owner",
        "organizationId": "northstar-forge",
    }
    handler.do_POST()

    assert responses[0][0] == 400
    assert responses[0][1]["ok"] is False
    assert "isolated" in responses[0][1]["error"]
    assert "in-place" in responses[0][1]["error"]
    assert started == []
    assert list(repository.iterdir()) == [marker]
    assert marker.read_text() == "source checkout"


def test_steel_mission_user_registry_is_the_only_capability_assignment_authority(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    user_registry = tmp_path / "users.json"
    legacy_assignments = tmp_path / "domain-capabilities.json"
    user_registry.write_text(json.dumps({
        "schemaVersion": 1,
        "users": [
            {
                "id": "pub-a",
                "name": "Publisher A",
                "role": "publisher",
                "status": "active",
                "assignedCapabilities": [],
            }
        ],
    }))
    legacy_assignments.write_text(json.dumps({
        "schemaVersion": 1,
        "assignments": [
            {
                "roleKey": "DC03",
                "fNumber": "DC03",
                "displayName": "Architecture",
                "publishers": ["pub-a"],
                "users": [],
            }
        ],
    }))
    globals_ = chat["corporate_workspace"].__globals__
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["DOMAIN_CAPABILITIES_PATH"] = legacy_assignments

    workspace = chat["corporate_workspace"]("publisher")

    assert workspace["visibleCapabilities"] == []
    assert all(
        not assignment["publishers"] and not assignment["users"]
        for assignment in workspace["assignments"]
    )
    shipped_users = json.loads((WORKER_DIR / "config" / "users.json").read_text())["users"]
    shipped_publisher = next(user for user in shipped_users if user["id"] == "avery-stone")
    assert {"DC01", "DC02", "DC08", "DC12"} <= set(shipped_publisher["assignedCapabilities"])
    assert not (WORKER_DIR / "config" / "domain-capabilities.json").exists()


def test_steel_mission_corporate_workspace_filters_by_user_domain_capability_access(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    role_registry = tmp_path / "role-registry.json"
    knowledge_registry = tmp_path / "role-knowledge-registry.json"
    assignment_registry = tmp_path / "domain-capabilities.json"
    user_registry = tmp_path / "users.json"
    role_registry.write_text(json.dumps({
        "epoch": "test",
        "foundations": [
            {"role_key": "KD01", "display_name": "Operating Context"},
            {"role_key": "KD02", "display_name": "Core Team Operating Doctrine"},
            {"role_key": "KD03", "display_name": "Core Team Roster"},
        ],
        "roles": [
            {"role_key": "DC03", "current_f_number": "DC03", "display_name": "Architecture", "canon_path": "DC03.md"},
            {"role_key": "DC11", "current_f_number": "DC12", "display_name": "Operations", "canon_path": "DC12.md"},
            {"role_key": "DC13", "current_f_number": "DC13", "display_name": "Delivery Coordinator", "canon_path": "DC13.md"},
        ],
    }))
    knowledge_registry.write_text(json.dumps({
        "revision": "test",
        "rule": "fixture",
        "roles": {
            "DC03": {"domainSources": [{"path": "arch.md"}]},
            "DC11": {"domainSources": [{"path": "ops.md"}]},
            "DC13": {"domainSources": [{"path": "cos.md"}]},
        },
    }))
    assignment_registry.write_text(json.dumps({
        "schemaVersion": 1,
        "userAssignments": [
            {"userId": "pub-a", "role": "publisher", "assignedCapabilities": ["DC03", "DC13"]},
            {"userId": "user-a", "role": "user", "assignedCapabilities": ["DC11", "DC13"]},
        ],
    }))
    user_registry.write_text(json.dumps({
        "schemaVersion": 1,
        "users": [
            {"id": "owner", "name": "Owner", "role": "owner", "status": "active", "assignedCapabilities": []},
            {"id": "admin", "name": "Admin", "role": "admin", "status": "active", "assignedCapabilities": []},
            {"id": "pub-a", "name": "Publisher A", "role": "publisher", "status": "active", "assignedCapabilities": ["DC03", "DC13"]},
            {"id": "pub-b", "name": "Publisher B", "role": "publisher", "status": "active", "assignedCapabilities": ["DC11"]},
            {"id": "user-a", "name": "User A", "role": "user", "status": "active", "assignedCapabilities": ["DC11", "DC13"]},
        ],
    }))
    globals_ = chat["corporate_workspace"].__globals__
    globals_["ROLE_REGISTRY_PATH"] = role_registry
    globals_["ROLE_KNOWLEDGE_REGISTRY_PATH"] = knowledge_registry
    globals_["DOMAIN_CAPABILITIES_PATH"] = assignment_registry
    globals_["USER_REGISTRY_PATH"] = user_registry
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"

    publisher = chat["corporate_workspace"]("publisher")
    publisher_a = chat["corporate_workspace"]("publisher", actor={
        "actorId": "pub-a",
        "role": "publisher",
        "capabilities": ["DC03", "DC13"],
    })
    user = chat["corporate_workspace"]("user")
    admin = chat["corporate_workspace"]("admin")

    assert publisher["canAssign"] is False
    assert {item["roleKey"] for item in publisher["visibleRoles"]} == {"DC03", "DC11", "DC13"}
    assert {item["roleKey"] for item in publisher_a["visibleRoles"]} == {"DC03", "DC13"}
    assert {item["roleKey"] for item in user["visibleRoles"]} == {"DC11", "DC13"}
    assert {item["roleKey"] for item in admin["visibleRoles"]} == {"DC03", "DC11", "DC13"}
    assert admin["canAssign"] is True
    assert {item["role_key"] for item in admin["foundations"]} == {"KD01", "KD02", "KD03"}
    assert {item["domainKey"] for item in admin["knowledgeDomains"]} == {"KD01", "KD02", "KD03"}
    assert {item["capabilityKey"] for item in admin["visibleCapabilities"]} == {"DC03", "DC11", "DC13"}

    saved = chat["save_user_registry"]({
        "users": [
            {"id": "pub-b", "name": "Publisher B", "role": "publisher", "status": "active", "assignedCapabilities": ["DC11"]},
            {"id": "user-b", "name": "User B", "role": "user", "status": "active", "assignedCapabilities": ["DC03", "DC13"]},
        ]
    }, "owner")

    assert saved["users"][0]["assignedCapabilities"] == ["DC11"]
    assert saved["users"][0]["assignedCapabilities"] == ["DC11"]
    assert saved["users"][1]["assignedCapabilities"] == ["DC03", "DC13"]
    assert saved["users"][1]["assignedCapabilities"] == ["DC03", "DC13"]
    assert json.loads(user_registry.read_text())["users"] == saved["users"]
    with pytest.raises(ValueError, match="only owner and admin"):
        chat["save_user_registry"]({"users": []}, "publisher")


def test_steel_mission_workspace_route_scopes_the_grant_to_the_authenticated_actor():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["Handler"].do_GET.__globals__
    responses = []
    globals_["corporate_workspace"] = lambda role, actor=None: {
        "ok": True,
        "role": role,
        "actorId": actor.get("actorId") if actor else "",
    }
    globals_["json_response"] = lambda _handler, status, payload: responses.append(
        (status, payload)
    )
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/publisher/workspace"
    handler.authenticate = lambda _path, _method: {
        "actorId": "pub-a",
        "role": "publisher",
        "capabilities": ["DC03"],
    }

    handler.do_GET()

    assert responses == [(200, {"ok": True, "role": "publisher", "actorId": "pub-a"})]


def test_steel_mission_general_knowledge_accepts_repositories_and_documents(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    registry_path = tmp_path / "general-knowledge.json"
    globals_ = chat["general_knowledge_registry"].__globals__
    globals_["GENERAL_KNOWLEDGE_PATH"] = registry_path
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"

    saved = chat["save_general_knowledge_registry"]({
        "repositories": [{"name": "project-alpha", "path": str(tmp_path / "alpha")}],
        "documents": [{"title": "Publisher Handbook", "path": str(tmp_path / "publisher.md")}],
    }, "admin")

    assert saved["repositories"][0]["name"] == "project-alpha"
    assert saved["repositories"][0]["path"] == str(tmp_path / "alpha")
    assert saved["repositories"][0]["exists"] is False
    assert saved["repositories"][0]["sourceKind"] == "repository"
    assert saved["documents"][0]["title"] == "Publisher Handbook"
    assert saved["documents"][0]["path"] == str(tmp_path / "publisher.md")
    assert saved["documents"][0]["exists"] is False
    assert saved["documents"][0]["sourceKind"] == "document"
    assert json.loads(registry_path.read_text())["documents"] == saved["documents"]
    with pytest.raises(ValueError, match="only owner and admin"):
        chat["save_general_knowledge_registry"]({"repositories": [], "documents": []}, "publisher")


def test_steel_mission_organization_registry_manages_identity_sources_and_bindings(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["save_organization_registry"].__globals__
    original_orgs = globals_["ORGANIZATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["ORGANIZATION_REGISTRY_PATH"] = tmp_path / "organizations.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    try:
        saved = chat["save_organization_registry"]({
            "activeOrganizationId": "acme-delivery",
            "organizations": [
                {
                    "id": "Acme Delivery",
                    "name": "Acme Delivery",
                    "slug": "acme-delivery",
                    "identifiers": {
                        "legalName": "Acme Delivery AG",
                        "domain": "acme.example",
                        "country": "CH",
                        "environment": "starter",
                        "dataClassification": "synthetic"
                    },
                    "knowledgeDomainKeys": ["KD01", "KD02", "BAD"],
                    "domainCapabilityKeys": ["DC03", "DC13", "BAD"],
                    "knowledgeSources": {
                        "repositories": [{"name": "acme-product", "path": str(tmp_path / "repo")}],
                        "documents": [{"title": "Acme Handbook", "path": str(tmp_path / "handbook.md")}],
                    },
                    "notes": "Synthetic customer starter."
                }
            ],
        }, "admin")

        organization = saved["organizations"][0]
        assert saved["activeOrganizationId"] == "acme-delivery"
        assert organization["id"] == "acme-delivery"
        assert organization["identifiers"]["legalName"] == "Acme Delivery AG"
        assert organization["knowledgeDomainKeys"] == ["KD01", "KD02"]
        assert organization["domainCapabilityKeys"] == ["DC03", "DC13"]
        assert organization["knowledgeSources"]["repositories"][0]["name"] == "acme-product"
        assert json.loads((tmp_path / "organizations.json").read_text())["organizations"][0]["id"] == "acme-delivery"
        workspace = chat["corporate_workspace"]("admin")
        assert workspace["activeOrganization"]["name"] == "Acme Delivery"
        assert workspace["canManageOrganizations"] is True
        ledger = chat["read_mutation_ledger"]("admin")
        assert ledger["mutations"][0]["action"] == "organizations-saved"
        with pytest.raises(ValueError, match="only owner and admin"):
            chat["save_organization_registry"]({"organizations": []}, "publisher")
    finally:
        globals_["ORGANIZATION_REGISTRY_PATH"] = original_orgs
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_org_knowledge_upload_stores_files_and_starts_prepare_snapshot(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["upload_organization_knowledge"].__globals__
    original_general = globals_["GENERAL_KNOWLEDGE_PATH"]
    original_orgs = globals_["ORGANIZATION_REGISTRY_PATH"]
    original_upload_root = globals_["ORG_KNOWLEDGE_UPLOAD_ROOT"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    original_starter = globals_["start_orchestrated_mission"]
    started: dict[str, object] = {}

    def fake_start_orchestrated_mission(template_id, objective, **kwargs):
        started.update({"templateId": template_id, "objective": objective, **kwargs})
        return {"ok": True, "missionId": "ms-" + "8" * 24, "jobId": "JOB-knowledge", "taskId": "DEV-900188"}

    globals_["GENERAL_KNOWLEDGE_PATH"] = tmp_path / "general-knowledge.json"
    globals_["ORGANIZATION_REGISTRY_PATH"] = tmp_path / "organizations.json"
    globals_["ORGANIZATION_REGISTRY_PATH"].write_text(json.dumps({
        "schemaVersion": 1,
        "activeOrganizationId": "acme",
        "organizations": [{"id": "acme", "name": "Acme", "knowledgeDomainKeys": ["KD01"], "domainCapabilityKeys": ["DC13"], "knowledgeSources": {"repositories": [], "documents": []}}],
    }))
    globals_["ORG_KNOWLEDGE_UPLOAD_ROOT"] = tmp_path / "org-knowledge-uploads"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    globals_["start_orchestrated_mission"] = fake_start_orchestrated_mission
    try:
        payload = {
            "label": "Project Alpha",
            "sourceKind": "folder",
            "profile": "dc13.alpha",
            "files": [
                {
                    "name": "guide.md",
                    "relativePath": "docs/guide.md",
                    "type": "text/markdown",
                    "contentBase64": base64.b64encode(b"Use this project as org knowledge.").decode(),
                }
            ],
        }
        result = chat["upload_organization_knowledge"](payload, "owner")

        stored_root = Path(result["uploadRoot"])
        assert result["fileCount"] == 1
        assert (stored_root / "docs" / "guide.md").read_text() == "Use this project as org knowledge."
        assert result["registry"]["repositories"][0]["name"] == "Project-Alpha"
        assert result["registry"]["repositories"][0]["path"] == str(stored_root)
        assert result["organizationId"] == "acme"
        assert result["organizationRegistry"]["organizations"][0]["knowledgeSources"]["repositories"][0]["path"] == str(stored_root)
        assert started["templateId"] == "prepare-knowledge"
        assert started["profile"] == "dc13.alpha"
        assert "Project Alpha" in started["objective"]
        assert result["mission"]["missionId"] == "ms-" + "8" * 24
        with pytest.raises(ValueError, match="only owner and admin"):
            chat["upload_organization_knowledge"](payload, "publisher")
    finally:
        globals_["GENERAL_KNOWLEDGE_PATH"] = original_general
        globals_["ORGANIZATION_REGISTRY_PATH"] = original_orgs
        globals_["ORG_KNOWLEDGE_UPLOAD_ROOT"] = original_upload_root
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        globals_["start_orchestrated_mission"] = original_starter


def test_steel_mission_prepare_knowledge_snapshot_payload_records_source_health(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["prepare_knowledge_snapshot_payload"].__globals__
    original_general = globals_["GENERAL_KNOWLEDGE_PATH"]
    original_orgs = globals_["ORGANIZATION_REGISTRY_PATH"]
    repo = tmp_path / "repo"
    org_repo = tmp_path / "org-repo"
    repo.mkdir()
    org_repo.mkdir()
    doc = tmp_path / "handbook.md"
    org_doc = tmp_path / "org-handbook.md"
    (repo / "guide.md").write_text("repo knowledge\n")
    (org_repo / "guide.md").write_text("organization repo knowledge\n")
    doc.write_text("document knowledge\n")
    org_doc.write_text("organization document knowledge\n")
    globals_["GENERAL_KNOWLEDGE_PATH"] = tmp_path / "general-knowledge.json"
    globals_["GENERAL_KNOWLEDGE_PATH"].write_text(json.dumps({
        "repositories": [{"name": "alpha", "path": str(repo)}],
        "documents": [{"title": "Handbook", "path": str(doc)}],
    }))
    globals_["ORGANIZATION_REGISTRY_PATH"] = tmp_path / "organizations.json"
    globals_["ORGANIZATION_REGISTRY_PATH"].write_text(json.dumps({
        "schemaVersion": 1,
        "activeOrganizationId": "acme",
        "organizations": [
            {
                "id": "acme",
                "name": "Acme",
                "knowledgeDomainKeys": ["KD01"],
                "domainCapabilityKeys": ["DC13"],
                "knowledgeSources": {
                    "repositories": [{"name": "org-alpha", "path": str(org_repo)}],
                    "documents": [{"title": "Organization Handbook", "path": str(org_doc)}],
                },
            }
        ],
    }))
    try:
        payload = chat["prepare_knowledge_snapshot_payload"]("dc13.local")
        assert payload["organization"]["id"] == "acme"
        assert payload["sourceCount"] == 4
        assert payload["availableSourceCount"] == 4
        assert payload["missingSourceCount"] == 0
        assert payload["fileCount"] == 4
        assert payload["registryHash"]
        assert payload["sources"][0]["sample"][0]["sha256"]
    finally:
        globals_["GENERAL_KNOWLEDGE_PATH"] = original_general
        globals_["ORGANIZATION_REGISTRY_PATH"] = original_orgs


def test_steel_mission_knowledge_quality_detects_stale_missing_and_conflicting_sources(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["knowledge_quality_report"].__globals__
    original_general = globals_["GENERAL_KNOWLEDGE_PATH"]
    original_orgs = globals_["ORGANIZATION_REGISTRY_PATH"]
    shared_policy = tmp_path / "shared-policy.md"
    organization_policy = tmp_path / "organization-policy.md"
    missing_runbook = tmp_path / "missing-runbook.md"
    shared_policy.write_text("Shared policy\n")
    organization_policy.write_text("Organization policy\n")
    globals_["GENERAL_KNOWLEDGE_PATH"] = tmp_path / "general-knowledge.json"
    globals_["GENERAL_KNOWLEDGE_PATH"].write_text(json.dumps({
        "documents": [
            {
                "title": "Security Policy",
                "path": str(shared_policy),
                "owner": "security@example.invalid",
                "lastReviewedAt": "2020-01-01T00:00:00Z",
                "maxAgeDays": 30,
                "required": True,
                "authoritative": True,
            }
        ],
    }))
    globals_["ORGANIZATION_REGISTRY_PATH"] = tmp_path / "organizations.json"
    globals_["ORGANIZATION_REGISTRY_PATH"].write_text(json.dumps({
        "schemaVersion": 1,
        "activeOrganizationId": "acme",
        "organizations": [
            {
                "id": "acme",
                "name": "Acme",
                "knowledgeDomainKeys": ["KD01"],
                "domainCapabilityKeys": ["DC13"],
                "knowledgeSources": {
                    "repositories": [],
                    "documents": [
                        {
                            "title": "Security Policy",
                            "path": str(organization_policy),
                            "owner": "legal@example.invalid",
                            "lastReviewedAt": "2026-08-19T00:00:00Z",
                            "maxAgeDays": 365,
                            "required": True,
                            "authoritative": True,
                        },
                        {
                            "title": "Incident Runbook",
                            "path": str(missing_runbook),
                            "owner": "operations@example.invalid",
                            "lastReviewedAt": "2026-08-19T00:00:00Z",
                            "maxAgeDays": 365,
                            "required": True,
                            "authoritative": False,
                        },
                    ],
                },
            }
        ],
    }))
    try:
        quality = chat["knowledge_quality_report"]()

        assert quality["status"] == "insufficient"
        assert quality["contextSufficient"] is False
        assert quality["staleSourceCount"] == 1
        assert quality["conflictCount"] == 1
        assert {item["id"] for item in quality["issues"]} >= {
            "conflicting-source",
            "missing-source",
            "stale-source",
        }
        assert {item["provenance"]["registry"] for item in quality["sources"]} == {
            "organization-registry",
            "shared-registry",
        }
        assert "Do not infer" in quality["confidenceDirective"]

        prepared = chat["prepare_knowledge_snapshot_payload"]("dc13.local")
        assert prepared["contextSufficient"] is False
        assert prepared["knowledgeQuality"]["status"] == "insufficient"
        assert prepared["knowledgeQuality"]["qualityHash"]
        assert {item["id"] for item in prepared["warnings"]} >= {
            "conflicting-source",
            "missing-source",
            "stale-source",
        }
    finally:
        globals_["GENERAL_KNOWLEDGE_PATH"] = original_general
        globals_["ORGANIZATION_REGISTRY_PATH"] = original_orgs


def test_steel_mission_user_registry_is_owner_admin_managed(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["save_user_registry"].__globals__
    original_users = globals_["USER_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    try:
        saved = chat["save_user_registry"]({
            "users": [
                {"id": "Team Owner", "name": "Team Owner", "email": "owner@example.test", "role": "owner"},
                {"id": "pub", "name": "Publisher", "email": "pub@example.test", "role": "publisher", "status": "disabled", "assignedCapabilities": ["DC13"]},
            ]
        }, "admin")

        assert saved["users"][0]["id"] == "Team-Owner"
        assert saved["users"][0]["role"] == "owner"
        assert saved["users"][0]["assignedCapabilities"] == []
        assert saved["users"][0]["assignedCapabilities"] == []
        assert saved["users"][1]["status"] == "disabled"
        assert saved["users"][1]["assignedCapabilities"] == ["DC13"]
        assert saved["users"][1]["assignedCapabilities"] == ["DC13"]
        assert json.loads((tmp_path / "users.json").read_text())["users"] == saved["users"]
        ledger = chat["read_mutation_ledger"]("admin")
        assert ledger["ok"] is True
        assert ledger["mutations"][0]["action"] == "users-saved"
        assert chat["read_mutation_ledger"]("publisher")["ok"] is False
        with pytest.raises(ValueError, match="only owner and admin"):
            chat["save_user_registry"]({"users": []}, "user")
    finally:
        globals_["USER_REGISTRY_PATH"] = original_users
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_chat_uploads_are_inline_context_not_organization_knowledge(tmp_path):
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    summaries, context = chat["chat_upload_context"]([
        {
            "name": "meeting-notes.txt",
            "relativePath": "meeting-notes.txt",
            "type": "text/plain",
            "contentBase64": base64.b64encode(b"Temporary decision context only.").decode(),
        }
    ])

    assert summaries == [{
        "name": "meeting-notes.txt",
        "relativePath": "meeting-notes.txt",
        "type": "text/plain",
        "size": len(b"Temporary decision context only."),
        "truncated": False,
    }]
    assert "Temporary decision context only." in context
    assert "Organization Knowledge" not in context


def test_steel_mission_work_mode_frames_request_while_preserving_transcript():
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    assert chat["normalize_work_mode"]("normal") == "normal"
    assert chat["normalize_work_mode"]("domain-capabilities") == "domain-capabilities"
    assert chat["normalize_work_mode"]("unexpected") == "domain-capabilities"

    requirement = chat["build_requirement"]("Help me decide.", [
        {"role": "user", "content": "Prior normal chat context."},
        {"role": "user", "content": "# Work mode\n\nNormal chat mode: answer conversationally and directly."},
    ])

    assert "## Work mode" in requirement
    assert "Normal chat mode" in requirement
    assert "Prior normal chat context." in requirement
    assert requirement.count("# Work mode") == 1

    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    job_id = chat["start_job"]("ask me to decide", [], True, work_mode="normal")
    try:
        with lock:
            job = jobs[job_id]
            assert job["workMode"] == "normal"
            assert job["messages"][-1]["content"].startswith("# Work mode")
        payload = chat["job_api_payload"](job_id, job)
        assert payload["progress"]["workMode"] == "normal"
    finally:
        with lock:
            task_id = jobs.get(job_id, {}).get("taskId")
            if job_id in jobs:
                jobs[job_id]["state"] = "cancelled"
            jobs.pop(job_id, None)
        if task_id:
            purge_task(task_id)


def test_general_knowledge_documents_flow_into_dc13_snapshot_policy(tmp_path, monkeypatch):
    cli = _load_cli_module()
    document = tmp_path / "publisher-handbook.md"
    document.write_text("Publisher operating notes\nUse assigned domain capabilities parts only.\n")
    knowledge_path = tmp_path / "general-knowledge.json"
    knowledge_path.write_text(json.dumps({
        "schemaVersion": 1,
        "repositories": [{"name": "external-project", "path": str(tmp_path)}],
        "documents": [{"title": "Publisher Handbook", "path": str(document)}],
    }))
    organization_path = tmp_path / "organizations.json"
    organization_path.write_text(json.dumps({
        "schemaVersion": 1,
        "activeOrganizationId": "empty-org",
        "organizations": [
            {"id": "empty-org", "name": "Empty Org", "knowledgeSources": {"repositories": [], "documents": []}}
        ],
    }))
    monkeypatch.setenv("PRESENT_GENERAL_KNOWLEDGE_REGISTRY", str(knowledge_path))
    monkeypatch.setenv("PRESENT_ORGANIZATION_REGISTRY", str(organization_path))
    profile = {
        "schemaVersion": 1,
        "id": "dc13.knowledge",
        "label": "DC13 Knowledge",
        "status": "active",
        "modelRole": "dc13.coordination-report",
        "modelProvider": "glimmer",
        "snapshotProfile": "knowledge-fixture",
        "defaultFor": [],
        "editableBy": ["owner", "admin"],
        "visibilityRoleKeys": ["DC13"],
        "includeCollections": ["generalDocuments"],
        "limits": {
            "tasks": 0,
            "advisoryTasks": 0,
            "buildJobs": 0,
            "verifyResults": 0,
            "distributedWorkflows": 0,
            "brokerStateSources": 0,
            "brokerArtifacts": 0,
            "operatorAudit": 0,
            "generalDocuments": 4,
        },
        "taskSelector": {"mode": "latest"},
        "sources": {
            "taskRoots": [],
            "logRoots": [],
            "buildJobRoots": [],
            "verifyResultRoots": [],
            "brokerStatePaths": [],
            "repositoryRoots": [],
            "documentPaths": [],
        },
    }

    policy = cli._runtime_profile_to_snapshot_policy(profile)
    snapshot = cli._coordinator_state_snapshot(policy)

    assert policy["sources"]["repositoryRoots"][0]["name"] == "external-project"
    assert policy["sources"]["documentPaths"][0]["title"] == "Publisher Handbook"
    assert snapshot["generalDocuments"][0]["title"] == "Publisher Handbook"
    assert "assigned domain capabilities" in snapshot["generalDocuments"][0]["excerpt"]
    collections = {item["name"]: item for item in snapshot["snapshotCollections"]}
    assert collections["generalDocuments"]["returned"] == 1


def test_active_organization_sources_flow_into_runtime_snapshot_policy(tmp_path, monkeypatch):
    cli = _load_cli_module()
    organization_repo = tmp_path / "organization-repo"
    organization_repo.mkdir()
    organization_doc = tmp_path / "organization-handbook.md"
    organization_doc.write_text("Organization-specific delivery context.\n")
    knowledge_path = tmp_path / "general-knowledge.json"
    knowledge_path.write_text(json.dumps({"schemaVersion": 1, "repositories": [], "documents": []}))
    organization_path = tmp_path / "organizations.json"
    organization_path.write_text(json.dumps({
        "schemaVersion": 1,
        "activeOrganizationId": "acme-delivery",
        "organizations": [
            {
                "id": "acme-delivery",
                "name": "Acme Delivery",
                "knowledgeSources": {
                    "repositories": [{"name": "organization-repo", "path": str(organization_repo)}],
                    "documents": [{"title": "Organization Handbook", "path": str(organization_doc)}],
                },
            }
        ],
    }))
    monkeypatch.setenv("PRESENT_GENERAL_KNOWLEDGE_REGISTRY", str(knowledge_path))
    monkeypatch.setenv("PRESENT_ORGANIZATION_REGISTRY", str(organization_path))
    profile = {
        "schemaVersion": 1,
        "id": "dc13.organization",
        "label": "DC13 Organization",
        "status": "active",
        "modelRole": "dc13.coordination-report",
        "modelProvider": "glimmer",
        "snapshotProfile": "organization-fixture",
        "defaultFor": [],
        "editableBy": ["owner", "admin"],
        "visibilityRoleKeys": ["DC13"],
        "includeCollections": ["generalDocuments"],
        "limits": {
            "tasks": 0,
            "advisoryTasks": 0,
            "buildJobs": 0,
            "verifyResults": 0,
            "distributedWorkflows": 0,
            "brokerStateSources": 0,
            "brokerArtifacts": 0,
            "operatorAudit": 0,
            "generalDocuments": 4,
        },
        "taskSelector": {"mode": "latest"},
        "sources": {
            "taskRoots": [],
            "logRoots": [],
            "buildJobRoots": [],
            "verifyResultRoots": [],
            "brokerStatePaths": [],
            "repositoryRoots": [],
            "documentPaths": [],
        },
    }

    policy = cli._runtime_profile_to_snapshot_policy(profile)
    assert policy["sources"]["repositoryRoots"][0]["name"] == "organization-repo"
    assert policy["sources"]["documentPaths"][0]["title"] == "Organization Handbook"


def test_steel_mission_follow_up_steers_active_job_and_persists_event(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    job_id = "JOB-follow-up"
    task_id = "DEV-900024"
    purge_task(task_id)
    try:
        with lock:
            jobs[job_id] = {
                "state": "running",
                "createdAt": "2026-08-18T00:00:00Z",
                "startedEpoch": time.time() - 3,
                "taskId": task_id,
                "mock": False,
                "question": "Where are we?",
                "scope": [],
                "followUps": [],
                "steeringRevision": 0,
                "restartRequested": False,
            }
        event = chat["append_follow_up"](job_id, "Narrow this to acceptance blockers only.")
        assert event["revision"] == 1
        assert event["intent"] == "scope-change"
        assert event["effect"] == "restart-active-run"
        with lock:
            job = jobs[job_id]
            assert job["restartRequested"] is True
            assert job["steeringRevision"] == 1
            assert job["followUps"][0]["content"] == "Narrow this to acceptance blockers only."
        persisted = json.loads((common.TASKS_DIR / task_id / "steel-mission-steering-events.json").read_text())
        assert persisted["taskId"] == task_id
        assert persisted["events"][0]["revision"] == 1

        requirement = chat["build_requirement"]("Where are we?", [], [event])
        assert "Active follow-up updates" in requirement
        assert "the follow-up wins" in requirement
        assert "Narrow this to acceptance blockers only." in requirement

        payload = chat["job_api_payload"](job_id, job)
        assert payload["progress"]["steeringEvents"][0]["revision"] == 1
        assert payload["progress"]["timeline"][-1]["label"] == "You changed the focus"
        html = chat["render_running"](job_id, job)
        assert "Follow-Up Updates" in html
        assert "[scope-change]" not in html
        assert "You changed the focus" in html
        assert "acceptance blockers only" in html
    finally:
        with lock:
            jobs.pop(job_id, None)
        purge_task(task_id)


def test_steel_mission_follow_up_intents_handle_status_and_cancellation(monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    task_id = "DEV-900025"
    progress_job = "JOB-progress"
    pause_job = "JOB-pause"
    cancel_job = "JOB-cancel"
    signalled_groups: list[int] = []
    monkeypatch.setattr(chat["os"], "killpg", lambda pgid, _signal: signalled_groups.append(pgid))
    purge_task(task_id)
    try:
        progress_dir = common.TASKS_DIR / task_id
        progress_dir.mkdir(parents=True)
        (progress_dir / "progress.json").write_text(json.dumps({
            "phase": "Model stream opened",
            "elapsedSeconds": 1,
            "modelPgid": 2222,
        }))
        with lock:
            jobs[progress_job] = {
                "state": "running",
                "createdAt": "2026-08-18T00:00:00Z",
                "startedEpoch": time.time() - 5,
                "taskId": task_id,
                "mock": False,
                "question": "Where are we?",
                "scope": [],
                "followUps": [],
                "steeringRevision": 0,
                "restartRequested": False,
            }
        event = chat["append_follow_up"](progress_job, "what is happening?")
        assert event["intent"] == "progress-check"
        assert event["effect"] == "report-progress"
        with lock:
            job = jobs[progress_job]
            assert job["state"] == "running"
            assert job["steeringRevision"] == 0
            assert job["restartRequested"] is False
        progress = chat["job_api_payload"](progress_job, job)["progress"]
        assert progress["timeline"][-1]["label"] == "You asked for progress"
        assert progress["phase"].startswith("You asked for live progress")
        assert "refreshed this status view" in progress["timeline"][-1]["detail"]
        assert "progress-check" not in chat["steering_events_text"](progress["steeringEvents"])
        assert "You asked for progress" in chat["steering_events_text"](progress["steeringEvents"])

        with lock:
            jobs[pause_job] = {
                "state": "running",
                "createdAt": "2026-08-18T00:00:00Z",
                "startedEpoch": time.time() - 6,
                "taskId": task_id,
                "mock": False,
                "question": "Where are we?",
                "scope": [],
                "followUps": [],
                "steeringRevision": 0,
                "restartRequested": False,
                "activePid": 1111,
            }
        paused = chat["append_follow_up"](pause_job, "pause this job")
        assert paused["intent"] == "pause"
        assert paused["effect"] == "pause-active-job"
        with lock:
            job = jobs[pause_job]
            assert job["state"] == "paused"
            assert job["steeringRevision"] == 1
            assert job["restartRequested"] is False
        progress = chat["job_api_payload"](pause_job, job)["progress"]
        assert progress["timeline"][-1]["label"] == "You paused the job"
        assert "Press play" in progress["timeline"][-1]["detail"]
        assert signalled_groups == [2222, 1111]

        resumed = chat["append_follow_up"](pause_job, "resume this job")
        assert resumed["intent"] == "resume"
        assert resumed["effect"] == "resume-active-job"
        with lock:
            job = jobs[pause_job]
            assert job["state"] == "running"
            assert job["steeringRevision"] == 2
            assert job["restartRequested"] is True
        progress = chat["job_api_payload"](pause_job, job)["progress"]
        assert progress["timeline"][-1]["label"] == "You resumed the job"

        with lock:
            jobs[cancel_job] = {
                "state": "running",
                "createdAt": "2026-08-18T00:00:00Z",
                "startedEpoch": time.time() - 7,
                "taskId": task_id,
                "mock": False,
                "question": "Where are we?",
                "scope": [],
                "followUps": [],
                "steeringRevision": 0,
                "restartRequested": False,
            }
        cancelled = chat["append_follow_up"](cancel_job, "stop the job")
        assert cancelled["intent"] == "cancel"
        assert cancelled["effect"] == "cancel-active-job"
        with lock:
            job = jobs[cancel_job]
            assert job["state"] == "cancelled"
            assert job["ok"] is False
            assert job["error"] == "Delivery Coordinator job cancelled by your follow-up."
        payload = chat["job_api_payload"](cancel_job, job)
        assert payload["state"] == "cancelled"
        assert payload["progress"]["timeline"][-1]["label"] == "You cancelled the job"
        html = chat["render_job"](cancel_job, job)
        assert "Delivery Coordinator job cancelled" in html
        assert "[cancel]" not in html
        assert "You cancelled the job" in html
    finally:
        with lock:
            jobs.pop(progress_job, None)
            jobs.pop(pause_job, None)
            jobs.pop(cancel_job, None)
        purge_task(task_id)


def test_steel_mission_decision_request_exposes_options_default_and_free_text():
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    task_id = "DEV-900026"
    job_id = "JOB-decision"
    purge_task(task_id)
    try:
        with lock:
            jobs[job_id] = {
                "state": "running",
                "createdAt": "2026-08-18T00:00:00Z",
                "startedEpoch": time.time() - 11,
                "taskId": task_id,
                "mock": False,
                "question": "Where are we?",
                "scope": [],
                "followUps": [],
                "steeringRevision": 0,
                "restartRequested": False,
            }
        request = chat["request_user_decision"](
            job_id,
            "Which recovery path should DC13 use?",
            "The verify node is blocked because live evidence is missing. Choosing the default reruns only verify.",
            [
                {"id": "rerun-verify", "label": "Rerun verify", "description": "Default: rerun only the blocked verifier."},
                {"id": "full-rerun", "label": "Full rerun", "description": "Rebuild the whole workflow before verifying."},
                {"id": "pause", "label": "Pause", "description": "Leave the job waiting until more context is available."},
            ],
            "rerun-verify",
        )
        assert request["defaultOptionId"] == "rerun-verify"
        assert len(request["options"]) == 3
        assert request["options"][0]["default"] is True
        with lock:
            job = jobs[job_id]
            assert job["state"] == "waiting_for_decision"
            assert job["decisionRequest"]["question"].startswith("Which recovery path")
            assert job["steeringRevision"] == 1
        payload = chat["job_api_payload"](job_id, job)
        assert payload["state"] == "waiting_for_decision"
        assert payload["progress"]["decisionRequest"]["defaultOptionId"] == "rerun-verify"
        assert payload["progress"]["timeline"][-1]["label"] == "Your decision is needed"
        html = chat["render_running"](job_id, job)
        assert "Decision Needed" in html
        assert "Rerun verify (default)" in chat["decision_request_text"](request)
        assert "You can choose one option and add free text." in html

        event = chat["append_decision_response"](job_id, "", "Use the smallest rerun and keep artifacts for comparison.")
        assert event["intent"] == "user-decision"
        assert event["effect"] == "answer-decision"
        assert event["selectedOptionId"] == "rerun-verify"
        assert "smallest rerun" in event["freeText"]
        with lock:
            job = jobs[job_id]
            assert job["state"] == "running"
            assert "decisionRequest" not in job
            assert job["restartRequested"] is True
            assert job["steeringRevision"] == 2
        persisted = json.loads((common.TASKS_DIR / task_id / "steel-mission-steering-events.json").read_text())
        assert persisted["events"][0]["selectedOptionId"] == "rerun-verify"
    finally:
        with lock:
            jobs.pop(job_id, None)
        purge_task(task_id)


def test_steel_mission_follow_up_can_invoke_demo_decision_request():
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    task_id = "DEV-900027"
    job_id = "JOB-decision-demo"
    purge_task(task_id)
    try:
        with lock:
            jobs[job_id] = {
                "state": "running",
                "createdAt": "2026-08-18T00:00:00Z",
                "startedEpoch": time.time() - 4,
                "taskId": task_id,
                "mock": False,
                "question": "Where are we?",
                "scope": [],
                "followUps": [],
                "steeringRevision": 0,
                "restartRequested": False,
            }
        event = chat["append_follow_up"](job_id, "ask me to decide")
        assert event["intent"] == "decision-request-demo"
        assert event["effect"] == "request-user-decision"
        with lock:
            job = jobs[job_id]
            assert job["state"] == "waiting_for_decision"
            assert job["decisionRequest"]["defaultOptionId"] == "continue-narrow"
            assert job["decisionRequest"]["freeText"]["enabled"] is True
        progress = chat["job_api_payload"](job_id, job)["progress"]
        assert progress["decisionRequest"]["options"][0]["default"] is True
        assert progress["timeline"][-1]["label"] == "Your decision is needed"
    finally:
        with lock:
            jobs.pop(job_id, None)
        purge_task(task_id)


def test_steel_mission_initial_question_can_open_decision_panel_without_racing_mock_job():
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    job_id = chat["start_job"]("ask me to decide", [], True)
    try:
        with lock:
            job = jobs[job_id]
            assert job["state"] == "waiting_for_decision"
            assert job["mock"] is True
            assert job["decisionRequest"]["defaultOptionId"] == "continue-narrow"
        payload = chat["job_api_payload"](job_id, job)
        assert payload["progress"]["decisionRequest"]["defaultOptionId"] == "continue-narrow"
        assert payload["progress"]["phase"].startswith("Delivery Coordinator needs your decision")
    finally:
        with lock:
            task_id = jobs.get(job_id, {}).get("taskId")
            if job_id in jobs:
                jobs[job_id]["state"] = "cancelled"
        time.sleep(0.3)
        with lock:
            jobs.pop(job_id, None)
        if task_id:
            purge_task(task_id)


def test_steel_mission_mission_control_persists_records_and_audit(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    task_id = None
    job_id = chat["start_job"]("ask me to decide", [], True, operator_role="publisher")
    try:
        with lock:
            job = jobs[job_id]
            mission_id = job["missionId"]
            task_id = job["taskId"]
        mission = chat["read_mission_record"](mission_id)
        assert mission["jobId"] == job_id
        assert mission["taskId"] == task_id
        assert mission["operatorRole"] == "publisher"
        assert mission["state"] == "waiting_for_decision"
        assert mission["auditCount"] >= 2
        assert mission["snapshotPolicySummary"]["policyHash"]
        assert schema_check.validate(mission, "canonical/mission-control-v1.json") == []

        audit = chat["read_mission_audit"](mission_id)
        assert [event["action"] for event in audit[:2]] == ["mission-started", "decision-requested"]
        assert schema_check.validate(audit[0], "canonical/mission-audit-event-v1.json") == []

        listed = chat["mission_list"]("publisher")
        assert listed["missions"][0]["missionId"] == mission_id
        detail = chat["mission_detail"](mission_id, "publisher")
        assert detail["ok"] is True
        assert detail["mission"]["audit"][0]["action"] == "mission-started"
    finally:
        with lock:
            if job_id in jobs:
                jobs[job_id]["state"] = "cancelled"
            jobs.pop(job_id, None)
        globals_["MISSION_ROOT"] = original_root
        if task_id:
            purge_task(task_id)


def test_steel_mission_mission_evidence_and_audit_are_signed_in_one_chain(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    mission_id = "ms-" + "2" * 24
    try:
        chat["update_mission"](
            mission_id,
            jobId="JOB-signed",
            taskId="DEV-900030",
            producer="steel-mission-chat mission-control",
            state="running",
            operatorRole="admin",
            profile="dc13.local",
            snapshotPolicySummary={
                "policyHash": "1" * 64,
                "includeCollections": ["missions"],
                "sourceCounts": {"missionRoots": 1},
            },
            auditCount=0,
        )
        ref = chat["write_mission_evidence"](
            mission_id,
            "delivery-plan",
            "delivery-plan",
            {"plan": "signed"},
            task_id="DEV-900030",
            job_id="JOB-signed",
            operator_role="admin",
            summary="Signed evidence fixture.",
        )
        evidence = json.loads(Path(ref["path"]).read_text())
        audit = chat["read_mission_audit"](mission_id)
        chain = chat["mission_integrity_chain"](mission_id, limit=0)

        assert evidence["integrity"]["signatureScheme"] == "hmac-sha256-local-alpha"
        assert re.fullmatch(r"[a-f0-9]{64}", evidence["integrity"]["signature"])
        assert ref["integrityHash"] == chain[0]["chainHash"]
        assert len(chain) == 2
        assert audit[0]["integrity"]["previousHash"] == chain[0]["chainHash"]
        assert audit[0]["integrity"]["recordKind"] == "audit:evidence-recorded"
        assert schema_check.validate(audit[0], "canonical/mission-audit-event-v1.json") == []
        assert schema_check.validate(evidence, "canonical/mission-evidence-record-v1.json") == []
    finally:
        globals_["MISSION_ROOT"] = original_root


def test_steel_mission_orchestrated_mission_runs_template_and_records_evidence(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_report = globals_["run_coordinator_report"]
    original_users = globals_["USER_REGISTRY_PATH"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["USER_REGISTRY_PATH"].write_text(json.dumps({
        "schemaVersion": 1,
        "users": [
            {"id": "pub-a", "name": "Publisher A", "role": "publisher", "status": "active", "assignedCapabilities": ["DC13", "DC03"]},
            {"id": "user-a", "name": "User A", "role": "user", "status": "active", "assignedCapabilities": ["DC13"]},
        ],
    }))
    captured_questions: list[str] = []

    def fake_coordinator_report(task_id, question, messages, mock, follow_ups=None, **kwargs):
        captured_questions.append(question)
        return {
            "ok": True,
            "taskId": task_id,
            "exitCode": 0,
            "payload": {
                "summary": "stubbed Delivery Coordinator mission readout",
                "packIdentity": {"probe": "ok"},
                "items": [],
                "notChecked": [],
                "contradictions": [],
            },
        }

    globals_["run_coordinator_report"] = fake_coordinator_report
    task_id = None
    job_id = None
    try:
        started = chat["start_orchestrated_mission"](
            "investigate",
            "Find the delivery-control risks.",
            mock=True,
            operator_role="user",
            profile="dc13.local",
            user_ids=["user-a"],
            domain_capability_keys=["DC13"],
        )
        mission_id = started["missionId"]
        job_id = started["jobId"]
        task_id = started["taskId"]
        deadline = time.time() + 12
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "done":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "done"
        assert mission["missionKind"] == "orchestrated"
        assert mission["templateId"] == "investigate"
        assert mission["missionUsers"][0]["id"] == "user-a"
        assert mission["missionUsers"][0]["assignedCapabilities"] == ["DC13"]
        assert mission["capabilityWorkSet"][0]["roleKey"] == "DC13"
        assert mission["capabilityWorkSet"][0]["capabilityKey"] == "DC13"
        assert [node["state"] for node in mission["nodes"]] == ["done", "done", "done"]
        assert len(mission["evidenceLedger"]) == 3
        assert mission["evidenceLedger"][0]["sha256"]
        assert schema_check.validate(mission, "canonical/mission-control-v1.json") == []

        evidence_path = Path(mission["evidenceLedger"][0]["path"])
        assert evidence_path.exists()
        evidence = json.loads(evidence_path.read_text())
        assert schema_check.validate(evidence, "canonical/mission-evidence-record-v1.json") == []

        audit = chat["read_mission_audit"](mission_id)
        actions = [event["action"] for event in audit]
        assert "mission-node-started" in actions
        assert "evidence-recorded" in actions
        assert actions[-1] == "mission-completed"
        assert any("Mission users: User A (user)" in question for question in captured_questions)
        assert any("Capability work set: DC13 DC13" in question for question in captured_questions)
    finally:
        if job_id:
            with lock:
                jobs.pop(job_id, None)
        globals_["run_coordinator_report"] = original_report
        globals_["MISSION_ROOT"] = original_root
        globals_["USER_REGISTRY_PATH"] = original_users
        if task_id:
            purge_task(task_id)


def test_steel_mission_orchestrated_mission_waits_for_approval_and_resumes(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_report = globals_["run_coordinator_report"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"

    def fake_coordinator_report(task_id, question, messages, mock, follow_ups=None, **kwargs):
        return {
            "ok": True,
            "taskId": task_id,
            "exitCode": 0,
            "payload": {
                "summary": "stubbed implementation brief",
                "packIdentity": {"probe": "ok"},
                "items": [],
                "notChecked": [],
                "contradictions": [],
            },
        }

    globals_["run_coordinator_report"] = fake_coordinator_report
    task_id = None
    job_id = None
    try:
        started = chat["start_orchestrated_mission"](
            "implement",
            "Prepare the next implementation slice.",
            mock=True,
            operator_role="publisher",
            profile="dc13.local",
        )
        mission_id = started["missionId"]
        job_id = started["jobId"]
        task_id = started["taskId"]
        deadline = time.time() + 12
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "waiting_for_approval":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "waiting_for_approval"
        assert mission["currentNodeId"] == "implementation-approval"
        assert mission["nodes"][1]["state"] == "waiting_for_approval"

        approval = chat["approve_mission"](mission_id, "publisher", "Approved for alpha test.")
        assert approval["ok"] is True

        deadline = time.time() + 12
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "done":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "done"
        assert mission["approvals"][0]["actorRole"] == "publisher"
        assert [node["state"] for node in mission["nodes"]] == ["done", "done", "done", "done"]
        assert len(mission["evidenceLedger"]) == 4
        assert schema_check.validate(mission, "canonical/mission-control-v1.json") == []
        assert "mission-approved" in [event["action"] for event in chat["read_mission_audit"](mission_id)]
    finally:
        if job_id:
            with lock:
                jobs.pop(job_id, None)
        globals_["run_coordinator_report"] = original_report
        globals_["MISSION_ROOT"] = original_root
        if task_id:
            purge_task(task_id)


def test_steel_mission_approval_request_emits_configured_connector_evidence(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_registry = globals_["INTEGRATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["INTEGRATION_REGISTRY_PATH"] = tmp_path / "integration-registry.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    chat["save_integration_registry"]({
        "connectors": [
            {
                "id": "slack",
                "label": "Slack",
                "kind": "approval-notifications",
                "enabled": True,
                "mode": "outbox",
                "events": ["approval-requested"],
            }
        ],
    }, "admin")
    task_id = None
    job_id = None
    try:
        started = chat["start_orchestrated_mission"](
            "implement",
            "Notify approvers before implementation.",
            mock=True,
            operator_role="publisher",
            profile="dc13.local",
        )
        mission_id = started["missionId"]
        job_id = started["jobId"]
        task_id = started["taskId"]
        deadline = time.time() + 5
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "waiting_for_approval":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        connector_refs = [ref for ref in mission["evidenceLedger"] if ref["kind"] == "connector-event"]
        assert connector_refs
        connector_event = json.loads(Path(connector_refs[0]["path"]).read_text())
        assert connector_event["payload"]["eventType"] == "approval-requested"
        assert connector_event["payload"]["connector"]["id"] == "slack"
        assert Path(connector_event["payload"]["execution"]["path"]).exists()
        approval_audit = [event for event in chat["read_mission_audit"](mission_id) if event["action"] == "approval-requested"][-1]
        assert approval_audit["artifactRefs"][0]["kind"] == "connector-event"
    finally:
        if job_id:
            with lock:
                jobs.pop(job_id, None)
        globals_["MISSION_ROOT"] = original_root
        globals_["INTEGRATION_REGISTRY_PATH"] = original_registry
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        if task_id:
            purge_task(task_id)


def test_steel_mission_delivery_execution_mission_records_lifecycle_evidence(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_users = globals_["USER_REGISTRY_PATH"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["USER_REGISTRY_PATH"].write_text(json.dumps({
        "schemaVersion": 1,
        "users": [
            {"id": "pub-a", "name": "Publisher A", "role": "publisher", "status": "active", "assignedCapabilities": ["DC13", "DC03"]},
            {"id": "user-a", "name": "User A", "role": "user", "status": "active", "assignedCapabilities": ["DC13"]},
        ],
    }))
    task_id = None
    job_id = None
    try:
        started = chat["start_orchestrated_mission"](
            "delivery-execution",
            "Ship the delivery-control loop.",
            mock=True,
            operator_role="publisher",
            profile="dc13.local",
            user_ids=["pub-a", "user-a"],
            domain_capability_keys=["DC13", "DC03"],
            delivery_context={
                "repositoryPath": str(WORKER_DIR),
                "branch": current_git_branch(),
                "buildCommand": "python3 -m py_compile steel-mission-chat/server.py",
                "testCommand": "pytest -q tests/test_worker.py",
                "inspectCommand": "curl -fsS http://127.0.0.1:8765/api/health",
                "prTarget": "northstar-forge/steel-mission-demo",
                "deployTarget": "local alpha",
                "repairBudget": 3,
            },
        )
        mission_id = started["missionId"]
        job_id = started["jobId"]
        task_id = started["taskId"]
        deadline = time.time() + 5
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "waiting_for_approval":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "waiting_for_approval"
        assert mission["deliveryContext"]["repositoryPath"] == str(WORKER_DIR)
        assert mission["deliveryContext"]["repairBudget"] == 3
        assert mission["missionUsers"][0]["id"] == "pub-a"
        assert {item["capabilityKey"] for item in mission["capabilityWorkSet"]} >= {"DC13", "DC03"}

        approval = chat["approve_mission"](mission_id, "publisher", "Approved for delivery alpha.")
        assert approval["ok"] is True

        deadline = time.time() + 12
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "done":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "done"
        assert [node["state"] for node in mission["nodes"]] == ["done"] * 11
        assert schema_check.validate(mission, "canonical/mission-control-v1.json") == []

        evidence_kinds = []
        delivery_phases = []
        for ref in mission["evidenceLedger"]:
            evidence = json.loads(Path(ref["path"]).read_text())
            assert schema_check.validate(evidence, "canonical/mission-evidence-record-v1.json") == []
            evidence_kinds.append(evidence["kind"])
            if evidence["kind"] == "delivery-step":
                delivery_phases.append(evidence["payload"]["phase"])
                assert evidence["payload"]["executionMode"] == "alpha-control-plane"
        assert "delivery-plan" in evidence_kinds
        assert delivery_phases == ["modify", "build", "test", "inspect", "repair", "pr", "deploy"]
        assert "mission-approved" in [event["action"] for event in chat["read_mission_audit"](mission_id)]
    finally:
        if job_id:
            with lock:
                jobs.pop(job_id, None)
        globals_["MISSION_ROOT"] = original_root
        globals_["USER_REGISTRY_PATH"] = original_users
        if task_id:
            purge_task(task_id)


def test_steel_mission_delivery_step_requires_guarded_runner_for_configured_build_command(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("delivery runner\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    direct = chat["delivery_step_payload"](
        {
            "mock": False,
            "deliveryContext": {
                "repositoryPath": str(repo),
                "buildCommand": "python3 -c \"from pathlib import Path; Path('built.txt').write_text('ok')\"",
                "repairBudget": 1,
            },
            "missionUsers": [],
            "capabilityWorkSet": [],
        },
        {"nodeId": "delivery-build", "phase": "build"},
    )

    assert direct["ok"] is False
    assert direct["status"] == "blocked"
    assert direct["preflight"]["decision"] == "block"
    assert "guarded control-plane runner" in "; ".join(direct["blockers"])
    assert not (repo / "built.txt").exists()

    guarded = chat["delivery_step_payload"](
        {
            "mock": False,
            "controlPlaneExecution": True,
            "deliveryContext": {
                "repositoryPath": str(repo),
                "buildCommand": "python3 -c \"from pathlib import Path; Path('built.txt').write_text('ok')\"",
                "repairBudget": 1,
            },
            "missionUsers": [],
            "capabilityWorkSet": [],
        },
        {"nodeId": "delivery-build", "phase": "build"},
    )

    assert guarded["ok"] is True
    assert guarded["status"] == "succeeded"
    assert guarded["adapter"]["kind"] == "command.build"
    assert guarded["preflight"]["controlPlaneExecution"] is True
    assert guarded["commandResult"]["exitCode"] == 0
    assert (repo / "built.txt").read_text() == "ok"


def test_steel_mission_mock_delivery_tolerates_detached_branch_but_real_execution_blocks_mismatch(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("branch validation\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    node = {"nodeId": "delivery-build", "phase": "build"}
    context = {
        "repositoryPath": str(repo),
        "branch": "definitely-not-the-current-branch",
        "buildCommand": "python3 -c \"from pathlib import Path; Path('must-not-run').write_text('no')\"",
    }

    mocked = chat["delivery_step_payload"](
        {"mock": True, "controlPlaneExecution": True, "deliveryContext": context},
        node,
    )
    assert mocked["ok"] is True
    assert mocked["status"] == "mocked"

    real = chat["delivery_step_payload"](
        {"mock": False, "controlPlaneExecution": True, "deliveryContext": context},
        node,
    )
    assert real["ok"] is False
    assert real["status"] == "blocked"
    assert any("expected definitely-not-the-current-branch" in item for item in real["blockers"])
    assert not (repo / "must-not-run").exists()


def test_steel_mission_delivery_preflight_blocks_unsafe_command_before_execution(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("preflight block\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    payload = chat["delivery_step_payload"](
        {
            "mock": False,
            "deliveryContext": {
                "repositoryPath": str(repo),
                "modifyCommand": "sh -c \"printf nope > should-not-exist && sudo true\"",
                "repairBudget": 1,
            },
            "approvals": [{"nodeId": "implementation-approval", "decision": "approved"}],
            "missionUsers": [],
            "capabilityWorkSet": [],
        },
        {"nodeId": "delivery-modify", "phase": "modify"},
    )

    assert payload["ok"] is False
    assert payload["status"] == "blocked"
    assert payload["preflight"]["decision"] == "block"
    assert payload["preflight"]["risk"]["level"] == "critical"
    assert payload["commandResult"] == {}
    assert not (repo / "should-not-exist").exists()


def test_steel_mission_delivery_preflight_requires_approval_for_production_deploy(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("deploy gate\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    delivery_context = {
        "repositoryPath": str(repo),
        "deployProvider": "command",
        "deployEnvironment": "production",
        "deployTarget": "production",
        "deployCommand": "python3 -c \"from pathlib import Path; Path('deployed.txt').write_text('ok')\"",
        "repairBudget": 1,
    }

    blocked = chat["delivery_step_payload"](
        {"mock": False, "deliveryContext": delivery_context, "missionUsers": [], "capabilityWorkSet": []},
        {"nodeId": "deploy-readiness", "phase": "deploy"},
    )
    assert blocked["ok"] is False
    assert blocked["status"] == "blocked"
    assert blocked["preflight"]["decision"] == "block"
    assert "guarded control-plane runner" in "; ".join(blocked["blockers"])
    assert not (repo / "deployed.txt").exists()

    approved = chat["delivery_step_payload"](
        {
            "mock": False,
            "controlPlaneExecution": True,
            "deliveryContext": delivery_context,
            "approvals": [{"nodeId": "implementation-approval", "decision": "approved"}],
            "missionUsers": [],
            "capabilityWorkSet": [],
        },
        {"nodeId": "deploy-readiness", "phase": "deploy"},
    )
    assert approved["ok"] is True
    assert approved["status"] == "succeeded"
    assert approved["preflight"]["decision"] == "allow"
    assert (repo / "deployed.txt").read_text() == "ok"


def test_steel_mission_control_plane_registry_exposes_policy_integrations_and_compliance():
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))

    policy = chat["control_policy"]()
    registry = chat["integration_registry"]("admin")
    compliance = chat["compliance_evidence"]()

    assert policy["modelIndependence"]["required"] is True
    assert policy["customerBoundary"]["deployment"] == "customer-vpc-or-private-cloud"
    assert any("sudo" in pattern for pattern in policy["blockedCommandPatterns"])
    assert {item["id"] for item in registry["connectors"]} >= {"github", "gitlab", "jira", "linear", "slack", "ci-cd", "siem"}
    siem = {item["id"]: item for item in registry["connectors"]}["siem"]
    assert siem["enabled"] is False
    assert siem.get("locked") is not True
    assert "enterpriseFeature" not in siem
    assert {item["id"] for item in registry["modelProviders"]} >= {"claude", "openai", "glimmer", "local"}
    assert registry["workflowEmbedding"]["strategy"] == "existing-tools-first"
    assert registry["workflowEmbedding"]["controlSurfaceRole"] == "administration-investigation-and-fallback"
    assert set(registry["workflowEmbedding"]["requirements"]) == {
        "preserve-originating-identity-and-thread",
        "return-status-approvals-decisions-and-evidence-to-source",
        "deep-link-to-investigation-without-forced-relocation",
    }
    assert set(compliance["standards"]) >= {"SOC 2", "ISO 27001", "ISO 42001"}


def test_steel_mission_core_includes_identity_siem_and_external_signing(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    monkeypatch.delenv("STEEL_MISSION_EDITION", raising=False)
    monkeypatch.delenv("STEEL_MISSION_LICENSE_KEY", raising=False)
    monkeypatch.delenv("STEEL_MISSION_LICENSE_KEY_SHA256", raising=False)
    monkeypatch.delenv(chat["EVIDENCE_SIGNER_COMMAND_ENV"], raising=False)
    monkeypatch.delenv("PRESENT_REQUIRE_EXTERNAL_SIGNING", raising=False)
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_registry = globals_["INTEGRATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    original_mission_root = globals_["MISSION_ROOT"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["INTEGRATION_REGISTRY_PATH"] = tmp_path / "integration-registry.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    try:
        saved = chat["save_auth_policy"]({
            "oidc": {"enabled": True, "issuer": "https://idp.example.invalid", "jwksUrl": "https://idp.example.invalid/jwks.json"},
            "kms": {"enabled": True, "provider": "customer-kms", "keyId": "customer-key", "signCommand": "printf signed", "requireExternalSigning": True},
        }, "owner")
        assert saved["oidc"]["enabled"] is True
        assert saved["oidc"]["jwksUrl"] == "https://idp.example.invalid/jwks.json"
        assert saved["kms"]["enabled"] is True
        policy = chat["auth_policy"]()
        assert policy["entitlement"]["enterpriseEnabled"] is False
        assert policy["oidc"]["enabled"] is True
        assert policy["oidc"]["jwksUrl"] == "https://idp.example.invalid/jwks.json"
        assert policy["kms"]["enabled"] is True
        assert chat["external_evidence_signer_command"]() == "printf signed"
        assert chat["external_signing_required"](policy) is True
        assert chat["evidence_signer_health"](policy)["status"] == "succeeded"
        chat["save_integration_registry"]({
            "connectors": [{"id": "siem", "kind": "security-evidence-export", "enabled": True, "mode": "outbox"}],
        }, "owner")
        registry = chat["integration_registry"]("admin")
        siem = {item["id"]: item for item in registry["connectors"]}["siem"]
        assert siem.get("locked") is not True
        assert siem["enabled"] is True
        mission_id = "ms-" + "1" * 24
        chat["update_mission"](mission_id, operatorRole="admin", state="done")
        siem_export = chat["mission_siem_jsonl"](mission_id, "admin")
        assert siem_export["ok"] is True
        assert siem_export["stream"] == "present.control-plane.siem"
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["INTEGRATION_REGISTRY_PATH"] = original_registry
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        globals_["MISSION_ROOT"] = original_mission_root


def test_steel_mission_control_policy_can_be_saved_and_changes_preflight(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["control_policy"].__globals__
    original_policy = globals_["CONTROL_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["CONTROL_POLICY_PATH"] = tmp_path / "control-plane-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("policy save\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    try:
        saved = chat["save_control_policy"]({
            "blockedCommandPatterns": [r"\bwrite_text\b"],
            "approvalRequired": {"phases": ["modify"], "prModes": ["create"], "deployProviders": ["command"], "deployEnvironments": ["production"]},
            "complianceMappings": {"SOC 2": ["CC6.1"], "ISO 27001": ["A.8.16"], "ISO 42001": ["A.7.4"]},
        }, "admin")
        assert saved["policyId"] == "present.delivery-control.alpha"
        assert chat["read_mutation_ledger"]("admin")["mutations"][0]["action"] == "control-policy-saved"
        payload = chat["delivery_step_payload"](
            {
                "mock": False,
                "deliveryContext": {
                    "repositoryPath": str(repo),
                    "modifyCommand": "python3 -c \"from pathlib import Path; Path('blocked.txt').write_text('no')\"",
                },
                "approvals": [{"nodeId": "implementation-approval", "decision": "approved"}],
                "missionUsers": [],
                "capabilityWorkSet": [],
            },
            {"nodeId": "delivery-modify", "phase": "modify"},
        )
        assert payload["preflight"]["decision"] == "block"
        assert payload["ok"] is False
        assert not (repo / "blocked.txt").exists()
        assert payload["preflight"]["complianceEvidence"]["standards"]["ISO 42001"] == ["A.7.4"]
    finally:
        globals_["CONTROL_POLICY_PATH"] = original_policy
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_auth_policy_sessions_and_guarded_execution_are_signed(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_mission_root = globals_["MISSION_ROOT"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("guarded auth\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    try:
        saved = chat["save_auth_policy"]({
            "enforcementMode": "signed-session-required-for-control-plane",
            "sessionTtlSeconds": 900,
            "acceptedIssuers": ["present-local-alpha"],
            "acceptedAudiences": ["present-control-plane"],
            "oidc": {"enabled": True, "issuer": "https://idp.example.invalid", "audience": "present-control-plane", "jwksUrl": "https://idp.example.invalid/jwks.json"},
            "kms": {"enabled": True, "provider": "aws-kms", "keyId": "arn:aws:kms:example"},
        }, "owner")
        assert saved["oidc"]["enabled"] is True
        assert saved["kms"]["provider"] == "aws-kms"
        session = chat["issue_control_plane_session"]("admin@example.invalid", "admin")
        verified = chat["verify_control_plane_session"](session["accessToken"])
        assert verified["ok"] is True
        assert verified["actorId"] == "admin@example.invalid"
        assert verified["role"] == "admin"
        assert chat["verify_control_plane_session"](session["accessToken"] + "bad")["ok"] is False

        result = chat["control_plane_execute_action"](
            {
                "phase": "modify",
                "repositoryPath": str(repo),
                "command": "sh -c \"printf nope > should-not-exist && sudo true\"",
                "approved": True,
            },
            {**verified, "sessionVerified": True},
        )
        assert result["ok"] is False
        assert result["result"]["preflight"]["decision"] == "block"
        assert not (repo / "should-not-exist").exists()
        mission = chat["read_mission_record"](result["missionId"], include_audit=True)
        assert mission["state"] == "error"
        assert mission["evidenceLedger"][0]["kind"] == "control-plane-execution"
        evidence = json.loads(Path(mission["evidenceLedger"][0]["path"]).read_text())
        assert evidence["integrity"]["signature"]
        assert [event["action"] for event in mission["audit"]] == [
            "control-plane-execution-requested",
            "evidence-recorded",
            "control-plane-execution-blocked",
        ]
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MISSION_ROOT"] = original_mission_root
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_oidc_rs256_session_verification_uses_configured_jwks(tmp_path, monkeypatch):
    import runpy
    from cryptography.hazmat.primitives import hashes as crypto_hashes
    from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
    from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    monkeypatch.delenv("STEEL_MISSION_EDITION", raising=False)
    monkeypatch.delenv("STEEL_MISSION_LICENSE_KEY", raising=False)
    monkeypatch.delenv("STEEL_MISSION_LICENSE_KEY_SHA256", raising=False)
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    try:
        private_key = crypto_rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private_key.public_key().public_numbers()
        jwk = {
            "kty": "RSA",
            "kid": "test-key",
            "alg": "RS256",
            "use": "sig",
            "n": chat["b64url_encode"](numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": chat["b64url_encode"](numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }
        jwks_path = tmp_path / "jwks.json"
        jwks_path.write_text(json.dumps({"keys": [jwk]}))
        chat["save_auth_policy"]({
            "acceptedIssuers": ["https://idp.example.invalid"],
            "acceptedAudiences": ["present-control-plane"],
            "roleClaims": ["present_role"],
            "subjectClaims": ["email"],
            "oidc": {
                "enabled": True,
                "issuer": "https://idp.example.invalid",
                "audience": "present-control-plane",
                "jwksPath": str(jwks_path),
            },
        }, "admin")
        header = {"alg": "RS256", "typ": "JWT", "kid": "test-key"}
        claims = {
            "iss": "https://idp.example.invalid",
            "aud": "present-control-plane",
            "email": "admin@example.invalid",
            "present_role": "admin",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        }
        encoded_header = chat["b64url_encode"](json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
        encoded_claims = chat["b64url_encode"](json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signature = private_key.sign(f"{encoded_header}.{encoded_claims}".encode(), crypto_padding.PKCS1v15(), crypto_hashes.SHA256())
        token = f"{encoded_header}.{encoded_claims}.{chat['b64url_encode'](signature)}"

        verified = chat["verify_control_plane_session"](token)
        assert verified["ok"] is True
        assert verified["issuerKind"] == "oidc-rs256"
        assert verified["actorId"] == "admin@example.invalid"
        assert verified["role"] == "admin"
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_oidc_required_maps_server_owned_identity_and_revokes_sessions(tmp_path, monkeypatch):
    import runpy
    from cryptography.hazmat.primitives import hashes as crypto_hashes
    from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
    from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original = {name: globals_[name] for name in (
        "AUTH_POLICY_PATH", "USER_REGISTRY_PATH", "MUTATION_LEDGER_PATH",
        "AUTH_REVOCATION_LEDGER_PATH", "AUTH_AUDIT_LEDGER_PATH",
    )}
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutations.jsonl"
    globals_["AUTH_REVOCATION_LEDGER_PATH"] = tmp_path / "revocations.jsonl"
    globals_["AUTH_AUDIT_LEDGER_PATH"] = tmp_path / "auth-audit.jsonl"
    monkeypatch.setenv("PRESENT_AUTH_SIGNING_KEY", "identity-test-signing-key")
    try:
        globals_["USER_REGISTRY_PATH"].write_text(json.dumps({
            "users": [{
                "id": "registry-admin",
                "name": "Registry Admin",
                "email": "admin@example.invalid",
                "role": "admin",
                "status": "active",
                "assignedCapabilities": ["DC13"],
                "organizationIds": ["northstar-forge"],
                "identitySubjects": ["https://idp.example.invalid|provider-subject"],
            }],
        }))
        private_key = crypto_rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private_key.public_key().public_numbers()
        jwks_path = tmp_path / "jwks.json"
        jwks_path.write_text(json.dumps({"keys": [{
            "kty": "RSA", "kid": "prod-key", "alg": "RS256", "use": "sig",
            "n": chat["b64url_encode"](numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": chat["b64url_encode"](numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }]}))
        chat["save_auth_policy"]({
            "identityBoundary": {"mode": "oidc-required", "allowLoopbackDevelopmentIdentity": False},
            "acceptedIssuers": ["https://idp.example.invalid", "present-local-alpha"],
            "acceptedAudiences": ["present-control-plane"],
            "oidc": {
                "enabled": True,
                "issuer": "https://idp.example.invalid",
                "audience": "present-control-plane",
                "jwksPath": str(jwks_path),
                "authorizationEndpoint": "https://idp.example.invalid/authorize",
                "tokenEndpoint": "https://idp.example.invalid/token",
                "clientId": "steel-mission",
            },
        }, "owner")
        header = {"alg": "RS256", "typ": "JWT", "kid": "prod-key"}
        claims = {
            "iss": "https://idp.example.invalid",
            "aud": "present-control-plane",
            "sub": "provider-subject",
            "email": "admin@example.invalid",
            "present_role": "owner",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        }
        encoded_header = chat["b64url_encode"](json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
        encoded_claims = chat["b64url_encode"](json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signature = private_key.sign(f"{encoded_header}.{encoded_claims}".encode(), crypto_padding.PKCS1v15(), crypto_hashes.SHA256())
        oidc_token = f"{encoded_header}.{encoded_claims}.{chat['b64url_encode'](signature)}"

        verified_oidc = chat["verify_control_plane_session"](oidc_token)
        assert verified_oidc["ok"] is True
        assert verified_oidc["actorId"] == "registry-admin"
        assert verified_oidc["role"] == "admin"  # token's forged owner role is ignored
        session = chat["issue_oidc_exchange_session"](oidc_token)
        assert session["claims"]["authn_method"] == "oidc-exchange"
        assert session["claims"]["present_role"] == "admin"
        assert chat["verify_control_plane_session"](session["accessToken"])["ok"] is True
        with pytest.raises(PermissionError, match="local self-issued"):
            chat["issue_control_plane_session"]("attacker", "owner")
        chat["revoke_control_plane_session"](session["accessToken"], "registry-admin")
        assert chat["verify_control_plane_session"](session["accessToken"])["error"] == "session is revoked"
    finally:
        for name, value in original.items():
            globals_[name] = value


def test_steel_mission_actor_scope_and_separation_of_duties_are_server_enforced(tmp_path):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["mission_detail"].__globals__
    original_root = globals_["MISSION_ROOT"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    try:
        mission_id = "ms-" + "8" * 24
        chat["update_mission"](
            mission_id,
            state="waiting_for_approval",
            operatorRole="publisher",
            actorUserId="initiator",
            organizationId="northstar-forge",
            nodes=[{"nodeId": "approval", "title": "Approval", "state": "waiting_for_approval"}],
            approvals=[],
        )
        initiator = {"actorId": "initiator", "role": "publisher", "organizationId": "northstar-forge", "organizationIds": ["northstar-forge"]}
        approver = {"actorId": "separate-approver", "role": "admin", "organizationId": "northstar-forge", "organizationIds": ["northstar-forge"]}
        outsider = {"actorId": "outsider", "role": "owner", "organizationId": "other-org", "organizationIds": ["other-org"]}
        assert chat["mission_detail"](mission_id, "publisher", actor=initiator)["ok"] is True
        assert chat["mission_detail"](mission_id, "owner", actor=outsider)["ok"] is False
        with pytest.raises(PermissionError, match="separation of duties"):
            chat["approve_mission"](mission_id, "publisher", actor_user_id="initiator", actor_context=initiator)
        approved = chat["approve_mission"](mission_id, "admin", actor_user_id="separate-approver", actor_context=approver)
        assert approved["approval"]["actorId"] == "separate-approver"
    finally:
        globals_["MISSION_ROOT"] = original_root


def test_steel_mission_http_identity_boundary_is_fail_closed_and_loopback_only(tmp_path, monkeypatch):
    import runpy
    from types import SimpleNamespace

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutations.jsonl"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    try:
        chat["save_auth_policy"]({"identityBoundary": {"mode": "oidc-required"}}, "owner")
        request = SimpleNamespace(headers={}, client_address=("127.0.0.1", 12345))
        with pytest.raises(PermissionError, match="OIDC-authenticated"):
            chat["authenticate_http_request"](request, "/api/knowledge", "GET")

        chat["save_auth_policy"]({
            "identityBoundary": {"mode": "development-local", "allowLoopbackDevelopmentIdentity": True},
        }, "owner")
        remote = SimpleNamespace(headers={"X-Present-Role": "owner"}, client_address=("192.0.2.10", 12345))
        with pytest.raises(PermissionError, match="loopback"):
            chat["authenticate_http_request"](remote, "/api/owner/users", "GET")
        local = SimpleNamespace(headers={"X-Present-Role": "admin", "X-Present-Actor": "riley-chen"}, client_address=("127.0.0.1", 12345))
        actor = chat["authenticate_http_request"](local, "/api/admin/users", "GET")
        assert actor["actorId"] == "riley-chen"
        assert actor["role"] == "admin"
        assert actor["identitySource"] == "user-registry"
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_headerless_loopback_whoami_resolves_the_active_organization_owner(
    tmp_path, monkeypatch
):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["Handler"].do_GET.__globals__
    user_registry = json.loads((WORKER_DIR / "config" / "users.json").read_text())
    owner = next(user for user in user_registry["users"] if user["role"] == "owner")
    owner["id"] = "installation-owner"
    (tmp_path / "users.json").write_text(json.dumps(user_registry))

    globals_["AUTH_POLICY_PATH"] = WORKER_DIR / "config" / "auth-policy.json"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["ORGANIZATION_REGISTRY_PATH"] = WORKER_DIR / "config" / "organizations.json"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    responses = []
    globals_["json_response"] = lambda _handler, status, response: responses.append(
        (status, response)
    )
    handler = object.__new__(chat["Handler"])
    handler.path = "/api/auth/whoami"
    handler.headers = {}
    handler.client_address = ("127.0.0.1", 51000)

    handler.do_GET()

    status, payload = responses[0]
    assert status == 200
    assert payload["actor"]["actorId"] == "installation-owner"
    assert payload["actor"]["role"] == "owner"
    assert payload["actor"]["organizationId"] == "northstar-forge"
    assert payload["actor"]["identitySource"] == "user-registry"
    chat["require_actor_role"](payload["actor"], {"owner", "admin"})


def test_steel_mission_registered_user_role_outranks_loopback_header_claim(monkeypatch):
    import runpy
    from types import SimpleNamespace

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["authenticate_http_request"].__globals__
    globals_["AUTH_POLICY_PATH"] = WORKER_DIR / "config" / "auth-policy.json"
    globals_["USER_REGISTRY_PATH"] = WORKER_DIR / "config" / "users.json"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    request = SimpleNamespace(
        headers={"X-Present-Role": "owner", "X-Present-Actor": "avery-stone"},
        client_address=("127.0.0.1", 51000),
    )

    actor = chat["authenticate_http_request"](request, "/api/owner/users", "GET")

    assert actor["actorId"] == "avery-stone"
    assert actor["role"] == "publisher"
    assert actor["identitySource"] == "user-registry"
    with pytest.raises(PermissionError, match="not allowed"):
        chat["require_actor_role"](actor, {"owner"})


def test_steel_mission_stale_loopback_identity_falls_back_to_the_registered_owner(
    tmp_path, monkeypatch
):
    import runpy
    from types import SimpleNamespace

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["authenticate_http_request"].__globals__
    user_registry = json.loads((WORKER_DIR / "config" / "users.json").read_text())
    owner = next(user for user in user_registry["users"] if user["role"] == "owner")
    owner["id"] = "installation-owner"
    (tmp_path / "users.json").write_text(json.dumps(user_registry))
    globals_["AUTH_POLICY_PATH"] = WORKER_DIR / "config" / "auth-policy.json"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["ORGANIZATION_REGISTRY_PATH"] = WORKER_DIR / "config" / "organizations.json"
    globals_["AUTH_AUDIT_LEDGER_PATH"] = tmp_path / "auth-audit.jsonl"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    request = SimpleNamespace(
        headers={"X-Present-Role": "owner", "X-Present-Actor": "removed-owner"},
        client_address=("127.0.0.1", 51000),
    )

    actor = chat["authenticate_http_request"](request, "/api/owner/users", "GET")

    assert actor["actorId"] == "installation-owner"
    assert actor["role"] == "owner"
    assert actor["identitySource"] == "user-registry"
    audit = [json.loads(line) for line in (tmp_path / "auth-audit.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "stale-development-identity-discarded"
    assert audit[-1]["actorId"] == "removed-owner"
    assert audit[-1]["details"] == {
        "path": "/api/owner/users",
        "declaredRole": "owner",
        "reason": "declared actor is not an active registered user",
        "resolvedActorId": "installation-owner",
    }


def test_steel_mission_external_kms_signer_is_used_for_mission_integrity(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    signer = tmp_path / "signer.py"
    signer.write_text(
        "import hashlib,json,sys\n"
        "payload=json.loads(sys.stdin.read())\n"
        "print(hashlib.sha256(('kms:'+payload['recordHash']).encode()).hexdigest())\n"
    )
    mission_id = "ms-" + "6" * 24
    try:
        chat["save_auth_policy"]({
            "kms": {"enabled": True, "provider": "local-kms-test", "keyId": "kms-test-key", "signCommand": f"python3 {signer}"},
        }, "admin")
        chat["update_mission"](
            mission_id,
            jobId="JOB-kms",
            taskId="DEV-900031",
            producer="steel-mission-chat mission-control",
            state="running",
            operatorRole="admin",
            profile="dc13.local",
            snapshotPolicySummary={"policyHash": "2" * 64, "includeCollections": ["missions"], "sourceCounts": {"missionRoots": 1}},
            auditCount=0,
        )
        ref = chat["write_mission_evidence"](
            mission_id,
            "delivery-plan",
            "delivery-plan",
            {"plan": "external signer"},
            task_id="DEV-900031",
            job_id="JOB-kms",
            operator_role="admin",
            summary="External signed evidence fixture.",
        )
        evidence = json.loads(Path(ref["path"]).read_text())
        assert evidence["integrity"]["signatureScheme"] == "external-kms-or-signer-v1"
        assert evidence["integrity"]["signerId"] == "kms-test-key"
        assert evidence["integrity"]["externalSigner"]["status"] == "succeeded"
        audit = chat["read_mission_audit"](mission_id)[0]
        assert schema_check.validate(audit, "canonical/mission-audit-event-v1.json") == []
    finally:
        globals_["MISSION_ROOT"] = original_root
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_required_external_signer_fails_closed(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    mission_id = "ms-" + "7" * 24
    try:
        chat["save_auth_policy"]({
            "kms": {
                "enabled": True,
                "provider": "missing-kms",
                "keyId": "missing-key",
                "signCommand": "python3 /definitely/not/a/signer.py",
                "requireExternalSigning": True,
            },
        }, "admin")
        chat["update_mission"](
            mission_id,
            jobId="JOB-kms-required",
            taskId="DEV-900032",
            producer="steel-mission-chat mission-control",
            state="running",
            operatorRole="admin",
            profile="dc13.local",
            snapshotPolicySummary={"policyHash": "3" * 64, "includeCollections": ["missions"], "sourceCounts": {"missionRoots": 1}},
            auditCount=0,
        )
        with pytest.raises(RuntimeError, match="external evidence signer is required"):
            chat["write_mission_evidence"](
                mission_id,
                "delivery-plan",
                "delivery-plan",
                {"plan": "must not be locally signed"},
                task_id="DEV-900032",
                job_id="JOB-kms-required",
                operator_role="admin",
                summary="Should fail before fallback signing.",
            )
    finally:
        globals_["MISSION_ROOT"] = original_root
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_control_plane_readiness_meets_alpha_and_production_targets(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_registry = globals_["INTEGRATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    original_runner_health = globals_["private_runner_health"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["INTEGRATION_REGISTRY_PATH"] = tmp_path / "integration-registry.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    signer = tmp_path / "readiness-signer.py"
    signer.write_text("import hashlib,json,sys\npayload=json.loads(sys.stdin.read())\nprint(hashlib.sha256(payload['recordHash'].encode()).hexdigest())\n")
    monkeypatch.setenv("GITHUB_TOKEN", "readiness-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "readiness-webhook-secret")
    globals_["private_runner_health"] = lambda: {
        "schemaVersion": 1,
        "ok": True,
        "status": "ready",
        "isolationLevel": "container",
        "productionEligible": True,
        "controls": ["ephemeral-worker", "no-container-runtime-socket"],
    }
    try:
        chat["save_auth_policy"]({
            "acceptedIssuers": ["present-local-alpha", "https://idp.example.invalid"],
            "acceptedAudiences": ["present-control-plane"],
            "identityBoundary": {"mode": "oidc-required", "allowLoopbackDevelopmentIdentity": False},
            "authorization": {"preventSelfApproval": True},
            "oidc": {
                "enabled": True,
                "issuer": "https://idp.example.invalid",
                "audience": "present-control-plane",
                "jwksUrl": "https://idp.example.invalid/jwks.json",
                "authorizationEndpoint": "https://idp.example.invalid/authorize",
                "tokenEndpoint": "https://idp.example.invalid/token",
                "clientId": "steel-mission",
            },
            "kms": {"enabled": True, "provider": "local-kms-test", "keyId": "readiness-key", "signCommand": f"python3 {signer}", "requireExternalSigning": True},
        }, "admin")
        chat["save_integration_registry"]({
            "connectors": [
                {"id": "siem", "label": "SIEM", "kind": "security-evidence-export", "enabled": True, "mode": "outbox", "events": ["audit", "evidence", "control-decision"]},
            ],
        }, "owner")
        payload = chat["control_plane_production_readiness"]("admin")
        assert payload["alphaScore"] >= 95
        assert payload["productionScore"] == 100
        assert payload["meetsAlphaTarget"] is True
        assert payload["meetsProductionTarget"] is True
        assert payload["entitlement"]["enterpriseEnabled"] is False
        assert payload["guardedEntrypoints"]["requiresSignedSession"] is True
        assert payload["guardedEntrypoints"]["guardedRunnerRequired"] is True
        assert payload["guardedEntrypoints"]["directCommandMode"] == "block"
        assert payload["evidenceSigner"]["ok"] is True
        assert {item["id"] for item in payload["checks"]} >= {
            "model-independent",
            "customer-controlled",
            "pre-execution-blocking",
            "tamper-evident-evidence",
            "baseline-auth",
        }
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["INTEGRATION_REGISTRY_PATH"] = original_registry
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        globals_["private_runner_health"] = original_runner_health


def test_present_control_plane_cli_executes_only_with_signed_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("guarded cli\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    env = {**os.environ, "PRESENT_MISSIONS_DIR": str(tmp_path / "missions"), "PRESENT_AUTH_SIGNING_KEY": "test-auth-key"}
    session_result = subprocess.run(
        [str(WORKER_DIR / "bin" / "present-control-plane"), "session", "--actor", "publisher@example.invalid", "--role", "publisher"],
        cwd=WORKER_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert session_result.returncode == 0, session_result.stderr
    token = json.loads(session_result.stdout)["session"]["accessToken"]
    denied = subprocess.run(
        [str(WORKER_DIR / "bin" / "present-control-plane"), "exec", "--json", json.dumps({
            "phase": "build",
            "repositoryPath": str(repo),
            "command": "python3 -c \"from pathlib import Path; Path('built.txt').write_text('ok')\"",
        })],
        cwd=WORKER_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert denied.returncode == 1
    assert "signed session is required" in denied.stderr
    allowed = subprocess.run(
        [str(WORKER_DIR / "bin" / "present-control-plane"), "exec", "--token", token, "--json", json.dumps({
            "phase": "build",
            "repositoryPath": str(repo),
            "command": "python3 -c \"from pathlib import Path; Path('built.txt').write_text('ok')\"",
        })],
        cwd=WORKER_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
    payload = json.loads(allowed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["preflight"]["decision"] == "allow"
    assert (repo / "built.txt").read_text() == "ok"


def test_private_runner_dry_run_is_hardened_attested_and_keeps_secrets_out_of_argv(tmp_path):
    key = "private-runner-test-key"
    request = signed_private_runner_request(
        tmp_path,
        key,
        environment={"GITHUB_TOKEN": "must-not-appear-in-argv"},
        stdin="bounded payload",
    )
    result = subprocess.run(
        [str(PRIVATE_RUNNER), "execute"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
        env={
            **os.environ,
            "PRESENT_PRIVATE_RUNNER_MODE": "dry-run",
            "PRESENT_PRIVATE_RUNNER_SIGNING_KEY": key,
        },
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert schema_check.validate(request, "canonical/private-runner-request-v1.json") == []
    assert schema_check.validate(payload, "canonical/private-runner-result-v1.json") == []
    argv = payload["runnerArgv"]
    for expected in (
        "--interactive", "--read-only", "--cap-drop", "ALL", "no-new-privileges:true",
        "--pids-limit", "--memory", "--cpus", "--user", "--tmpfs", "--mount", "--network", "none",
    ):
        assert expected in argv
    assert "must-not-appear-in-argv" not in json.dumps(argv)
    assert "GITHUB_TOKEN" in argv
    assert all("docker.sock" not in item and "/run/containerd" not in item for item in argv)
    assert payload["isolation"]["mode"] == "container"
    basis = {key_name: value for key_name, value in payload.items() if key_name != "attestation"}
    payload_hash = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert payload["attestation"]["payloadHash"] == payload_hash
    assert hmac.compare_digest(
        payload["attestation"]["signature"],
        hmac.new(key.encode(), payload_hash.encode(), hashlib.sha256).hexdigest(),
    )


def test_private_runner_rejects_tampering_broad_mounts_symlinks_and_runtime_escape_tools(tmp_path):
    key = "private-runner-adversarial-key"
    env = {
        **os.environ,
        "PRESENT_PRIVATE_RUNNER_MODE": "local",
        "PRESENT_PRIVATE_RUNNER_ALLOW_LOCAL": "1",
        "PRESENT_PRIVATE_RUNNER_SIGNING_KEY": key,
    }

    tampered = signed_private_runner_request(tmp_path, key)
    tampered["argv"] = ["python3", "-c", "print('tampered')"]
    cases = [(tampered, "attestation")]
    for workspace, label in ((Path("/"), "broad"), (Path.home(), "home")):
        cases.append((signed_private_runner_request(workspace, key), label))
    symlink = tmp_path.parent / f"{tmp_path.name}-workspace-link"
    symlink.symlink_to(tmp_path, target_is_directory=True)
    cases.append((signed_private_runner_request(symlink, key), "symlink"))
    cases.append((signed_private_runner_request(tmp_path, key, argv=["docker", "ps"]), "forbidden"))
    cases.append((signed_private_runner_request(tmp_path, key, argv=["python3", "/var/run/docker.sock"]), "socket"))

    for request, label in cases:
        result = subprocess.run(
            [str(PRIVATE_RUNNER), "execute"],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
            env=env,
        )
        assert result.returncode == 30, f"{label}: {result.stdout} {result.stderr}"
        payload = json.loads(result.stderr)
        assert payload["status"] == "blocked"


def test_control_plane_private_runner_filters_host_environment_and_verifies_result(tmp_path, monkeypatch):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    monkeypatch.setenv("PRESENT_AUTH_SIGNING_KEY", "control-plane-runner-key")
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-cross-boundary")
    result = chat["run_delivery_private_runner"](
        "python3 -c \"import os,sys; print(os.environ.get('HOST_ONLY_SECRET','missing') + '|' + sys.stdin.read())\"",
        tmp_path,
        {"missionId": "ms-" + "4" * 24, "taskId": "DEV-234567"},
        "inspect",
        stdin_text="connector-payload",
    )
    assert result["ok"] is True
    assert result["attestationVerification"]["valid"] is True
    assert result["isolation"]["mode"] == "development-local"
    assert result["stdout"].strip() == "missing|connector-payload"


def test_control_plane_private_runner_fails_closed_on_unattested_result(tmp_path, monkeypatch):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["control_policy"].__globals__
    original_policy = globals_["CONTROL_POLICY_PATH"]
    fake_runner = tmp_path / "fake-runner.py"
    fake_runner.write_text("import json\nprint(json.dumps({'schemaVersion':1,'ok':True,'status':'succeeded'}))\n")
    globals_["CONTROL_POLICY_PATH"] = tmp_path / "control-policy.json"
    monkeypatch.setenv("PRESENT_AUTH_SIGNING_KEY", "control-plane-runner-key")
    try:
        chat["save_control_policy"]({
            "executionBoundary": {
                "privateRunnerMode": "development-local",
                "privateRunnerCommand": ["python3", str(fake_runner)],
                "privateRunnerStatusCommand": ["python3", str(fake_runner)],
            },
        }, "owner")
        result = chat["run_delivery_private_runner"](
            "python3 -c \"print('should-not-be-trusted')\"",
            tmp_path,
            {"missionId": "ms-" + "5" * 24, "taskId": "DEV-345678"},
            "inspect",
        )
        assert result["ok"] is False
        assert result["status"] == "blocked"
        assert "attestation" in result["error"]
    finally:
        globals_["CONTROL_POLICY_PATH"] = original_policy


def test_steel_mission_connector_runtime_executes_configured_command_and_outbox(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["integration_registry"].__globals__
    original_registry = globals_["INTEGRATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    original_mission_root = globals_["MISSION_ROOT"]
    output = tmp_path / "connector.json"
    globals_["INTEGRATION_REGISTRY_PATH"] = tmp_path / "integration-registry.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    try:
        saved = chat["save_integration_registry"]({
            "connectors": [
                {
                    "id": "slack",
                    "label": "Slack",
                    "kind": "approval-notifications",
                    "enabled": True,
                    "mode": "command",
                    "command": f"python3 -c \"import pathlib,sys; pathlib.Path('{output}').write_text(sys.stdin.read())\"",
                    "events": ["approval-requested"],
                },
                {
                    "id": "siem",
                    "label": "SIEM",
                    "kind": "security-evidence-export",
                    "enabled": True,
                    "mode": "outbox",
                    "events": ["control-decision"],
                },
            ],
        }, "owner")
        assert any(item["id"] == "slack" and item["enabled"] for item in saved["connectors"])
        result = chat["execute_connector_action"]("slack", "approval-requested", {"missionId": "ms-" + "3" * 24}, role="admin")
        assert result["ok"] is True
        assert result["plan"]["interface"] == ["plan", "preflight", "execute", "observe", "evidence", "rollback/export"]
        assert result["plan"]["interactionModel"] == "workflow-embedded"
        assert result["plan"]["controlSurfaceRole"] == "administration-investigation-and-fallback"
        assert json.loads(output.read_text())["eventType"] == "approval-requested"
        outbox = chat["execute_connector_action"]("siem", "control-decision", {"decision": "allow"}, role="admin")
        assert outbox["ok"] is True
        assert Path(outbox["execution"]["path"]).exists()
        assert json.loads(Path(outbox["execution"]["path"]).read_text())["eventType"] == "control-decision"
    finally:
        globals_["INTEGRATION_REGISTRY_PATH"] = original_registry
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        globals_["MISSION_ROOT"] = original_mission_root


def test_signed_github_slack_and_jira_ingress_is_idempotent_and_preserves_origin(tmp_path, monkeypatch):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["integration_registry"].__globals__
    original_registry = globals_["INTEGRATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    original_mission_root = globals_["MISSION_ROOT"]
    original_start = globals_["start_orchestrated_mission"]
    globals_["INTEGRATION_REGISTRY_PATH"] = tmp_path / "integration-registry.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    starts = []

    def fake_start(template_id, objective, **kwargs):
        starts.append({"templateId": template_id, "objective": objective, **kwargs})
        suffix = f"{len(starts):024x}"
        return {"ok": True, "missionId": f"ms-{suffix}", "taskId": f"DEV-{len(starts):06d}"}

    globals_["start_orchestrated_mission"] = fake_start
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-secret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "slack-secret")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "jira-secret")
    try:
        saved = chat["save_integration_registry"]({
            "connectors": [
                {"id": "github", "enabled": True, "mode": "native", "adapter": "github", "secretEnv": "GITHUB_WEBHOOK_SECRET", "tokenEnv": "GITHUB_TOKEN", "ingressRole": "owner", "events": ["status"]},
                {"id": "slack", "enabled": True, "mode": "native", "adapter": "slack", "secretEnv": "SLACK_SIGNING_SECRET", "tokenEnv": "SLACK_BOT_TOKEN", "events": ["status"]},
                {"id": "jira", "enabled": True, "mode": "native", "adapter": "jira", "secretEnv": "JIRA_WEBHOOK_SECRET", "tokenEnv": "JIRA_API_TOKEN", "baseUrlEnv": "JIRA_BASE_URL", "events": ["status"]},
            ],
        }, "owner")
        assert next(item for item in saved["connectors"] if item["id"] == "github")["ingressRole"] == "user"

        github_payload = {
            "action": "created",
            "repository": {"full_name": "acme/widgets"},
            "issue": {"number": 42, "title": "CI is flaky", "body": "Investigate failures", "html_url": "https://github.example/acme/widgets/issues/42"},
            "comment": {"body": "/steel-mission find the flaky test", "html_url": "https://github.example/acme/widgets/issues/42#comment"},
            "sender": {"id": 17, "login": "octo-user"},
        }
        github_body = json.dumps(github_payload, separators=(",", ":")).encode()
        github_headers = {
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "delivery-42",
            "X-Hub-Signature-256": "sha256=" + hmac.new(b"github-secret", github_body, hashlib.sha256).hexdigest(),
        }
        status, github = chat["process_workflow_ingress"]("github", github_headers, github_body)
        assert status == 202 and github["status"] == "accepted"
        assert schema_check.validate(github["event"], "canonical/workflow-connector-event-v1.json") == []
        assert github["event"]["origin"]["repository"] == "acme/widgets"
        assert github["event"]["origin"]["issueNumber"] == "42"
        duplicate_status, duplicate = chat["process_workflow_ingress"]("github", github_headers, github_body)
        assert duplicate_status == 200 and duplicate["duplicate"] is True
        assert len(starts) == 1

        tampered_body = github_body.replace(b"flaky", b"unsafe", 1)
        denied_status, denied = chat["process_workflow_ingress"]("github", github_headers, tampered_body)
        assert denied_status == 401 and denied["ok"] is False

        timestamp = str(int(time.time()))
        slack_payload = {
            "type": "event_callback",
            "event_id": "Ev-42",
            "event": {"type": "app_mention", "user": "U123", "channel": "C456", "ts": "1720000000.100", "text": "<@BOT> investigate the failed deployment"},
        }
        slack_body = json.dumps(slack_payload, separators=(",", ":")).encode()
        slack_headers = {
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": "v0=" + hmac.new(b"slack-secret", b"v0:" + timestamp.encode() + b":" + slack_body, hashlib.sha256).hexdigest(),
        }
        status, slack = chat["process_workflow_ingress"]("slack", slack_headers, slack_body)
        assert status == 202 and slack["status"] == "accepted"
        assert slack["event"]["origin"]["channelId"] == "C456"
        assert slack["event"]["origin"]["threadTs"] == "1720000000.100"
        assert schema_check.validate(slack["event"], "canonical/workflow-connector-event-v1.json") == []

        ordinary_payload = {"type": "event_callback", "event_id": "Ev-ordinary", "event": {"type": "message", "user": "U123", "channel": "C456", "ts": "1720000000.200", "text": "ordinary team chat"}}
        ordinary_body = json.dumps(ordinary_payload, separators=(",", ":")).encode()
        ordinary_headers = {
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": "v0=" + hmac.new(b"slack-secret", b"v0:" + timestamp.encode() + b":" + ordinary_body, hashlib.sha256).hexdigest(),
        }
        status, ordinary = chat["process_workflow_ingress"]("slack", ordinary_headers, ordinary_body)
        assert status == 202 and ordinary["status"] == "ignored"

        replay_headers = {**slack_headers, "X-Slack-Request-Timestamp": str(int(timestamp) - 301)}
        replay_status, replay = chat["process_workflow_ingress"]("slack", replay_headers, slack_body)
        assert replay_status == 401 and replay["signature"]["replaySafe"] is False

        jira_payload = {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "OPS-42", "self": "https://jira.example/rest/api/2/issue/OPS-42", "fields": {"summary": "Deployment failed", "description": "Run governed investigation", "labels": ["steel-mission"]}},
            "user": {"accountId": "jira-user-1", "displayName": "Jira User"},
        }
        jira_body = json.dumps(jira_payload, separators=(",", ":")).encode()
        jira_headers = {
            "X-Atlassian-Webhook-Identifier": "jira-delivery-42",
            "X-Steel-Mission-Signature": "sha256=" + hmac.new(b"jira-secret", jira_body, hashlib.sha256).hexdigest(),
        }
        status, jira = chat["process_workflow_ingress"]("jira", jira_headers, jira_body)
        assert status == 202 and jira["status"] == "accepted"
        assert jira["event"]["origin"]["issueKey"] == "OPS-42"
        assert schema_check.validate(jira["event"], "canonical/workflow-connector-event-v1.json") == []
        assert len(starts) == 3
        assert all(item["templateId"] == "investigate" and item["operator_role"] == "user" for item in starts)
        assert starts[0]["workflow_origin"]["threadId"] == "github:acme/widgets:42"
    finally:
        globals_["INTEGRATION_REGISTRY_PATH"] = original_registry
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        globals_["MISSION_ROOT"] = original_mission_root
        globals_["start_orchestrated_mission"] = original_start


def test_native_workflow_egress_returns_to_github_slack_and_jira_origin(tmp_path, monkeypatch):
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["integration_registry"].__globals__
    original_registry = globals_["INTEGRATION_REGISTRY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    original_urlopen = globals_["urlopen"]
    globals_["INTEGRATION_REGISTRY_PATH"] = tmp_path / "integration-registry.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    calls = []

    class FakeResponse:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.body

    def fake_urlopen(request, timeout=30):
        calls.append({
            "url": request.full_url,
            "headers": {key.lower(): value for key, value in request.header_items()},
            "body": json.loads(request.data.decode()),
            "timeout": timeout,
        })
        body = b'{"ok":true,"ts":"1720000000.300"}' if request.full_url == "https://slack.com/api/chat.postMessage" else b'{"id":"comment-1"}'
        return FakeResponse(body)

    globals_["urlopen"] = fake_urlopen
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "slack-secret")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "jira-secret")
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example")
    try:
        chat["save_integration_registry"]({
            "connectors": [
                {"id": "github", "enabled": True, "mode": "native", "adapter": "github", "secretEnv": "GITHUB_WEBHOOK_SECRET", "tokenEnv": "GITHUB_TOKEN", "events": ["status"]},
                {"id": "slack", "enabled": True, "mode": "native", "adapter": "slack", "secretEnv": "SLACK_SIGNING_SECRET", "tokenEnv": "SLACK_BOT_TOKEN", "events": ["status"]},
                {"id": "jira", "enabled": True, "mode": "native", "adapter": "jira", "secretEnv": "JIRA_WEBHOOK_SECRET", "tokenEnv": "JIRA_API_TOKEN", "baseUrlEnv": "JIRA_BASE_URL", "events": ["status"]},
            ],
        }, "owner")
        common_payload = {"missionId": "ms-" + "6" * 24, "summary": "Tests are passing", "investigationPath": "/mission/ms-test"}
        github = chat["execute_connector_action"]("github", "status", {**common_payload, "origin": {"sourceSystem": "github", "repository": "acme/widgets", "issueNumber": "42", "threadId": "github:acme/widgets:42"}})
        slack = chat["execute_connector_action"]("slack", "status", {**common_payload, "origin": {"sourceSystem": "slack", "channelId": "C456", "threadTs": "1720000000.100", "threadId": "slack:C456:1720000000.100"}})
        jira = chat["execute_connector_action"]("jira", "status", {**common_payload, "origin": {"sourceSystem": "jira", "issueKey": "OPS-42", "threadId": "jira:OPS-42"}})
        assert github["ok"] is True and slack["ok"] is True and jira["ok"] is True
        assert calls[0]["url"] == "https://api.github.com/repos/acme/widgets/issues/42/comments"
        assert calls[1]["url"] == "https://slack.com/api/chat.postMessage"
        assert calls[1]["body"]["channel"] == "C456"
        assert calls[1]["body"]["thread_ts"] == "1720000000.100"
        assert calls[2]["url"] == "https://jira.example/rest/api/2/issue/OPS-42/comment"
        assert all("Tests are passing" in call["body"].get("body", call["body"].get("text", "")) for call in calls)
        assert calls[0]["headers"]["authorization"] == "Bearer github-token"
        assert calls[1]["headers"]["authorization"] == "Bearer slack-token"
        assert calls[2]["headers"]["authorization"] == "Bearer jira-token"

        mismatched = chat["execute_connector_action"]("github", "status", {**common_payload, "origin": {"sourceSystem": "slack", "channelId": "C456"}})
        assert mismatched["ok"] is True and mismatched["execution"]["status"] == "skipped"
        assert len(calls) == 3
    finally:
        globals_["INTEGRATION_REGISTRY_PATH"] = original_registry
        globals_["MUTATION_LEDGER_PATH"] = original_ledger
        globals_["urlopen"] = original_urlopen


def test_steel_mission_github_pr_adapter_builds_native_readiness_without_creating_pr(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:northstar-forge/steel-mission-demo.git"], cwd=repo, check=True)
    (repo / "README.md").write_text("native pr adapter\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "codex/native-pr"], cwd=repo, check=True)
    (repo / "feature.txt").write_text("changed\n")

    payload = chat["delivery_step_payload"](
        {
            "mock": False,
            "missionId": "ms-" + "1" * 24,
            "objective": "Prepare native PR evidence.",
            "deliveryContext": {
                "repositoryPath": str(repo),
                "branch": "codex/native-pr",
                "baseBranch": "master",
                "prProvider": "github",
                "prMode": "readiness",
                "ciProvider": "github-actions",
                "ciRequired": False,
                "repairBudget": 1,
            },
            "missionUsers": [],
            "capabilityWorkSet": [],
        },
        {"nodeId": "pr-readiness", "phase": "pr"},
    )

    assert payload["ok"] is True
    assert payload["adapter"]["kind"] == "github.pr"
    assert payload["prReadiness"]["provider"] == "github"
    assert payload["prReadiness"]["githubRepository"] == "northstar-forge/steel-mission-demo"
    assert payload["prReadiness"]["github"]["mode"] == "readiness"
    assert payload["prReadiness"]["github"]["commandPreview"][:3] == ["gh", "pr", "create"]
    assert payload["ciReadiness"]["provider"] == "github-actions"
    assert payload["ciReadiness"]["required"] is False
    assert payload["changeSet"]["files"][0]["path"] == "feature.txt"


def test_steel_mission_delivery_execution_uses_isolated_worktree_and_proof_pack(tmp_path, monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_users = globals_["USER_REGISTRY_PATH"]
    original_snapshot = globals_["snapshot_scope"]
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("isolated delivery\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["USER_REGISTRY_PATH"].write_text(json.dumps({"schemaVersion": 1, "users": []}))
    globals_["snapshot_scope"] = lambda profile=None: []
    task_id = None
    job_id = None
    try:
        started = chat["start_orchestrated_mission"](
            "delivery-execution",
            "Prove isolated delivery worktree.",
            mock=False,
            operator_role="publisher",
            profile="dc13.local",
            delivery_context={
                "repositoryPath": str(repo),
                "baseBranch": base_branch,
                "deliveryBranch": "codex/delivery-isolated-test",
                "worktreeMode": "isolated",
                "modifyCommand": "python3 -c \"from pathlib import Path; Path('feature.txt').write_text('ok')\"",
                "buildCommand": "python3 -c \"from pathlib import Path; assert Path('feature.txt').read_text() == 'ok'\"",
                "testCommand": "python3 -c \"print('tests ok')\"",
                "inspectCommand": "python3 -c \"print('inspect ok')\"",
                "prProvider": "github",
                "prMode": "readiness",
                "prTarget": "northstar-forge/steel-mission-demo",
                "prTitle": "Isolated delivery proof",
                "ciProvider": "github-actions",
                "ciRequired": False,
                "deployProvider": "manual",
                "deployTarget": "local alpha",
                "deployEnvironment": "preview",
                "deployHealthCommand": "python3 -c \"print('deploy health ok')\"",
                "rollbackCommand": "python3 -c \"print('rollback ready')\"",
                "repairBudget": 1,
            },
        )
        mission_id = started["missionId"]
        job_id = started["jobId"]
        task_id = started["taskId"]
        deadline = time.time() + 8
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "waiting_for_approval":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert chat["approve_mission"](mission_id, "publisher", "Approved isolated delivery.")["ok"] is True
        deadline = time.time() + 12
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "done":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "done"
        assert mission["deliveryContext"]["worktreeMode"] == "isolated"
        assert mission["deliveryWorkspace"]["ok"] is True
        assert mission["deliveryWorkspace"]["branch"] == "codex/delivery-isolated-test"
        worktree = Path(mission["deliveryWorkspace"]["path"])
        assert (worktree / "feature.txt").read_text() == "ok"
        assert not (repo / "feature.txt").exists()
        proof = chat["mission_proof_bundle"](mission_id, "publisher")
        assert proof["ok"] is True
        payload = proof["proof"]["payload"]
        assert payload["deliveryWorkspace"]["path"] == str(worktree)
        assert payload["changeSet"]["fileCount"] == 1
        assert payload["changeSet"]["files"][0]["path"] == "feature.txt"
        assert {item["phase"] for item in payload["adapterManifest"]} >= {"modify", "build", "test", "inspect", "pr", "deploy"}
        assert payload["prReadiness"]["title"] == "Isolated delivery proof"
        assert payload["ciReadiness"]["provider"] == "github-actions"
        assert payload["deployReadiness"]["healthCommandConfigured"] is True
        assert payload["deployReadiness"]["rollbackConfigured"] is True
        assert {item["phase"] for item in payload["controlDecisions"]} >= {"modify", "build", "test", "inspect", "pr", "deploy"}
        assert payload["complianceEvidence"]["standards"]["SOC 2"]
        assert payload["complianceEvidence"]["standards"]["ISO 27001"]
        assert payload["complianceEvidence"]["standards"]["ISO 42001"]
        assert payload["integrityChain"]["recordCount"] >= len(payload["evidence"])
        assert payload["integrityChain"]["latestHash"]
        report_ref = mission["deliveryReportRef"]
        assert Path(report_ref["path"]).read_text().startswith("# Agentic Software Delivery Proof")
        pack_ref = mission["deliveryProofPackRef"]
        assert Path(pack_ref["path"]).exists()
        with zipfile.ZipFile(pack_ref["path"]) as archive:
            names = set(archive.namelist())
        assert {"proof.json", "delivery-report.md", "changes.patch", "siem-events.jsonl", "proof-pack-manifest.json"} <= names
        assert any(name.startswith("evidence/") for name in names)
        report = chat["mission_report_markdown"](mission_id, "publisher")
        assert "## Adapters" in report["markdown"]
        assert "## Change Set" in report["markdown"]
        assert "## CI Readiness" in report["markdown"]
        assert "## Pre-Execution Controls" in report["markdown"]
        assert "## Compliance Evidence" in report["markdown"]
        siem = chat["mission_siem_jsonl"](mission_id, "publisher")
        assert siem["ok"] is True
        assert '"eventType":"control-decision"' in siem["jsonl"]
        assert '"eventType":"integrity"' in siem["jsonl"]
    finally:
        if job_id:
            with lock:
                jobs.pop(job_id, None)
        globals_["MISSION_ROOT"] = original_root
        globals_["USER_REGISTRY_PATH"] = original_users
        globals_["snapshot_scope"] = original_snapshot
        if task_id:
            purge_task(task_id)


def test_steel_mission_delivery_closure_proofs_work_in_two_consecutive_runs(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_users = globals_["USER_REGISTRY_PATH"]
    original_snapshot = globals_["snapshot_scope"]
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("delivery closure\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["USER_REGISTRY_PATH"].write_text(json.dumps({"schemaVersion": 1, "users": []}))
    globals_["snapshot_scope"] = lambda profile=None: []
    task_ids: list[str] = []
    job_ids: list[str] = []

    def run_delivery(index: int) -> dict[str, object]:
        build_file = f"build-{index}.txt"
        started = chat["start_orchestrated_mission"](
            "delivery-execution",
            f"Close delivery run {index}.",
            mock=False,
            operator_role="publisher",
            profile="dc13.local",
            actor_user_id=f"publisher-{index}",
            delivery_context={
                "repositoryPath": str(repo),
                "branch": branch,
                "buildCommand": f"python3 -c \"from pathlib import Path; Path('{build_file}').write_text('ok')\"",
                "testCommand": f"python3 -c \"from pathlib import Path; assert Path('{build_file}').read_text() == 'ok'\"",
                "inspectCommand": "python3 -c \"print('inspect ok')\"",
                "prTarget": "northstar-forge/steel-mission-demo",
                "deployTarget": "local alpha",
                "repairBudget": 1,
            },
        )
        mission_id = started["missionId"]
        job_ids.append(started["jobId"])
        task_ids.append(started["taskId"])
        deadline = time.time() + 8
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "waiting_for_approval":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "waiting_for_approval"
        assert mission["actorUserId"] == f"publisher-{index}"
        assert chat["approve_mission"](mission_id, "publisher", "Approved for closure proof.", f"publisher-{index}")["ok"] is True
        deadline = time.time() + 10
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "done":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "done"
        assert mission["deliveryClosure"]["status"] == "ready_for_deploy"
        assert mission["deliveryClosure"]["ok"] is True
        assert mission["deliveryClosure"]["repairAttempts"] == 0
        proof = chat["mission_proof_bundle"](mission_id, "publisher")
        assert proof["ok"] is True
        assert proof["proof"]["payload"]["closureGate"]["status"] == "ready_for_deploy"
        assert proof["proof"]["payload"]["missionId"] == mission_id
        assert proof["proof"]["payload"]["adapterManifest"]
        assert proof["proof"]["payload"]["prReadiness"]["target"] == "northstar-forge/steel-mission-demo"
        report = chat["mission_report_markdown"](mission_id, "publisher")
        assert report["ok"] is True
        assert "Agentic Software Delivery Proof" in report["markdown"]
        assert "ready_for_deploy" in report["markdown"]
        assert "## PR Readiness" in report["markdown"]
        assert Path(mission["deliveryReportRef"]["path"]).exists()
        detail_html = chat["render_mission_detail_page"](mission_id, "publisher")
        assert "Delivery report" in detail_html
        assert "Proof JSON" in detail_html
        assert mission_id in detail_html
        return {"missionId": mission_id, "proofRef": mission["deliveryProofRef"], "buildFile": build_file}

    try:
        first = run_delivery(1)
        second = run_delivery(2)
        assert first["missionId"] != second["missionId"]
        assert first["proofRef"]["path"] != second["proofRef"]["path"]
        assert (repo / first["buildFile"]).read_text() == "ok"
        assert (repo / second["buildFile"]).read_text() == "ok"
    finally:
        with lock:
            for job_id in job_ids:
                jobs.pop(job_id, None)
        globals_["MISSION_ROOT"] = original_root
        globals_["USER_REGISTRY_PATH"] = original_users
        globals_["snapshot_scope"] = original_snapshot
        for task_id in task_ids:
            purge_task(task_id)


def test_steel_mission_delivery_repair_budget_reruns_failed_build(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    jobs = chat["JOBS"]
    lock = chat["JOBS_LOCK"]
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    original_users = globals_["USER_REGISTRY_PATH"]
    original_snapshot = globals_["snapshot_scope"]
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("repair closure\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    globals_["USER_REGISTRY_PATH"] = tmp_path / "users.json"
    globals_["USER_REGISTRY_PATH"].write_text(json.dumps({"schemaVersion": 1, "users": []}))
    globals_["snapshot_scope"] = lambda profile=None: []
    task_id = None
    job_id = None
    try:
        started = chat["start_orchestrated_mission"](
            "delivery-execution",
            "Repair and close delivery.",
            mock=False,
            operator_role="publisher",
            profile="dc13.local",
            delivery_context={
                "repositoryPath": str(repo),
                "branch": branch,
                "buildCommand": "python3 -c \"from pathlib import Path; import sys; sys.exit(0 if Path('fixed.txt').exists() else 1)\"",
                "testCommand": "python3 -c \"from pathlib import Path; assert Path('fixed.txt').read_text() == 'yes'\"",
                "inspectCommand": "python3 -c \"print('inspect ok')\"",
                "repairCommand": "python3 -c \"from pathlib import Path; Path('fixed.txt').write_text('yes')\"",
                "prTarget": "northstar-forge/steel-mission-demo",
                "deployTarget": "local alpha",
                "repairBudget": 1,
            },
        )
        mission_id = started["missionId"]
        job_id = started["jobId"]
        task_id = started["taskId"]
        deadline = time.time() + 8
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "waiting_for_approval":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert chat["approve_mission"](mission_id, "publisher", "Approved repair.")["ok"] is True
        deadline = time.time() + 10
        mission = chat["read_mission_record"](mission_id)
        while time.time() < deadline and mission and mission["state"] != "done":
            time.sleep(0.05)
            mission = chat["read_mission_record"](mission_id)
        assert mission is not None
        assert mission["state"] == "done"
        assert mission["repairAttemptsUsed"] == 1
        assert mission["deliveryClosure"]["repairAttempts"] == 1
        assert mission["deliveryClosure"]["ok"] is True
        assert "delivery-repair-attempted" in [event["action"] for event in chat["read_mission_audit"](mission_id)]
        assert (repo / "fixed.txt").read_text() == "yes"
    finally:
        if job_id:
            with lock:
                jobs.pop(job_id, None)
        globals_["MISSION_ROOT"] = original_root
        globals_["USER_REGISTRY_PATH"] = original_users
        globals_["snapshot_scope"] = original_snapshot
        if task_id:
            purge_task(task_id)


def test_steel_mission_mutation_ledger_records_config_changes(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["mutation_ledger_path"].__globals__
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutation-ledger.jsonl"
    try:
        event = chat["record_mutation"](
            "runtime-profile-saved",
            "admin",
            tmp_path / "runtime-profiles.json",
            before={"profiles": []},
            after={"profiles": [{"id": "dc13.local"}]},
            details={"profileId": "dc13.local"},
        )
        assert event["changed"] is True
        assert schema_check.validate(event, "canonical/mutation-ledger-event-v1.json") == []
        ledger = chat["read_mutation_ledger"]("admin")
        assert ledger["ok"] is True
        assert ledger["mutations"][0]["mutationId"] == event["mutationId"]
        assert chat["read_mutation_ledger"]("user")["ok"] is False
    finally:
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_steel_mission_startup_supervisor_marks_running_orchestrated_mission_resumable(tmp_path):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["mission_dir"].__globals__
    original_root = globals_["MISSION_ROOT"]
    globals_["MISSION_ROOT"] = tmp_path / "missions"
    mission_id = "ms-" + "4" * 24
    try:
        chat["update_mission"](
            mission_id,
            jobId="JOB-orphan",
            taskId="DEV-900145",
            state="running",
            operatorRole="admin",
            mock=True,
            missionKind="orchestrated",
            templateId="investigate",
            templateTitle="Investigate",
            objective="Resume after restart.",
            question="Resume after restart.",
            profile="dc13.local",
            snapshotPolicySummary={
                "sourceProfile": "test",
                "policyHash": "c" * 64,
                "includeCollections": ["missions"],
                "sourceCounts": {"missionRoots": 1},
            },
            nodes=[{
                "nodeId": "steel-mission-readout",
                "title": "Delivery Coordinator readout",
                "kind": "coordination-report",
                "capability": "dc13.coordination-report",
                "state": "running",
                "attempts": 1,
                "evidenceRefs": [],
            }],
            evidenceLedger=[],
            approvals=[],
            resumable=True,
        )
        result = chat["supervise_missions_on_startup"]()
        mission = chat["read_mission_record"](mission_id)
        assert result["orphaned"] == 1
        assert mission["state"] == "paused"
        assert mission["orphanedAtStartup"] is True
        assert schema_check.validate(mission, "canonical/mission-control-v1.json") == []
        assert chat["read_mission_audit"](mission_id)[-1]["action"] == "mission-orphaned-at-startup"
    finally:
        globals_["MISSION_ROOT"] = original_root


def test_coordinator_state_snapshot_includes_mission_control_audit(tmp_path):
    cli = _load_cli_module()
    mission_id = "ms-" + "1" * 24
    task_id = "DEV-900034"
    root = tmp_path / "missions" / mission_id
    root.mkdir(parents=True)
    mission = {
        "schemaVersion": 1,
        "missionId": mission_id,
        "jobId": "JOB-mission",
        "taskId": task_id,
        "producer": "steel-mission-chat mission-control",
        "createdAt": common.utc_now(),
        "updatedAt": common.utc_now(),
        "state": "running",
        "operatorRole": "admin",
        "missionKind": "orchestrated",
        "templateId": "verify",
        "templateTitle": "Verify",
        "objective": "Check schema readiness.",
        "profile": "dc13.auto",
        "nodes": [
            {
                "nodeId": "schema-gate",
                "title": "Schema gate",
                "kind": "schema-gate",
                "capability": "schema-authority.validate",
                "state": "done",
                "resultSummary": "Schema gate completed.",
            }
        ],
        "evidenceLedger": [
            {
                "evidenceId": "me-" + "3" * 24,
                "nodeId": "schema-gate",
                "kind": "schema-gate",
                "sha256": "b" * 64,
                "producedAt": common.utc_now(),
                "summary": "Schema gate completed.",
            }
        ],
        "snapshotPolicySummary": {
            "sourceProfile": "mission-fixture",
            "policyHash": "a" * 64,
            "includeCollections": ["missions"],
            "sourceCounts": {"missionRoots": 1},
        },
        "auditCount": 1,
        "lastPhase": "Reconciling mission evidence",
    }
    audit = {
        "schemaVersion": 1,
        "auditId": "ma-" + "2" * 24,
        "missionId": mission_id,
        "taskId": task_id,
        "jobId": "JOB-mission",
        "producedAt": common.utc_now(),
        "actor": "worker",
        "operatorRole": "admin",
        "action": "model-event-observed",
        "summary": "Model stream opened",
        "details": {},
        "artifactRefs": [],
    }
    (root / "mission.json").write_text(json.dumps(mission))
    (root / "audit.jsonl").write_text(json.dumps(audit) + "\n")
    policy = {
        "schemaVersion": 1,
        "sourceProfile": "mission-fixture",
        "includeCollections": ["missions"],
        "limits": {
            "tasks": 0,
            "advisoryTasks": 0,
            "buildJobs": 0,
            "verifyResults": 0,
            "distributedWorkflows": 0,
            "brokerStateSources": 0,
            "brokerArtifacts": 0,
            "operatorAudit": 8,
            "generalDocuments": 0,
            "missions": 5,
        },
        "sources": {
            "taskRoots": [],
            "logRoots": [],
            "buildJobRoots": [],
            "missionRoots": [str(tmp_path / "missions")],
            "verifyResultRoots": [],
            "brokerStatePaths": [],
            "repositoryRoots": [],
            "documentPaths": [],
        },
    }

    snapshot = cli._coordinator_state_snapshot(policy)

    assert snapshot["missions"][0]["missionId"] == mission_id
    assert snapshot["missions"][0]["templateId"] == "verify"
    assert snapshot["missions"][0]["nodes"][0]["state"] == "done"
    assert snapshot["missions"][0]["evidenceLedger"][0]["sha256"] == "b" * 64
    assert snapshot["missions"][0]["evidenceCount"] == 1
    assert snapshot["missions"][0]["recentAudit"][0]["action"] == "model-event-observed"
    collections = {item["name"]: item for item in snapshot["snapshotCollections"]}
    assert collections["missions"]["returned"] == 1
    assert {item["collection"] for item in snapshot["sourceExclusions"]} == {
        "tasks", "advisoryTasks", "buildJobs", "verifyResults", "distributedWorkflows", "generalDocuments"}


def test_steel_mission_final_answer_renders_as_copyable_plain_text():
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    report = {
        "summary": "The broker hardening slice is implemented.",
        "packIdentity": {"probe": "ok", "corpusGeneration": "test"},
        "items": [{
            "subject": "Broker state",
            "status": "VERIFIED",
            "stateClass": "canonical",
            "freshness": "current",
            "source": "broker-state.json",
            "note": "The query API returns workflow status.",
        }],
        "notChecked": [{"subject": "Cloud credentials", "reason": "not configured"}],
        "contradictions": [],
        "advisoryNote": "Advisory only.",
        "jobNarratives": [{
            "summary": "DEV-900143: 4 recorded steps, status SUCCEEDED.",
            "plainText": "\n".join([
                "1. The control plane admitted workflow wf-ops for task DEV-900143.",
                "2. The control plane placed node plan on worker macbook-local:ops.",
                "3. Worker macbook-local:ops ran node verify and reported SUCCEEDED.",
                "4. Deterministic acceptance finished with decision PASS: evidence accepted.",
            ]),
        }],
        "acceptanceDiagnostics": [{
            "taskId": "DEV-900143",
            "decision": "INCONCLUSIVE",
            "checks": [{
                "id": "mock-evidence",
                "status": "INCONCLUSIVE",
                "detail": "mock evidence is present",
            }],
            "remediation": [{
                "checkId": "mock-evidence",
                "action": "rerun the workflow without --mock to produce gate-eligible evidence",
            }],
        }],
        "followUps": [{
            "revision": 1,
            "createdAt": "2026-08-18T00:00:01Z",
            "role": "user",
            "intent": "scope-change",
            "content": "Narrow this to acceptance blockers only.",
            "effect": "restart-active-run",
        }],
    }
    text = chat["plain_answer_text"](report)
    assert "The broker hardening slice is implemented." in text
    assert "- Broker state [VERIFIED]. The query API returns workflow status. Source: broker-state.json." in text
    assert "- Cloud credentials: not configured" in text
    assert "What happened in the background:" in text
    assert "The control plane placed node plan on worker macbook-local:ops." in text
    assert "Deterministic acceptance finished with decision PASS" in text
    assert "Acceptance status:" in text
    assert "rerun the workflow without --mock" in text
    assert "Follow-up updates:" in text
    assert "Narrow this to acceptance blockers only." in text
    html = chat["render_job"]("JOB1", {
        "state": "done",
        "ok": True,
        "taskId": "DEV-900019",
        "durationSeconds": 12,
        "payload": report,
    })
    assert 'class="answer-text"' in html
    assert '<section class="panel">' not in html
    assert "The broker hardening slice is implemented." in html
    assert "What happened in the background:" in html
    assert "The control plane placed node plan on worker macbook-local:ops." in html
    assert "Acceptance status:" in html
    assert "Follow-up updates:" in html


def test_chat_api_payload_includes_live_progress():
    """The JS chat poller should see the same progress as the HTML job page."""
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["job_api_payload"].__globals__
    original = globals_["read_progress"]
    original_narratives = globals_["broker_narratives"]
    original_diagnostics = globals_["broker_acceptance_diagnostics"]
    narrative = {
        "summary": "DEV-900143: 2 live recorded steps, status DISTRIBUTED_RUNNING.",
        "plainText": "\n".join([
            "1. The control plane dispatched node plan to worker macbook-local:ops.",
            "2. Worker macbook-local:ops finished node plan with status SUCCEEDED.",
        ]),
    }
    try:
        globals_["read_progress"] = lambda task_id: {
            "phase": f"Reconciling {task_id}",
            "thinkingTokens": 321,
            "firstEventSeconds": 1.2,
            "elapsedSeconds": 5,
            "timeline": [
                {"elapsedSeconds": 0.2, "label": "Snapshot ready", "detail": "Prepared worker state"},
                {"elapsedSeconds": 1.2, "label": "Thinking started", "detail": "Model began reconciling"},
            ],
        }
        globals_["broker_narratives"] = lambda limit=3: [narrative]
        globals_["broker_acceptance_diagnostics"] = lambda limit=3: [{
            "taskId": "DEV-900143",
            "decision": "INCONCLUSIVE",
            "checks": [{"id": "mock-evidence", "status": "INCONCLUSIVE", "detail": "mock evidence is present"}],
            "remediation": [{"checkId": "mock-evidence", "action": "rerun without --mock"}],
        }]
        payload = chat["job_api_payload"]("JOB1", {
            "state": "running", "taskId": "DEV-900022", "startedEpoch": time.time() - 20,
        })
        assert payload["ok"] is False
        assert payload["jobId"] == "JOB1"
        assert payload["progress"]["phase"] == "Reconciling DEV-900022"
        assert payload["progress"]["thinkingTokens"] == 321
        assert payload["progress"]["jobElapsedSeconds"] >= 19
        assert payload["progress"]["silentSeconds"] >= 14
        assert payload["progress"]["timeline"][-1]["label"] == "Waiting for stream update"
        assert payload["progress"]["timeline"][-2]["label"] == "Thinking started"
        assert payload["progress"]["jobNarratives"][0]["summary"].startswith("DEV-900143")
        assert payload["progress"]["acceptanceDiagnostics"][0]["decision"] == "INCONCLUSIVE"

        html = chat["render_running"]("JOB1", {
            "state": "running", "taskId": "DEV-900022", "startedEpoch": time.time() - 20,
        })
        assert "What Is Happening In The Background" in html
        assert "The control plane dispatched node plan" in html
        assert "Acceptance Status" in html
        assert "rerun without --mock" in html
    finally:
        globals_["read_progress"] = original
        globals_["broker_narratives"] = original_narratives
        globals_["broker_acceptance_diagnostics"] = original_diagnostics


def test_steel_mission_chat_routes_coordinator_report_to_configured_role(monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    captured: dict = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def communicate(self, input_text, timeout):
            captured["bundle"] = json.loads(input_text)
            captured["timeout"] = timeout
            return json.dumps({"schemaVersion": 1, "summary": "ok"}), ""

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProcess()

    def fake_run(cmd, **kwargs):
        class FakeCompleted:
            returncode = 0
            stdout = json.dumps({
                "schemaVersion": 1,
                "runtimeProfile": {
                    "schemaVersion": 1,
                    "id": "dc13.local",
                    "label": "DC13 Local / Glimmer",
                    "status": "active",
                    "modelRole": "dc13.coordination-report",
                    "modelProvider": "glimmer",
                    "snapshotProfile": "worker-local-glimmer-fallback",
                    "defaultFor": [],
                    "editableBy": ["local-user", "org-admin"],
                    "visibilityRoleKeys": ["DC13", "DC11", "DC12"],
                    "registryPath": "/tmp/runtime-profiles.json",
                    "registryHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "resolvedAt": "2026-08-18T00:00:00Z",
                },
                "modelPolicy": {
                    "schemaVersion": 1,
                    "role": "dc13.coordination-report",
                    "selectedModel": "qwen2.5-coder:14b",
                    "provider": "glimmer",
                    "transport": "ollama",
                    "snapshotProfile": "worker-local-glimmer-fallback",
                },
                "snapshotPolicy": {
                    "schemaVersion": 1,
                    "sourceProfile": "worker-local-glimmer-fallback",
                    "includeCollections": ["tasks"],
                    "limits": {
                        "tasks": 12,
                        "advisoryTasks": 2,
                        "buildJobs": 12,
                        "verifyResults": 12,
                        "distributedWorkflows": 4,
                        "brokerStateSources": 4,
                        "brokerArtifacts": 6,
                        "operatorAudit": 4,
                    },
                    "sources": {
                        "taskRoots": ["/tmp/present/tasks"],
                        "logRoots": ["/tmp/present/logs"],
                        "buildJobRoots": ["/tmp/present/jobs"],
                        "verifyResultRoots": ["/tmp/present/test-results"],
                        "brokerStatePaths": ["/tmp/present/state.json"],
                        "repositoryRoots": [],
                    },
                },
            })
        return FakeCompleted()

    monkeypatch.setenv(chat["COORDINATOR_PROVIDER_ENV"], "glimmer")
    monkeypatch.setattr(chat["subprocess"], "Popen", fake_popen)
    monkeypatch.setattr(chat["subprocess"], "run", fake_run)

    result = chat["run_coordinator_report"]("DEV-900144", "Where are we?", [], False)

    assert result["ok"] is True
    assert "--provider" not in captured["cmd"]
    assert "--role" not in captured["cmd"]
    assert "--profile" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--profile") + 1] == "dc13.local"
    assert captured["bundle"]["contract"]["runtimeProfile"]["id"] == "dc13.local"
    assert captured["bundle"]["contract"]["runtimeProfile"]["modelRole"] == "dc13.coordination-report"
    assert captured["bundle"]["contract"]["modelPolicy"]["role"] == "dc13.coordination-report"
    assert captured["bundle"]["contract"]["modelPolicy"]["provider"] == "glimmer"
    assert captured["bundle"]["contract"]["modelPolicy"]["selectedModel"] == "qwen2.5-coder:14b"
    assert captured["bundle"]["contract"]["snapshotPolicy"]["sourceProfile"] == "worker-local-glimmer-fallback"


def test_steel_mission_health_summary_reports_configured_glimmer_model(monkeypatch):
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))

    def fake_run(cmd, **kwargs):
        class FakeCompleted:
            returncode = 0
            stdout = json.dumps({
                "schemaVersion": 1,
                "runtimeProfile": {
                    "schemaVersion": 1,
                    "id": "dc13.local",
                    "label": "DC13 Local / Glimmer",
                    "status": "active",
                    "modelRole": "dc13.coordination-report",
                    "modelProvider": "glimmer",
                    "snapshotProfile": "worker-local-glimmer-fallback",
                    "defaultFor": [],
                    "editableBy": ["local-user", "org-admin"],
                    "visibilityRoleKeys": ["DC13"],
                    "registryPath": "/tmp/runtime-profiles.json",
                    "registryHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "resolvedAt": "2026-08-18T00:00:00Z",
                },
                "modelPolicy": {
                    "schemaVersion": 1,
                    "role": "dc13.coordination-report",
                    "selectedModel": "qwen2.5-coder:14b",
                    "provider": "glimmer",
                    "transport": "ollama",
                    "snapshotProfile": "worker-local-glimmer-fallback",
                },
                "snapshotPolicy": {"schemaVersion": 1, "includeCollections": ["tasks"], "limits": {}, "sources": {}},
            })

        if "glimmer" in cmd:
            FakeCompleted.stdout = json.dumps({
                "provider": "glimmer",
                "model": "qwen2.5-coder:14b",
                "model_loaded": True,
                "ready": True,
            })
        return FakeCompleted()

    monkeypatch.setenv(chat["COORDINATOR_PROVIDER_ENV"], "glimmer")
    monkeypatch.setattr(chat["subprocess"], "run", fake_run)

    summary = chat["cos_provider_summary"]()

    assert summary == {
        "profile": "dc13.local",
        "profileLabel": "DC13 Local / Glimmer",
        "role": "dc13.coordination-report",
        "provider": "glimmer",
        "model": "qwen2.5-coder:14b",
        "modelLoaded": True,
        "ready": True,
    }




def test_incomplete_pack_identity_becomes_an_honest_failed_probe(tmp_path):
    """A pack-identity failure is a report that says so, not a protocol error.

    Authority ruling (protocol-2.1-coordination-report, 2026-08-17): the failed branch
    is a successful transport of an honest advisory failure report -- never
    guessed identity, and not automatically exit 30. Exit 30 stays for
    transport corruption or an inability to build a valid failure report.
    """
    cli = _load_cli_module()
    original = cli.COORDINATOR_PACK_MANIFEST
    manifest = tmp_path / "DC13_Delivery-Coordinator_pack.manifest.json"
    manifest.write_text(json.dumps({"package_id": "DC13 incomplete"}))
    try:
        cli.COORDINATOR_PACK_MANIFEST = manifest
        identity = cli._coordinator_pack_identity()
        assert identity["probe"] == "failed"
        assert identity["reason"] and "incomplete" in identity["reason"]
        assert "packageId" not in identity, "a failed probe must not report guessed identity"

        cli.COORDINATOR_PACK_MANIFEST = tmp_path / "absent.json"
        gone = cli._coordinator_pack_identity()
        assert gone["probe"] == "failed" and gone["reason"]
    finally:
        cli.COORDINATOR_PACK_MANIFEST = original


def test_failed_probe_report_is_canonical_and_makes_no_model_call():
    """The failed branch requires no model call -- the cheapest thing to get wrong."""
    called = []
    original = claude_adapter._invoke
    try:
        claude_adapter._invoke = lambda *a, **k: called.append(1) or ({}, None)
        report = claude_adapter.coordinator_report(
            "DEV-900031", "live", "where are we?", {},
            {"probe": "failed", "reason": "pack manifest could not be read"})
    finally:
        claude_adapter._invoke = original
    assert not called, "a failed probe must not invoke the model"
    assert report["items"] == [] and report["contradictions"] == []
    assert report["notChecked"], "the unreachable sources must be named"
    assert "unavailable" in report["summary"]
    assert schema_check.validate(report, "canonical/coordination-report-v1.json") == []


def test_steel_mission_request_uses_the_command_free_authority_contract():
    """`task-contract-v1` is frozen; command-free requests use the additive one."""
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    model_policy = {
        "schemaVersion": 1,
        "role": "dc13.coordination-report",
        "selectedModel": "qwen2.5-coder:14b",
        "provider": "glimmer",
        "transport": "ollama",
        "snapshotProfile": "focused-user-request",
    }
    runtime_profile = {
        "schemaVersion": 1,
        "id": "dc13.focused",
        "label": "DC13 Focused",
        "status": "active",
        "modelRole": "dc13.coordination-report",
        "modelProvider": "glimmer",
        "snapshotProfile": "focused-user-request",
        "defaultFor": [],
        "editableBy": ["local-user"],
        "visibilityRoleKeys": ["DC13", "DC11", "DC12"],
        "registryPath": "/tmp/runtime-profiles.json",
        "registryHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "resolvedAt": "2026-08-18T00:00:00Z",
    }
    contract = chat["build_bundle"]("DEV-900032", "Where are we?", {
        "schemaVersion": 1,
        "sourceProfile": "focused-user-request",
        "includeCollections": ["tasks", "verifyResults"],
        "limits": {
            "tasks": 10,
            "advisoryTasks": 0,
            "buildJobs": 0,
            "verifyResults": 10,
            "distributedWorkflows": 0,
            "brokerStateSources": 0,
            "brokerArtifacts": 0,
            "operatorAudit": 0,
        },
        "taskSelector": {"mode": "latest"},
        "sources": {
            "taskRoots": [str(common.TASKS_DIR)],
            "logRoots": [str(WORKER_DIR / "logs")],
            "buildJobRoots": [],
            "verifyResultRoots": [str(common.TEST_RESULTS_DIR)],
            "brokerStatePaths": [],
            "repositoryRoots": [],
        },
    }, model_policy, runtime_profile)["contract"]
    assert contract["verb"] == "coordination-report"
    assert contract["advisory"] is True and contract["verificationAuthority"] is False
    assert "verification" not in contract and "build" not in contract
    assert contract["provenance"]["source"] == "worker-local-advisory-client"
    assert contract["runtimeProfile"] == runtime_profile
    assert contract["modelPolicy"] == model_policy
    assert contract["snapshotPolicy"]["sourceProfile"] == "focused-user-request"
    assert schema_check.validate(contract, "canonical/coordination-report-request-v1.json") == []

    # Accepted for the advisory verb, refused for every pipeline verb.
    assert common._validate_contract_for_verb(contract, "DEV-900032", "coordination-report") == contract
    for verb in ("verify", "fix", "build", "plan"):
        try:
            common._validate_contract_for_verb(contract, "DEV-900032", verb)
        except common.TaskBundleError:
            pass
        else:
            raise AssertionError(f"{verb} must not accept the advisory request contract")


def test_steel_mission_request_snapshot_policy_rejects_unknown_sources():
    import runpy
    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    contract = chat["build_bundle"]("DEV-900033", "Where are we?", {
        "schemaVersion": 1,
        "includeCollections": ["tasks", "gmail"],
        "limits": {
            "tasks": 10,
            "advisoryTasks": 0,
            "buildJobs": 0,
            "verifyResults": 0,
            "distributedWorkflows": 0,
            "brokerStateSources": 0,
            "brokerArtifacts": 0,
            "operatorAudit": 0,
        },
        "sources": {
            "taskRoots": [str(common.TASKS_DIR)],
            "logRoots": [str(WORKER_DIR / "logs")],
            "buildJobRoots": [],
            "verifyResultRoots": [],
            "brokerStatePaths": [],
            "repositoryRoots": [],
        },
    }, {
        "schemaVersion": 1,
        "role": "dc13.coordination-report",
        "selectedModel": "qwen2.5-coder:14b",
        "provider": "glimmer",
        "transport": "ollama",
        "snapshotProfile": "worker-local-glimmer-fallback",
    }, {
        "schemaVersion": 1,
        "id": "dc13.local",
        "label": "DC13 Local / Glimmer",
        "status": "active",
        "modelRole": "dc13.coordination-report",
        "modelProvider": "glimmer",
        "snapshotProfile": "worker-local-glimmer-fallback",
        "defaultFor": [],
        "editableBy": ["local-user"],
        "visibilityRoleKeys": ["DC13"],
        "registryPath": "/tmp/runtime-profiles.json",
        "registryHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "resolvedAt": "2026-08-18T00:00:00Z",
    })["contract"]

    errors = schema_check.validate(contract, "canonical/coordination-report-request-v1.json")
    assert errors
    assert "gmail" in errors[0]


# The four guards below all work, and none of them had a test: mutation testing
# on 2026-08-17 disabled each in turn and the whole suite still passed. They are
# the oldest input-validation layer, untouched while newer work accumulated
# coverage elsewhere. Fixtures here are locally authored on purpose -- these are
# worker-local regressions, not authority conformance, and must not sit among
# the vendored fixtures in schemas/fixtures/.
def _contract_with_verification(task_id: str, verification: dict) -> dict:
    """A well-formed v1 contract whose verification section is the variable."""
    return {
        "schemaVersion": 1, "taskId": task_id, "producedAt": "2026-08-16T20:00:00Z",
        "producer": "pytest", "provenance": {"source": "control-plain"},
        "verification": verification,
    }


def _command(**overrides) -> dict:
    command = {"name": "declared-command", "argv": ["/usr/bin/true"],
               "expectedExitCode": 0, "timeoutSeconds": 10}
    command.update(overrides)
    return command


def test_argv_must_be_a_bounded_list_of_clean_strings():
    """Command injection: argv is the only thing the worker ever executes.

    Each case is an otherwise-valid command object varying argv alone, so a
    rejection can only come from argv validation and not from some earlier
    shape check -- which is what let this go untested: the existing
    shell-string test passes strings where command objects belong and dies
    before argv is ever examined.
    """
    task_id = "DEV-900041"
    common.validate_task_contract(
        _contract_with_verification(task_id, {"target": "worker", "commands": [_command()]}), task_id)

    for label, argv in {
        "a shell string instead of a list": "true; echo pwned",
        "an argument containing NUL": ["/usr/bin/true", "a\x00b"],
        "33 arguments (limit is 32)": ["/usr/bin/true"] * 33,
        "an empty list": [],
        "a non-string argument": ["/usr/bin/true", 7],
        "an empty argument": ["/usr/bin/true", ""],
    }.items():
        contract = _contract_with_verification(
            task_id, {"target": "worker", "commands": [_command(argv=argv)]})
        try:
            common.validate_task_contract(contract, task_id)
        except common.TaskBundleError as exc:
            assert "argv" in str(exc), f"{label} was rejected, but not for argv: {exc}"
        else:
            raise AssertionError(f"argv with {label} must be rejected")


def test_verification_target_must_be_allowlisted():
    """Scope escape: the target decides which tree a command runs against."""
    task_id = "DEV-900042"
    for target in ("worker", "present-repository"):
        common.validate_task_contract(
            _contract_with_verification(task_id, {"target": target, "commands": [_command()]}), task_id)

    for label, target in {"an absolute path": "/etc", "a traversal": "../../etc",
                          "an unknown name": "corpus", "a non-string": 1}.items():
        contract = _contract_with_verification(
            task_id, {"target": target, "commands": [_command()]})
        try:
            common.validate_task_contract(contract, task_id)
        except common.TaskBundleError as exc:
            assert "allowlist" in str(exc), f"{label} rejected for the wrong reason: {exc}"
        else:
            raise AssertionError(f"a verification target that is {label} must be rejected")


def test_binary_rejects_a_malformed_task_id_without_the_ssh_guard():
    """Path traversal: the task id becomes a directory name.

    The transport is covered elsewhere; this covers the binary itself, which
    the chat server and any local caller invoke directly with no guard in
    front of it. The bundle carries the same malformed id throughout, so the
    identity check cannot reject it first and the id pattern is the only guard
    left standing -- without that, this passes whether or not the guard exists.

    The safe id is checked first and the assertion stops the loop, so a
    regression is caught before any traversing id is ever handed to the worker.
    """
    tasks = common.TASKS_DIR
    for bad in ("DEV-ABCDEF", "DEV-1", "DEV-0000001", "../../etc/passwd"):
        bundle = json.loads(_bundle("DEV-000001", ["/usr/bin/true"]))
        bundle["taskId"] = bad
        bundle["task"]["taskId"] = bad
        bundle["contract"]["taskId"] = bad
        before = {p.name for p in tasks.iterdir()} if tasks.exists() else set()
        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "verify", bad, input_text=json.dumps(bundle))
        after = {p.name for p in tasks.iterdir()} if tasks.exists() else set()
        assert after == before, f"{bad!r} staged {after - before}"
        assert code == 30, f"{bad!r} must exit 30, got {code}"
        assert stdout == "", f"{bad!r} must leave stdout empty, got {stdout[:80]!r}"
        assert payload.get("status") == "PROTOCOL_ERROR", f"{bad!r} must report PROTOCOL_ERROR"
        assert "task id" in payload.get("reason", ""), (
            f"{bad!r} must be rejected by the id pattern, not another guard: {payload}")


def test_oversize_bundle_is_refused_before_it_is_parsed():
    """Resource exhaustion: the bundle is read from stdin before anything validates it."""
    task_id = "DEV-900043"
    purge_task(task_id)
    try:
        bundle = json.loads(_bundle(task_id, ["/usr/bin/true"]))
        bundle["requirement"] += "A" * (common.MAX_BUNDLE_BYTES + 1)
        oversize = json.dumps(bundle)
        assert len(oversize.encode()) > common.MAX_BUNDLE_BYTES

        code, stdout_payload, payload, stdout, stderr = run_worker_result(
            "verify", task_id, input_text=oversize)
        assert code == 30
        assert stdout == ""
        assert payload.get("status") == "PROTOCOL_ERROR"
        assert "bytes" in payload.get("reason", ""), f"must name the size limit: {payload}"
        assert not (common.TASKS_DIR / task_id).exists(), "nothing may be staged"
    finally:
        purge_task(task_id)


def test_project_and_milestone_schemas_are_registered_and_coherent():
    """The delivery-planning records must be real contracts, not loose files.

    A schema that exists but is unregistered validates nothing, and a registry
    entry whose file is missing is worse: it claims a contract that cannot be
    enforced. Both directions are checked.
    """
    registry = json.loads((WORKER_DIR / "schemas" / "schema-registry.json").read_text())
    entries = {entry["id"]: entry for entry in registry["schemas"]}

    for schema_id in ("project-v1", "milestone-v1"):
        assert schema_id in entries, f"{schema_id} is not in the schema registry"
        entry = entries[schema_id]
        path = WORKER_DIR / "schemas" / "canonical" / entry["schemaFile"]
        assert path.exists(), f"{schema_id} is registered but {entry['schemaFile']} is absent"
        schema = json.loads(path.read_text())
        assert schema["additionalProperties"] is False, f"{schema_id} must be closed"

    # Every registered schema resolves, not only the two added here.
    for entry in registry["schemas"]:
        assert (WORKER_DIR / "schemas" / "canonical" / entry["schemaFile"]).exists(), entry["id"]


def test_milestone_schedules_work_without_acquiring_authority():
    """A milestone organises tasks; it never becomes evidence about them.

    Membership is scheduling. If a milestone could imply verification or
    completion, closing one would silently upgrade the state of every task in
    it -- the same collapse the verifier exists to prevent.
    """
    milestone = {
        "schemaVersion": 1, "milestoneId": "MS-0001", "title": "Alpha hardening",
        "state": "ACTIVE", "createdAt": "2026-08-19T00:00:00Z",
        "projectId": "PRJ-0001", "targetDate": "2026-09-30",
        "outcome": "Trust boundaries are enforced and evidenced.",
        "notInScope": "Production deployment.",
        "taskIds": ["DEV-000123"], "completionEvidence": "All linked tasks closed.",
    }
    assert schema_check.validate(milestone, "canonical/milestone-v1.json") == []

    schema = json.loads(
        (WORKER_DIR / "schemas" / "canonical" / "milestone-v1.json").read_text())
    states = set(schema["properties"]["state"]["enum"])
    assert "PASS" not in states and "VERIFIED" not in states, (
        "a milestone state must never read as a verification outcome")

    for label, bad in {
        "a task id in the milestone id field": {**milestone, "milestoneId": "DEV-000123"},
        "an unknown property": {**milestone, "gateEligible": True},
        "a task id that is not DEV-NNNNNN": {**milestone, "taskIds": ["MS-0001"]},
        "an invented state": {**milestone, "state": "PASSED"},
    }.items():
        assert schema_check.validate(bad, "canonical/milestone-v1.json"), (
            f"a milestone with {label} must be rejected")


def test_project_owns_milestones_and_stays_closed():
    project = {
        "schemaVersion": 1, "projectId": "PRJ-0001", "title": "Steel Mission alpha",
        "state": "ACTIVE", "createdAt": "2026-08-19T00:00:00Z",
        "summary": "Governed agent delivery plane.", "milestoneIds": ["MS-0001"],
    }
    assert schema_check.validate(project, "canonical/project-v1.json") == []
    for label, bad in {
        "a milestone id in the project id field": {**project, "projectId": "MS-0001"},
        "a task id where a milestone belongs": {**project, "milestoneIds": ["DEV-000123"]},
        "an unknown property": {**project, "gateEligible": True},
    }.items():
        assert schema_check.validate(bad, "canonical/project-v1.json"), (
            f"a project with {label} must be rejected")


def test_container_client_gets_a_login_path_that_can_actually_complete(tmp_path, monkeypatch):
    """A published container port makes every browser non-loopback.

    The peer is the container network gateway, never 127.0.0.1, so development
    identity is refused for the first API call. The 401 used to advertise
    /auth/login unconditionally; in development-local mode that route began an
    OIDC flow with no provider configured, so the browser was sent to a route
    that could only report OIDC disabled. The page loaded and nothing worked.
    """
    import runpy
    from types import SimpleNamespace

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutations.jsonl"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    try:
        chat["save_auth_policy"]({
            "identityBoundary": {"mode": "development-local", "allowLoopbackDevelopmentIdentity": True},
        }, "owner")

        # The refusal names the address it saw. Behind a container port that
        # address is the whole diagnosis, and it used to be absent.
        bridge = SimpleNamespace(headers={"X-Present-Role": "admin"}, client_address=("172.18.0.1", 51000))
        with pytest.raises(PermissionError) as refused:
            chat["authenticate_http_request"](bridge, "/api/knowledge", "GET")
        assert "172.18.0.1" in str(refused.value)

        # In development-local mode the advertised path is one that can complete.
        assert chat["development_login_available"]() is True
        assert chat["login_path_for"]() == "/auth/login"
        assert chat["unauthenticated_payload"]("denied")["loginPath"] == "/auth/login"

        # With OIDC required and no provider enabled, no path can complete, so
        # none is advertised. Sending a browser to a dead end is worse than
        # telling it there is nowhere to go.
        chat["save_auth_policy"]({"identityBoundary": {"mode": "oidc-required"}}, "owner")
        assert chat["development_login_available"]() is False
        assert chat["login_path_for"]() is None
        assert "loginPath" not in chat["unauthenticated_payload"]("denied")
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_development_session_authenticates_a_non_loopback_client(tmp_path, monkeypatch):
    """The signed session is what makes a containerised browser work.

    It is verified exactly as a bearer token is, so the sign-in page grants no
    authority of its own: it turns a token the operator already had into a
    browser cookie.
    """
    import runpy
    from types import SimpleNamespace

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutations.jsonl"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    try:
        chat["save_auth_policy"]({
            "identityBoundary": {"mode": "development-local", "allowLoopbackDevelopmentIdentity": True},
        }, "owner")
        issued = chat["issue_control_plane_session"]("riley-chen", "admin")
        token = str(issued["accessToken"])

        # Same non-loopback peer as above, now carrying the session cookie.
        signed_in = SimpleNamespace(
            headers={"Cookie": f"present_session={token}"},
            client_address=("172.18.0.1", 51000),
        )
        actor = chat["authenticate_http_request"](signed_in, "/api/knowledge", "GET")
        assert actor["actorId"] == "riley-chen"
        assert actor["sessionVerified"] is True

        # A token that was not issued here is refused, from any address.
        forged = SimpleNamespace(
            headers={"Cookie": "present_session=not-a-real-token"},
            client_address=("172.18.0.1", 51000),
        )
        with pytest.raises(PermissionError):
            chat["authenticate_http_request"](forged, "/api/knowledge", "GET")
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_the_development_sign_in_page_actually_renders():
    """Rendering, not just the helpers that decide to render it.

    The first version of this page was built with %-formatting and it embeds a
    stylesheet. A stylesheet is full of characters % reads as conversion
    specifiers, so the route raised ValueError and the browser got no response at
    all -- while every helper around it tested green.
    """
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    page = chat["DEVELOPMENT_LOGIN_PAGE"].replace("__CONTAINER__", "steel-mission")
    assert "__CONTAINER__" not in page
    assert 'action="/auth/login"' in page and 'name="token"' in page
    assert "present-control-plane session" in page
    page.encode("utf-8")


def test_stale_session_cookie_does_not_lock_out_a_loopback_developer(tmp_path, monkeypatch):
    """Cookies are not scoped by port, so a container's session reaches a dev server.

    A session issued by the container on one port is sent by the browser to a
    development server on another. It fails verification, the console follows the
    login path it is handed, and a developer is asked to sign in to a server that
    would have admitted them with no credential at all. The cookie is ambient --
    the browser attached it, the caller never offered it -- so it is discarded and
    cleared rather than treated as a failed assertion.
    """
    import runpy
    from types import SimpleNamespace

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    globals_ = chat["auth_policy"].__globals__
    original_auth = globals_["AUTH_POLICY_PATH"]
    original_ledger = globals_["MUTATION_LEDGER_PATH"]
    globals_["AUTH_POLICY_PATH"] = tmp_path / "auth-policy.json"
    globals_["MUTATION_LEDGER_PATH"] = tmp_path / "mutations.jsonl"
    monkeypatch.delenv("PRESENT_IDENTITY_MODE", raising=False)
    try:
        chat["save_auth_policy"]({
            "identityBoundary": {"mode": "development-local", "allowLoopbackDevelopmentIdentity": True},
        }, "owner")

        stale = SimpleNamespace(
            headers={"Cookie": "present_session=not.a.valid.token", "X-Present-Role": "owner"},
            client_address=("127.0.0.1", 51000),
        )
        actor = chat["authenticate_http_request"](stale, "/api/knowledge", "GET")
        assert actor["sessionVerified"] is False
        assert actor["cookieAuthenticated"] is False

        # And it is cleared on the way out, so the next request is clean without
        # anyone having to open developer tools.
        expiring = [v for n, v in getattr(stale, "response_headers", []) if n == "Set-Cookie"]
        assert any(v.startswith("present_session=;") and "Max-Age=0" in v for v in expiring)

        # A token the caller deliberately asserted is still an error. The
        # distinction is the whole point: a header is a claim, a cookie is ambient.
        asserted = SimpleNamespace(
            headers={"Authorization": "Bearer not.a.valid.token"},
            client_address=("127.0.0.1", 51000),
        )
        with pytest.raises(PermissionError):
            chat["authenticate_http_request"](asserted, "/api/knowledge", "GET")

        # Off loopback there is no development identity to fall back to, so a bad
        # cookie is still a refusal rather than a way in.
        remote = SimpleNamespace(
            headers={"Cookie": "present_session=not.a.valid.token", "X-Present-Role": "owner"},
            client_address=("172.18.0.1", 51000),
        )
        with pytest.raises(PermissionError):
            chat["authenticate_http_request"](remote, "/api/knowledge", "GET")

        # A valid session is unaffected.
        issued = chat["issue_control_plane_session"]("riley-chen", "admin")
        good = SimpleNamespace(
            headers={"Cookie": f"present_session={issued['accessToken']}"},
            client_address=("127.0.0.1", 51000),
        )
        assert chat["authenticate_http_request"](good, "/api/knowledge", "GET")["sessionVerified"] is True
    finally:
        globals_["AUTH_POLICY_PATH"] = original_auth
        globals_["MUTATION_LEDGER_PATH"] = original_ledger


def test_chat_start_response_reports_the_actor_the_job_was_recorded_against():
    """The console should not have to re-derive who owns a job it just created.

    A job is owned by whoever posted it, and the poll is refused if it arrives as
    anyone else. Making the console resolve its identity a second time means the
    two only have to disagree once -- a session that expires, a cookie discarded
    mid-run, an edited actor field -- for the server to refuse someone the chat
    they are looking at. The server states the owner instead.
    """
    import runpy

    chat = runpy.run_path(str(WORKER_DIR / "steel-mission-chat" / "server.py"))
    source = (WORKER_DIR / "steel-mission-chat" / "server.py").read_text()
    start = source.index('"jobId": job_id, "state": "running"')
    response = source[start:start + 240]
    assert '"actorUserId"' in response and '"operatorRole"' in response, (
        "the chat start response does not say which actor the job was recorded against"
    )
