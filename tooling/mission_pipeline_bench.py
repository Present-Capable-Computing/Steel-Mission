#!/usr/bin/env python3
"""Disposable C1 four-stage mission bench.

The bench rehearses the D8/D9 pipeline without becoming a product dispatch path.
Its only durable contract is the agent-session status feed it validates before
every append. Runtime grants, worktrees, checkpoints, and evidence packs live in
an explicitly supplied state directory outside the product repository.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
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
from typing import Any

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
STAGE_DETAILS = {
    "plan": ("plan", "claude", "planner", "opus"),
    "develop": ("develop-and-commit", "local", "developer", "qwen2.5-coder:14b"),
    "review": ("review-loop", "codex", "reviewer", "codex"),
    "acceptance": ("final-review-and-merge", "claude", "acceptance", "opus"),
}
PROVIDER_AUTH_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY",),
    "local": (),
}
PROVIDER_CREDENTIAL_ENVS = {
    name for names in PROVIDER_AUTH_ENV.values() for name in names
}
UNTRUSTED_BASE_ENV = (
    "PATH", "USER", "LOGNAME", "SHELL", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "OLLAMA_HOST",
)
DIAGNOSTIC_TAIL_BYTES = 20_000
COMPLETE_STDOUT_BYTES = 1_000_000
PARENT_CONTAINMENT_READY = False


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


def atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    """Replace a parent-owned text artifact without following its destination."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, mode)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise BenchError("cannot write a trusted parent text artifact") from exc


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
        if token_env in PROVIDER_CREDENTIAL_ENVS:
            raise BenchError("GitHub token reference cannot use a provider credential variable")
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
    if token_env is not None:
        token_env = require_text(token_env, "decisionApi.tokenEnv")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", token_env):
            raise BenchError("decisionApi.tokenEnv must name an environment variable")
        if token_env in PROVIDER_CREDENTIAL_ENVS:
            raise BenchError("GitHub token reference cannot use a provider credential variable")
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


def protect_parent_credentials() -> None:
    """Deny same-UID Linux children access to this process through /proc."""
    if not sys.platform.startswith("linux"):
        raise BenchError("mission execution requires Linux parent credential isolation")
    global PARENT_CONTAINMENT_READY
    libc = ctypes.CDLL(None, use_errno=True)
    protections = (
        (4, 0, "protect parent credential memory"),
        (36, 1, "enable child-subreaper containment"),
    )
    for option, value, label in protections:
        if libc.prctl(option, value, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise BenchError(f"cannot {label}: {os.strerror(error_number)}")
    PARENT_CONTAINMENT_READY = True


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filters", ctypes.POINTER(_SockFilter)),
    ]


def _landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        raise BenchError("untrusted filesystem isolation requires Linux Landlock")
    machine = os.uname().machine.lower()
    if machine not in {"x86_64", "amd64", "aarch64", "arm64", "riscv64"}:
        raise BenchError(f"unsupported Linux architecture for Landlock isolation: {machine}")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(444, None, 0, 1)
    if result < 1:
        error_number = ctypes.get_errno()
        raise BenchError(f"Linux Landlock isolation is unavailable: {os.strerror(error_number)}")
    return int(result)


