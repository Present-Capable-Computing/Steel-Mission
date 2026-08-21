import json
import runpy
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from adapters import schema_check
import tooling.mission_pipeline_bench as mission_bench
from tooling.mission_pipeline_bench import (
    AgentDriver,
    BenchError,
    CommandResult,
    DecisionClient,
    GitHubPlatform,
    PipelineBench,
    SubprocessRunner,
    reported_opus_major,
)


REQUIREMENT = "Change the smallest observable thing and keep every existing behaviour green."
ACCEPTANCE = "A focused regression fails first, then the full release check passes."


def grant() -> dict:
    return {
        "schemaVersion": 1,
        "missionId": "ms-0123456789abcdef01234567",
        "repository": "Present-Capable-Computing/Steel-Mission",
        "issueNumber": 999,
        "baseBranch": "main",
        "branch": "ms-0014/rehearsal-small-change",
        "grantedAt": "2026-08-21T08:30:00Z",
        "grantedBy": "andrewHermann",
        "requirement": REQUIREMENT,
        "acceptanceEvidence": ACCEPTANCE,
        "definitionOfDone": {
            "redTest": ["python3", "-m", "pytest", "tests/test_rehearsal.py", "-q"],
            "test": ["make", "test"],
            "releaseCheck": ["make", "release-check"],
        },
        "budgets": {
            stage: {"elapsedSeconds": 900, "turns": 6}
            for stage in ("plan", "develop", "review", "acceptance")
        },
        "abortConditions": ["Any command exceeds its budget", "The granted requirement changes"],
        "maxReviewRounds": 2,
        "machineAccounts": {
            "claude": {"login": "sm-agent-claude", "email": "claude@example.invalid", "tokenEnv": "SM_AGENT_CLAUDE_GITHUB_TOKEN"},
            "codex": {"login": "sm-agent-codex", "email": "codex@example.invalid", "tokenEnv": "SM_AGENT_CODEX_GITHUB_TOKEN"},
            "local": {"login": "sm-agent-qwen", "email": "qwen@example.invalid", "tokenEnv": "SM_AGENT_QWEN_GITHUB_TOKEN"},
        },
        "decisionApi": {"baseUrl": "http://127.0.0.1:8765"},
    }


def issue(*, labels: list[str] | None = None) -> dict:
    return {
        "number": 999,
        "url": "https://github.com/Present-Capable-Computing/Steel-Mission/issues/999",
        "title": "A small rehearsal change",
        "labels": [{"name": label} for label in (labels or ["task", "area:ui"])],
        "body": (
            f"## Requirement\n\n{REQUIREMENT}\n\n"
            f"## Acceptance evidence\n\n{ACCEPTANCE}\n\n"
            "## Surface\n\nnone of the above\n"
        ),
    }


