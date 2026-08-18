"""Credential-backed advisory adapters implemented with Claude Code.

These handlers produce plans, reviews, and adversarial findings. None of their
outputs can certify PASS; only the deterministic verifier has that authority.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import common

BINARY = "claude"
LIVE_IMPLEMENTED = True
MAX_PROMPT_CHARS = 120_000

# Structured output is delivered through a tool round-trip, so a call that
# produces one costs at least two turns even with no tools available. The
# previous limit of 1 left zero headroom: measured 2026-08-17, every failing
# coordination-report ended `error_max_turns` rather than on any fault of the model,
# and the runs that did succeed reported num_turns=2 -- passing on the exact
# boundary. Three gives that round-trip room without granting any capability;
# `--tools ""` still means no tools.
MAX_TURNS = 6
# Prepended to every prompt. A server-side `advisor` tool was firing mid-run on
# roughly half of coordination-reports, costing 82-110s and accounting for most of the
# run-to-run spread (105.6s when it did not fire, 170-258s when it did).
# `--tools ""` does not reach it -- it is server-side, not a user tool -- and no
# CLI flag controls it, so the instruction is the only available lever. It is a
# request, never a guarantee: measure the invocation rate, never assume it.
NO_CONSULTATION_DIRECTIVE = (
    "Answer solely from the material in this prompt. Do not consult, research, or seek advice "
    "from any external source: everything needed is already here, and nothing outside it is "
    "authoritative for this task. Use only the structured-output tool that returns your answer.\n\n"
)
# A schema-conformance failure is a property of the attempt, not the request,
# so one retry is worth having. Both attempts share the caller's deadline --
# a retry never extends the budget, it only uses what is left of it.
MAX_INVOKE_ATTEMPTS = 2
MIN_RETRY_SECONDS = 90
# Silence, not duration, is what distinguishes a stalled run from a slow one.
# Set above the largest legitimate gap measured on 2026-08-17 (179.1s, spent
# inside the server-side advisor tool) so a healthy run is never killed for
# thinking; a genuinely stalled run stops here instead of waiting out the
# whole budget.
IDLE_TIMEOUT_SECONDS = 210
TURN_LIMIT_HINT = (
    f" (the run exhausted --max-turns {MAX_TURNS}; structured output needs a tool round-trip, "
    "so it costs at least two turns)"
)

# A coordination-report is schema-constrained classification of a snapshot the worker
# already gathered, so it does not need the default model's full reasoning
# budget. Measured 2026-08-17 against the real snapshot, one run each:
#   default model, default effort  288s  (past any interactive budget)
#   sonnet, effort low              53s  but PROVIDER_ERROR / an empty
#                                        "placeholder" stub claiming nothing
#                                        was unchecked -- the fabrication the
#                                        role canon forbids, so never low
#   sonnet, effort medium          112s  14 items, 6 notChecked -- substantive
#   sonnet, default effort         251s  43 items, but no headroom under the
#                                        270s budget
# Medium is the only setting that is both truthful and comfortably in budget.
COORDINATOR_MODEL = "claude-sonnet-5"
COORDINATOR_EFFORT = "medium"
# Raised from 270s not because runs need longer -- measured 2026-08-17, no
# successful run exceeded 194s -- but because 270s was truncating the CLI's
# own retry loop and reporting it as a timeout. Every "timeout" at 270.2s was
# really an `error_max_structured_output_retries` that never got to say so.
# The budget must outlast the failure so the failure can name itself, and
# leave room for one retry within it.
COORDINATOR_TIMEOUT_SECONDS = 450
# Roughly one run in four returned a single item over a populated snapshot --
# valid schema, plausible prose, no survey. Re-ask once when that happens; the
# thresholds are deliberately conservative so a narrow question that honestly
# warrants few items is never re-run (a scoped question measured 4-5 items).
COORDINATOR_SURVEY_ATTEMPTS = 2
COORDINATOR_SURVEY_MIN_ITEMS = 2
COORDINATOR_SURVEY_MIN_RECORDS = 10
COORDINATOR_MIN_RESURVEY_SECONDS = 100

# Claude Code's default credential store is the macOS login keychain, which a
# BatchMode SSH session cannot unlock. A long-lived token from `claude
# setup-token`, stored in this file by the operator (0600, never written by the
# worker), gives every transport the same answer. Resolution order:
# caller-provided CLAUDE_CODE_OAUTH_TOKEN env, then this file, then keychain.
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# Session-coupling variables the worker must not inherit from its caller, and
# the one that must survive because it is the credential.
SESSION_ENV_PREFIXES = ("CLAUDE_", "CLAUDECODE")
SESSION_ENV_KEEP = {TOKEN_ENV}
OUTPUT_TOKEN_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
MODEL_OUTPUT_TOKEN_BUDGET = "64000"
DEFAULT_TOKEN_FILE = Path.home() / ".claude" / "present-worker-token"
TOKEN_FILE_OVERRIDE_ENV = "PRESENT_WORKER_CLAUDE_TOKEN_FILE"


def token_file() -> Path:
    override = os.environ.get(TOKEN_FILE_OVERRIDE_ENV)
    return Path(override) if override else DEFAULT_TOKEN_FILE


def _credential_env() -> tuple[dict[str, str] | None, str | None]:
    """Return (env for claude invocations, defect reason).

    (None, None) means use the default environment -- either the caller already
    exported the token or no token file exists and the keychain decides. A
    token file that exists but cannot be used safely is a defect surfaced as a
    probe failure, never silently ignored: an operator who installed the file
    intended it to be the credential path. The token value itself never
    appears in any log, error, or artifact.
    """
    if os.environ.get(TOKEN_ENV):
        return None, None
    path = token_file()
    try:
        if not path.exists():
            return None, None
        mode = path.stat().st_mode
        if mode & 0o077:
            return None, f"token file {path} is group/other-accessible; refusing to use it (chmod 600)"
        token = path.read_text().strip()
    except OSError as exc:
        return None, f"token file {path} is unreadable: {exc.__class__.__name__}"
    if not token:
        return None, f"token file {path} is empty"
    return {**common.execution_env(), TOKEN_ENV: token}, None

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "steps", "openQuestions"],
    "properties": {
        "summary": {"type": "string"},
        "steps": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "description", "dependsOn"],
            "properties": {
                "id": {"type": "string"}, "description": {"type": "string"},
                "dependsOn": {"type": "array", "items": {"type": "string"}},
            },
        }},
        "openQuestions": {"type": "array", "items": {"type": "string"}},
    },
}

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"enum": ["ACCEPTED", "REVIEW_REJECTED", "CHANGES_REQUESTED"]},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["severity", "summary"],
            "properties": {
                "severity": {"enum": ["blocking", "major", "minor", "note"]},
                "summary": {"type": "string"}, "file": {"type": "string"}, "line": {"type": "integer"},
            },
        }},
    },
}

# DC13 Delivery Coordinator default status vocabulary, verbatim from the
# starter domain capability canon. The canon owns this
# list; refine it there first, never here.
COORDINATOR_STATUS_TERMS = [
    "ACTIVE",
    "WAITING ON TEAM",
    "WAITING ON FOUNDER",
    "AWAITING PROPAGATION",
    "IMPLEMENTING / VERIFYING",
    "CLOSED / IN FORCE",
    "BLOCKED",
    "DEFERRED",
    "SUPERSEDED / REJECTED",
    "UNKNOWN / UNVERIFIED",
]

# DC13's mandatory three-state distinction, plus the honest fourth answer.
COORDINATOR_STATE_CLASSES = ["conversation", "work-product", "canonical", "unknown"]

COORDINATOR_REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "items", "notChecked", "contradictions", "advisoryNote"],
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["subject", "status", "stateClass", "source", "freshness"],
            "properties": {
                "subject": {"type": "string"},
                "status": {"enum": COORDINATOR_STATUS_TERMS},
                "stateClass": {"enum": COORDINATOR_STATE_CLASSES},
                "source": {"type": "string"},
                "freshness": {"type": "string"},
                "note": {"type": "string"},
            },
        }},
        "notChecked": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["subject", "reason"],
            "properties": {"subject": {"type": "string"}, "reason": {"type": "string"}},
        }},
        "contradictions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["subject", "sources", "detail"],
            "properties": {
                "subject": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "detail": {"type": "string"},
            },
        }},
        "advisoryNote": {"type": "string"},
    },
}

COORDINATOR_ADVISORY_NOTE = (
    "Advisory coordination analysis by the DC13 Delivery Coordinator capability. Visibility confers no "
    "authority: this report approves, adopts, certifies, and gates nothing, and it "
    "is never verification evidence. Only present-worker verify produces PASS."
)

ADVERSARIAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["attacksAttempted", "breaches", "coverageNote"],
    "properties": {
        "attacksAttempted": {"type": "integer", "minimum": 0},
        "breaches": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["summary", "severity"],
            "properties": {
                "summary": {"type": "string"},
                "severity": {"enum": ["critical", "high", "medium", "low"]},
                "reproduction": {"type": "string"},
            },
        }},
        "coverageNote": {"type": "string"},
    },
}


def installed() -> bool:
    return common.which(BINARY) is not None


def authenticated() -> tuple[bool, dict[str, Any]]:
    """Report authentication, and whether the probe could answer at all.

    Without a token file, `claude auth status` reads the macOS login keychain.
    A BatchMode SSH session has no unlocked login keychain, so the probe fails
    there while succeeding in a desktop session -- the same worker answers
    differently depending on how it was invoked. "The probe could not
    determine this" is therefore a distinct state from "not authenticated",
    and the caller must be able to tell them apart rather than reading a probe
    failure as a definitive negative.

    With a token file, the probe answers identically over every transport, and
    `meta.authMethod` reports `oauth_token`. Like Codex's auth.json, presence
    is what the probe establishes; a revoked or expired token still probes as
    authenticated and surfaces as PROVIDER_ERROR at invocation time.
    """
    if not installed():
        return False, {"probe": "ok"}
    env, defect = _credential_env()
    if defect:
        return False, {"probe": "failed", "probeError": defect}
    try:
        result = common.run([BINARY, "auth", "status"], timeout=15, env=env)
    except Exception as exc:  # noqa: BLE001
        return False, {"probe": "failed", "probeError": str(exc)[:300]}
    if result.returncode != 0:
        return False, {"probe": "failed", "probeError": result.stderr.strip()[:300]}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, {"probe": "failed", "probeError": "unparseable auth status output"}
    return bool(data.get("loggedIn")), {
        "probe": "ok", "authMethod": data.get("authMethod"), "apiProvider": data.get("apiProvider"),
    }


def status() -> dict[str, Any]:
    if not installed():
        return {"installed": False, "authenticated": False, "ready": False,
                "live_implemented": True, "probe": "ok"}
    auth, meta = authenticated()
    probe = meta.pop("probe", "ok")
    probe_error = meta.pop("probeError", None)
    result = {"installed": True, "authenticated": auth, "live_implemented": True,
              "ready": auth, "probe": probe}
    if probe_error:
        result["probeError"] = probe_error
    if probe == "failed":
        result["note"] = ("credential probe could not determine authentication; not ready, but this is "
                          "not evidence of a missing credential. Claude Code reads the macOS login "
                          "keychain, which a non-interactive SSH session cannot unlock.")
    meta = {key: value for key, value in meta.items() if value is not None}
    if meta:
        result["meta"] = meta
    return result


def _credential_refusal(meta: dict[str, Any]) -> dict[str, Any]:
    """Absence of a credential and inability to check are different answers."""
    if meta.get("probe") == "failed":
        return common.credential_probe_failed(BINARY, meta.get("probeError", ""))
    return common.credential_missing(BINARY)


def _model_env() -> tuple[dict[str, str] | None, str | None]:
    """The environment a model call runs in, independent of its caller.

    Two leaks are closed here. Variables like `CLAUDE_CODE_SESSION_ID`,
    `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_EFFORT` couple the call to
    whatever Claude Code session happened to launch the worker -- and
    `CLAUDE_EFFORT` can contradict the effort this adapter passes explicitly.
    A deterministic worker must not behave differently depending on who
    started it. The credential variable is the sole exception: it is how the
    call authenticates.

    The output-token budget is then declared rather than inherited. Reports run
    10-24k output tokens; a worker invoked from a plain SSH shell would carry
    no such variable at all, and a truncated structured output fails the schema
    rather than degrading visibly.
    """
    env, defect = _credential_env()
    if defect:
        return None, defect
    base = dict(env) if env else common.execution_env()
    scrubbed = {key: value for key, value in base.items()
                if key in SESSION_ENV_KEEP or not key.startswith(SESSION_ENV_PREFIXES)}
    scrubbed[OUTPUT_TOKEN_ENV] = MODEL_OUTPUT_TOKEN_BUDGET
    return scrubbed, None


def _bounded(value: str) -> str:
    return value if len(value) <= MAX_PROMPT_CHARS else value[:MAX_PROMPT_CHARS] + "\n[INPUT TRUNCATED BY WORKER]"


def _terminal_result(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Locate the terminal result object in a `claude -p --output-format json` reply.

    The CLI answers with either a single result object or an event array in
    which rate-limit notices, `thinking_tokens` events and assistant turns
    precede the terminal `type: "result"` element. Both shapes are accepted.
    Anything else is reported as a provider defect rather than raised: an
    upstream shape change must degrade to PROVIDER_ERROR, never take the worker
    down, which is how an array reply crashed coordination-report on 2026-08-17.
    """
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"Claude returned invalid JSON: {exc}"
    if isinstance(outer, list):
        events = [e for e in outer if isinstance(e, dict) and e.get("type") == "result"]
        if not events:
            return None, "Claude returned an event array with no terminal result event"
        outer = events[-1]
    if not isinstance(outer, dict):
        return None, f"Claude returned {type(outer).__name__}; expected an object or event array"
    return outer, None