def _restrict_filesystem_with_landlock(
    abi: int,
    read_roots: tuple[str, ...],
    write_roots: tuple[str, ...],
) -> None:
    execute = 1 << 0
    read_file = 1 << 2
    read_dir = 1 << 3
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13
    if abi >= 3:
        handled |= 1 << 14
    read_access = execute | read_file | read_dir
    write_access = handled
    libc = ctypes.CDLL(None, use_errno=True)
    ruleset = _LandlockRulesetAttr(handled_access_fs=handled)
    ruleset_fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "cannot create Landlock ruleset")
    opened: list[int] = []
    try:
        roots = [(path, read_access) for path in read_roots]
        roots.extend((path, write_access) for path in write_roots)
        for value, access in roots:
            path = Path(value)
            if not path.exists():
                continue
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            opened.append(descriptor)
            rule = _LandlockPathBeneathAttr(
                allowed_access=access,
                parent_fd=descriptor,
            )
            if libc.syscall(445, ruleset_fd, 1, ctypes.byref(rule), 0) != 0:
                raise OSError(ctypes.get_errno(), f"cannot add Landlock rule for {path}")
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot enable no-new-privileges")
        if libc.syscall(446, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot apply Landlock ruleset")
    finally:
        for descriptor in opened:
            os.close(descriptor)
        os.close(ruleset_fd)


def _deny_descendant_process_group_escape() -> None:
    machine = os.uname().machine.lower()
    denied_by_architecture = {
        "x86_64": (109, 112),
        "amd64": (109, 112),
        "aarch64": (154, 157),
        "arm64": (154, 157),
        "riscv64": (154, 157),
    }
    denied = denied_by_architecture.get(machine)
    if denied is None:
        raise OSError(95, f"unsupported architecture for process containment: {machine}")
    load_syscall_number = 0x20
    jump_equal = 0x15
    return_errno = 0x00050000 | 1
    return_allow = 0x7FFF0000
    instructions = [_SockFilter(load_syscall_number, 0, 0, 0)]
    for syscall_number in denied:
        instructions.extend((
            _SockFilter(jump_equal, 0, 1, syscall_number),
            _SockFilter(0x06, 0, 0, return_errno),
        ))
    instructions.append(_SockFilter(0x06, 0, 0, return_allow))
    filters = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(length=len(instructions), filters=filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable no-new-privileges")
    if libc.prctl(22, 2, ctypes.byref(program)) != 0:
        raise OSError(ctypes.get_errno(), "cannot install process-containment filter")


def isolated_command_env(
    session_dir: Path,
    credential_envs: set[str],
    *,
    scrub_all_credentials: bool = False,
) -> dict[str, str]:
    """Remove GitHub authority from repository-authored and model commands."""
    sandbox_root = session_dir / "worker-sandbox"
    gh_config = sandbox_root / "no-github-credentials"
    hooks = sandbox_root / "no-git-hooks"
    sandbox_home = sandbox_root / "home"
    sandbox_tmp = sandbox_root / "tmp"
    sandbox_directories = (
        sandbox_root,
        gh_config,
        hooks,
        sandbox_home,
        sandbox_tmp,
        sandbox_home / ".config",
        sandbox_home / ".cache",
        sandbox_home / ".runtime",
        sandbox_home / ".codex",
        sandbox_home / ".claude",
    )
    for directory in sandbox_directories:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise BenchError(f"cannot inspect worker sandbox directory: {directory}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise BenchError(f"worker sandbox path is not a real directory: {directory}")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(directory, flags)
            try:
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise BenchError(f"cannot secure worker sandbox directory: {directory}") from exc
    environment = {
        "HOME": str(sandbox_home),
        "TMPDIR": str(sandbox_tmp),
        "TEMP": str(sandbox_tmp),
        "TMP": str(sandbox_tmp),
        "XDG_CONFIG_HOME": str(sandbox_home / ".config"),
        "XDG_CACHE_HOME": str(sandbox_home / ".cache"),
        "XDG_RUNTIME_DIR": str(sandbox_home / ".runtime"),
        "CODEX_HOME": str(sandbox_home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(sandbox_home / ".claude"),
        "SM_BENCH_SANDBOX_ROOT": str(sandbox_root),
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

    @staticmethod
    def _kill_adopted_descendants() -> None:
        if not PARENT_CONTAINMENT_READY:
            return
        task_root = Path("/proc/self/task")
        deadline = time.monotonic() + 2
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            children: set[int] = set()
            try:
                child_files = list(task_root.glob("*/children"))
                for child_file in child_files:
                    try:
                        children.update(int(value) for value in child_file.read_text().split())
                    except FileNotFoundError:
                        continue
            except OSError as exc:
                raise BenchError(f"cannot enumerate contained descendants: {exc}") from exc
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            while True:
                try:
                    waited, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if waited == 0:
                    break
            if children:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= 0.25:
                return
            time.sleep(0.01)
        raise BenchError("contained descendants could not be reaped")

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
        execution_argv = list(argv)
        landlock_setup: tuple[int, tuple[str, ...], tuple[str, ...]] | None = None
        if not inherit_env and sys.platform.startswith("linux"):
            located = execution_argv[0] if os.path.isabs(execution_argv[0]) else shutil.which(
                execution_argv[0],
                path=environment.get("PATH"),
            )
            if not located:
                raise BenchError(f"untrusted executable is unavailable: {execution_argv[0]}")
            executable = Path(located).resolve()
            execution_argv[0] = str(executable)
            sandbox_root = environment.pop("SM_BENCH_SANDBOX_ROOT", "")
            extra_read_roots = environment.pop("SM_BENCH_SANDBOX_READ_ROOTS", "")
            read_roots = [
                path for path in (
                    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt",
                    "/nix/store", "/proc/self", "/proc/thread-self", "/sys",
                    str(executable.parent),
                )
                if Path(path).exists()
            ]
            read_roots.extend(
                path for path in extra_read_roots.split(os.pathsep) if path
            )
            write_roots = [str(cwd), "/dev"]
            if sandbox_root:
                write_roots.append(sandbox_root)
            abi = _landlock_abi()
            landlock_setup = (
                abi,
                tuple(dict.fromkeys(read_roots)),
                tuple(dict.fromkeys(write_roots)),
            )
        preexec_fn = None
        if landlock_setup is not None or (
            sys.platform.startswith("linux") and PARENT_CONTAINMENT_READY
        ):
            def child_setup() -> None:
                if landlock_setup is not None:
                    _restrict_filesystem_with_landlock(*landlock_setup)
                if PARENT_CONTAINMENT_READY:
                    _deny_descendant_process_group_escape()

            preexec_fn = child_setup
        started = time.monotonic()
        if timeout <= 0:
            raise BenchError(f"command exceeded its {timeout}s budget: {argv[0]}")
        with tempfile.TemporaryFile() as stdin:
            stdin.write(input_text.encode())
            stdin.seek(0)
            try:
                process = subprocess.Popen(
                    execution_argv,
                    cwd=cwd,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
            except (OSError, subprocess.SubprocessError) as exc:
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
                self._kill_adopted_descendants()
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

    def remaining(self) -> float:
        remaining = self.limit_seconds - max(0.0, time.monotonic() - self.started)
        if remaining <= 0:
            raise BenchError("session elapsed-time budget is exhausted")
        return remaining

    def use_turn(self) -> float:
        if self.turns >= self.limit_turns:
            raise BenchError("session turn budget is exhausted")
        self.turns += 1
        return self.remaining()

    def spent(self) -> dict[str, int]:
        return {"elapsedSeconds": self.elapsed(), "turns": self.turns}

    def limit(self) -> dict[str, int]:
        return {"elapsedSeconds": self.limit_seconds, "turns": self.limit_turns}


class CoupledStageBudget:
    """Charge a correction to both the development and active review grants."""

    def __init__(self, *budgets: StageBudget):
        self.budgets = budgets

    def remaining(self) -> float:
        return min(budget.remaining() for budget in self.budgets)

    def use_turn(self) -> float:
        for budget in self.budgets:
            budget.use_turn()
        return self.remaining()

    def spent(self) -> dict[str, int]:
        values = [budget.spent() for budget in self.budgets]
        return {
            "elapsedSeconds": max(value["elapsedSeconds"] for value in values),
            "turns": max(value["turns"] for value in values),
        }

    def limit(self) -> dict[str, int]:
        values = [budget.limit() for budget in self.budgets]
        return {
            "elapsedSeconds": min(value["elapsedSeconds"] for value in values),
            "turns": min(value["turns"] for value in values),
        }


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
                ["gh", "api", "user", "--jq", ".login"],
                extra_env=environment,
                label=f"{worker} machine-account check",
            )
            actual = result.stdout.strip()
            if actual != expected:
                raise BenchError(f"{worker} credential belongs to {actual!r}, expected {expected!r}")
            if actual in seen:
                raise BenchError("machine-account credentials resolve to duplicate GitHub users")
            seen.add(actual)
            email_result = self._run(
                ["gh", "api", "user/emails"],
                extra_env=environment,
                complete_stdout=True,
                label=f"{worker} verified-email check",
            )
            try:
                emails = json.loads(email_result.stdout)
            except json.JSONDecodeError as exc:
                raise BenchError(f"{worker} verified-email check returned invalid JSON") from exc
            verified = {
                str(item.get("email", "")).lower()
                for item in emails
                if isinstance(item, dict) and item.get("verified") is True
            } if isinstance(emails, list) else set()
            if account["email"].lower() not in verified:
                raise BenchError(
                    f"{worker} commit email is not verified by its authenticated GitHub account"
                )

    def validate_repository_wall(self, grant: dict[str, Any], timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise BenchError("repository-wall validation exceeded its stage budget")
            return value

        encoded_branch = urllib.parse.quote(grant["baseBranch"], safe="")
        ref_endpoint = f"repos/{grant['repository']}/git/ref/heads/{encoded_branch}"
        environment = self._account_env(grant, "local")

        def live_base_oid() -> str:
            result = self._run(
                ["gh", "api", ref_endpoint],
                timeout=remaining(),
                extra_env=environment,
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
        codeowners_result = self._run([
            "gh", "api", "-H", "Accept: application/vnd.github.raw+json",
            f"repos/{grant['repository']}/contents/.github/CODEOWNERS?ref={base_oid}",
        ], timeout=remaining(), extra_env=environment, complete_stdout=True,
            label="live-base CODEOWNERS read")
        result = self._run([
            "gh", "api",
            f"repos/{grant['repository']}/branches/{encoded_branch}/protection",
        ], timeout=remaining(), extra_env=environment, complete_stdout=True,
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
        required_contexts = {
            item.get("context")
            for item in required_checks
            if isinstance(item, dict) and isinstance(item.get("context"), str)
        }
        interpreter_checks = {
            "Python test suite (3.11)",
            "Python test suite (3.12)",
        }
        if not interpreter_checks.issubset(required_contexts):
            raise BenchError("branch protection must require both interpreter checks")
        if not isinstance(checks, dict) or checks.get("strict") is not True:
            raise BenchError("required checks must cover the current base branch")
        acceptance_login = grant["machineAccounts"]["claude"]["login"]
        machine_logins = {
            account["login"].lower() for account in grant["machineAccounts"].values()
        }
        codeowners = codeowners_result.stdout.splitlines()
        if live_base_oid() != base_oid:
            raise BenchError("base branch changed during repository-wall validation")
        self.validated_base_oids[grant["baseBranch"]] = base_oid
        rules = [line.split() for line in codeowners if line.strip() and not line.lstrip().startswith("#")]
        default_rule = next((rule for rule in reversed(rules) if rule[0] == "*"), [])
        default_owners = {token.lstrip("@") for token in default_rule[1:]}
        if {owner.lower() for owner in default_owners} != {acceptance_login.lower()}:
            raise BenchError("Claude acceptance account must be the sole default CODEOWNER")
        authority_patterns = (
            "/schemas/canonical/",
            "/schemas/schema-registry.json",
            "/docs/workplan.md",
            "/.github/CODEOWNERS",
        )

        def matches(pattern: str, path: str) -> bool:
            normalized = pattern.lstrip("/")
            candidate = path.lstrip("/")
            directory = normalized.endswith("/")
            if directory:
                normalized += "**"
            pieces: list[str] = []
            index = 0
            while index < len(normalized):
                character = normalized[index]
                if character == "*":
                    if index + 1 < len(normalized) and normalized[index + 1] == "*":
                        pieces.append(".*")
                        index += 2
                    else:
                        pieces.append("[^/]*")
                        index += 1
                elif character == "?":
                    pieces.append("[^/]")
                    index += 1
                else:
                    pieces.append(re.escape(character))
                    index += 1
            prefix = "" if "/" in normalized else "(?:.*/)?"
            return re.fullmatch(prefix + "".join(pieces), candidate) is not None

        for authority_pattern in authority_patterns:
            probe = (
                authority_pattern + ".mission-bench-codeowners-probe"
                if authority_pattern.endswith("/")
                else authority_pattern
            )
            matching_rules = [
                (index, rule)
                for index, rule in enumerate(rules)
                if matches(rule[0], probe)
            ]
            if not matching_rules:
                raise BenchError(f"{authority_pattern} must remain Person-owned in CODEOWNERS")
            effective_index, effective_rule = matching_rules[-1]
            effective_owners = {token.lstrip("@").lower() for token in effective_rule[1:]}
            if not effective_owners:
                raise BenchError(f"{authority_pattern} must remain Person-owned in CODEOWNERS")
            if effective_owners & machine_logins:
                raise BenchError(
                    "machine accounts cannot own authority paths in final effective CODEOWNERS rules"
                )
            if not authority_pattern.endswith("/"):
                continue
            authority_prefix = authority_pattern.lstrip("/")
            for rule in rules[effective_index + 1:]:
                rule_owners = {token.lstrip("@").lower() for token in rule[1:]}
                if not rule_owners & machine_logins:
                    continue
                static_prefix = re.split(r"[*?[]", rule[0].lstrip("/"), maxsplit=1)[0]
                if (
                    static_prefix.startswith(authority_prefix)
                    or authority_prefix.startswith(static_prefix)
                ):
                    raise BenchError(
                        "machine accounts cannot own authority paths in final effective CODEOWNERS rules"
                    )

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
        self._run(
            ["git", "clone", "--no-hardlinks", "--no-checkout", str(self.repository_root), str(worktree)],
            timeout=180,
            label="isolated repository creation",
        )
        self._run(
            ["git", "checkout", "-b", grant["branch"], fetched_base],
            cwd=worktree,
            timeout=120,
            label="isolated branch creation",
        )
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
                "--name-status", "-z", "--find-renames", f"{base_ref}...HEAD",
            ],
            cwd=worktree,
            extra_env=environment,
            inherit_env=False,
            complete_stdout=True,
            label="changed path read",
        )
        paths: list[str] = []
        fields = result.stdout.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        index = 0
        while index < len(fields):
            status = fields[index]
            path_count = 2 if status.startswith(("R", "C")) else 1
            end = index + 1 + path_count
            if not status or end > len(fields):
                raise BenchError("changed path read returned malformed NUL-delimited output")
            candidates = fields[index + 1:end]
            for path in candidates:
                if path and path not in paths:
                    paths.append(path)
            index = end
        return paths

    def push(self, grant: dict[str, Any], worktree: Path, timeout: float) -> None:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise BenchError("machine-account push exceeded the develop budget")
            return value

        token_env = grant["machineAccounts"]["local"]["tokenEnv"]
        token = os.environ.get(token_env)
        if not token:
            raise BenchError(f"machine-account credential {token_env} is not available")
        with tempfile.TemporaryDirectory(prefix="steel-mission-push-auth-") as auth_directory:
            trusted_root = Path(auth_directory)
            trusted_hooks = trusted_root / "hooks"
            trusted_home = trusted_root / "home"
            trusted_tmp = trusted_root / "tmp"
            for directory in (trusted_hooks, trusted_home, trusted_tmp):
                directory.mkdir(mode=0o700)
            preparation_environment = isolated_command_env(
                worktree.parent,
                self._credential_envs(grant),
                scrub_all_credentials=True,
            )
            preparation_environment.update({
                "HOME": str(trusted_home),
                "TMPDIR": str(trusted_tmp),
                "TEMP": str(trusted_tmp),
                "TMP": str(trusted_tmp),
                "GIT_CONFIG_VALUE_3": str(trusted_hooks),
            })
            askpass = Path(auth_directory) / "git-askpass.sh"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(askpass, flags, 0o700)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(
                        "#!/bin/sh\n"
                        "case \"$1\" in\n"
                        "  *Username*) printf '%s\\n' x-access-token ;;\n"
                        "  *) printf '%s\\n' \"$SM_BENCH_PUSH_TOKEN\" ;;\n"
                        "esac\n"
                    )
                    os.fchmod(stream.fileno(), 0o700)
            except OSError as exc:
                raise BenchError("cannot create a safe machine-account askpass helper") from exc
            environment = dict(preparation_environment)
            environment.update({
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "SM_BENCH_PUSH_TOKEN": token,
            })
            with tempfile.TemporaryDirectory(prefix="clean-push-", dir=worktree.parent) as directory:
                clean_git_dir = Path(directory)
                self._run(
                    ["git", "init", "--bare", str(clean_git_dir)],
                    cwd=worktree.parent,
                    timeout=remaining(),
                    extra_env=preparation_environment,
                    inherit_env=False,
                    label="clean push repository creation",
                )
                self._run(
                    ["git", "--git-dir", str(clean_git_dir), "fetch", "--no-tags", str(worktree), "HEAD"],
                    cwd=worktree.parent,
                    timeout=remaining(),
                    extra_env=preparation_environment,
                    inherit_env=False,
                    label="reviewed commit import",
                )
                self._run([
                    "git", "--git-dir", str(clean_git_dir), "push",
                    f"https://github.com/{grant['repository']}.git",
                    f"FETCH_HEAD:refs/heads/{grant['branch']}",
                ], cwd=worktree.parent, timeout=remaining(), extra_env=environment, inherit_env=False,
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
        ], cwd=self.repository_root, timeout=timeout, extra_env=self._account_env(grant, "local"),
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
            "--json", "headRefOid,baseRefName",
        ], timeout=timeout, extra_env=self._account_env(grant, "local"), complete_stdout=True,
            label="pull request head read")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("headRefOid") != expected_commit:
            raise BenchError("pull request head changed outside the granted mission")
        if value.get("baseRefName") != grant["baseBranch"]:
            raise BenchError("pull request base branch changed outside the granted mission")

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


def reported_opus_major(output: str) -> int | None:
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
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
            "-m", "qwen2.5-coder:14b", "--ephemeral", "-s", "workspace-write",
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

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise BenchError("decision API request exceeded its stage budget")
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
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise BenchError(f"decision API request failed: {exc}") from exc
        if not isinstance(value, dict) or "error" in value:
            raise BenchError(f"decision API refused the request: {value}")
        return value

    def request(
        self,
        context: str,
        timeout: float = 30,
        *,
        question: str = "How should this mission resolve the unclean plan?",
        options: list[dict[str, str]] | None = None,
        default_option_id: str = "pause",
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise BenchError("decision request exceeded its stage budget")
            return value

        choices = options or [
            {
                "id": "revise-within-grant",
                "label": "Revise within grant",
                "description": "Return the stated constraint to the planner without changing the granted outcome.",
            },
            {
                "id": "pause",
                "label": "Pause mission",
                "description": "Keep the mission stopped until a Person supplies different direction.",
            },
        ]
        value = self._request("/api/chat", method="POST", timeout=remaining(), payload={
            "question": question,
            "messages": [{"role": "user", "content": context[:12000]}],
            "workMode": "normal",
            "profile": "dc13.claude",
            "mock": True,
            "decisionRequest": {
                "question": question,
                "context": context[:12000],
                "options": choices,
                "defaultOptionId": default_option_id,
            },
        })
        remaining()
        job_id = require_text(value.get("jobId"), "decision job id")
        request = value.get("decisionRequest") if isinstance(value.get("decisionRequest"), dict) else {}
        if not request:
            raise BenchError("decision API did not create a pending decision")
        return {"jobId": job_id, "decisionRequest": request, "url": f"{self.base_url}/job/{job_id}"}

    def wait_for_answer(self, job_id: str, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            value = self._request(f"/api/chat/{job_id}", timeout=min(15, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
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
            time.sleep(min(2, max(0, deadline - time.monotonic())))
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
        self.requires_parent_protection = isinstance(self.platform, GitHubPlatform)
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
        self.claimed_at = utc_now()
        self.issue_payload: dict[str, Any] = {}
        self.stage_started: dict[str, str] = {}
        self.active_stage: str | None = None
        self.active_budget: StageBudget | None = None
        self.auto_merge_armed = False
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
        self.active_stage = stage
        self.active_budget = budget
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
            f"The mission touched human-owned paths and cannot merge them unattended: {authority_paths}\n\n{contract}",
            budget.remaining(),
            question="How should this authority-owned change be handed off?",
            options=[
                {
                    "id": "acknowledge-human-delivery",
                    "label": "Hand off to a Person",
                    "description": "Stop the unattended mission and leave the authority-owned change for Person review.",
                },
                {
                    "id": "pause",
                    "label": "Pause mission",
                    "description": "Keep the mission stopped until a Person supplies different direction.",
                },
            ],
            default_option_id="acknowledge-human-delivery",
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

    def _enforce_live_issue_guards(self, budget: StageBudget) -> None:
        issue = self.platform.issue(self.grant, budget.remaining())
        labels = {
            str(item.get("name")) for item in issue.get("labels", [])
            if isinstance(item, dict) and item.get("name")
        }
        if "security-review" in labels:
            raise BenchError("mission bench refuses issues labelled security-review")
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
        self._enforce_path_abort_conditions(paths)
        self._stop_for_authority_paths(paths, budget, contract)

    def run(self) -> dict[str, Any]:
        try:
            if self.requires_parent_protection:
                protect_parent_credentials()
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
        if self.active_stage is None or self.active_budget is None:
            return error
        reason = str(error)[:2000] or "mission bench stopped"
        exhausted = "budget" in reason.lower() or "exceeded its" in reason.lower()
        state = "budget-exhausted" if exhausted else "failed"
        self.evidence["state"] = state
        self.evidence["failure"] = {"reason": reason}
        self.evidence["completedAt"] = utc_now()
        self._save_evidence()
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
        plan_budget = StageBudget(self.grant["budgets"]["plan"])
        self._emit(
            "plan",
            "working",
            "The granted mission is preparing its isolated planning worktree.",
            plan_budget,
            event_kind="stage-started",
        )
        worktree = self.platform.prepare_worktree(self.grant, self.session_dir)
        contract = self._contract()

        self._emit("plan", "working", "Claude Opus is validating the granted plan.", plan_budget)
        plan_prompt = (
            "Plan this granted mission. Do not change its requirement, acceptance evidence, budgets, or authority. "
            "Return clean=false when any assumption, scope boundary, or acceptance command is unresolved.\n\n"
            f"{contract}\n\nAbort conditions: {json.dumps(self.grant['abortConditions'])}"
        )
        plan = self.agents.plan(plan_prompt, worktree, plan_budget, self.session_dir)
        if plan.get("clean") is not True:
            summary = require_text(plan.get("summary"), "unclean plan summary")
            handle = self.decisions.request(
                f"The granted mission plan is unclean. {summary}\n\n{contract}",
                plan_budget.remaining(),
            )
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
        self.evidence["stages"]["plan"] = plan
        self._save_evidence()
        self._emit("plan", "idle", require_text(plan.get("summary"), "plan summary"), plan_budget, event_kind="stage-completed")

        develop_budget = StageBudget(self.grant["budgets"]["develop"])
        self._emit(
            "develop",
            "working",
            "The granted regression is being observed before development begins.",
            develop_budget,
            event_kind="stage-started",
        )
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
            "machine-account git identity. Do not push or create a pull request.\n\n"
            f"{contract}\n\nApproved plan: {json.dumps(plan, sort_keys=True)}"
        )
        self.agents.develop(develop_prompt, worktree, develop_budget, self.session_dir)
        commit = self.platform.assert_machine_commit(self.grant, worktree)
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
        with tempfile.TemporaryDirectory(prefix="steel-mission-pr-body-") as directory:
            body_path = Path(directory) / "pull-request.md"
            atomic_text(body_path, self._pr_body())
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
            correction_budget = CoupledStageBudget(develop_budget, review_budget)
            self.agents.fix(fix_prompt, worktree, correction_budget, self.session_dir)
            correction_budget.remaining()
            baseline_commit = previous_commit
            correction_commit = self.platform.assert_machine_commit(
                self.grant, worktree, previous_commit=baseline_commit
            )
            correction_paths = self.platform.changed_paths(self.grant, worktree)
            self._validate_changed_paths(correction_paths, correction_budget, contract)
            correction_gates = self._green_gates(worktree, correction_budget)
            self.platform.assert_unchanged_machine_commit(
                self.grant,
                worktree,
                expected_commit=correction_commit,
                previous_commit=baseline_commit,
            )
            correction_paths = self.platform.changed_paths(self.grant, worktree)
            self._validate_changed_paths(correction_paths, correction_budget, contract)
            previous_commit = correction_commit
            self.evidence["stages"].setdefault("corrections", []).append({
                "round": round_number,
                "commit": previous_commit,
                "gates": correction_gates,
            })
            self.evidence["reviewCorrectionRounds"] = corrections
            self._save_evidence()
            self._enforce_live_issue_guards(correction_budget)
            self.platform.push(self.grant, worktree, correction_budget.remaining())
        if clean_review is None:
            raise BenchError("Codex review loop exhausted its bounded correction rounds")

        with tempfile.TemporaryDirectory(prefix="steel-mission-pr-body-") as directory:
            body_path = Path(directory) / "pull-request.md"
            atomic_text(body_path, self._pr_body())
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
        self.platform.assert_pr_head(
            self.grant,
            int(pull_request["number"]),
            previous_commit,
            acceptance_budget.remaining(),
        )
        acceptance_prompt = (
            "Perform the final read-only acceptance review. Approve only if the committed diff, failing-test evidence, "
            "green release gates, Codex correction loop, and CI satisfy the unchanged grant.\n\n"
            f"{contract}\n\nCI evidence: {ci}\n\nCodex review: {json.dumps(clean_review, sort_keys=True)}"
        )
        acceptance = self.agents.accept(
            acceptance_prompt, worktree, acceptance_budget, self.session_dir
        )
        if acceptance.get("verdict") != "approve":
            raise BenchError(f"Claude acceptance rejected the mission: {acceptance.get('summary')}")
        summary = require_text(acceptance.get("summary"), "acceptance summary")
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
        self.platform.approve(
            self.grant,
            int(pull_request["number"]),
            summary,
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
        queued = {
            "state": "queued",
            "armedAt": utc_now(),
            "headCommit": previous_commit,
        }
        self.evidence["stages"]["acceptance"] = {
            "ci": ci,
            "review": acceptance,
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
