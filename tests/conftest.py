"""Shared fixtures for durable-core integration and failure-injection tests."""
from __future__ import annotations

import collections
import dataclasses
import http.server
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

import pytest


REPO_DIR = Path(__file__).resolve().parent.parent


@dataclasses.dataclass
class ManagedProcess:
    """A subprocess whose output and process group are owned by one test."""

    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    _stdout_stream: BinaryIO
    _stderr_stream: BinaryIO

    def wait(self, timeout: float = 30) -> int:
        return self.process.wait(timeout=timeout)

    def stdout(self) -> str:
        self._stdout_stream.flush()
        return self.stdout_path.read_text()

    def stderr(self) -> str:
        self._stderr_stream.flush()
        return self.stderr_path.read_text()

    def stop(self, timeout: float = 5) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=timeout)
        self._stdout_stream.close()
        self._stderr_stream.close()


class ProcessManager:
    """Starts subprocesses in isolated groups and reaps all of them at teardown."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True)
        self._processes: list[ManagedProcess] = []

    def start(
        self,
        command: Sequence[str],
        *,
        name: str,
        cwd: Path = REPO_DIR,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess:
        sequence = len(self._processes) + 1
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-") or "process"
        stdout_path = self.root / f"{sequence:02d}-{safe_name}.stdout"
        stderr_path = self.root / f"{sequence:02d}-{safe_name}.stderr"
        stdout_stream = stdout_path.open("wb")
        stderr_stream = stderr_path.open("wb")
        environment = os.environ.copy()
        if env:
            environment.update(env)
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
            )
        except BaseException:
            stdout_stream.close()
            stderr_stream.close()
            raise
        managed = ManagedProcess(
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            _stdout_stream=stdout_stream,
            _stderr_stream=stderr_stream,
        )
        self._processes.append(managed)
        return managed

    def close(self) -> None:
        for process in reversed(self._processes):
            process.stop()


@dataclasses.dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclasses.dataclass(frozen=True)
class ScriptedResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class FakeConnector:
    """A scriptable loopback endpoint that records connector requests in order."""

    def __init__(self):
        self._condition = threading.Condition()
        self._requests: list[RecordedRequest] = []
        self._responses: collections.deque[ScriptedResponse] = collections.deque()
        connector = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle_request(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                request = RecordedRequest(
                    method=self.command,
                    path=self.path,
                    headers={key: value for key, value in self.headers.items()},
                    body=body,
                )
                with connector._condition:
                    connector._requests.append(request)
                    response = (
                        connector._responses.popleft()
                        if connector._responses
                        else ScriptedResponse(status=200, headers={}, body=b"")
                    )
                    connector._condition.notify_all()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                if response.body:
                    self.wfile.write(response.body)

            do_DELETE = handle_request
            do_GET = handle_request
            do_PATCH = handle_request
            do_POST = handle_request
            do_PUT = handle_request

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-connector",
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def enqueue(
        self,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        with self._condition:
            self._responses.append(
                ScriptedResponse(status=status, headers=dict(headers or {}), body=body)
            )

    def wait_for_requests(
        self,
        count: int,
        *,
        timeout: float = 10,
    ) -> list[RecordedRequest]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"expected {count} connector requests, got {len(self._requests)}"
                    )
                self._condition.wait(timeout=remaining)
            return list(self._requests)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@dataclasses.dataclass(frozen=True)
class CrashPoint:
    """Environment contract and sentinel used by whole-process crash tests."""

    name: str
    sentinel: Path

    @property
    def environment(self) -> dict[str, str]:
        return {
            "STEEL_MISSION_TEST_CRASH_POINT": self.name,
            "STEEL_MISSION_TEST_CRASH_SENTINEL": str(self.sentinel),
        }

    def wait(self, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sentinel.exists():
                return
            time.sleep(0.01)
        raise AssertionError(f"crash point {self.name!r} was not reached")


@pytest.fixture
def temporary_store(tmp_path: Path) -> Path:
    """Return an absent path reserved for one test's SQLite store."""

    return tmp_path / "steel-mission.sqlite3"


@pytest.fixture
def daemon_process_manager(tmp_path: Path) -> Iterator[ProcessManager]:
    manager = ProcessManager(tmp_path / "daemon-processes")
    yield manager
    manager.close()


@pytest.fixture
def agent_process_manager(tmp_path: Path) -> Iterator[ProcessManager]:
    manager = ProcessManager(tmp_path / "agent-processes")
    yield manager
    manager.close()


@pytest.fixture
def fake_connector() -> Iterator[FakeConnector]:
    connector = FakeConnector()
    yield connector
    connector.close()


@pytest.fixture
def crash_point(tmp_path: Path):
    def create(name: str) -> CrashPoint:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-") or "crash"
        return CrashPoint(name=name, sentinel=tmp_path / f"{safe_name}.reached")

    return create
