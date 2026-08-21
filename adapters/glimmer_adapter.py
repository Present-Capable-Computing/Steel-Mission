"""Adapter for the local default builder role (Glimmer), backed by the
Apple-MLX-enabled Ollama build already present on this machine
(~/codebase/ollama, built from github.com/ollama/ollama with MLX_VERSION /
MLX_C_VERSION vendored -- i.e. Ollama's native Apple Silicon inference
engine). ~/.ollama already has 51 GB of models downloaded, including
qwen2.5-coder:14b, so no new model download is required for this to work.

Design constraints from the spec, honored here:
- endpoint is localhost-only (Ollama's default bind, never overridden);
- explicit start/stop/status, no auto-start at boot;
- load once per session (long keep_alive), never reload per task;
- never claim ready until a real inference smoke test succeeds;
- refuse to start under severe memory pressure rather than thrash swap.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import common

BINARY = "ollama"
LOCAL_OLLAMA = Path.home() / "codebase" / "ollama" / "ollama"
HOST = "127.0.0.1"
PORT = 11434
# A container reaches the host's loopback-only Ollama service through Docker
# Desktop's host gateway. Keep the native localhost default, but allow the
# launch contract to provide that local-machine bridge explicitly.
BASE_URL = os.environ.get("STEEL_MISSION_OLLAMA_BASE_URL", f"http://{HOST}:{PORT}").rstrip("/")
DEFAULT_MODEL = "qwen2.5-coder:14b"
KEEP_ALIVE = "30m"
REWARM_TIMEOUT_SECONDS = 90.0
PID_FILE = common.LOGS_DIR / "glimmer.pid"
SERVE_LOG = common.LOGS_DIR / "glimmer-serve.log"

# A 14B Q4 model needs roughly 9-10 GB resident. Refuse to start below this
# much reclaimable memory rather than forcing swap thrash on a machine that
# is also the user's active workstation.
MIN_FREE_GB_TO_START = 11.0


def model_binding_error(model: str, effort: str | None = None) -> str | None:
    """Validate the Ollama model reference without requiring its server to be running."""
    if not model or any(char.isspace() or ord(char) < 32 for char in model):
        return f"provider 'glimmer' does not recognize model {model!r}"
    if effort is not None:
        return f"provider 'glimmer' does not support reasoning effort {effort!r}"
    return None


def _ollama_path() -> str | None:
    found = common.which(BINARY)
    if found:
        return found
    if LOCAL_OLLAMA.is_file() and os.access(LOCAL_OLLAMA, os.X_OK):
        return str(LOCAL_OLLAMA)
    return None


def installed() -> bool:
    # A client container does not need the Ollama executable when it can reach
    # the host-owned service. Treat that service as the installed transport.
    return _ollama_path() is not None or server_running()


def _http_get(path: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


def _http_post(path: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any] | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


def _generate_streaming_json(payload: dict[str, Any], timeout: float,
                             progress: Callable[[dict[str, Any]], None] | None = None
                             ) -> tuple[dict[str, Any] | None, str | None]:
    data = json.dumps({**payload, "stream": True}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/generate", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    pieces: list[str] = []
    last_progress = 0.0
    try:
        if progress is not None:
            progress({"type": "system", "subtype": "glimmer_request_started"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if not raw.strip():
                    continue
                event = json.loads(raw.decode())
                if not isinstance(event, dict):
                    continue
                chunk = event.get("response")
                if isinstance(chunk, str):
                    pieces.append(chunk)
                now = time.monotonic()
                if progress is not None and now - last_progress >= 1.0:
                    progress({"type": "assistant", "subtype": "glimmer_chunk", "chars": sum(len(p) for p in pieces)})
                    last_progress = now
                if event.get("done"):
                    if progress is not None:
                        usage = {}
                        for source, target in (
                            ("prompt_eval_count", "input_tokens"),
                            ("eval_count", "output_tokens"),
                        ):
                            count = event.get(source)
                            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                                usage[target] = count
                        progress({
                            "type": "result",
                            "subtype": "success",
                            **({"usage": usage} if usage else {}),
                        })
                    break
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
        return None, f"glimmer generation failed: {exc.__class__.__name__}"
    text = "".join(pieces).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"glimmer returned non-JSON structured output: {exc}"
    if not isinstance(parsed, dict):
        return None, f"glimmer returned {type(parsed).__name__}; expected object"
    return parsed, None


def server_running() -> bool:
    return _http_get("/api/tags") is not None


def model_available(model: str = DEFAULT_MODEL) -> bool:
    tags = _http_get("/api/tags")
    if tags is not None:
        names = {m.get("name") for m in tags.get("models", [])}
        return model in names or any(n and n.startswith(model.split(":")[0]) for n in names)

    # Server not up -- `ollama list` also requires the server, so read the
    # local manifest layout directly instead of shelling out.
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    manifest = Path.home() / ".ollama" / "models" / "manifests" / "registry.ollama.ai" / "library" / name / tag
    return manifest.exists()


def model_loaded(model: str = DEFAULT_MODEL) -> bool:
    ps = _http_get("/api/ps")
    if ps is None:
        return False
    return any(m.get("name") == model for m in ps.get("models", []))


def memory_check() -> dict[str, Any]:
    """Reclaimable memory estimate via vm_stat, stdlib-only parsing."""
    try:
        result = common.run(["vm_stat"], timeout=5)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    page_size = 16384
    stats = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().rstrip(".")
        if val.isdigit():
            stats[key.strip()] = int(val)
    if "page size of" in result.stdout:
        try:
            page_size = int(result.stdout.split("page size of")[1].split()[0])
        except (IndexError, ValueError):
            pass
    free_pages = stats.get("Pages free", 0) + stats.get("Pages speculative", 0)
    free_gb = (free_pages * page_size) / (1024**3)
    return {"ok": True, "free_gb": round(free_gb, 2), "min_required_gb": MIN_FREE_GB_TO_START}


def status(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    inst = installed()
    if not inst:
        return {
            "provider": "glimmer",
            "installed": False,
            "model_available": False,
            "service_running": False,
            "ready": False,
        }
    running = server_running()
    avail = model_available(model)
    loaded = model_loaded(model) if running else False
    return {
        "provider": "glimmer",
        "installed": True,
        "model": model,
        "model_available": avail,
        "service_running": running,
        "model_loaded": loaded,
        "ready": running and avail and loaded,
    }


def rewarm(model: str = DEFAULT_MODEL, *,
           progress: Callable[[dict[str, Any]], None] | None = None,
           timeout: float = REWARM_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Reload an installed model into an already-running Ollama server."""
    st = status(model)
    if st["ready"]:
        return {"status": "READY", "provider": "glimmer", "model": model, "rewarmed": False}
    if not st.get("service_running"):
        return common.glimmer_not_ready(
            "Ollama server is not running; refusing to cold-start it for a chat. "
            "Run the explicit Glimmer start command first."
        )
    if not st.get("model_available"):
        return common.glimmer_not_ready(
            f"model {model!r} is not present in the local Ollama library "
            "(WAITING_FOR_MODEL -- not substituting another model silently)"
        )

    if progress is not None:
        progress({"type": "system", "subtype": "glimmer_rewarm_started", "model": model})
    loaded = _http_post(
        "/api/generate",
        {"model": model, "prompt": "", "stream": False, "keep_alive": KEEP_ALIVE},
        timeout=max(0.1, min(float(timeout), REWARM_TIMEOUT_SECONDS)),
    )
    if loaded is None or not model_loaded(model):
        reason = "Glimmer model re-warm failed: Ollama did not confirm the model load"
        if progress is not None:
            progress({
                "type": "system",
                "subtype": "glimmer_rewarm_failed",
                "model": model,
                "reason": reason,
            })
        return common.glimmer_not_ready(reason)
    if progress is not None:
        progress({"type": "system", "subtype": "glimmer_rewarm_completed", "model": model})
    return {
        "status": "READY",
        "provider": "glimmer",
        "model": model,
        "rewarmed": True,
        "keep_alive": KEEP_ALIVE,
    }


