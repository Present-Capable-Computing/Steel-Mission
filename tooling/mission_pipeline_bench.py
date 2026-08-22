#!/usr/bin/env python3
"""Disposable C1 four-stage mission bench.

The bench rehearses the D8/D9 pipeline without becoming a product dispatch path.
Its only durable contract is the agent-session status feed it validates before
every append. Runtime grants, worktrees, checkpoints, and evidence packs live in
an explicitly supplied state directory outside the product repository.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters import schema_check  # noqa: E402


SESSION_SCHEMA = "canonical/agent-session-status-v1.json"
AUTHORITY_PATHS = (
    "schemas/canonical/",
    "schemas/schema-registry.json",
    "docs/workplan.md",
    ".github/CODEOWNERS",
)
LOCAL_DEVELOPER_MODEL = "qwen3-coder:30b"
STAGE_DETAILS = {
    "plan": ("plan", "claude", "planner", "opus"),
    "develop": ("develop-and-commit", "local", "developer", LOCAL_DEVELOPER_MODEL),
    "review": ("review-loop", "codex", "reviewer", "codex"),
    "acceptance": ("final-review-and-merge", "claude", "acceptance", "opus"),
}
PROVIDER_AUTH_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY",),
    "local": (),
}
UNTRUSTED_BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM",
    "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "OLLAMA_HOST",
)
DIAGNOSTIC_TAIL_BYTES = 20_000
COMPLETE_STDOUT_BYTES = 1_000_000


class BenchError(RuntimeError):
    """The mission stopped without redefining its grant or definition of done."""


def codeowners_pattern_matches(pattern: str, path: str) -> bool:
    """Return whether a supported CODEOWNERS pattern applies to a repository path."""
    normalized = pattern.removeprefix("/")
    candidate = path.removeprefix("/")
    if not normalized or pattern.startswith("!") or "[" in pattern or "**" in normalized:
        raise BenchError(f"unsupported CODEOWNERS pattern in repository wall: {pattern}")
    if normalized == "*":
        return True
    if normalized.endswith("/"):
        return candidate.startswith(normalized)
    if "/" not in normalized:
        return any(fnmatch.fnmatchcase(part, normalized) for part in candidate.split("/"))
    return fnmatch.fnmatchcase(candidate, normalized) or candidate.startswith(normalized + "/")


def codeowners_patterns_overlap(authority_pattern: str, candidate_pattern: str) -> bool:
    """Conservatively detect whether a later rule can override an authority rule."""
    authority = authority_pattern.removeprefix("/")
    candidate = candidate_pattern.removeprefix("/")
    if candidate == "*":
        return True
    if authority.endswith("/"):
        if not candidate_pattern.startswith("/") or any(mark in candidate for mark in "*?"):
            codeowners_pattern_matches(candidate_pattern, authority + "__steel_mission_wall_probe__")
            return True
        if candidate.endswith("/"):
            return authority.startswith(candidate) or candidate.startswith(authority)
        return candidate.startswith(authority) or codeowners_pattern_matches(
            candidate_pattern, authority + "__steel_mission_wall_probe__"
        )
    return codeowners_pattern_matches(candidate_pattern, authority)


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
    granted_by = require_text(grant.get("grantedBy"), "grantedBy")
    if not re.fullmatch(r"(?=.{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", granted_by):
        raise BenchError("grantedBy must be a GitHub login")
    require_text(grant.get("requirement"), "requirement")
    require_text(grant.get("acceptanceEvidence"), "acceptanceEvidence")
    allowed_paths = grant.get("allowedPaths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise BenchError("allowedPaths must be a non-empty array")
    for path in allowed_paths:
        normalized = require_text(path, "allowedPaths entry")
        if normalized.startswith(("/", "../")) or "/../" in normalized or normalized == "..":
            raise BenchError("allowedPaths entries must be repository-relative")

    surfaces = grant.get("surfaces")
    surface_names = {
        "authentication",
        "networkService",
        "subprocessExecution",
        "authoritySchemas",
    }
    if not isinstance(surfaces, dict) or set(surfaces) != surface_names:
        raise BenchError(
            "surfaces must declare exactly authentication, networkService, "
            "subprocessExecution, and authoritySchemas"
        )
    if not all(isinstance(surfaces[name], bool) for name in surface_names):
        raise BenchError("surfaces declarations must be booleans")

    definition = grant.get("definitionOfDone")
    if not isinstance(definition, dict):
        raise BenchError("definitionOfDone must be an object")
    for key in ("redTest", "test", "releaseCheck"):
        require_argv(definition.get(key), f"definitionOfDone.{key}")
    red_failure = definition.get("redFailure")
    if not isinstance(red_failure, dict):
        raise BenchError("definitionOfDone.redFailure must be an object")
    exit_codes = red_failure.get("exitCodes")
    if (
        not isinstance(exit_codes, list)
        or not exit_codes
        or not all(isinstance(code, int) and not isinstance(code, bool) and code != 0 for code in exit_codes)
    ):
        raise BenchError("definitionOfDone.redFailure.exitCodes must contain nonzero integers")
    output_pattern = require_text(
        red_failure.get("outputPattern"),
        "definitionOfDone.redFailure.outputPattern",
    )
    try:
        re.compile(output_pattern)
    except re.error as exc:
        raise BenchError(f"definitionOfDone.redFailure.outputPattern is invalid: {exc}") from exc

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
    for index, condition in enumerate(abort_conditions):
        if not isinstance(condition, dict):
            raise BenchError("abortConditions entries must be machine-checkable objects")
        kind = condition.get("kind")
        if kind in {"budget-exhausted", "grant-drift"}:
            if set(condition) != {"kind"}:
                raise BenchError(f"abortConditions[{index}] has unsupported fields")
            continue
        if kind != "path-changed":
            raise BenchError(f"abortConditions[{index}].kind is unsupported")
        paths = condition.get("paths")
        if set(condition) != {"kind", "paths"} or not isinstance(paths, list) or not paths:
            raise BenchError(f"abortConditions[{index}].paths must be a non-empty array")
        for path in paths:
            normalized = require_text(path, f"abortConditions[{index}].paths entry")
            if normalized.startswith(("/", "../")) or "/../" in normalized or normalized == "..":
                raise BenchError(f"abortConditions[{index}].paths entries must be repository-relative")
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
        logins.append(login.lower())
    if len(set(logins)) != len(logins):
        raise BenchError("each model worker requires a distinct machine account")
    if granted_by.lower() in logins:
        raise BenchError("grantedBy must be distinct from every machine account")

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


def isolated_command_env(
    session_dir: Path,
    credential_envs: set[str],
    *,
    scrub_all_credentials: bool = False,
) -> dict[str, str]:
    """Remove GitHub authority from repository-authored and model commands."""
    gh_config = session_dir / "no-github-credentials"
    hooks = session_dir / "no-git-hooks"
    gh_config.mkdir(parents=True, exist_ok=True)
    hooks.mkdir(parents=True, exist_ok=True)
    environment = {
        "GH_CONFIG_DIR": str(gh_config),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "false",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "6",
        "GIT_CONFIG_KEY_0": "remote.origin.pushurl",
        "GIT_CONFIG_VALUE_0": "disabled://mission-bench-command-cannot-push",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_2": "",
        "GIT_CONFIG_KEY_3": "core.hooksPath",
        "GIT_CONFIG_VALUE_3": str(hooks),
        "GIT_CONFIG_KEY_4": "core.fsmonitor",
        "GIT_CONFIG_VALUE_4": "false",
        "GIT_CONFIG_KEY_5": "diff.external",
        "GIT_CONFIG_VALUE_5": "",
    }
    environment.update({name: "" for name in credential_envs})
    if scrub_all_credentials:
        markers = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "PRIVATE_KEY")
        environment.update({name: "" for name in os.environ if any(marker in name.upper() for marker in markers)})
    return environment


class SubprocessRunner:
    """Run argv-only commands with bounded output and no implicit shell."""

    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: float,
        *,
        input_text: str = "",
        extra_env: dict[str, str] | None = None,
        inherit_env: bool = True,
        complete_stdout: bool = False,
    ) -> CommandResult:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise BenchError("refusing a non-argv command")
        environment = os.environ.copy() if inherit_env else {
            name: os.environ[name] for name in UNTRUSTED_BASE_ENV if name in os.environ
        }
        environment.pop("GH_TOKEN", None)
        environment.pop("GITHUB_TOKEN", None)
        if extra_env:
            environment.update(extra_env)
        started = time.monotonic()
        if timeout <= 0:
            raise BenchError(f"command exceeded its {timeout}s budget: {argv[0]}")
        with tempfile.TemporaryFile() as stdin:
            stdin.write(input_text.encode())
            stdin.seek(0)
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    start_new_session=True,
                )
            except OSError as exc:
                raise BenchError(f"command failed to start: {argv[0]}: {exc}") from exc
            deadline = started + timeout
            streams = selectors.DefaultSelector()
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            stdout_overflow = False
            assert process.stdout is not None and process.stderr is not None
            streams.register(process.stdout, selectors.EVENT_READ, "stdout")
            streams.register(process.stderr, selectors.EVENT_READ, "stderr")
            try:
                while streams.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _mask in streams.select(timeout=min(0.25, remaining)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            streams.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        buffer = buffers[key.data]
                        if key.data == "stdout" and complete_stdout:
                            if len(buffer) + len(chunk) > COMPLETE_STDOUT_BYTES:
                                stdout_overflow = True
                            elif not stdout_overflow:
                                buffer.extend(chunk)
                        else:
                            buffer.extend(chunk)
                            if len(buffer) > DIAGNOSTIC_TAIL_BYTES:
                                del buffer[:-DIAGNOSTIC_TAIL_BYTES]
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise BenchError(f"command exceeded its {timeout}s budget: {argv[0]}") from exc
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                streams.close()
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
        if complete_stdout and stdout_overflow:
            raise BenchError(
                f"structured command output exceeded its {COMPLETE_STDOUT_BYTES}-byte safety limit: {argv[0]}"
            )
        return CommandResult(
            argv=list(argv),
            returncode=process.returncode,
            stdout=bytes(buffers["stdout"]).decode(errors="replace"),
            stderr=bytes(buffers["stderr"]).decode(errors="replace"),
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
        self.worktree_bases: dict[Path, str] = {}
        self.validated_base_oids: dict[str, str] = {}
        self.trusted_executables = {
            name: self._resolve_trusted_executable(name)
            for name in ("git", "gh")
        }
        trusted_directories = [
            self.trusted_executables["git"].parent,
            self.trusted_executables["gh"].parent,
        ]
        self.trusted_path = os.pathsep.join(
            dict.fromkeys(str(directory) for directory in trusted_directories)
        )

    @staticmethod
    def _resolve_trusted_executable(name: str) -> Path:
        located = shutil.which(name)
        if not located:
            raise BenchError(f"required trusted executable is unavailable: {name}")
        path = Path(located).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise BenchError(f"required trusted executable is not executable: {path}")
        return path

    def _trusted_invocation(
        self,
        argv: list[str],
        extra_env: dict[str, str] | None,
    ) -> tuple[list[str], dict[str, str]]:
        executable = self.trusted_executables.get(argv[0]) if argv else None
        if executable is None:
            raise BenchError("GitHub platform commands must use a pinned git or gh executable")
        environment = dict(extra_env or {})
        environment["PATH"] = self.trusted_path
        return [str(executable), *argv[1:]], environment

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 120,
        input_text: str = "",
        extra_env: dict[str, str] | None = None,
        inherit_env: bool = True,
        complete_stdout: bool = False,
        label: str | None = None,
    ) -> CommandResult:
        trusted_argv, trusted_env = self._trusted_invocation(argv, extra_env)
        kwargs = {"input_text": input_text, "extra_env": trusted_env}
        if not inherit_env:
            kwargs["inherit_env"] = False
        if complete_stdout:
            kwargs["complete_stdout"] = True
        result = self.runner.run(trusted_argv, cwd or self.repository_root, timeout, **kwargs)
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

    @staticmethod
    def _credential_envs(grant: dict[str, Any]) -> set[str]:
        names = {account["tokenEnv"] for account in grant["machineAccounts"].values()}
        decision_token_env = grant["decisionApi"].get("tokenEnv")
        if decision_token_env:
            names.add(decision_token_env)
        return names

    @classmethod
    def _operator_env(cls, grant: dict[str, Any]) -> dict[str, str]:
        """Use the operator's ambient gh session without exposing machine tokens."""
        return {name: "" for name in cls._credential_envs(grant)}

    def _untrusted_git_env(self, grant: dict[str, Any], worktree: Path) -> dict[str, str]:
        return isolated_command_env(
            worktree.parent,
            self._credential_envs(grant),
            scrub_all_credentials=True,
        )

    def issue(self, grant: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        result = self._run([
            "gh", "issue", "view", str(grant["issueNumber"]),
            "--repo", grant["repository"],
            "--json", "number,title,body,labels,url,assignees",
        ], timeout=timeout, extra_env=self._account_env(grant, "local"),
            complete_stdout=True, label="issue read")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise BenchError("issue read returned a non-object")
        return value

    def validate_machine_accounts(self, grant: dict[str, Any]) -> None:
        seen: set[str] = set()
        for worker in ("claude", "codex", "local"):
            account = grant["machineAccounts"][worker]
            expected = account["login"]
            environment = self._account_env(grant, worker)
            result = self._run(
                ["gh", "api", "user"],
                extra_env=environment,
                complete_stdout=True,
                label=f"{worker} machine-account check",
            )
            try:
                identity = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise BenchError(f"{worker} machine-account check returned invalid JSON") from exc
            if not isinstance(identity, dict):
                raise BenchError(f"{worker} machine-account check returned a non-object")
            actual = identity.get("login")
            account_id = identity.get("id")
            if not isinstance(actual, str) or not actual:
                raise BenchError(f"{worker} machine-account check omitted its login")
            if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
                raise BenchError(f"{worker} machine-account check omitted its numeric account id")
            if actual != expected:
                raise BenchError(f"{worker} credential belongs to {actual!r}, expected {expected!r}")
            if actual in seen:
                raise BenchError("machine-account credentials resolve to duplicate GitHub users")
            seen.add(actual)
            expected_email = f"{account_id}+{actual}@users.noreply.github.com"
            if account["email"].lower() != expected_email.lower():
                raise BenchError(
                    f"{worker} commit email must match its authenticated GitHub account's "
                    "canonical no-reply identity"
                )

    def validate_repository_wall(self, grant: dict[str, Any], timeout: float = 120) -> dict[str, str]:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise BenchError("repository-wall validation exceeded its stage budget")
            return value

        encoded_branch = urllib.parse.quote(grant["baseBranch"], safe="")
        ref_endpoint = f"repos/{grant['repository']}/git/ref/heads/{encoded_branch}"
        environment = self._operator_env(grant)

        def live_base_oid() -> str:
            result = self._run(
                ["gh", "api", ref_endpoint],
                timeout=remaining(),
                extra_env=environment,
                inherit_env=False,
                complete_stdout=True,
                label="base branch revision read",
            )
            try:
                value = json.loads(result.stdout)
                oid = value["object"]["sha"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise BenchError("base branch revision read returned invalid JSON") from exc
            if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", oid):
                raise BenchError("base branch revision read returned an invalid commit OID")
            return oid.lower()

        base_oid = live_base_oid()
        validated_base = self.validated_base_oids.get(grant["baseBranch"])
        if validated_base is not None and base_oid != validated_base:
            raise BenchError("base branch advanced after mission validation")
        codeowners_result = self._run([
            "gh", "api", "-H", "Accept: application/vnd.github.raw+json",
            f"repos/{grant['repository']}/contents/.github/CODEOWNERS?ref={base_oid}",
        ], timeout=remaining(), extra_env=environment, inherit_env=False, complete_stdout=True,
            label="live-base CODEOWNERS read")
        result = self._run([
            "gh", "api",
            f"repos/{grant['repository']}/branches/{grant['baseBranch']}/protection",
        ], timeout=remaining(), extra_env=environment, inherit_env=False, complete_stdout=True,
            label="branch protection read")
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
        if approvals != 1:
            raise BenchError("branch protection must require exactly one approving review")
        if not isinstance(reviews, dict) or reviews.get("require_code_owner_reviews") is not True:
            raise BenchError("branch protection must require code-owner review")
        if reviews.get("dismiss_stale_reviews") is not True:
            raise BenchError("branch protection must dismiss approvals after a new push")
        if not isinstance(conversations, dict) or conversations.get("enabled") is not True:
            raise BenchError("branch protection must require review conversation resolution")
        if not isinstance(required_checks, list) or not required_checks:
            raise BenchError("branch protection must require continuous-integration checks")
        check_names = {
            str(check.get("context"))
            for check in required_checks
            if isinstance(check, dict) and check.get("context")
        }
        required_interpreters = {
            "Python test suite (3.11)",
            "Python test suite (3.12)",
        }
        if not required_interpreters.issubset(check_names):
            raise BenchError("branch protection must require the Python 3.11 and 3.12 test suites")
        if not isinstance(checks, dict) or checks.get("strict") is not True:
            raise BenchError("required checks must cover the current base branch")
        acceptance_login = grant["machineAccounts"]["claude"]["login"]
        codeowners = codeowners_result.stdout.splitlines()
        if live_base_oid() != base_oid:
            raise BenchError("base branch changed during repository-wall validation")
        self.validated_base_oids[grant["baseBranch"]] = base_oid
        rules = [line.split() for line in codeowners if line.strip() and not line.lstrip().startswith("#")]
        for rule in rules:
            codeowners_pattern_matches(rule[0], "__steel_mission_wall_probe__")
        default_rule = next((rule for rule in reversed(rules) if rule[0] == "*"), [])
        default_owners = {token.lstrip("@").lower() for token in default_rule[1:]}
        person_login = grant["grantedBy"].lower()
        expected_non_authority_owners = {
            person_login,
            acceptance_login.lower(),
        }
        if default_owners != expected_non_authority_owners:
            raise BenchError("Founder and Claude acceptance must co-own default non-authority paths")
        for pattern in ("/steel_core/", "/tooling/"):
            rule = next((candidate for candidate in reversed(rules) if candidate[0] == pattern), [])
            owners = {token.lstrip("@").lower() for token in rule[1:]}
            if owners != expected_non_authority_owners:
                raise BenchError(f"Founder and Claude acceptance must co-own {pattern}")
        registered_person_only_patterns = (
            "/schemas/",
            "/schemas/canonical/",
            "/schemas/schema-registry.json",
            "/bin/",
            "/steel-mission-chat/",
            "/adapters/",
            "/.github/CODEOWNERS",
            "/.github/workflows/",
            "/Dockerfile.private-runner",
            "/requirements-dev.txt",
            "/package.json",
            "/package-lock.json",
            "/plan/",
            "/docs/workplan.md",
        )
        person_only_patterns = tuple(
            rule[0] for rule in rules
            if {token.lstrip("@").lower() for token in rule[1:]} == {person_login}
        )
        if set(person_only_patterns) - set(registered_person_only_patterns):
            raise BenchError("Founder-only CODEOWNERS patterns changed")
        authority_witnesses = {
            pattern: pattern.removeprefix("/") + "__steel_mission_wall_probe__"
            if pattern.endswith("/")
            else pattern.removeprefix("/")
            for pattern in registered_person_only_patterns
        }
        for pattern in registered_person_only_patterns:
            exact_index = next((
                index for index in range(len(rules) - 1, -1, -1)
                if rules[index][0] == pattern
            ), -1)
            exact_rule = rules[exact_index] if exact_index >= 0 else []
            exact_owners = {token.lstrip("@").lower() for token in exact_rule[1:]}
            if exact_owners != {person_login}:
                raise BenchError(f"{pattern} must remain Founder-owned in CODEOWNERS")
            effective_rule = next((
                rule for rule in reversed(rules)
                if codeowners_pattern_matches(rule[0], authority_witnesses[pattern])
            ), [])
            effective_owners = {
                token.lstrip("@").lower() for token in effective_rule[1:]
            }
            if effective_owners != {person_login}:
                raise BenchError(
                    f"{pattern} effective CODEOWNERS rule must remain Founder-only"
                )
            for later_rule in rules[exact_index + 1:]:
                later_owners = {
                    token.lstrip("@").lower() for token in later_rule[1:]
                }
                if (
                    codeowners_patterns_overlap(pattern, later_rule[0])
                    and later_owners != {person_login}
                ):
                    raise BenchError(
                        f"{pattern} effective CODEOWNERS rule must remain Founder-only"
                    )
        return {
            "credentialBoundary": "operator-ambient",
            "baseCommit": base_oid,
        }

    def claim_issue(
        self,
        grant: dict[str, Any],
        session_id: str,
        on_assigned: Callable[[], None] | None = None,
    ) -> None:
        account = grant["machineAccounts"]["local"]
        environment = self._account_env(grant, "local")
        self._run([
            "gh", "issue", "edit", str(grant["issueNumber"]),
            "--repo", grant["repository"], "--add-assignee", account["login"],
        ], extra_env=environment, label="issue assignment")
        if on_assigned:
            on_assigned()
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
        remote_argv, remote_env = self._trusted_invocation(
            ["git", "remote", "get-url", "origin"],
            None,
        )
        remote = self.runner.run(
            remote_argv,
            self.repository_root,
            30,
            extra_env=remote_env,
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
        fetched_base = self._run(
            ["git", "rev-parse", base_ref],
            label="fetched base revision read",
        ).stdout.strip().lower()
        validated_base = self.validated_base_oids.get(grant["baseBranch"])
        if validated_base is not None and fetched_base != validated_base:
            raise BenchError("fetched base branch changed after repository-wall validation")
        self._run([
            "git", "worktree", "add", "-b", grant["branch"], str(worktree), base_ref,
        ], timeout=120, label="isolated worktree creation")
        self.worktree_bases[worktree.resolve()] = self._run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            label="isolated worktree base read",
        ).stdout.strip()
        return worktree

    def run_command(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int,
        extra_env: dict[str, str],
    ) -> CommandResult:
        return self.runner.run(argv, cwd, timeout, extra_env=extra_env, inherit_env=False)

    def assert_machine_commit(
        self,
        grant: dict[str, Any],
        worktree: Path,
        previous_commit: str | None = None,
    ) -> str:
        environment = self._untrusted_git_env(grant, worktree)
        baseline = previous_commit or self.worktree_bases.get(worktree.resolve())
        if not baseline:
            raise BenchError("isolated worktree base commit is unavailable")
        commits = self._run(
            ["git", "rev-list", "--reverse", f"{baseline}..HEAD"],
            cwd=worktree,
            extra_env=environment,
            inherit_env=False,
            label="new commit read",
        ).stdout.splitlines()
        if not commits:
            raise BenchError("local developer did not create an attributable commit")
        if len(commits) != 1:
            raise BenchError("local developer must create exactly one attributable commit per turn")
        head = commits[0]
        identity = self._run(
            ["git", "show", "-s", "--format=%an%x00%ae", "HEAD"],
            cwd=worktree,
            extra_env=environment,
            inherit_env=False,
            label="commit identity read",
        ).stdout.strip().split("\x00", 1)
        expected = grant["machineAccounts"]["local"]
        if identity != [expected["login"], expected["email"]]:
            raise BenchError("local developer commit does not use its machine-account identity")
        status = self._run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            extra_env=environment,
            inherit_env=False,
            label="worktree status",
        )
        if status.stdout.strip():
            raise BenchError("local developer left uncommitted changes after its commit")
        return head

    def assert_unchanged_machine_commit(
        self,
        grant: dict[str, Any],
        worktree: Path,
        expected_commit: str,
        previous_commit: str | None = None,
    ) -> None:
        actual = self.assert_machine_commit(grant, worktree, previous_commit=previous_commit)
        if actual != expected_commit:
            raise BenchError("repository gate changed the reviewed machine commit")

    def changed_paths(self, grant: dict[str, Any], worktree: Path) -> list[str]:
        base_ref = self.worktree_bases.get(worktree.resolve())
        if not base_ref:
            raise BenchError("isolated worktree base commit is unavailable")
        environment = self._untrusted_git_env(grant, worktree)
        result = self._run(
            [
                "git", "diff", "--no-ext-diff", "--no-textconv",
                "--name-status", "--find-renames", f"{base_ref}...HEAD",
            ],
            cwd=worktree,
            extra_env=environment,
            inherit_env=False,
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

    def push(self, grant: dict[str, Any], worktree: Path, timeout: float) -> None:
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
        environment = isolated_command_env(
            worktree.parent,
            self._credential_envs(grant),
            scrub_all_credentials=True,
        )
        environment.update({
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "SM_BENCH_PUSH_TOKEN": token,
        })
        self._run([
            "git", "push", f"https://github.com/{grant['repository']}.git",
            f"HEAD:refs/heads/{grant['branch']}",
        ], cwd=worktree, timeout=timeout, extra_env=environment, inherit_env=False,
            label="machine-account push")

    def create_pr(
        self,
        grant: dict[str, Any],
        worktree: Path,
        body_path: Path,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        result = self._run([
            "gh", "pr", "create", "--repo", grant["repository"],
            "--base", grant["baseBranch"], "--head", grant["branch"],
            "--title", f"Mission #{grant['issueNumber']}: {grant['branch'].split('/', 1)[-1].replace('-', ' ')}",
            "--body-file", str(body_path),
        ], cwd=worktree, timeout=timeout, extra_env=self._account_env(grant, "local"),
            label="pull request creation")
        url = result.stdout.strip().splitlines()[-1]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BenchError("pull request creation exceeded the develop budget")
        viewed = self._run([
            "gh", "pr", "view", url, "--repo", grant["repository"], "--json", "number,url",
        ], timeout=remaining, extra_env=self._account_env(grant, "local"), complete_stdout=True,
            label="pull request read")
        value = json.loads(viewed.stdout)
        if not isinstance(value, dict):
            raise BenchError("pull request read returned a non-object")
        return value

    def update_pr_body(
        self,
        grant: dict[str, Any],
        pr_number: int,
        body_path: Path,
        timeout: float,
    ) -> None:
        self._run([
            "gh", "pr", "edit", str(pr_number), "--repo", grant["repository"],
            "--body-file", str(body_path),
        ], timeout=timeout, extra_env=self._account_env(grant, "local"),
            label="pull request evidence update")

    def post_codex_review(
        self,
        grant: dict[str, Any],
        pr_number: int,
        review: dict[str, Any],
        timeout: float,
    ) -> None:
        findings = review.get("findings") if isinstance(review.get("findings"), list) else []
        lines = [f"Codex bench review: {review.get('summary') or review.get('verdict')}"]
        for finding in findings:
            if isinstance(finding, dict):
                lines.append(f"- {finding.get('priority', 'P2')}: {finding.get('body', 'Finding')}" )
        body = "\n".join(lines)
        self._run([
            "gh", "pr", "review", str(pr_number), "--repo", grant["repository"],
            "--comment", "--body", body,
        ], timeout=timeout, extra_env=self._account_env(grant, "codex"),
            label="Codex machine-account review")

    def assert_pr_head(
        self,
        grant: dict[str, Any],
        pr_number: int,
        expected_commit: str,
        timeout: float,
    ) -> None:
        result = self._run([
            "gh", "pr", "view", str(pr_number), "--repo", grant["repository"],
            "--json", "headRefOid",
        ], timeout=timeout, extra_env=self._account_env(grant, "local"), complete_stdout=True,
            label="pull request head read")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("headRefOid") != expected_commit:
            raise BenchError("pull request head changed outside the granted mission")

    def arm_auto_merge(
        self,
        grant: dict[str, Any],
        pr_number: int,
        head_commit: str,
        timeout: float,
    ) -> None:
        self._run([
            "gh", "pr", "merge", str(pr_number), "--repo", grant["repository"], "--auto", "--merge",
            "--match-head-commit", head_commit,
        ], timeout=timeout, extra_env=self._account_env(grant, "local"), label="auto-merge arming")

    def assert_auto_merge_waiting(
        self,
        grant: dict[str, Any],
        pr_number: int,
        expected_commit: str,
        timeout: float,
    ) -> dict[str, Any]:
        result = self._run([
            "gh", "pr", "view", str(pr_number), "--repo", grant["repository"],
            "--json", "state,headRefOid,reviewDecision,autoMergeRequest",
        ], timeout=timeout, extra_env=self._account_env(grant, "local"), complete_stdout=True,
            label="armed auto-merge read")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BenchError("armed auto-merge read returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BenchError("armed auto-merge read returned a non-object")
        if value.get("state") != "OPEN" or value.get("headRefOid") != expected_commit:
            raise BenchError("auto-merge is not waiting on the granted pull request head")
        if value.get("reviewDecision") != "REVIEW_REQUIRED":
            raise BenchError("auto-merge was not held by the required approval wall")
        if not isinstance(value.get("autoMergeRequest"), dict):
            raise BenchError("auto-merge is not armed")
        return value

    def disable_auto_merge(self, grant: dict[str, Any], pr_number: int) -> None:
        self._run([
            "gh", "pr", "merge", str(pr_number), "--repo", grant["repository"], "--disable-auto",
        ], extra_env=self._account_env(grant, "local"), label="auto-merge cancellation")

    def wait_for_ci(self, grant: dict[str, Any], pr_number: int, timeout: int) -> str:
        result = self._run([
            "gh", "pr", "checks", str(pr_number), "--repo", grant["repository"],
            "--watch", "--interval", "10",
        ], timeout=timeout, extra_env=self._account_env(grant, "local"), label="continuous integration")
        return command_tail(result)

    def approve(
        self,
        grant: dict[str, Any],
        pr_number: int,
        summary: str,
        head_commit: str,
        timeout: float,
    ) -> None:
        self._run([
            "gh", "api", "--method", "POST",
            f"repos/{grant['repository']}/pulls/{pr_number}/reviews",
            "-f", "event=APPROVE", "-f", f"body={summary}", "-f", f"commit_id={head_commit}",
        ], timeout=timeout, extra_env=self._account_env(grant, "claude"),
            label="Claude machine-account approval")

    def wait_for_merge(self, grant: dict[str, Any], pr_number: int, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = self._run([
                "gh", "pr", "view", str(pr_number), "--repo", grant["repository"],
                "--json", "state,mergedAt,url",
            ], timeout=remaining, extra_env=self._account_env(grant, "local"), complete_stdout=True,
                label="pull request merge read")
            value = json.loads(result.stdout)
            if isinstance(value, dict) and value.get("state") == "MERGED":
                return value
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        raise BenchError("auto-merge did not land within the acceptance budget")


def _result_envelope(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((
            item
            for item in reversed(value)
            if isinstance(item, dict) and item.get("type") == "result"
        ), None)
    return None


def reported_opus_major(output: str) -> int | None:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return None
    envelope = _result_envelope(value)
    if envelope is None:
        return None
    trusted_names: list[str] = []
    model = envelope.get("model")
    if isinstance(model, str):
        trusted_names.append(model)
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict):
        trusted_names.extend(str(name) for name in usage)
    versions = [
        int(match.group(1))
        for name in trusted_names
        if (match := re.search(r"(?:^|[-_])opus[-_](\d+)(?:[-_]|$)", name.lower()))
    ]
    return min(versions) if versions else None


def structured_value(result: CommandResult) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BenchError(f"model returned non-JSON output: {result.stdout[-1000:]}") from exc
    envelope = _result_envelope(value)
    if envelope is None:
        if isinstance(value, list):
            raise BenchError("model returned no result object")
        raise BenchError("model returned a non-object")
    if envelope.get("type") == "result" and (
        envelope.get("is_error") is True
        or envelope.get("api_error_status") is not None
        or envelope.get("subtype") not in (None, "success")
    ):
        raise BenchError("model result reported an error")
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
        "required": ["verdict", "summary", "securityFindings"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "summary": {"type": "string"},
            "securityFindings": {
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

    def __init__(
        self,
        runner: SubprocessRunner | None = None,
        credential_envs: set[str] | None = None,
        developer_identity: tuple[str, str] | None = None,
    ):
        self.runner = runner or SubprocessRunner()
        self.credential_envs = set(credential_envs or ())
        self.developer_identity = developer_identity

    def _agent_env(self, session_dir: Path, provider: str) -> dict[str, str]:
        environment = isolated_command_env(
            session_dir,
            self.credential_envs,
            scrub_all_credentials=True,
        )
        environment.update({
            name: os.environ[name]
            for name in PROVIDER_AUTH_ENV[provider]
            if name in os.environ
        })
        if provider == "local" and self.developer_identity:
            name, email = self.developer_identity
            environment.update({
                "GIT_CONFIG_COUNT": "8",
                "GIT_CONFIG_KEY_6": "user.name",
                "GIT_CONFIG_VALUE_6": name,
                "GIT_CONFIG_KEY_7": "user.email",
                "GIT_CONFIG_VALUE_7": email,
            })
        return environment

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
        ], cwd, timeout, input_text=prompt, extra_env=self._agent_env(session_dir, "claude"),
            inherit_env=False, complete_stdout=True)
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
        ], cwd, timeout, input_text=prompt, extra_env=self._agent_env(session_dir, "codex"), inherit_env=False)
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
            "-m", LOCAL_DEVELOPER_MODEL, "--ephemeral", "-s", "workspace-write",
            "-C", str(worktree), "-",
        ], worktree, timeout, input_text=prompt, extra_env=self._agent_env(_session_dir, "local"), inherit_env=False)
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
        local_account = self.grant["machineAccounts"]["local"]
        self.agents = agents or AgentDriver(
            credential_envs=self.credential_envs,
            developer_identity=(local_account["login"], local_account["email"]),
        )
        self.decisions = decisions or DecisionClient(self.grant["decisionApi"])
        self.sequence = 0
        self.claimed_at: str | None = None
        self.issue_payload: dict[str, Any] = {}
        self.stage_started: dict[str, str] = {}
        self.active_stage: str | None = None
        self.active_budget: StageBudget | None = None
        self.auto_merge_armed = False
        self.security_review_required = any(self.grant["surfaces"].values())
        self.evidence: dict[str, Any] = {
            "schemaVersion": 1,
            "missionId": self.grant["missionId"],
            "sessionId": self.session_id,
            "grant": self.grant,
            "startedAt": utc_now(),
            "stages": {},
            "reviewCorrectionRounds": 0,
            "claim": {"status": "unclaimed"},
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
        self.active_stage = stage
        self.active_budget = budget
        feed_summary = summary if len(summary) <= 2000 else summary[:1997] + "..."
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
                "claimedAt": require_text(self.claimed_at, "issue claim timestamp"),
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
                "summary": feed_summary,
            },
            "budgetLimit": budget.limit(),
            "budgetSpent": budget.spent(),
            "outcome": {
                "status": outcome_status,
                "summary": feed_summary,
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
        environment = isolated_command_env(
            self.session_dir,
            self.credential_envs,
            scrub_all_credentials=True,
        )
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
        surfaces = self.grant["surfaces"]

        def surface_checkbox(name: str, label: str) -> str:
            mark = "x" if surfaces[name] else " "
            return f"- [{mark}] {label}"

        surface_lines = "\n".join((
            surface_checkbox("authentication", "Authentication, authorization, or session handling"),
            surface_checkbox("networkService", "A network-listening service, or its bind address"),
            surface_checkbox("subprocessExecution", "Subprocess or container execution"),
            surface_checkbox(
                "authoritySchemas",
                "Authority-owned schemas (`schemas/canonical/`, `schemas/schema-registry.json`)",
            ),
            f"- [{'x' if not any(surfaces.values()) else ' '}] None of the above",
        ))
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
            f"{surface_lines}\n\n"
            "These machine-checkable declarations are part of the mission grant. Anything ticked above requires "
            "the repository's security-review path before merge; authority-owned paths also stop for Person delivery.\n\n"
            "## Reversibility\n\n"
            "Revert the mission commit or close this pull request without merging. The bench does not mutate main directly.\n"
        )

    def _decision_pending(
        self,
        handle: dict[str, Any],
        kind: str = "plan-unclean",
    ) -> dict[str, Any]:
        request = handle.get("decisionRequest") if isinstance(handle.get("decisionRequest"), dict) else {}
        return {
            "decisionId": require_text(request.get("id"), "decision request id"),
            "kind": kind,
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
        pending = self._decision_pending(handle, "blocked")
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

    def _enforce_path_abort_conditions(self, paths: list[str]) -> None:
        for condition in self.grant["abortConditions"]:
            if condition["kind"] != "path-changed":
                continue
            matched = [
                path for path in paths
                if any(
                    path == prefix.rstrip("/")
                    or path.startswith(prefix.rstrip("/") + "/")
                    for prefix in condition["paths"]
                )
            ]
            if matched:
                raise BenchError(
                    f"abort condition matched changed repository paths: {matched}"
                )

    def _enforce_allowed_paths(self, paths: list[str]) -> None:
        outside = [
            path for path in paths
            if not any(
                path == prefix.rstrip("/")
                or path.startswith(prefix.rstrip("/") + "/")
                for prefix in self.grant["allowedPaths"]
            )
        ]
        if outside:
            raise BenchError(f"repository paths are outside the granted path wall: {outside}")

    def _enforce_live_issue_guards(self, budget: StageBudget) -> None:
        issue = self.platform.issue(self.grant, budget.remaining())
        labels = {
            str(item.get("name")) for item in issue.get("labels", [])
            if isinstance(item, dict) and item.get("name")
        }
        self.security_review_required = self.security_review_required or "security-review" in labels
        if not any(
            condition["kind"] == "grant-drift"
            for condition in self.grant["abortConditions"]
        ):
            return
        requirement = issue_section(str(issue.get("body") or ""), "Requirement")
        acceptance = issue_section(str(issue.get("body") or ""), "Acceptance evidence")
        if (
            requirement != self.grant["requirement"]
            or acceptance != self.grant["acceptanceEvidence"]
        ):
            raise BenchError("abort condition matched a changed issue contract")

    def _validate_changed_paths(
        self,
        paths: list[str],
        budget: StageBudget,
        contract: str,
    ) -> None:
        self._stop_for_authority_paths(paths, budget, contract)
        self._enforce_allowed_paths(paths)
        self._enforce_path_abort_conditions(paths)

    def run(self) -> dict[str, Any]:
        try:
            return self._run_pipeline()
        except BenchError as exc:
            final_error = self._record_failure(exc)
            if final_error is not exc:
                raise final_error from exc
            raise

    def _record_failure(self, error: BenchError) -> BenchError:
        if self.auto_merge_armed and isinstance(self.evidence.get("pullRequest"), dict):
            try:
                self.platform.disable_auto_merge(
                    self.grant,
                    int(self.evidence["pullRequest"]["number"]),
                )
                self.auto_merge_armed = False
            except BenchError as cancel_error:
                error = BenchError(f"{error}; auto-merge cancellation also failed: {cancel_error}")
        reason = str(error)[:2000] or "mission bench stopped"
        exhausted = "budget" in reason.lower() or "exceeded its" in reason.lower()
        state = "budget-exhausted" if exhausted else "failed"
        self.evidence["state"] = state
        self.evidence["failure"] = {"reason": reason}
        self.evidence["completedAt"] = utc_now()
        self._save_evidence()
        if self.active_stage is None or self.active_budget is None:
            return error
        self._emit(
            self.active_stage,
            state,
            reason,
            self.active_budget,
            event_kind="session-stopped",
            outcome_status=state,
        )
        return error

    def _run_pipeline(self) -> dict[str, Any]:
        issue = self.platform.issue(self.grant)
        self.issue_payload = issue
        labels = {
            str(item.get("name")) for item in issue.get("labels", [])
            if isinstance(item, dict) and item.get("name")
        }
        self.security_review_required = self.security_review_required or "security-review" in labels
        requirement = issue_section(str(issue.get("body") or ""), "Requirement")
        acceptance = issue_section(str(issue.get("body") or ""), "Acceptance evidence")
        if requirement != self.grant["requirement"] or acceptance != self.grant["acceptanceEvidence"]:
            raise BenchError("the issue requirement or acceptance evidence changed after mission grant")

        self.platform.validate_machine_accounts(self.grant)
        self.platform.validate_repository_wall(self.grant)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._save_evidence()
        plan_budget = StageBudget(self.grant["budgets"]["plan"])

        def record_assignment() -> None:
            self.claimed_at = utc_now()
            self.evidence["claim"] = {
                "status": "assigned",
                "claimedAt": self.claimed_at,
            }
            self._save_evidence()
            self._emit(
                "plan",
                "working",
                "The granted mission assigned its issue under the validated repository wall.",
                plan_budget,
                event_kind="stage-started",
            )

        self.platform.claim_issue(
            self.grant,
            self.session_id,
            on_assigned=record_assignment,
        )
        self.evidence["claim"]["status"] = "claimed"
        self._save_evidence()
        self._emit(
            "plan",
            "working",
            "The granted mission is preparing its isolated planning worktree.",
            plan_budget,
        )
        worktree = self.platform.prepare_worktree(self.grant, self.session_dir)
        contract = self._contract()

        self._emit("plan", "working", "Claude Opus is validating the granted plan.", plan_budget)
        plan_prompt = (
            "Plan this granted mission. Do not change its requirement, acceptance evidence, budgets, or authority. "
            "Return clean=false when any assumption, scope boundary, or acceptance command is unresolved. "
            "Every touchedPaths entry must stay inside the granted path wall.\n\n"
            f"{contract}\n\nAllowed paths: {json.dumps(self.grant['allowedPaths'])}\n\n"
            f"Definition of done: {json.dumps(self.grant['definitionOfDone'], sort_keys=True)}\n\n"
            f"Abort conditions: {json.dumps(self.grant['abortConditions'])}"
        )
        plan = self.agents.plan(plan_prompt, worktree, plan_budget, self.session_dir)
        if plan.get("clean") is not True:
            summary = require_text(plan.get("summary"), "unclean plan summary")
            handle = self.decisions.request(f"The granted mission plan is unclean. {summary}\n\n{contract}")
            pending = self._decision_pending(handle)
            self._emit(
                "plan", "waiting-on-person", summary, plan_budget,
                event_kind="decision-requested", pending_decision=pending,
            )
            self.evidence["decision"] = {"request": handle}
            self._save_evidence()
            answer = self.decisions.wait_for_answer(handle["jobId"], plan_budget.remaining())
            self.evidence["decision"]["answer"] = answer
            self._save_evidence()
            if answer.get("selectedOptionId") == "pause":
                raise BenchError("the Person paused the mission during plan escalation")
            plan = self.agents.plan(
                plan_prompt + "\n\nThe Person answered through the decision flow: " + json.dumps(answer, sort_keys=True),
                worktree,
                plan_budget,
                self.session_dir,
            )
            if plan.get("clean") is not True:
                raise BenchError("plan remained unclean after the Person decision")
        plan_paths = plan.get("touchedPaths")
        if not isinstance(plan_paths, list) or not all(isinstance(path, str) for path in plan_paths):
            raise BenchError("plan touchedPaths must be an array of repository paths")
        self._enforce_allowed_paths(plan_paths)
        self.evidence["stages"]["plan"] = plan
        self._save_evidence()
        self._emit("plan", "idle", require_text(plan.get("summary"), "plan summary"), plan_budget, event_kind="stage-completed")

        develop_budget = StageBudget(self.grant["budgets"]["develop"])
        red_result = self.platform.run_command(
            self.grant["definitionOfDone"]["redTest"],
            worktree,
            develop_budget.remaining(),
            isolated_command_env(
                self.session_dir,
                self.credential_envs,
                scrub_all_credentials=True,
            ),
        )
        expected_red = self.grant["definitionOfDone"]["redFailure"]
        red_output = command_tail(red_result, limit=20000)
        if (
            red_result.returncode not in expected_red["exitCodes"]
            or re.search(expected_red["outputPattern"], red_output) is None
        ):
            raise BenchError("the red test did not match its granted failure signal")
        self.evidence["redTest"] = {
            "argv": red_result.argv,
            "returncode": red_result.returncode,
            "outputTail": red_output,
            "expectedFailure": expected_red,
        }
        self._save_evidence()

        self._emit("develop", "working", "The local developer is implementing and committing in its isolated worktree.", develop_budget, event_kind="stage-started")
        develop_prompt = (
            "Implement the granted mission in this isolated worktree. The configured focused regression has already "
            "been observed failing. Make it pass, keep the full suite green, and commit all changes using the existing "
            "machine-account git identity. For file edits, invoke the apply_patch executable through the shell; the "
            "local-provider router does not support a direct apply_patch tool call. Do not push or create a pull "
            "request. Stay inside the granted path wall: "
            f"{json.dumps(self.grant['allowedPaths'])}.\n\n"
            f"{contract}\n\nDefinition of done: "
            f"{json.dumps(self.grant['definitionOfDone'], sort_keys=True)}\n\n"
            f"Approved plan: {json.dumps(plan, sort_keys=True)}"
        )
        for attempt in range(develop_budget.limit_turns):
            prompt = develop_prompt
            if attempt:
                prompt = (
                    "Continue the same granted implementation now. The previous local turn returned without a "
                    "commit; preserve its valid in-wall edits, finish every remaining plan step, run the required "
                    "checks, and create exactly one commit. Use the apply_patch executable through the shell for "
                    "all file edits.\n\n" + develop_prompt
                )
            self.agents.develop(prompt, worktree, develop_budget, self.session_dir)
            try:
                commit = self.platform.assert_machine_commit(self.grant, worktree)
                break
            except BenchError as exc:
                if str(exc) != "local developer did not create an attributable commit":
                    raise
                if attempt + 1 >= develop_budget.limit_turns:
                    raise
                self._emit(
                    "develop", "working",
                    "The local developer returned without a commit; the bounded develop stage is retrying.",
                    develop_budget,
                )
        paths = self.platform.changed_paths(self.grant, worktree)
        self._validate_changed_paths(paths, develop_budget, contract)
        gates = self._green_gates(worktree, develop_budget)
        self.platform.assert_unchanged_machine_commit(
            self.grant,
            worktree,
            expected_commit=commit,
        )
        paths = self.platform.changed_paths(self.grant, worktree)
        self._validate_changed_paths(paths, develop_budget, contract)
        self.evidence["stages"]["develop"] = {"commit": commit, "paths": paths, "gates": gates}
        self._save_evidence()
        self._emit("develop", "idle", "Local developer commit and all pre-push gates are green.", develop_budget, event_kind="stage-completed")

        self._enforce_live_issue_guards(develop_budget)
        self.platform.push(self.grant, worktree, develop_budget.remaining())
        body_path = self.session_dir / "pull-request.md"
        body_path.write_text(self._pr_body())
        pull_request = self.platform.create_pr(
            self.grant,
            worktree,
            body_path,
            develop_budget.remaining(),
        )
        self.evidence["pullRequest"] = pull_request
        self._save_evidence()

        previous_commit = commit
        corrections = 0
        clean_review: dict[str, Any] | None = None
        review_budget = StageBudget(self.grant["budgets"]["review"])
        for round_number in range(1, self.grant["maxReviewRounds"] + 1):
            self._emit("review", "working", f"Codex review round {round_number} is inspecting the committed diff.", review_budget, event_kind="stage-started")
            review_prompt = (
                "Review the committed diff against the granted requirement and acceptance evidence. Report only "
                "actionable correctness, security, or regression findings.\n\n"
                f"{contract}\n\nApproved plan: {json.dumps(plan, sort_keys=True)}"
            )
            review = self.agents.review(review_prompt, worktree, review_budget, self.session_dir)
            self.platform.post_codex_review(
                self.grant,
                int(pull_request["number"]),
                review,
                review_budget.remaining(),
            )
            review_budget.remaining()
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
            self.agents.fix(fix_prompt, worktree, develop_budget, self.session_dir)
            baseline_commit = previous_commit
            correction_commit = self.platform.assert_machine_commit(
                self.grant, worktree, previous_commit=baseline_commit
            )
            correction_paths = self.platform.changed_paths(self.grant, worktree)
            self._validate_changed_paths(correction_paths, develop_budget, contract)
            correction_gates = self._green_gates(worktree, develop_budget)
            self.platform.assert_unchanged_machine_commit(
                self.grant,
                worktree,
                expected_commit=correction_commit,
                previous_commit=baseline_commit,
            )
            correction_paths = self.platform.changed_paths(self.grant, worktree)
            self._validate_changed_paths(correction_paths, develop_budget, contract)
            previous_commit = correction_commit
            self.evidence["stages"].setdefault("corrections", []).append({
                "round": round_number,
                "commit": previous_commit,
                "gates": correction_gates,
            })
            self.evidence["reviewCorrectionRounds"] = corrections
            self._save_evidence()
            self._enforce_live_issue_guards(develop_budget)
            self.platform.push(self.grant, worktree, develop_budget.remaining())
        if clean_review is None:
            raise BenchError("Codex review loop exhausted its bounded correction rounds")

        body_path.write_text(self._pr_body())
        self.platform.update_pr_body(
            self.grant,
            int(pull_request["number"]),
            body_path,
            review_budget.remaining(),
        )
        review_budget.remaining()
        self._emit("review", "idle", require_text(clean_review.get("summary"), "review summary"), review_budget, event_kind="stage-completed")

        acceptance_budget = StageBudget(self.grant["budgets"]["acceptance"])
        self._enforce_live_issue_guards(acceptance_budget)
        self.platform.validate_repository_wall(
            self.grant,
            acceptance_budget.remaining(),
        )
        self.platform.assert_pr_head(
            self.grant,
            int(pull_request["number"]),
            previous_commit,
            acceptance_budget.remaining(),
        )
        self._emit("acceptance", "working", "Required CI is running before final acceptance.", acceptance_budget, event_kind="stage-started")
        ci = self.platform.wait_for_ci(
            self.grant, int(pull_request["number"]), acceptance_budget.remaining()
        )
        self.platform.validate_repository_wall(
            self.grant,
            acceptance_budget.remaining(),
        )
        self.platform.assert_pr_head(
            self.grant,
            int(pull_request["number"]),
            previous_commit,
            acceptance_budget.remaining(),
        )
        security_review_requested = self.security_review_required
        declared_surfaces = [
            name for name, declared in self.grant["surfaces"].items() if declared
        ]
        security_instruction = (
            "This acceptance is also the required security review against workplan section 4.5. "
            f"Declared surfaces: {json.dumps(declared_surfaces)}. Return every actionable security finding in "
            "securityFindings and reject when that array is non-empty. Return an empty array only after finding "
            "the labelled and declared surfaces clean."
            if security_review_requested
            else (
                "No dedicated security review is required for this mission; report any actionable security "
                "finding you nonetheless observe in securityFindings."
            )
        )
        acceptance_prompt = (
            "Perform the final read-only acceptance review. Approve only if the committed diff, failing-test evidence, "
            "green release gates, Codex correction loop, and CI satisfy the unchanged grant.\n\n"
            f"{security_instruction}\n\n{contract}\n\nCI evidence: {ci}\n\n"
            f"Codex review: {json.dumps(clean_review, sort_keys=True)}"
        )
        acceptance = self.agents.accept(
            acceptance_prompt, worktree, acceptance_budget, self.session_dir
        )
        summary = require_text(acceptance.get("summary"), "acceptance summary")
        security_findings = acceptance.get("securityFindings")
        if not isinstance(security_findings, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("priority"), str)
            and isinstance(item.get("body"), str)
            and item["body"].strip()
            for item in security_findings
        ):
            raise BenchError("Claude acceptance returned invalid security findings")
        security_review = {
            "required": security_review_requested,
            "status": (
                "escalated" if security_findings
                else "clean" if security_review_requested
                else "not-performed"
            ),
            "findings": security_findings,
        }
        self.evidence["stages"]["acceptance"] = {
            "ci": ci,
            "review": acceptance,
            "securityReview": security_review,
        }
        self._save_evidence()
        if security_findings:
            handle = self.decisions.request(
                "The final acceptance security finding requires Founder resolution: "
                f"{json.dumps(security_findings, sort_keys=True)}\n\n{contract}"
            )
            pending = self._decision_pending(handle, "blocked")
            security_review["decision"] = {"request": handle}
            self._save_evidence()
            self._emit(
                "acceptance",
                "waiting-on-person",
                summary,
                acceptance_budget,
                event_kind="decision-requested",
                pending_decision=pending,
            )
            answer = self.decisions.wait_for_answer(handle["jobId"], acceptance_budget.remaining())
            security_review["decision"]["answer"] = answer
            self._save_evidence()
            raise BenchError("security finding requires Founder resolution outside the mission bench")
        if acceptance.get("verdict") != "approve":
            raise BenchError(f"Claude acceptance rejected the mission: {summary}")
        self._enforce_live_issue_guards(acceptance_budget)
        if self.security_review_required and not security_review_requested:
            raise BenchError("security review became required after final acceptance")
        repository_wall = self.platform.validate_repository_wall(
            self.grant,
            acceptance_budget.remaining(),
        )
        self.platform.assert_pr_head(
            self.grant,
            int(pull_request["number"]),
            previous_commit,
            acceptance_budget.remaining(),
        )
        arm_timeout = acceptance_budget.remaining()
        self.auto_merge_armed = True
        self.platform.arm_auto_merge(
            self.grant,
            int(pull_request["number"]),
            previous_commit,
            arm_timeout,
        )
        waiting = self.platform.assert_auto_merge_waiting(
            self.grant,
            int(pull_request["number"]),
            previous_commit,
            acceptance_budget.remaining(),
        )
        self.platform.approve(
            self.grant,
            int(pull_request["number"]),
            summary,
            previous_commit,
            acceptance_budget.remaining(),
        )
        queued = {
            "state": "queued",
            "armedAt": utc_now(),
            "headCommit": previous_commit,
            "waitingBeforeApproval": waiting,
        }
        self.evidence["stages"]["acceptance"] = {
            "ci": ci,
            "review": acceptance,
            "securityReview": security_review,
            "repositoryWall": repository_wall,
            "autoMerge": queued,
        }
        self.evidence["state"] = "queued"
        self.evidence["completedAt"] = utc_now()
        self._save_evidence()
        artifacts = [
            {"kind": "evidence-pack", "uri": str(self.evidence_path)},
            {"kind": "pull-request", "uri": str(pull_request["url"])},
        ]
        self._emit(
            "acceptance", "succeeded", f"{summary} Auto-merge is queued.", acceptance_budget,
            event_kind="session-completed", outcome_status="succeeded", artifact_refs=artifacts,
        )
        return {
            "ok": True,
            "state": "queued",
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