class FakePlatform:
    def __init__(
        self,
        root: Path,
        *,
        issue_payload: dict | None = None,
        gate_failure: bool = False,
        merge_failure: bool = False,
        path_batches: list[list[str]] | None = None,
    ):
        self.root = root
        self.issue_payload = issue_payload or issue()
        self.gate_failure = gate_failure
        self.merge_failure = merge_failure
        self.path_batches = list(path_batches or [])
        self.calls: list[str] = []
        self.commit = 0

    def issue(self, _grant):
        self.calls.append("issue")
        return self.issue_payload

    def validate_machine_accounts(self, _grant):
        self.calls.append("accounts")

    def validate_repository_wall(self, _grant):
        self.calls.append("branch-protection")

    def claim_issue(self, _grant, session_id):
        self.calls.append(f"claim:{session_id}")

    def prepare_worktree(self, _grant, session_dir):
        self.calls.append("worktree")
        path = session_dir / "worktree"
        path.mkdir(parents=True)
        return path

    def run_command(self, argv, _cwd, timeout, extra_env):
        assert all(
            extra_env[account["tokenEnv"]] == ""
            for account in grant()["machineAccounts"].values()
        )
        label = " ".join(argv)
        self.calls.append(f"command:{label}:{timeout}")
        if argv == grant()["definitionOfDone"]["redTest"]:
            return CommandResult(argv, 1, "1 failed", "", 0.1)
        if self.gate_failure and argv == grant()["definitionOfDone"]["test"]:
            return CommandResult(argv, 1, "1 failed", "", 0.1)
        return CommandResult(argv, 0, "366 passed", "", 0.1)

    def assert_machine_commit(self, _grant, _worktree, previous_commit=None):
        self.commit += 1
        value = f"commit-{self.commit}"
        self.calls.append(f"commit:{value}")
        assert value != previous_commit
        return value

    def changed_paths(self, _grant, _worktree):
        self.calls.append("paths")
        if self.path_batches:
            return self.path_batches.pop(0)
        return ["tests/test_rehearsal.py"]

    def push(self, _grant, _worktree):
        self.calls.append("push")

    def create_pr(self, _grant, _worktree, body_path):
        self.calls.append("create-pr")
        assert "failing test" in body_path.read_text().lower()
        return {"number": 321, "url": "https://github.test/pull/321"}

    def update_pr_body(self, _grant, _pr_number, body_path):
        body = body_path.read_text()
        self.calls.append("update-pr-body")
        assert "commit-1" in body
        assert "commit-2" in body
        assert "Codex correction rounds: 1" in body

    def post_codex_review(self, _grant, _pr_number, review):
        self.calls.append(f"codex-review:{review['verdict']}")

    def arm_auto_merge(self, _grant, _pr_number):
        self.calls.append("auto-merge")

    def disable_auto_merge(self, _grant, _pr_number):
        self.calls.append("disable-auto-merge")

    def wait_for_ci(self, _grant, _pr_number, timeout):
        self.calls.append(f"ci:{timeout}")
        return "all checks passed"

    def approve(self, _grant, _pr_number, summary):
        self.calls.append(f"approve:{summary}")

    def wait_for_merge(self, _grant, _pr_number, timeout):
        self.calls.append(f"merged:{timeout}")
        if self.merge_failure:
            raise BenchError("auto-merge did not land within the acceptance budget")
        return {"state": "MERGED", "mergedAt": "2026-08-21T09:00:00Z"}


class FakeAgents:
    def __init__(self, plans=None, reviews=None):
        self.plans = list(plans or [{"clean": True, "summary": "Clean plan", "steps": ["Add the test", "Make it pass"], "touchedPaths": ["tests/test_rehearsal.py"]}])
        self.reviews = list(reviews or [
            {"verdict": "changes-requested", "summary": "One correction", "findings": [{"priority": "P1", "body": "Cover the empty case."}]},
            {"verdict": "clean", "summary": "Correction verified", "findings": []},
        ])
        self.prompts: list[tuple[str, str]] = []

    def plan(self, prompt, _worktree, _budget, _session_dir):
        self.prompts.append(("plan", prompt))
        return self.plans.pop(0)

    def develop(self, prompt, _worktree, _budget, _session_dir):
        self.prompts.append(("develop", prompt))

    def review(self, prompt, _worktree, _budget, _session_dir):
        self.prompts.append(("review", prompt))
        return self.reviews.pop(0)

    def fix(self, prompt, _worktree, _budget, _session_dir):
        self.prompts.append(("fix", prompt))

    def accept(self, prompt, _worktree, _budget, _session_dir):
        self.prompts.append(("acceptance", prompt))
        return {"verdict": "approve", "summary": "Definition of done verified."}


class FakeDecision:
    def __init__(self, answer=None):
        self.calls: list[str] = []
        self.answer = answer or {
            "selectedOptionId": "continue-narrow",
            "freeText": "Stay inside the grant.",
        }

    def request(self, context):
        self.calls.append("request")
        assert "unclean" in context.lower() or "human-owned" in context.lower()
        return {
            "jobId": "JOB-decision",
            "decisionRequest": {
                "id": "decision-1",
                "question": "How should the mission continue?",
                "requestedAt": "2026-08-21T08:40:00Z",
            },
            "url": "http://127.0.0.1:8765/job/JOB-decision",
        }

    def wait_for_answer(self, job_id, timeout):
        self.calls.append(f"wait:{job_id}:{timeout}")
        return self.answer


class ProtectionRunner:
    def __init__(self, protection):
        self.protection = protection

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None):
        assert argv[:2] == ["gh", "api"]
        return CommandResult(argv, 0, json.dumps(self.protection), "", 0.1)


class DiffRunner:
    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None):
        assert argv[:4] == ["git", "diff", "--name-status", "--find-renames"]
        return CommandResult(
            argv,
            0,
            "R100\tschemas/canonical/job-v2.json\tdocs/job-v2.json\nM\ttests/test_job.py\n",
            "",
            0.1,
        )