def start(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    if not installed():
        return common.glimmer_not_ready("ollama binary not found on PATH")

    if server_running():
        pass  # already up, fall through to warm/verify
    else:
        mem = memory_check()
        if mem.get("ok") and mem["free_gb"] < MIN_FREE_GB_TO_START:
            return common.glimmer_not_ready(
                f"insufficient free memory to start Glimmer safely: "
                f"{mem['free_gb']} GB free, need >= {mem['min_required_gb']} GB"
            )
        common.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with SERVE_LOG.open("a") as log_f:
            env = common.execution_env()
            env["OLLAMA_HOST"] = f"{HOST}:{PORT}"
            proc = subprocess.Popen(
                [_ollama_path() or BINARY, "serve"],
                stdout=log_f,
                stderr=log_f,
                env=env,
                start_new_session=True,
            )
        PID_FILE.write_text(str(proc.pid))
        for _ in range(30):
            if server_running():
                break
            time.sleep(0.5)
        else:
            return common.glimmer_not_ready("ollama serve did not become reachable within 15s")

    if not model_available(model):
        return common.glimmer_not_ready(
            f"model '{model}' is not present in the local Ollama library "
            f"(WAITING_FOR_MODEL -- not substituting another model silently)"
        )

    smoke = _http_post(
        "/api/generate",
        {
            "model": model,
            "prompt": "Reply with exactly one word: ready",
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": 8},
        },
        timeout=90.0,
    )
    if not smoke or "response" not in smoke:
        return common.glimmer_not_ready("smoke-test inference request failed or returned no response")

    return {
        "status": "READY",
        "provider": "glimmer",
        "model": model,
        "smoke_test_response": smoke["response"].strip()[:80],
        "keep_alive": KEEP_ALIVE,
    }


def stop(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    unloaded = False
    if server_running():
        _http_post("/api/generate", {"model": model, "prompt": "", "keep_alive": 0}, timeout=15.0)
        unloaded = not model_loaded(model)

    killed = False
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            killed = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        PID_FILE.unlink(missing_ok=True)

    return {"status": "STOPPED", "provider": "glimmer", "model_unloaded": unloaded, "process_killed": killed}


def build(task_id: str, mode: str, prompt: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    if mode == "mock":
        return {
            "status": "MOCK",
            "provider": "glimmer",
            "task_id": task_id,
            "model": model,
            "response": "mock build output -- no real model was invoked",
            "production_pass": False,
        }

    st = status(model)
    if not st["ready"]:
        warmed = rewarm(model)
        if warmed.get("status") != "READY":
            return warmed

    result = _http_post(
        "/api/generate",
        {"model": model, "prompt": prompt, "stream": False, "keep_alive": KEEP_ALIVE},
        timeout=300.0,
    )
    if not result or "response" not in result:
        return common.glimmer_not_ready("build inference request failed")
    return {
        "status": "OK",
        "provider": "glimmer",
        "task_id": task_id,
        "model": model,
        "response": result["response"],
    }


def coordinator_report(task_id: str, mode: str, requirement: str,
               state_snapshot: dict[str, Any], pack_identity: dict[str, Any],
               timeout: int = 300,
               progress: Callable[[dict[str, Any]], None] | None = None,
               model: str = DEFAULT_MODEL) -> dict[str, Any]:
    from . import claude_adapter  # Reuse the canonical DC13 vocabulary/schema.

    envelope = common.canonical_envelope(task_id, "present-worker coordination-report (glimmer)", mocked=mode == "mock")
    envelope["packIdentity"] = pack_identity
    if mode == "mock":
        return {**envelope, "summary": "mock chief-of-staff report; no model was invoked and no state was retrieved",
                "items": [], "notChecked": [
                    {"subject": "all workstreams", "reason": "mock run; nothing was retrieved"}
                ], "contradictions": [], "advisoryNote": claude_adapter.COORDINATOR_ADVISORY_NOTE}
    if pack_identity.get("probe") != "ok":
        return {**envelope,
                "summary": "The requested state report is unavailable because pack identity could not be established.",
                "items": [],
                "notChecked": [{"subject": "all pack-scoped state sources",
                                "reason": "pack identity probe failed before snapshot classification"}],
                "contradictions": [],
                "advisoryNote": claude_adapter.COORDINATOR_ADVISORY_NOTE}
    st = status(model)
    if not st["ready"]:
        caller_timeout = float(timeout)
        started = time.monotonic()
        warmed = rewarm(model, progress=progress, timeout=min(caller_timeout, REWARM_TIMEOUT_SECONDS))
        if warmed.get("status") != "READY":
            return warmed
        remaining = caller_timeout - (time.monotonic() - started)
        if remaining <= 0:
            reason = (
                f"Glimmer re-warm exhausted the {caller_timeout:g}s caller timeout "
                "before advisory generation"
            )
            if progress is not None:
                progress({
                    "type": "system",
                    "subtype": "glimmer_request_budget_exhausted",
                    "model": model,
                    "reason": reason,
                })
            return {
                "status": "PROVIDER_ERROR",
                "provider": "glimmer",
                "reason": reason,
                "retryable": True,
            }
        timeout = remaining

    prompt = (
        "You are DC13 Delivery Coordinator answering 'Where are we?' for this worker-visible snapshot. "
        "Use only the STATE JSON below. Do not use memory, inference outside the data, tools, "
        "or external sources. Anything material not established by STATE must be "
        "UNKNOWN / UNVERIFIED or listed in notChecked. Preserve the distinction between "
        "conversation, work-product, canonical, and unknown state. Report contradictions rather "
        "than merging them. This report is advisory only and never gates, certifies, adopts, "
        "approves, or claims PASS.\n\n"
        f"Allowed status values: {json.dumps(claude_adapter.COORDINATOR_STATUS_TERMS)}\n"
        f"Allowed stateClass values: {json.dumps(claude_adapter.COORDINATOR_STATE_CLASSES)}\n\n"
        f"PACK IDENTITY\n{json.dumps(pack_identity, sort_keys=True)}\n\n"
        f"REQUIREMENT\n{requirement}\n\n"
        f"STATE\n{json.dumps(state_snapshot, indent=1, sort_keys=True)}\n\n"
        "Return only JSON matching the supplied schema."
    )
    output, error = _generate_streaming_json({
        "model": model,
        "prompt": prompt,
        "format": claude_adapter.COORDINATOR_REPORT_SCHEMA,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.1, "num_ctx": 32768, "num_predict": 8192},
    }, timeout=float(timeout), progress=progress)
    if error:
        return {"status": "PROVIDER_ERROR", "provider": "glimmer", "reason": error, "retryable": True}
    return {**envelope, **(output or {}), "advisoryNote": claude_adapter.COORDINATOR_ADVISORY_NOTE}
