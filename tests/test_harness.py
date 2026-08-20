"""Characterization tests for the shared integration-test harness."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def test_temporary_store_is_an_empty_writable_path(temporary_store):
    assert temporary_store.name == "steel-mission.sqlite3"
    assert not temporary_store.exists()

    temporary_store.write_bytes(b"store")

    assert temporary_store.read_bytes() == b"store"


def test_process_managers_capture_output(daemon_process_manager, agent_process_manager):
    daemon = daemon_process_manager.start(
        [sys.executable, "-c", "print('daemon ready', flush=True)"],
        name="daemon",
    )
    agent = agent_process_manager.start(
        [sys.executable, "-c", "print('agent ready', flush=True)"],
        name="agent",
    )

    assert daemon.wait(timeout=10) == 0
    assert agent.wait(timeout=10) == 0
    assert daemon.stdout() == "daemon ready\n"
    assert agent.stdout() == "agent ready\n"


def test_process_manager_kills_descendants_after_the_leader_exits(
    tmp_path,
    daemon_process_manager,
):
    child_pid_path = tmp_path / "child.pid"
    child_ready_path = tmp_path / "child.ready"
    child_script = (
        "import signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_ready_path)!r}).touch(); time.sleep(60)"
    )
    parent_script = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        f"open({str(child_pid_path)!r}, 'w').write(str(child.pid))"
    )
    parent = daemon_process_manager.start(
        [sys.executable, "-c", parent_script],
        name="parent-with-child",
    )

    assert parent.wait(timeout=10) == 0
    deadline = time.monotonic() + 10
    while not child_ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_ready_path.exists()
    child_pid = int(child_pid_path.read_text())

    try:
        daemon_process_manager.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            raise AssertionError(f"descendant process {child_pid} survived teardown")
    finally:
        try:
            os.killpg(parent.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_fake_connector_is_scriptable_and_records_requests(fake_connector):
    fake_connector.enqueue(status=429, headers={"Retry-After": "1"}, body=b"retry")
    fake_connector.enqueue(status=200, body=b"accepted")

    request = urllib.request.Request(
        f"{fake_connector.url}/deliver",
        data=b'{"message":"hello"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code == 429
        assert error.headers["Retry-After"] == "1"
        assert error.read() == b"retry"
    else:
        raise AssertionError("the first scripted response must reject the request")

    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.read() == b"accepted"

    recorded = fake_connector.wait_for_requests(2, timeout=5)
    assert [item.path for item in recorded] == ["/deliver", "/deliver"]
    assert all(item.body == b'{"message":"hello"}' for item in recorded)


def test_crash_point_exposes_an_inert_test_hook_contract(crash_point):
    crash = crash_point("after-side-effect")
    script = (
        "from pathlib import Path; import os; "
        "Path(os.environ['STEEL_MISSION_TEST_CRASH_SENTINEL']).touch()"
    )
    environment = os.environ.copy()
    environment.update(crash.environment)

    subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=True,
        timeout=10,
    )

    crash.wait(timeout=5)
    assert crash.environment == {
        "STEEL_MISSION_TEST_CRASH_POINT": "after-side-effect",
        "STEEL_MISSION_TEST_CRASH_SENTINEL": str(crash.sentinel),
    }
