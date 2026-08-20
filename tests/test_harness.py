"""Characterization tests for the shared integration-test harness."""
from __future__ import annotations

import os
import subprocess
import sys
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