class AccountRunner:
    def __init__(self, accounts, unverified=()):
        self.accounts = accounts
        self.unverified = set(unverified)

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None):
        login, email = self.accounts[extra_env["GH_TOKEN"]]
        if argv == ["gh", "api", "user", "--jq", ".login"]:
            output = login + "\n"
        else:
            assert argv == ["gh", "api", "user/emails"]
            output = json.dumps([{"email": email, "verified": email not in self.unverified}])
        return CommandResult(argv, 0, output, "", 0.1)


class PushRunner:
    def __init__(self, expected_blank):
        self.expected_blank = expected_blank

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None):
        assert argv[:2] == ["git", "push"]
        assert all(extra_env[name] == "" for name in self.expected_blank)
        assert extra_env["SM_BENCH_PUSH_TOKEN"] == "local-token"
        assert extra_env["GIT_CONFIG_KEY_3"] == "core.hooksPath"
        assert Path(extra_env["GIT_CONFIG_VALUE_3"]).is_dir()
        return CommandResult(argv, 0, "pushed", "", 0.1)


def protected_repository():
    return {
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "require_code_owner_reviews": True,
        },
        "required_conversation_resolution": {"enabled": True},
        "required_status_checks": {"checks": [{"context": "interpreter-checks"}]},
    }


def write_grant(path: Path, value: dict | None = None) -> Path:
    path.write_text(json.dumps(value or grant()))
    return path


def test_pipeline_orders_red_evidence_green_gates_review_correction_and_acceptance(tmp_path):
    platform = FakePlatform(tmp_path)
    agents = FakeAgents()
    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    ).run()

    assert result["state"] == "merged"
    assert result["reviewCorrectionRounds"] == 1
    first_push = platform.calls.index("push")
    assert any(call.startswith("command:make test") for call in platform.calls[:first_push])
    assert any(call.startswith("command:make release-check") for call in platform.calls[:first_push])
    assert platform.calls.index("auto-merge") < next(
        index for index, call in enumerate(platform.calls) if call.startswith("approve:")
    )
    assert "update-pr-body" in platform.calls
    assert all(REQUIREMENT in prompt and ACCEPTANCE in prompt for _, prompt in agents.prompts)

    feed = [json.loads(line) for line in Path(result["feedPath"]).read_text().splitlines()]
    assert feed[-1]["outcome"]["status"] == "succeeded"
    assert all(schema_check.validate(item, "canonical/agent-session-status-v1.json") == [] for item in feed)


def test_each_elapsed_budget_starts_when_its_stage_starts(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(mission_bench.time, "monotonic", lambda: clock[0])

    class SlowPlanAgents(FakeAgents):
        def plan(self, prompt, worktree, budget, session_dir):
            value = super().plan(prompt, worktree, budget, session_dir)
            clock[0] += 4
            return value

    value = grant()
    value["budgets"]["plan"]["elapsedSeconds"] = 5
    for stage in ("develop", "review", "acceptance"):
        value["budgets"][stage]["elapsedSeconds"] = 1

    result = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=FakePlatform(tmp_path),
        agents=SlowPlanAgents(),
        decisions=FakeDecision(),
    ).run()

    assert result["state"] == "merged"


def test_security_review_issue_is_refused_before_claim_or_execution(tmp_path):
    platform = FakePlatform(tmp_path, issue_payload=issue(labels=["task", "security-review"]))
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="security-review"):
        bench.run()

    assert platform.calls == ["issue"]


def test_unclean_plan_waits_on_existing_decision_flow_then_replans_inside_grant(tmp_path):
    platform = FakePlatform(tmp_path)
    agents = FakeAgents(plans=[
        {"clean": False, "summary": "Unclean because an assumption is unresolved", "steps": [], "touchedPaths": []},
        {"clean": True, "summary": "Clean after the decision", "steps": ["Stay narrow"], "touchedPaths": ["tests/test_rehearsal.py"]},
    ])
    decisions = FakeDecision()

    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=decisions,
    ).run()

    assert result["state"] == "merged"
    assert decisions.calls[0] == "request"
    assert decisions.calls[1].startswith("wait:JOB-decision:")
    assert platform.calls.index("worktree") < platform.calls.index("commit:commit-1")
    feed = [json.loads(line) for line in Path(result["feedPath"]).read_text().splitlines()]
    waiting = next(item for item in feed if item["state"] == "waiting-on-person")
    assert waiting["pendingDecision"]["decisionId"] == "decision-1"


