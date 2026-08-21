#!/usr/bin/env python3
"""Disposable C1 four-stage mission bench.

The bench rehearses the D8/D9 pipeline without becoming a product dispatch path.
Its only durable contract is the agent-session status feed it validates before
every append. Runtime grants, worktrees, checkpoints, and evidence packs live in
an explicitly supplied state directory outside the product repository.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters import schema_check  # noqa: E402


SESSION_SCHEMA = "canonical/agent-session-status-v1.json"
AUTHORITY_PATHS = ("schemas/canonical/", "schemas/schema-registry.json", "docs/workplan.md")
STAGE_DETAILS = {
    "plan": ("plan", "claude", "planner", "opus"),
    "develop": ("develop-and-commit", "local", "developer", "qwen2.5-coder:14b"),
    "review": ("review-loop", "codex", "reviewer", "codex"),
    "acceptance": ("final-review-and-merge", "claude", "acceptance", "opus"),
}


class BenchError(RuntimeError):
    """The mission stopped without redefining its grant or definition of done."""


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"grant is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchError("grant must be a JSON object")
    return value


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BenchError(f"{label} must be a non-empty string")
    return value.strip()


def require_argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise BenchError(f"{label} must be a non-empty argv array")
    return list(value)


def validate_grant(grant: dict[str, Any]) -> dict[str, Any]:
    if grant.get("schemaVersion") != 1:
        raise BenchError("grant schemaVersion must be 1")
    mission_id = require_text(grant.get("missionId"), "missionId")
    if not re.fullmatch(r"ms-[a-f0-9]{24}", mission_id):
        raise BenchError("missionId must use the canonical mission id shape")
    repository = require_text(grant.get("repository"), "repository")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BenchError("repository must be owner/name")
    issue_number = grant.get("issueNumber")
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
        raise BenchError("issueNumber must be a positive integer")
    require_text(grant.get("baseBranch"), "baseBranch")
    require_text(grant.get("branch"), "branch")
    require_text(grant.get("grantedAt"), "grantedAt")
    require_text(grant.get("grantedBy"), "grantedBy")
    require_text(grant.get("requirement"), "requirement")
    require_text(grant.get("acceptanceEvidence"), "acceptanceEvidence")

    definition = grant.get("definitionOfDone")
    if not isinstance(definition, dict):
        raise BenchError("definitionOfDone must be an object")
    for key in ("redTest", "test", "releaseCheck"):
        require_argv(definition.get(key), f"definitionOfDone.{key}")

    budgets = grant.get("budgets")
    if not isinstance(budgets, dict):
        raise BenchError("budgets must be an object")
    for stage in STAGE_DETAILS:
        value = budgets.get(stage)
        if not isinstance(value, dict):
            raise BenchError(f"budgets.{stage} must be an object")
        for key in ("elapsedSeconds", "turns"):
            amount = value.get(key)
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
                raise BenchError(f"budgets.{stage}.{key} must be a positive integer")

    abort_conditions = grant.get("abortConditions")
    if not isinstance(abort_conditions, list) or not abort_conditions:
        raise BenchError("abortConditions must be a non-empty array")
    if not all(isinstance(item, str) and item.strip() for item in abort_conditions):
        raise BenchError("abortConditions entries must be non-empty strings")
    rounds = grant.get("maxReviewRounds")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 10:
        raise BenchError("maxReviewRounds must be between 1 and 10")

    accounts = grant.get("machineAccounts")
    if not isinstance(accounts, dict):
        raise BenchError("machineAccounts must be an object")
    logins: list[str] = []
    for worker in ("claude", "codex", "local"):
        account = accounts.get(worker)
        if not isinstance(account, dict):
            raise BenchError(f"machineAccounts.{worker} must be an object")
        login = require_text(account.get("login"), f"machineAccounts.{worker}.login")
        require_text(account.get("email"), f"machineAccounts.{worker}.email")
        token_env = require_text(account.get("tokenEnv"), f"machineAccounts.{worker}.tokenEnv")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", token_env):
            raise BenchError(f"machineAccounts.{worker}.tokenEnv must name an environment variable")
        if any(key in account for key in ("token", "accessToken", "secret")):
            raise BenchError("grants carry credential references, never credential values")
        logins.append(login)
    if len(set(logins)) != len(logins):
        raise BenchError("each model worker requires a distinct machine account")

    decision = grant.get("decisionApi")
    if not isinstance(decision, dict):
        raise BenchError("decisionApi must be an object")
    require_text(decision.get("baseUrl"), "decisionApi.baseUrl")
    token_env = decision.get("tokenEnv")
    if token_env is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]+", require_text(token_env, "decisionApi.tokenEnv")):
        raise BenchError("decisionApi.tokenEnv must name an environment variable")
    return grant


def issue_section(body: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise BenchError(f"issue is missing its {heading!r} section")
    return match.group("body").strip()


def command_tail(result: CommandResult, limit: int = 4000) -> str:
    text = (result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr).strip()
    return text[-limit:]


def isolated_command_env(session_dir: Path, credential_envs: set[str]) -> dict[str, str]:
    """Remove GitHub authority from repository-authored and model commands."""
    gh_config = session_dir / "no-github-credentials"
    gh_config.mkdir(parents=True, exist_ok=True)
    environment = {
        "GH_CONFIG_DIR": str(gh_config),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "false",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "remote.origin.pushurl",
        "GIT_CONFIG_VALUE_0": "disabled://mission-bench-command-cannot-push",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_2": "",
    }
    environment.update({name: "" for name in credential_envs})
    return environment


class SubprocessRunner:
    """Run argv-only commands with bounded output and no implicit shell."""

    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int,
        *,
        input_text: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> CommandResult:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise BenchError("refusing a non-argv command")
        environment = os.environ.copy()
        environment.pop("GH_TOKEN", None)
        environment.pop("GITHUB_TOKEN", None)
        if extra_env:
            environment.update(extra_env)
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=max(1, timeout))
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise BenchError(f"command exceeded its {timeout}s budget: {argv[0]}") from exc
        return CommandResult(
            argv=list(argv),
            returncode=process.returncode,
            stdout=stdout[-20000:],
            stderr=stderr[-20000:],
            elapsed_seconds=time.monotonic() - started,
        )


class StageBudget:
    def __init__(self, value: dict[str, int]):
        self.limit_seconds = int(value["elapsedSeconds"])
        self.limit_turns = int(value["turns"])
        self.started = time.monotonic()
        self.turns = 0

    def elapsed(self) -> int:
        return max(0, int(time.monotonic() - self.started))

    def remaining(self) -> int:
        remaining = self.limit_seconds - self.elapsed()
        if remaining < 1:
            raise BenchError("session elapsed-time budget is exhausted")
        return remaining

    def use_turn(self) -> int:
        if self.turns >= self.limit_turns:
            raise BenchError("session turn budget is exhausted")
        self.turns += 1
        return self.remaining()

    def spent(self) -> dict[str, int]:
        return {"elapsedSeconds": self.elapsed(), "turns": self.turns}

    def limit(self) -> dict[str, int]:
        return {"elapsedSeconds": self.limit_seconds, "turns": self.limit_turns}


class GitHubPlatform:
    """Git and GitHub side effects, separated so the orchestration can be failure-injected."""

    def __init__(self, repository_root: Path, runner: SubprocessRunner | None = None):
        self.repository_root = repository_root.resolve()
        self.runner = runner or SubprocessRunner()

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 120,
        input_text: str = "",
        extra_env: dict[str, str] | None = None,
        label: str | None = None,
    ) -> CommandResult:
        result = self.runner.run(
            argv,
            cwd or self.repository_root,
            timeout,
            input_text=input_text,
            extra_env=extra_env,
        )
        if result.returncode != 0:
            detail = command_tail(result) or f"exit {result.returncode}"
            raise BenchError(f"{label or argv[0]} failed: {detail}")
        return result

    @staticmethod
    def _account_env(grant: dict[str, Any], worker: str) -> dict[str, str]:
        account = grant["machineAccounts"][worker]
        token_env = account["tokenEnv"]
        token = os.environ.get(token_env)
        if not token:
            raise BenchError(f"machine-account credential {token_env} is not available")
        return {"GH_TOKEN": token}

    def issue(self, grant: dict[str, Any]) -> dict[str, Any]:
        result = self._run([
            "gh", "issue", "view", str(grant["issueNumber"]),
            "--repo", grant["repository"],
            "--json", "number,title,body,labels,url,assignees",
        ], label="issue read")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise BenchError("issue read returned a non-object")
        return value

    def validate_machine_accounts(self, grant: dict[str, Any]) -> None:
        seen: set[str] = set()
        for worker in ("claude", "codex", "local"):
            expected = grant["machineAccounts"][worker]["login"]
            result = self._run(
                ["gh", "api", "user", "--jq", ".login"],
                extra_env=self._account_env(grant, worker),
                label=f"{worker} machine-account check",
            )
            actual = result.stdout.strip()
            if actual != expected:
                raise BenchError(f"{worker} credential belongs to {actual!r}, expected {expected!r}")
            if actual in seen:
                raise BenchError("machine-account credentials resolve to duplicate GitHub users")
            seen.add(actual)

    def validate_repository_wall(self, grant: dict[str, Any]) -> None:
        result = self._run([
            "gh", "api",
            f"repos/{grant['repository']}/branches/{grant['baseBranch']}/protection",
        ], label="branch protection read")
        try:
            protection = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BenchError("branch protection read returned invalid JSON") from exc
        reviews = protection.get("required_pull_request_reviews") if isinstance(protection, dict) else {}
        approvals = reviews.get("required_approving_review_count") if isinstance(reviews, dict) else 0
        conversations = protection.get("required_conversation_resolution") if isinstance(protection, dict) else {}
        checks = protection.get("required_status_checks") if isinstance(protection, dict) else {}
        required_checks = checks.get("checks") if isinstance(checks, dict) else []
        if not isinstance(approvals, int) or approvals < 1:
            raise BenchError("branch protection must require an approving review so acceptance triggers merge")
        if not isinstance(reviews, dict) or reviews.get("require_code_owner_reviews") is not True:
            raise BenchError("branch protection must require code-owner review")
        if not isinstance(conversations, dict) or conversations.get("enabled") is not True:
            raise BenchError("branch protection must require review conversation resolution")
        if not isinstance(required_checks, list) or not required_checks:
            raise BenchError("branch protection must require continuous-integration checks")
        acceptance_login = grant["machineAccounts"]["claude"]["login"]
        try:
            codeowners = (self.repository_root / ".github" / "CODEOWNERS").read_text().splitlines()
        except OSError as exc:
            raise BenchError(f"CODEOWNERS is unavailable: {exc}") from exc
        default_owner = next((line for line in codeowners if line.startswith("* ") or line.startswith("*\t")), "")
        default_owners = {token.lstrip("@") for token in default_owner.split()[1:]}
        if default_owners != {acceptance_login}:
            raise BenchError("Claude acceptance account must be the sole default CODEOWNER")
        authority_patterns = ("/schemas/canonical/", "/schemas/schema-registry.json", "/docs/workplan.md")
        for pattern in authority_patterns:
            line = next((item for item in codeowners if item.split(maxsplit=1)[0:1] == [pattern]), "")
            owners = {token.lstrip("@") for token in line.split()[1:]}
            if not owners or acceptance_login in owners:
                raise BenchError(f"{pattern} must remain Person-owned in CODEOWNERS")

    def claim_issue(self, grant: dict[str, Any], session_id: str) -> None:
        account = grant["machineAccounts"]["local"]
        environment = self._account_env(grant, "local")
        self._run([
            "gh", "issue", "edit", str(grant["issueNumber"]),
            "--repo", grant["repository"], "--add-assignee", account["login"],
        ], extra_env=environment, label="issue assignment")
        self._run([
            "gh", "issue", "comment", str(grant["issueNumber"]),
            "--repo", grant["repository"],
            "--body", f"Mission session `{session_id}` claimed this issue under its granted requirement and budgets.",
        ], extra_env=environment, label="issue claim comment")

    def prepare_worktree(self, grant: dict[str, Any], session_dir: Path) -> Path:
        worktree = session_dir / "worktree"
        session_dir.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            raise BenchError(f"isolated worktree already exists: {worktree}")
        remote = self.runner.run(
            ["git", "remote", "get-url", "origin"], self.repository_root, 30
        )
        if remote.returncode == 0:
            self._run(
                ["git", "fetch", "origin", grant["baseBranch"]],
                timeout=180,
                label="base branch fetch",
            )
            base_ref = f"origin/{grant['baseBranch']}"
        else:
            base_ref = grant["baseBranch"]
        self._run([
            "git", "worktree", "add", "-b", grant["branch"], str(worktree), base_ref,
        ], timeout=120, label="isolated worktree creation")
        local = grant["machineAccounts"]["local"]
        self._run(["git", "config", "user.name", local["login"]], cwd=worktree, label="git author name")
        self._run(["git", "config", "user.email", local["email"]], cwd=worktree, label="git author email")
        return worktree

    def run_command(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int,
        extra_env: dict[str, str],
    ) -> CommandResult:
        return self.runner.run(argv, cwd, timeout, extra_env=extra_env)

    def assert_machine_commit(
        self,
        grant: dict[str, Any],
        worktree: Path,
        previous_commit: str | None = None,
    ) -> str:
        baseline = previous_commit or f"origin/{grant['baseBranch']}"
        commits = self._run(
            ["git", "rev-list", "--reverse", f"{baseline}..HEAD"],
            cwd=worktree,
            label="new commit read",
        ).stdout.splitlines()
        if len(commits) != 1:
            raise BenchError("local developer must create exactly one attributable commit per turn")
        head = commits[0]
        identity = self._run(
            ["git", "show", "-s", "--format=%an%x00%ae", "HEAD"],
            cwd=worktree,
            label="commit identity read",
        ).stdout.strip().split("\x00", 1)
        expected = grant["machineAccounts"]["local"]
        if identity != [expected["login"], expected["email"]]:
            raise BenchError("local developer commit does not use its machine-account identity")
        status = self._run(["git", "status", "--porcelain"], cwd=worktree, label="worktree status")
        if status.stdout.strip():
            raise BenchError("local developer left uncommitted changes after its commit")
        return head

    def changed_paths(self, grant: dict[str, Any], worktree: Path) -> list[str]:
        base_ref = f"origin/{grant['baseBranch']}"
        result = self._run(
            ["git", "diff", "--name-status", "--find-renames", f"{base_ref}...HEAD"],
            cwd=worktree,
            label="changed path read",
        )
        paths: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            candidates = fields[1:3] if fields[0].startswith(("R", "C")) else fields[1:2]
            for path in candidates:
                if path and path not in paths:
                    paths.append(path)
        return paths

    def push(self, grant: dict[str, Any], worktree: Path) -> None:
        token_env = grant["machineAccounts"]["local"]["tokenEnv"]
        token = os.environ.get(token_env)
        if not token:
            raise BenchError(f"machine-account credential {token_env} is not available")
        askpass = worktree.parent / "git-askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' x-access-token ;;\n"
            "  *) printf '%s\\n' \"$SM_BENCH_PUSH_TOKEN\" ;;\n"
            "esac\n"
        )
        askpass.chmod(0o700)
        self._run([
            "git", "push", f"https://github.com/{grant['repository']}.git",
            f"HEAD:refs/heads/{grant['branch']}",
        ], cwd=worktree, timeout=300, extra_env={
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "SM_BENCH_PUSH_TOKEN": token,
        }, label="machine-account push")

    def create_pr(self, grant: dict[str, Any], worktree: Path, body_path: Path) -> dict[str, Any]:
        result = self._run([
            "gh", "pr", "create", "--repo", grant["repository"],
            "--base", grant["baseBranch"], "--head", grant["branch"],
            "--title", f"Mission #{grant['issueNumber']}: {grant['branch'].split('/', 1)[-1].replace('-', ' ')}",
            "--body-file", str(body_path),
        ], cwd=worktree, timeout=120, extra_env=self._account_env(grant, "local"), label="pull request creation")
        url = result.stdout.strip().splitlines()[-1]
        viewed = self._run([
            "gh", "pr", "view", url, "--repo", grant["repository"], "--json", "number,url",
        ], extra_env=self._account_env(grant, "local"), label="pull request read")
        value = json.loads(viewed.stdout)
        if not isinstance(value, dict):
            raise BenchError("pull request read returned a non-object")
        return value

    def update_pr_body(self, grant: dict[str, Any], pr_number: int, body_path: Path) -> None:
        self._run([
            "gh", "pr", "edit", str(pr_number), "--repo", grant["repository"],
            "--body-file", str(body_path),
        ], extra_env=self._account_env(grant, "local"), label="pull request evidence update")

    def post_codex_review(self, grant: dict[str, Any], pr_number: int, review: dict[str, Any]) -> None:
        findings = review.get("findings") if isinstance(review.get("findings"), list) else []
        lines = [f"Codex bench review: {review.get('summary') or review.get('verdict')}"]
        for finding in findings:
            if isinstance(finding, dict):
                lines.append(f"- {finding.get('priority', 'P2')}: {finding.get('body', 'Finding')}" )
        body = "\n".join(lines)
        self._run([
            "gh", "pr", "review", str(pr_number), "--repo", grant["repository"],
            "--comment", "--body", body,
        ], extra_env=self._account_env(grant, "codex"), label="Codex machine-account review")

    def arm_auto_merge(self, grant: dict[str, Any], pr_number: int) -> None:
        self._run([
            "gh", "pr", "merge", str(pr_number), "--repo", grant["repository"], "--auto", "--merge",
        ], extra_env=self._account_env(grant, "local"), label="auto-merge arming")

    def wait_for_ci(self, grant: dict[str, Any], pr_number: int, timeout: int) -> str:
        result = self._run([
            "gh", "pr", "checks", str(pr_number), "--repo", grant["repository"],
            "--watch", "--interval", "10",
        ], timeout=timeout, extra_env=self._account_env(grant, "local"), label="continuous integration")
        return command_tail(result)

    def approve(self, grant: dict[str, Any], pr_number: int, summary: str) -> None:
        self._run([
            "gh", "pr", "review", str(pr_number), "--repo", grant["repository"],
            "--approve", "--body", summary,
        ], extra_env=self._account_env(grant, "claude"), label="Claude machine-account approval")

    def wait_for_merge(self, grant: dict[str, Any], pr_number: int, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._run([
                "gh", "pr", "view", str(pr_number), "--repo", grant["repository"],
                "--json", "state,mergedAt,url",
            ], extra_env=self._account_env(grant, "local"), label="pull request merge read")
            value = json.loads(result.stdout)
            if isinstance(value, dict) and value.get("state") == "MERGED":
                return value
            time.sleep(5)
        raise BenchError("auto-merge did not land within the acceptance budget")


def reported_opus_major(output: str) -> int | None:
    versions = [int(value) for value in re.findall(r"\bopus[-_](\d+)", output.lower())]
    return max(versions) if versions else None


def structured_value(result: CommandResult) -> dict[str, Any]:
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BenchError(f"model returned non-JSON output: {result.stdout[-1000:]}") from exc
    if not isinstance(envelope, dict):
        raise BenchError("model returned a non-object")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result_text = envelope.get("result")
    if isinstance(result_text, str):
        try:
            value = json.loads(result_text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    return envelope


class AgentDriver:
    """The fixed D8 model assignments; grants cannot substitute another provider."""

    PLAN_SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["clean", "summary", "steps", "touchedPaths"],
        "properties": {
            "clean": {"type": "boolean"},
            "summary": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "string"}},
            "touchedPaths": {"type": "array", "items": {"type": "string"}},
        },
    }
    REVIEW_SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "summary", "findings"],
        "properties": {
            "verdict": {"type": "string", "enum": ["clean", "changes-requested"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["priority", "body"],
                    "properties": {
                        "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                        "body": {"type": "string"},
                    },
                },
            },
        },
    }
    ACCEPT_SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "summary"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "summary": {"type": "string"},
        },
    }

    def __init__(self, runner: SubprocessRunner | None = None, credential_envs: set[str] | None = None):
        self.runner = runner or SubprocessRunner()
        self.credential_envs = set(credential_envs or ())

    def _agent_env(self, session_dir: Path) -> dict[str, str]:
        return isolated_command_env(session_dir, self.credential_envs)

    def _claude(
        self,
        prompt: str,
        cwd: Path,
        budget: StageBudget,
        session_dir: Path,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        timeout = budget.use_turn()
        result = self.runner.run([
            "claude", "-p", "--model", "opus", "--effort", "high",
            "--permission-mode", "plan", "--no-session-persistence",
            "--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":")),
        ], cwd, timeout, input_text=prompt, extra_env=self._agent_env(session_dir))
        if result.returncode != 0:
            raise BenchError(f"Claude Opus stage failed: {command_tail(result)}")
        value = structured_value(result)
        major = reported_opus_major(result.stdout)
        if major is None or major < 5:
            raise BenchError("Claude stage did not report an Opus model at major version 5 or newer")
        return value

    def _codex_structured(
        self,
        prompt: str,
        cwd: Path,
        budget: StageBudget,
        session_dir: Path,
        schema: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        timeout = budget.use_turn()
        schema_path = session_dir / f"{label}-output-schema.json"
        output_path = session_dir / f"{label}-output.json"
        atomic_json(schema_path, schema)
        result = self.runner.run([
            "codex", "exec", "--ephemeral", "-s", "read-only", "-C", str(cwd),
            "--output-schema", str(schema_path), "-o", str(output_path), "-",
        ], cwd, timeout, input_text=prompt, extra_env=self._agent_env(session_dir))
        if result.returncode != 0:
            raise BenchError(f"Codex {label} stage failed: {command_tail(result)}")
        try:
            value = json.loads(output_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchError(f"Codex {label} stage did not write valid structured output") from exc
        if not isinstance(value, dict):
            raise BenchError(f"Codex {label} stage returned a non-object")
        return value

    def plan(self, prompt: str, worktree: Path, budget: StageBudget, session_dir: Path) -> dict[str, Any]:
        return self._claude(prompt, worktree, budget, session_dir, self.PLAN_SCHEMA)

    def develop(self, prompt: str, worktree: Path, budget: StageBudget, _session_dir: Path) -> None:
        timeout = budget.use_turn()
        result = self.runner.run([
            "codex", "exec", "--oss", "--local-provider", "ollama",
            "-m", "qwen2.5-coder:14b", "--ephemeral", "-s", "workspace-write",
            "-C", str(worktree), "-",
        ], worktree, timeout, input_text=prompt, extra_env=self._agent_env(_session_dir))
        if result.returncode != 0:
            raise BenchError(f"local developer stage failed: {command_tail(result)}")

    def review(self, prompt: str, worktree: Path, budget: StageBudget, session_dir: Path) -> dict[str, Any]:
        return self._codex_structured(prompt, worktree, budget, session_dir, self.REVIEW_SCHEMA, "review")

    def fix(self, prompt: str, worktree: Path, budget: StageBudget, _session_dir: Path) -> None:
        self.develop(prompt, worktree, budget, _session_dir)

    def accept(self, prompt: str, worktree: Path, budget: StageBudget, session_dir: Path) -> dict[str, Any]:
        return self._claude(prompt, worktree, budget, session_dir, self.ACCEPT_SCHEMA)


class DecisionClient:
    """Escalate through the console's existing chat decision endpoints."""

    def __init__(self, configuration: dict[str, Any]):
        self.base_url = require_text(configuration.get("baseUrl"), "decisionApi.baseUrl").rstrip("/")
        self.token_env = configuration.get("tokenEnv") if isinstance(configuration.get("tokenEnv"), str) else ""

    def _request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json", "X-Present-Role": "owner", "X-Present-Actor": "mission-bench"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise BenchError(f"decision credential {self.token_env} is not available")
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise BenchError(f"decision API request failed: {exc}") from exc
        if not isinstance(value, dict) or "error" in value:
            raise BenchError(f"decision API refused the request: {value}")
        return value

    def request(self, context: str) -> dict[str, Any]:
        value = self._request("/api/chat", method="POST", payload={
            "question": "ask me to decide",
            "messages": [{"role": "user", "content": context[:12000]}],
            "workMode": "normal",
            "profile": "dc13.claude",
            "mock": True,
        })
        job_id = require_text(value.get("jobId"), "decision job id")
        progress = value.get("progress") if isinstance(value.get("progress"), dict) else {}
        request = progress.get("decisionRequest") if isinstance(progress.get("decisionRequest"), dict) else {}
        if not request:
            polled = self._request(f"/api/chat/{job_id}")
            progress = polled.get("progress") if isinstance(polled.get("progress"), dict) else {}
            request = progress.get("decisionRequest") if isinstance(progress.get("decisionRequest"), dict) else {}
        if not request:
            raise BenchError("decision API did not create a pending decision")
        return {"jobId": job_id, "decisionRequest": request, "url": f"{self.base_url}/job/{job_id}"}

    def wait_for_answer(self, job_id: str, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self._request(f"/api/chat/{job_id}")
            progress = value.get("progress") if isinstance(value.get("progress"), dict) else {}
            events = progress.get("steeringEvents") if isinstance(progress.get("steeringEvents"), list) else []
            answers = [
                item for item in events
                if isinstance(item, dict) and item.get("effect") == "answer-decision"
            ]
            if answers:
                return answers[-1]
            if value.get("state") in {"cancelled", "error"}:
                raise BenchError("decision job stopped without a Person decision")
            time.sleep(2)
        raise BenchError("mission decision exceeded the plan session budget")


class PipelineBench:
    def __init__(
        self,
        grant_path: Path,
        state_root: Path,
        *,
        platform: Any | None = None,
        agents: Any | None = None,
        decisions: Any | None = None,
        repository_root: Path | None = None,
    ):
        self.grant = validate_grant(json_object(grant_path))
        self.repository_root = (repository_root or REPO_ROOT).resolve()
        self.state_root = state_root.resolve()
        if self.state_root == self.repository_root or self.state_root.is_relative_to(self.repository_root):
            raise BenchError("runtime state directory must be outside the product repository")
        self.session_id = "as-" + secrets.token_hex(12)
        self.session_dir = self.state_root / self.session_id
        self.feed_path = self.state_root / "agent-session-status.jsonl"
        self.evidence_path = self.session_dir / "evidence.json"
        self.platform = platform or GitHubPlatform(self.repository_root)
        self.credential_envs = {
            account["tokenEnv"] for account in self.grant["machineAccounts"].values()
        }
        decision_token_env = self.grant["decisionApi"].get("tokenEnv")
        if decision_token_env:
            self.credential_envs.add(decision_token_env)
        self.agents = agents or AgentDriver(credential_envs=self.credential_envs)
        self.decisions = decisions or DecisionClient(self.grant["decisionApi"])
        self.sequence = 0
        self.claimed_at = utc_now()
        self.issue_payload: dict[str, Any] = {}
        self.stage_started: dict[str, str] = {}
        self.evidence: dict[str, Any] = {
            "schemaVersion": 1,
            "missionId": self.grant["missionId"],
            "sessionId": self.session_id,
            "grant": self.grant,
            "startedAt": utc_now(),
            "stages": {},
            "reviewCorrectionRounds": 0,
        }

    def _save_evidence(self) -> None:
        atomic_json(self.evidence_path, self.evidence)

    def _contract(self) -> str:
        return (
            f"## Requirement\n\n{self.grant['requirement']}\n\n"
            f"## Acceptance evidence\n\n{self.grant['acceptanceEvidence']}"
        )

    def _emit(
        self,
        stage: str,
        state: str,
        summary: str,
        budget: StageBudget,
        *,
        event_kind: str = "progress",
        pending_decision: dict[str, Any] | None = None,
        outcome_status: str = "pending",
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.sequence += 1
        event_id = "ase-" + secrets.token_hex(12)
        stage_name, worker_key, role, model = STAGE_DETAILS[stage]
        account = self.grant["machineAccounts"][worker_key]
        produced_at = utc_now()
        self.stage_started.setdefault(stage, produced_at)
        record: dict[str, Any] = {
            "schemaVersion": 1,
            "recordType": "agent-session-status",
            "eventId": event_id,
            "sequence": self.sequence,
            "sessionId": self.session_id,
            "tenantId": "local",
            "missionId": self.grant["missionId"],
            "producedAt": produced_at,
            "producer": "steel-mission-bench",
            "issue": {
                "repository": self.grant["repository"],
                "number": self.grant["issueNumber"],
                "url": str(self.issue_payload.get("url") or f"https://github.com/{self.grant['repository']}/issues/{self.grant['issueNumber']}"),
                "claimedAt": self.claimed_at,
            },
            "stage": stage_name,
            "state": state,
            "worker": {
                "id": f"{worker_key}:{role}",
                "provider": "glimmer" if worker_key == "local" else worker_key,
                "model": model,
                "role": role,
            },
            "machineAccount": account["login"],
            "startedAt": self.stage_started[stage],
            "lastEvent": {
                "eventId": event_id,
                "sequence": self.sequence,
                "at": produced_at,
                "kind": event_kind,
                "summary": summary,
            },
            "budgetLimit": budget.limit(),
            "budgetSpent": budget.spent(),
            "outcome": {
                "status": outcome_status,
                "summary": summary,
                **({"artifactRefs": artifact_refs} if artifact_refs else {}),
            },
        }
        if pending_decision:
            record["pendingDecision"] = pending_decision
        errors = schema_check.validate(record, SESSION_SCHEMA)
        if errors:
            raise BenchError(f"session status feed record is invalid: {'; '.join(errors)}")
        self.feed_path.parent.mkdir(parents=True, exist_ok=True)
        with self.feed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    def _green_gates(self, worktree: Path, budget: StageBudget) -> dict[str, Any]:
        definition = self.grant["definitionOfDone"]
        environment = isolated_command_env(self.session_dir, self.credential_envs)
        test_result = self.platform.run_command(
            definition["test"], worktree, budget.remaining(), environment
        )
        if test_result.returncode != 0:
            raise BenchError(f"full test gate failed: {command_tail(test_result)}")
        release_result = self.platform.run_command(
            definition["releaseCheck"], worktree, budget.remaining(), environment
        )
        if release_result.returncode != 0:
            raise BenchError(f"release check failed: {command_tail(release_result)}")
        return {
            "test": {"argv": test_result.argv, "returncode": 0, "outputTail": command_tail(test_result)},
            "releaseCheck": {"argv": release_result.argv, "returncode": 0, "outputTail": command_tail(release_result)},
        }

    def _pr_body(self) -> str:
        red = self.evidence.get("redTest", {})
        gates = self.evidence.get("stages", {}).get("develop", {}).get("gates", {})
        corrections = self.evidence.get("stages", {}).get("corrections", [])
        commits = [self.evidence.get("stages", {}).get("develop", {}).get("commit")]
        commits.extend(
            item.get("commit") for item in corrections
            if isinstance(item, dict)
        )
        commit_lines = "\n".join(f"- `{commit}`" for commit in commits if commit)
        correction_lines = "\n".join(
            f"- Correction {item.get('round')}: "
            f"test {item.get('gates', {}).get('test', {}).get('outputTail', '')}; "
            f"release {item.get('gates', {}).get('releaseCheck', {}).get('outputTail', '')}"
            for item in corrections
            if isinstance(item, dict)
        ) or "- No correction commits were required."
        return (
            "## What this changes\n\n"
            f"Granted mission {self.grant['missionId']} implements issue #{self.grant['issueNumber']} "
            "through the four-stage bench. Every commit in the branch:\n\n"
            f"{commit_lines}\n\n"
            f"Closes #{self.grant['issueNumber']}\n\n"
            "## Evidence\n\n"
            f"- Failing test observed before development: exit {red.get('returncode')} from {red.get('argv')}\n"
            f"- Failing test output: {red.get('outputTail', '')}\n"
            f"- Full test gate: {gates.get('test', {}).get('outputTail', '')}\n"
            f"- Release check: {gates.get('releaseCheck', {}).get('outputTail', '')}\n"
            f"- Codex correction rounds: {self.evidence.get('reviewCorrectionRounds', 0)}\n"
            f"{correction_lines}\n\n"
            "## Surfaces touched\n\n"
            "The granted issue defines the touched surface. The bench refuses security-review-labelled issues "
            "and stops authority-owned paths for human review.\n\n"
            "## Reversibility\n\n"
            "Revert the mission commit or close this pull request without merging. The bench does not mutate main directly.\n"
        )

    def _decision_pending(self, handle: dict[str, Any]) -> dict[str, Any]:
        request = handle.get("decisionRequest") if isinstance(handle.get("decisionRequest"), dict) else {}
        return {
            "decisionId": require_text(request.get("id"), "decision request id"),
            "kind": "plan-unclean",
            "question": require_text(request.get("question"), "decision request question"),
            "requestedAt": str(request.get("requestedAt") or utc_now()),
            "url": require_text(handle.get("url"), "decision URL"),
        }

    def _stop_for_authority_paths(
        self,
        paths: list[str],
        budget: StageBudget,
        contract: str,
    ) -> None:
        authority_paths = [
            path for path in paths
            if any(path == prefix or path.startswith(prefix) for prefix in AUTHORITY_PATHS)
        ]
        if not authority_paths:
            return
        handle = self.decisions.request(
            f"The mission touched human-owned paths and cannot merge them unattended: {authority_paths}\n\n{contract}"
        )
        pending = self._decision_pending(handle)
        self._emit(
            "develop",
            "waiting-on-person",
            "Human-owned paths require a Person review.",
            budget,
            event_kind="decision-requested",
            pending_decision=pending,
        )
        self.decisions.wait_for_answer(handle["jobId"], budget.remaining())
        raise BenchError("authority-owned changes stop for human delivery outside the mission bench")

    def run(self) -> dict[str, Any]:
        issue = self.platform.issue(self.grant)
        self.issue_payload = issue
        labels = {
            str(item.get("name")) for item in issue.get("labels", [])
            if isinstance(item, dict) and item.get("name")
        }
        if "security-review" in labels:
            raise BenchError("mission bench refuses issues labelled security-review")
        requirement = issue_section(str(issue.get("body") or ""), "Requirement")
        acceptance = issue_section(str(issue.get("body") or ""), "Acceptance evidence")
        if requirement != self.grant["requirement"] or acceptance != self.grant["acceptanceEvidence"]:
            raise BenchError("the issue requirement or acceptance evidence changed after mission grant")

        self.platform.validate_machine_accounts(self.grant)
        self.platform.validate_repository_wall(self.grant)
        self.platform.claim_issue(self.grant, self.session_id)
        self.claimed_at = utc_now()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._save_evidence()
        worktree = self.platform.prepare_worktree(self.grant, self.session_dir)
        contract = self._contract()
        budgets = {stage: StageBudget(self.grant["budgets"][stage]) for stage in STAGE_DETAILS}

        self._emit("plan", "working", "Claude Opus is validating the granted plan.", budgets["plan"], event_kind="stage-started")
        plan_prompt = (
            "Plan this granted mission. Do not change its requirement, acceptance evidence, budgets, or authority. "
            "Return clean=false when any assumption, scope boundary, or acceptance command is unresolved.\n\n"
            f"{contract}\n\nAbort conditions: {json.dumps(self.grant['abortConditions'])}"
        )
        plan = self.agents.plan(plan_prompt, worktree, budgets["plan"], self.session_dir)
        if plan.get("clean") is not True:
            summary = require_text(plan.get("summary"), "unclean plan summary")
            handle = self.decisions.request(f"The granted mission plan is unclean. {summary}\n\n{contract}")
            pending = self._decision_pending(handle)
            self._emit(
                "plan", "waiting-on-person", summary, budgets["plan"],
                event_kind="decision-requested", pending_decision=pending,
            )
            self.evidence["decision"] = {"request": handle}
            self._save_evidence()
            answer = self.decisions.wait_for_answer(handle["jobId"], budgets["plan"].remaining())
            self.evidence["decision"]["answer"] = answer
            self._save_evidence()
            if answer.get("selectedOptionId") == "pause":
                raise BenchError("the Person paused the mission during plan escalation")
            plan = self.agents.plan(
                plan_prompt + "\n\nThe Person answered through the decision flow: " + json.dumps(answer, sort_keys=True),
                worktree,
                budgets["plan"],
                self.session_dir,
            )
            if plan.get("clean") is not True:
                raise BenchError("plan remained unclean after the Person decision")
        self.evidence["stages"]["plan"] = plan
        self._save_evidence()
        self._emit("plan", "idle", require_text(plan.get("summary"), "plan summary"), budgets["plan"], event_kind="stage-completed")

        red_result = self.platform.run_command(
            self.grant["definitionOfDone"]["redTest"],
            worktree,
            budgets["develop"].remaining(),
            isolated_command_env(self.session_dir, self.credential_envs),
        )
        if red_result.returncode == 0:
            raise BenchError("the acceptance regression was not observed failing before development")
        self.evidence["redTest"] = {
            "argv": red_result.argv,
            "returncode": red_result.returncode,
            "outputTail": command_tail(red_result),
        }
        self._save_evidence()

        self._emit("develop", "working", "The local developer is implementing and committing in its isolated worktree.", budgets["develop"], event_kind="stage-started")
        develop_prompt = (
            "Implement the granted mission in this isolated worktree. The configured focused regression has already "
            "been observed failing. Make it pass, keep the full suite green, and commit all changes using the existing "
            "machine-account git identity. Do not push or create a pull request.\n\n"
            f"{contract}\n\nApproved plan: {json.dumps(plan, sort_keys=True)}"
        )
        self.agents.develop(develop_prompt, worktree, budgets["develop"], self.session_dir)
        commit = self.platform.assert_machine_commit(self.grant, worktree)
        paths = self.platform.changed_paths(self.grant, worktree)
        self._stop_for_authority_paths(paths, budgets["develop"], contract)
        gates = self._green_gates(worktree, budgets["develop"])
        self.evidence["stages"]["develop"] = {"commit": commit, "paths": paths, "gates": gates}
        self._save_evidence()
        self._emit("develop", "idle", "Local developer commit and all pre-push gates are green.", budgets["develop"], event_kind="stage-completed")

        self.platform.push(self.grant, worktree)
        body_path = self.session_dir / "pull-request.md"
        body_path.write_text(self._pr_body())
        pull_request = self.platform.create_pr(self.grant, worktree, body_path)
        self.evidence["pullRequest"] = pull_request
        self._save_evidence()

        previous_commit = commit
        corrections = 0
        clean_review: dict[str, Any] | None = None
        for round_number in range(1, self.grant["maxReviewRounds"] + 1):
            self._emit("review", "working", f"Codex review round {round_number} is inspecting the committed diff.", budgets["review"], event_kind="stage-started")
            review_prompt = (
                "Review the committed diff against the granted requirement and acceptance evidence. Report only "
                "actionable correctness, security, or regression findings.\n\n"
                f"{contract}\n\nApproved plan: {json.dumps(plan, sort_keys=True)}"
            )
            review = self.agents.review(review_prompt, worktree, budgets["review"], self.session_dir)
            self.platform.post_codex_review(self.grant, int(pull_request["number"]), review)
            self.evidence["stages"].setdefault("review", []).append(review)
            self._save_evidence()
            if review.get("verdict") == "clean":
                clean_review = review
                break
            if review.get("verdict") != "changes-requested":
                raise BenchError("Codex review returned an unknown verdict")
            corrections += 1
            fix_prompt = (
                "Address every Codex finding inside the same grant. Add or tighten regressions as needed, run the "
                "relevant tests, and create a new commit with the machine-account identity. Do not push.\n\n"
                f"{contract}\n\nFindings: {json.dumps(review.get('findings'), sort_keys=True)}"
            )
            self.agents.fix(fix_prompt, worktree, budgets["develop"], self.session_dir)
            previous_commit = self.platform.assert_machine_commit(
                self.grant, worktree, previous_commit=previous_commit
            )
            correction_paths = self.platform.changed_paths(self.grant, worktree)
            self._stop_for_authority_paths(correction_paths, budgets["develop"], contract)
            correction_gates = self._green_gates(worktree, budgets["develop"])
            self.evidence["stages"].setdefault("corrections", []).append({
                "round": round_number,
                "commit": previous_commit,
                "gates": correction_gates,
            })
            self.evidence["reviewCorrectionRounds"] = corrections
            self._save_evidence()
            self.platform.push(self.grant, worktree)
        if clean_review is None:
            raise BenchError("Codex review loop exhausted its bounded correction rounds")
        self._emit("review", "idle", require_text(clean_review.get("summary"), "review summary"), budgets["review"], event_kind="stage-completed")

        body_path.write_text(self._pr_body())
        self.platform.update_pr_body(
            self.grant, int(pull_request["number"]), body_path
        )

        self.platform.arm_auto_merge(self.grant, int(pull_request["number"]))
        self._emit("acceptance", "working", "Auto-merge is armed and required CI is running before final acceptance.", budgets["acceptance"], event_kind="stage-started")
        ci = self.platform.wait_for_ci(
            self.grant, int(pull_request["number"]), budgets["acceptance"].remaining()
        )
        acceptance_prompt = (
            "Perform the final read-only acceptance review. Approve only if the committed diff, failing-test evidence, "
            "green release gates, Codex correction loop, and CI satisfy the unchanged grant.\n\n"
            f"{contract}\n\nCI evidence: {ci}\n\nCodex review: {json.dumps(clean_review, sort_keys=True)}"
        )
        acceptance = self.agents.accept(
            acceptance_prompt, worktree, budgets["acceptance"], self.session_dir
        )
        if acceptance.get("verdict") != "approve":
            raise BenchError(f"Claude acceptance rejected the mission: {acceptance.get('summary')}")
        summary = require_text(acceptance.get("summary"), "acceptance summary")
        self.platform.approve(self.grant, int(pull_request["number"]), summary)
        merged = self.platform.wait_for_merge(
            self.grant, int(pull_request["number"]), budgets["acceptance"].remaining()
        )
        self.evidence["stages"]["acceptance"] = {"ci": ci, "review": acceptance, "merge": merged}
        self.evidence["state"] = "merged"
        self.evidence["completedAt"] = utc_now()
        self._save_evidence()
        artifacts = [
            {"kind": "evidence-pack", "uri": str(self.evidence_path)},
            {"kind": "pull-request", "uri": str(pull_request["url"])},
        ]
        self._emit(
            "acceptance", "succeeded", summary, budgets["acceptance"],
            event_kind="session-completed", outcome_status="succeeded", artifact_refs=artifacts,
        )
        return {
            "ok": True,
            "state": "merged",
            "missionId": self.grant["missionId"],
            "sessionId": self.session_id,
            "pullRequest": pull_request,
            "reviewCorrectionRounds": corrections,
            "evidencePath": str(self.evidence_path),
            "feedPath": str(self.feed_path),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a granted mission through the disposable C1 pipeline bench")
    parser.add_argument("--grant", required=True, type=Path, help="Path to the immutable JSON grant")
    parser.add_argument("--state-dir", required=True, type=Path, help="Runtime state directory outside the product tree")
    parser.add_argument("--repository", type=Path, default=REPO_ROOT, help="Main repository checkout")
    args = parser.parse_args(argv)
    try:
        result = PipelineBench(
            args.grant,
            args.state_dir,
            repository_root=args.repository,
        ).run()
    except BenchError as exc:
        print(json.dumps({"ok": False, "status": "BENCH_STOPPED", "reason": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