def _failure_reason(outer: dict[str, Any]) -> str | None:
    """Why the CLI stopped, read from its own terminal result event.

    The event states the outcome in `subtype` and `is_error`. Reading them
    beats reporting the tail of stdout, which is what happened before: an
    `error_max_turns` reached the operator as
    `"cacheCreationInputTokens":0,"webSearchRequests":0,...` and took three
    experiments to identify on 2026-08-17.
    """
    subtype = outer.get("subtype")
    if isinstance(subtype, str) and subtype != "success":
        detail = str(outer.get("result") or "").strip()
        hint = TURN_LIMIT_HINT if subtype == "error_max_turns" else ""
        return f"Claude ended with {subtype}{hint}" + (f": {detail[:500]}" if detail else "")
    if outer.get("is_error"):
        return f"Claude reported an error: {str(outer.get('result') or '')[:800]}"
    return None


def _stream_events(command: list[str], *, timeout: int, idle_timeout: int,
                   env: dict[str, str] | None,
                   progress: Callable[[dict[str, Any]], None] | None
                   ) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Run the CLI in streaming mode under a deadline and a silence limit.

    Streaming makes liveness observable, which is what lets the total budget be
    generous without becoming dangerous: a run still emitting events is never
    killed merely for taking a while, and a genuinely stalled one is stopped
    without waiting out the whole budget. Before this, both looked identical
    from outside and a fixed total budget had to guess between them.

    A reader thread feeds a queue because a blocking read cannot be interrupted
    to check for silence. The child is started in its own process group so a
    stalled run is killed whole, never left orphaned.
    """
    events: list[dict[str, Any]] = []
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env, start_new_session=True)
    except OSError as exc:
        return [], None, f"could not start {BINARY}: {exc}"
    if progress is not None:
        try:
            progress({
                "type": "system",
                "subtype": "process_started",
                "pid": proc.pid,
                "pgid": os.getpgid(proc.pid),
            })
        except Exception:  # noqa: BLE001 -- progress is cosmetic, never fatal
            pass

    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        except Exception:  # noqa: BLE001 -- reader death must not hang the caller
            pass
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    last_event = time.monotonic()
    defect: str | None = None
    while True:
        now = time.monotonic()
        if now >= deadline:
            defect = f"Claude did not finish within {timeout}s"
            break
        try:
            line = lines.get(timeout=min(1.0, max(deadline - now, 0.05)))
        except queue.Empty:
            if time.monotonic() - last_event > idle_timeout:
                defect = (f"Claude produced no output for {idle_timeout}s "
                          f"(stalled, not merely slow)")
                break
            continue
        if line is None:
            break
        last_event = time.monotonic()
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
            if progress is not None:
                try:
                    progress(event)
                except Exception:  # noqa: BLE001 -- progress is cosmetic, never fatal
                    pass

    if defect:
        _terminate(proc)
        return events, None, defect
    try:
        returncode = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        returncode = None
    return events, returncode, None


def _terminate(proc: subprocess.Popen) -> None:
    """Stop the child and its group; never leave a model call orphaned."""
    for signaller in (os.killpg, os.kill):
        try:
            signaller(os.getpgid(proc.pid) if signaller is os.killpg else proc.pid, 15)
            proc.wait(timeout=5)
            return
        except Exception:  # noqa: BLE001
            continue
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _is_retryable(outer: dict[str, Any]) -> bool:
    """Whether the CLI's own stop reason is worth another attempt.

    `error_max_structured_output_retries` means the model could not shape its
    answer to the schema before exhausting the CLI's internal retries. That is
    a per-attempt outcome, not a statement about the request: the same prompt
    frequently succeeds next time. `error_max_turns` behaves the same way.
    Nothing else is retried -- a credential defect or an unparseable reply
    would fail identically however many times it is repeated.
    """
    return outer.get("subtype") in {"error_max_structured_output_retries", "error_max_turns"}


def _invoke(prompt: str, schema: dict[str, Any], *, timeout: int = 600,
            model: str | None = None, effort: str | None = None,
            progress: Callable[[dict[str, Any]], None] | None = None
            ) -> tuple[dict[str, Any] | None, str | None]:
    """Invoke the model, retrying a schema-conformance failure within budget.

    The whole timeout is a deadline shared across attempts, never granted
    afresh to each, so a retry can never push a caller past the budget it
    set. A second attempt is only started when enough of that deadline
    remains for it to plausibly finish.
    """
    deadline = time.monotonic() + timeout
    failure: str | None = None
    for attempt in range(MAX_INVOKE_ATTEMPTS):
        remaining = int(deadline - time.monotonic())
        if attempt and remaining < MIN_RETRY_SECONDS:
            return None, (f"{failure} (not retried: only {max(remaining, 0)}s of the "
                          f"{timeout}s budget remained)")
        output, failure, retryable = _invoke_once(
            prompt, schema, timeout=max(remaining, 1), model=model, effort=effort,
            progress=progress)
        if output is not None:
            return output, None
        if not retryable:
            return None, failure
    return None, f"{failure} (after {MAX_INVOKE_ATTEMPTS} attempts)"


def _invoke_once(prompt: str, schema: dict[str, Any], *, timeout: int,
                 model: str | None, effort: str | None,
                 idle_timeout: int = IDLE_TIMEOUT_SECONDS,
                 progress: Callable[[dict[str, Any]], None] | None = None
                 ) -> tuple[dict[str, Any] | None, str | None, bool]:
    command = [
        BINARY, "-p", "--output-format", "stream-json", "--verbose", "--json-schema",
        json.dumps(schema, separators=(",", ":")), "--max-turns", str(MAX_TURNS), "--tools", "",
        "--no-session-persistence",
        # The worker classifies a filesystem snapshot; it has no business
        # loading the operator's CLAUDE.md, plugins, hooks or MCP servers to do
        # it. Measured 2026-08-17: without this, every run initialised the
        # `magic`, Google Drive, Gmail and Calendar MCP servers. A deterministic
        # worker must not depend on whatever the operator happens to have
        # configured, and must not reach the operator's accounts.
        "--safe-mode",
        # `--safe-mode` does NOT strip settings.json: measured 2026-08-17, a
        # safe-mode run still reported the operator's `"model": "opus"`. That
        # file also carries `advisorModel`, which has no CLI override and is
        # what kept invoking a server-side advisor tool mid-run at 82-110s a
        # time. Loading no setting sources leaves the call governed only by the
        # flags below. Advisor invocations: 1/4 with settings, 0/12 without.
        "--setting-sources", "",
    ]
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    command.append(_bounded(NO_CONSULTATION_DIRECTIVE + prompt))
    env, defect = _model_env()
    if defect:
        return None, defect, False
    events, returncode, stream_defect = _stream_events(
        command, timeout=timeout, idle_timeout=idle_timeout, env=env, progress=progress)
    if stream_defect:
        return None, stream_defect, False
    # The terminal result event carries the outcome, including why a failing
    # run stopped -- worth far more than the raw stream it replaced.
    results = [e for e in events if e.get("type") == "result"]
    if not results:
        kinds = ", ".join(sorted({str(e.get("type")) for e in events})) or "none"
        return None, (f"Claude produced no terminal result event "
                      f"(exit {returncode}; event types seen: {kinds})"), False
    outer = results[-1]
    failure = _failure_reason(outer)
    if failure:
        return None, failure, _is_retryable(outer)
    if returncode not in (0, None):
        return None, f"Claude exited {returncode} after reporting success", False
    structured = outer.get("structured_output") or outer.get("structuredOutput")
    if not isinstance(structured, dict):
        return None, "Claude completed without validated structured_output", False
    return structured, None, False


def plan(task_id: str, mode: str, requirement: str) -> dict[str, Any]:
    mocked = mode == "mock"
    envelope = common.canonical_envelope(task_id, "present-worker plan (claude)", mocked=mocked)
    if mocked:
        return {**envelope, "summary": "synthetic plan; no model was invoked", "steps": [
            {"id": "s1", "description": "synthetic mock step", "dependsOn": []}
        ], "openQuestions": []}
    auth, meta = authenticated()
    if not auth:
        return _credential_refusal(meta)
    output, error = _invoke(
        "Create a concrete implementation plan for the requirement below. Do not claim that any work is verified.\n\n"
        f"REQUIREMENT\n{requirement}", PLAN_SCHEMA,
    )
    if error:
        return {"status": "PROVIDER_ERROR", "provider": "claude", "reason": error, "retryable": True}
    return {**envelope, **output}


def review(task_id: str, mode: str, requirement: str, plan: str, commit: str, diff: str, test_output: str,
           *, input_context: str = "") -> dict[str, Any]:
    mocked = mode == "mock"
    envelope = common.canonical_envelope(task_id, "present-worker review (claude)", mocked=mocked, commit=commit)
    if mocked:
        return {**envelope, "verdict": "ACCEPTED", "findings": [
            {"severity": "note", "summary": "mock review; no model was invoked"}
        ]}
    auth, meta = authenticated()
    if not auth:
        return _credential_refusal(meta)
    output, error = _invoke(
        "Review the candidate diff against the requirement and plan. This is advisory only; never claim verification or PASS.\n\n"
        f"REQUIREMENT\n{requirement}\n\nPLAN\n{plan}\n\nCOMMIT\n{commit}\n\n"
        f"WORKFLOW INPUT CONTEXT\n{input_context}\n\nDIFF\n{diff}\n\nTEST OUTPUT\n{test_output}",
        REVIEW_SCHEMA,
    )
    if error:
        return {"status": "PROVIDER_ERROR", "provider": "claude", "reason": error, "retryable": True}
    return {**envelope, **output}


def _survey_shortfall(report: dict[str, Any], snapshot: dict[str, Any]) -> str | None:
    """Whether a structurally valid report nonetheless failed to survey.

    Judged against the snapshot the run was actually given, never a fixed
    expectation, so a genuinely narrow question that warrants four items is
    not penalised while a one-item answer to a 48-task snapshot is caught.
    Returns the reason, or None when the report covered its ground.
    """
    collections = snapshot.get("snapshotCollections")
    if not isinstance(collections, list) or not collections:
        return None                      # nothing to measure against; invent no bar
    collections = [c for c in collections if isinstance(c, dict)]
    records = sum(c.get("returned", 0) or 0 for c in collections)
    if records < COORDINATOR_SURVEY_MIN_RECORDS:
        return None                      # a small snapshot may honestly yield few items
    items = report.get("items") if isinstance(report.get("items"), list) else []
    if len(items) < COORDINATOR_SURVEY_MIN_ITEMS:
        return (f"{len(items)} item(s) reported for a snapshot carrying {records} records "
                f"across {len(collections)} collections")
    truncated = [str(c.get("name")) for c in collections if (c.get("omittedFromSnapshot") or 0) > 0]
    not_checked = report.get("notChecked") if isinstance(report.get("notChecked"), list) else []
    if truncated and not not_checked:
        return (f"nothing recorded as unchecked although {', '.join(truncated)} "
                f"{'were' if len(truncated) > 1 else 'was'} truncated")
    return None


def coordinator_report(task_id: str, mode: str, requirement: str,
               state_snapshot: dict[str, Any], pack_identity: dict[str, Any],
               timeout: int = COORDINATOR_TIMEOUT_SECONDS,
               progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """DC13 Delivery Coordinator status report: retrieve -> classify -> reconcile -> report.

    The worker gathers the state deterministically and hands it over as data;
    the model's job is classification and reconciliation of that snapshot,
    never recall. Anything not present in the snapshot is UNKNOWN /
    UNVERIFIED, with a notChecked entry -- per the role canon, a source the
    role cannot reach is never reported as checked. `pack_identity` is
    worker-established from the pack manifest and recorded in the envelope,
    not requested from the model.
    """
    mocked = mode == "mock"
    envelope = common.canonical_envelope(task_id, "present-worker coordination-report (claude)", mocked=mocked)
    envelope["packIdentity"] = pack_identity
    if mocked:
        return {**envelope, "summary": "mock chief-of-staff report; no model was invoked and no state was retrieved",
                "items": [], "notChecked": [
                    {"subject": "all workstreams", "reason": "mock run; nothing was retrieved"}
                ], "contradictions": [], "advisoryNote": COORDINATOR_ADVISORY_NOTE}
    if pack_identity.get("probe") != "ok":
        # An honest advisory failure report, not a protocol error and not
        # guessed identity. Per the authority's ruling this branch requires no
        # model call at all: with pack identity unestablished there is nothing
        # to classify, so state that and name the sources left unchecked.
        return {**envelope,
                "summary": ("The requested state report is unavailable because pack identity "
                            "could not be established."),
                "items": [],
                "notChecked": [{"subject": "all pack-scoped state sources",
                                "reason": "pack identity probe failed before snapshot classification"}],
                "contradictions": [],
                "advisoryNote": COORDINATOR_ADVISORY_NOTE}
    auth, meta = authenticated()
    if not auth:
        return _credential_refusal(meta)
    prompt = (
        "You are acting as DC13 Delivery Coordinator, answering the operator question "
        "'Where are we?' for the Closed-Claw pipeline. Binding rules from the domain capability canon:\n"
        "1. Current-state retrieval only: classify and reconcile the STATE snapshot below. Never report "
        "from memory or inference; the snapshot is the entire retrievable surface of this run.\n"
        "2. Anything material that the snapshot does not establish is status 'UNKNOWN / UNVERIFIED' and "
        "gets a notChecked entry with the reason. Never say 'checked' about an unreached source.\n"
        "3. Keep the three-state distinction: conversation-state, work-product, and canonical/execution "
        "state are never collapsed into 'done'. Classify each item's stateClass accordingly; use "
        "'unknown' when the snapshot cannot establish the class.\n"
        "4. Contradictions between sources are reported as contradictions, never silently merged.\n"
        "5. State freshness per item from the snapshot's timestamps; a precise report of stale "
        "information is still a stale report, so say when the ground truth is older than the run.\n"
        "6. Visibility confers no authority: you approve, adopt, certify, and gate nothing. The report "
        "is advisory coordination analysis, never verification and never PASS.\n"
        "7. Know what each STATE field is derived from, so a difference in source is not reported as a "
        "disagreement between sources. Per task, `artifacts` is a filesystem listing of written artifact "
        "files, while `lastStageRecords` is the append-only stage log reduced to the last record per "
        "stage: a stage present in one and absent from the other is an expected consequence of two "
        "derivations, not a contradiction. `verifyResults` is a separate collection with its own bound, "
        "so it legitimately contains task ids absent from the truncated `tasks` view. `advisoryTasks` "
        "are tasks the chat minted to ask a question -- by-product of asking, never pipeline work. A "
        "real contradiction is two sources asserting incompatible things about the same subject.\n"
        "8. `distributedWorkflows` is broker-state-derived canonical execution state for distributed "
        "workflow DAGs. Treat `acceptanceDecision`, `replay`, `nodeStatusCounts`, "
        "`dispatchStatusCounts`, `artifactStatusCounts`, `brokerArtifacts`, `quarantinedWorkers`, "
        "`recentOperatorAudit`, and `cancelledNodes` as first-class execution signals. Report failed, "
        "inconclusive, cancelled, recovered, quarantined, missing-artifact, or redaction-risk states "
        "as findings. `brokerStateIssues` and `distributedWorkflowsOmittedFromSnapshot` are notChecked "
        "material: name what was unavailable or truncated. Do not infer acceptance from completion; "
        "only an acceptance manifest decision can establish acceptance, and even then this report "
        "remains advisory.\n"
        "9. Commit fields ending `Short` are 12-character abbreviations, never full identities. "
        "`outputCommitShort` on a fix is the commit that stage produced; `commitShort` on a verify "
        "result is the HEAD of the tree that verification actually ran against. They are comparable "
        "only within one repository, and `verificationTarget` says which: `worker` means the worker "
        "repo, `present-repository` means the Present corpus. Differing commits across different "
        "targets are two repositories, never a disagreement. Binding a PASS to the commit under test "
        "is what establishes that a result covers the work; where a stage records no commit, say the "
        "binding is unestablished rather than assuming either way.\n"
        "10. Report by finding, not by record. Records that share a state and a cause are ONE item "
        "naming the group and its count ('ten build tasks DEV-000057-068 share commit 775623c23d07, "
        "all SUCCEEDED, none verified'), never one item each. Enumerating near-identical records "
        "crowds out the findings that differ, which are the ones worth reading. State counts "
        "precisely so nothing is hidden by grouping.\n"
        "11. Grouping reduces repetition, never coverage. EVERY collection in STATE must be "
        "represented in the report, and every truncated collection must appear in notChecked with "
        "what was omitted. A handful of items for a broad question is under-reporting, not brevity: "
        "it is a failure of the survey and worse than an over-long list. A broad question over this "
        "snapshot warrants roughly a dozen substantive items. An empty notChecked is almost always "
        "wrong -- the snapshot is a bounded view and what it cannot establish must be said, which "
        "is the difference between a report and a claim of completeness you cannot support.\n\n"
        f"PACK IDENTITY (worker-established)\n{json.dumps(pack_identity, sort_keys=True)}\n\n"
        f"REQUIREMENT (the status question)\n{requirement}\n\n"
        f"STATE (deterministically gathered by the worker)\n{json.dumps(state_snapshot, indent=1, sort_keys=True)}")

    # A thin report is the worst failure here because it passes as success --
    # no error, valid schema, plausible prose, but the survey never happened.
    # Re-ask rather than enforce a floor: a minimum item count in the schema
    # would make the model produce items to satisfy it, and manufacturing
    # findings to reach a number is the fabrication the role canon forbids.
    # A thin report is a bug; a padded one is a lie.
    deadline = time.monotonic() + timeout
    shortfall: str | None = None
    for attempt in range(COORDINATOR_SURVEY_ATTEMPTS):
        remaining = int(deadline - time.monotonic())
        if attempt and remaining < COORDINATOR_MIN_RESURVEY_SECONDS:
            break
        output, error = _invoke(prompt, COORDINATOR_REPORT_SCHEMA, timeout=max(remaining, 1),
                                model=COORDINATOR_MODEL, effort=COORDINATOR_EFFORT, progress=progress)
        if error:
            return {"status": "PROVIDER_ERROR", "provider": "claude", "reason": error, "retryable": True}
        shortfall = _survey_shortfall(output, state_snapshot)
        if not shortfall:
            break

    result = {**envelope, **output, "advisoryNote": COORDINATOR_ADVISORY_NOTE}
    if shortfall:
        # Still thin after re-asking. Say so in the report itself rather than
        # letting it read as a complete survey: an unreliable answer that
        # announces its own limits is usable, one that hides them is not.
        # Worker-authored, like advisoryNote -- never model prose.
        existing = output.get("notChecked") if isinstance(output.get("notChecked"), list) else []
        result["notChecked"] = [*existing, {
            "subject": "the coverage of this report itself",
            "reason": (f"UNDER-SURVEYED: {shortfall}. The question was put to the model "
                       f"{COORDINATOR_SURVEY_ATTEMPTS} times and the survey remained thin, so this report "
                       f"is not a complete account of the snapshot and its silence on any subject "
                       f"establishes nothing."),
        }]
    return result


def adversarial(task_id: str, mode: str, requirement: str, plan: str, commit: str, diff: str,
                *, input_context: str = "") -> dict[str, Any]:
    mocked = mode == "mock"
    envelope = common.canonical_envelope(task_id, "present-worker adversarial (claude)", mocked=mocked, commit=commit)
    if mocked:
        return {**envelope, "attacksAttempted": 1, "breaches": [],
                "coverageNote": "mock adversarial analysis; absence of breaches is not evidence of correctness"}
    auth, meta = authenticated()
    if not auth:
        return _credential_refusal(meta)
    output, error = _invoke(
        "Adversarially analyze the candidate for malformed input, boundary, security, concurrency, and recovery failures. "
        "Report only reproducible candidate breaches. Finding none is not verification.\n\n"
        f"REQUIREMENT\n{requirement}\n\nPLAN\n{plan}\n\nCOMMIT\n{commit}\n\n"
        f"WORKFLOW INPUT CONTEXT\n{input_context}\n\nDIFF\n{diff}",
        ADVERSARIAL_SCHEMA,
    )
    if error:
        return {"status": "PROVIDER_ERROR", "provider": "claude", "reason": error, "retryable": True}
    return {**envelope, **output}