def test_pause_decision_stops_the_unclean_mission_before_development(tmp_path):
    platform = FakePlatform(tmp_path)
    agents = FakeAgents(plans=[{
        "clean": False,
        "summary": "Unclean because an assumption is unresolved",
        "steps": [],
        "touchedPaths": [],
    }])
    decisions = FakeDecision(answer={"selectedOptionId": "pause", "freeText": "Wait."})
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=decisions,
    )

    with pytest.raises(BenchError, match="Person paused"):
        bench.run()

    assert [kind for kind, _prompt in agents.prompts] == ["plan"]
    assert not any(call.startswith("commit:") for call in platform.calls)
    assert "push" not in platform.calls


def test_a_red_full_gate_prevents_every_push(tmp_path):
    platform = FakePlatform(tmp_path, gate_failure=True)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="full test gate failed"):
        bench.run()

    assert "push" not in platform.calls
    assert "create-pr" not in platform.calls
    feed = [json.loads(line) for line in bench.feed_path.read_text().splitlines()]
    assert feed[-1]["state"] == "failed"
    assert feed[-1]["outcome"]["status"] == "failed"
    assert json.loads(bench.evidence_path.read_text())["failure"]["reason"].startswith("full test gate failed")


def test_failure_after_auto_merge_is_armed_cancels_it_and_records_budget_exhaustion(tmp_path):
    platform = FakePlatform(tmp_path, merge_failure=True)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="acceptance budget"):
        bench.run()

    assert platform.calls.index("auto-merge") < platform.calls.index("disable-auto-merge")
    feed = [json.loads(line) for line in bench.feed_path.read_text().splitlines()]
    assert feed[-1]["state"] == "budget-exhausted"
    assert feed[-1]["outcome"]["status"] == "budget-exhausted"


def test_review_correction_that_touches_authority_stops_before_another_push(tmp_path):
    platform = FakePlatform(
        tmp_path,
        path_batches=[["tests/test_rehearsal.py"], ["docs/workplan.md"]],
    )
    decisions = FakeDecision()
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=decisions,
    )

    with pytest.raises(BenchError, match="authority-owned changes stop"):
        bench.run()

    assert platform.calls.count("push") == 1
    assert decisions.calls[0] == "request"


def test_isolated_worktree_keeps_main_clean_when_the_session_is_killed(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("main\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)

    value = grant()
    value["repository"] = "local/rehearsal"
    platform = GitHubPlatform(repository)
    session_dir = tmp_path / "session"
    worktree = platform.prepare_worktree(value, session_dir)
    child = subprocess.Popen([
        sys.executable,
        "-c",
        "from pathlib import Path; import time; Path('tracked.txt').write_text('changed\\n'); time.sleep(60)",
    ], cwd=worktree)
    child.kill()
    child.wait(timeout=5)

    status = subprocess.run(
        ["git", "status", "--short"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout
    assert status == ""


def test_timeout_terminates_background_descendants_before_they_can_keep_working(tmp_path):
    marker = tmp_path / "descendant-survived"
    child_code = (
        "import time; from pathlib import Path; time.sleep(1.2); "
        f"Path({str(marker)!r}).write_text('survived')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(60)"
    )

    with pytest.raises(BenchError, match="exceeded its 1s budget"):
        SubprocessRunner().run([sys.executable, "-c", parent_code], tmp_path, 1)

    time.sleep(0.5)
    assert not marker.exists()


def test_claude_stages_require_reported_opus_major_version_five_or_newer():
    assert reported_opus_major('{"modelUsage":{"claude-opus-5":{}}}') == 5
    assert reported_opus_major('{"model":"claude-opus-6-1"}') == 6
    assert reported_opus_major('{"model":"claude-opus-4-1"}') == 4
    assert reported_opus_major('{"model":"opus"}') is None


def test_model_subprocess_environment_scrubs_grant_credentials_and_git_helpers(tmp_path):
    environment = AgentDriver(credential_envs={"MISSION_GITHUB_TOKEN", "DECISION_TOKEN"})._agent_env(tmp_path)

    assert environment["MISSION_GITHUB_TOKEN"] == ""
    assert environment["DECISION_TOKEN"] == ""
    assert environment["GH_TOKEN"] == ""
    assert environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_1"] == ""
    assert environment["GIT_CONFIG_KEY_3"] == "core.hooksPath"


def test_runtime_state_must_live_outside_the_product_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    grant_path = write_grant(tmp_path / "grant.json")

    with pytest.raises(BenchError, match="outside the product repository"):
        PipelineBench(grant_path, repository / "runtime", repository_root=repository)


def test_repository_wall_requires_claude_acceptance_without_authority_ownership(tmp_path):
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(
        "* @sm-agent-claude\n"
        "/schemas/canonical/ @andrewHermann\n"
        "/schemas/schema-registry.json @andrewHermann\n"
        "/docs/workplan.md @andrewHermann\n"
        "/.github/CODEOWNERS @andrewHermann\n"
    )
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    platform.validate_repository_wall(grant())


def test_repository_wall_refuses_auto_merge_without_a_required_approval(tmp_path):
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text("* @sm-agent-claude\n")
    protection = protected_repository()
    protection["required_pull_request_reviews"]["required_approving_review_count"] = 0
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protection))

    with pytest.raises(BenchError, match="require an approving review"):
        platform.validate_repository_wall(grant())


def test_machine_commit_email_must_be_verified_by_the_authenticated_account(tmp_path, monkeypatch):
    value = grant()
    accounts = {}
    for worker, account in value["machineAccounts"].items():
        token = f"token-{worker}"
        monkeypatch.setenv(account["tokenEnv"], token)
        accounts[token] = (account["login"], account["email"])
    platform = GitHubPlatform(
        tmp_path,
        AccountRunner(accounts, unverified={value["machineAccounts"]["local"]["email"]}),
    )

    with pytest.raises(BenchError, match="local commit email is not verified"):
        platform.validate_machine_accounts(value)


def test_authenticated_push_disables_hooks_and_scrubs_every_unrelated_credential(tmp_path, monkeypatch):
    value = grant()
    credential_names = {
        account["tokenEnv"] for account in value["machineAccounts"].values()
    }
    for name in credential_names:
        monkeypatch.setenv(name, "local-token" if name == value["machineAccounts"]["local"]["tokenEnv"] else "secret")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "also-secret")
    worktree = tmp_path / "session" / "worktree"
    worktree.mkdir(parents=True)
    platform = GitHubPlatform(
        tmp_path,
        PushRunner(credential_names | {"UNRELATED_API_TOKEN"}),
    )

    platform.push(value, worktree)


def test_changed_paths_include_both_ends_of_an_authority_file_rename(tmp_path):
    paths = GitHubPlatform(tmp_path, DiffRunner()).changed_paths(grant(), tmp_path)

    assert paths == [
        "schemas/canonical/job-v2.json",
        "docs/job-v2.json",
        "tests/test_job.py",
    ]


def test_unclean_plan_client_uses_existing_decision_endpoint_and_observes_answer(tmp_path):
    chat = runpy.run_path(str(Path(__file__).resolve().parents[1] / "steel-mission-chat" / "server.py"))
    handler = chat["Handler"]
    globals_ = handler.do_POST.__globals__
    globals_["resolve_runtime_profile"] = lambda _profile=None: {
        "runtimeProfile": {"id": "dc13.claude"},
        "modelPolicy": {"provider": "claude", "selectedModel": "claude-opus-5"},
        "snapshotPolicy": {},
    }
    globals_["organization_registry"] = lambda: {"activeOrganizationId": "northstar-forge"}
    globals_["knowledge_quality_report"] = lambda: {"issues": [], "status": "ready"}
    globals_["update_mission"] = lambda *_args, **_kwargs: {}
    globals_["append_mission_audit"] = lambda *_args, **_kwargs: None
    globals_["persist_steering_events"] = lambda *_args, **_kwargs: None
    globals_["new_task_id"] = lambda: "DEV-999999"

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = DecisionClient({"baseUrl": f"http://{host}:{port}"})
    handle = client.request(f"The plan is unclean.\n\n{REQUIREMENT}\n\n{ACCEPTANCE}")
    try:
        request = Request(
            f"http://{host}:{port}/api/chat/{handle['jobId']}/decision",
            data=json.dumps({"optionId": "continue-narrow", "freeText": "Stay inside the grant."}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Present-Role": "owner",
                "X-Present-Actor": "mission-bench",
            },
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 202

        answer = client.wait_for_answer(handle["jobId"], 5)
        assert answer["selectedOptionId"] == "continue-narrow"
        assert answer["freeText"] == "Stay inside the grant."
    finally:
        with chat["JOBS_LOCK"]:
            chat["JOBS"].pop(handle["jobId"], None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
