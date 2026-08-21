import json
import os
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
    structured_value,
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
        "allowedPaths": ["tooling/", "tests/"],
        "surfaces": {
            "authentication": False,
            "networkService": False,
            "subprocessExecution": False,
            "authoritySchemas": False,
        },
        "definitionOfDone": {
            "redTest": ["python3", "-m", "pytest", "tests/test_rehearsal.py", "-q"],
            "redFailure": {"exitCodes": [1], "outputPattern": "1 failed"},
            "test": ["make", "test"],
            "releaseCheck": ["make", "release-check"],
        },
        "budgets": {
            stage: {"elapsedSeconds": 900, "turns": 6}
            for stage in ("plan", "develop", "review", "acceptance")
        },
        "abortConditions": [{"kind": "budget-exhausted"}],
        "maxReviewRounds": 2,
        "machineAccounts": {
            "claude": {"login": "sm-agent-claude", "email": "101+sm-agent-claude@users.noreply.github.com", "tokenEnv": "SM_AGENT_CLAUDE_GITHUB_TOKEN"},
            "codex": {"login": "sm-agent-codex", "email": "102+sm-agent-codex@users.noreply.github.com", "tokenEnv": "SM_AGENT_CODEX_GITHUB_TOKEN"},
            "local": {"login": "sm-agent-qwen", "email": "103+sm-agent-qwen@users.noreply.github.com", "tokenEnv": "SM_AGENT_QWEN_GITHUB_TOKEN"},
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
        red_output: str = "1 failed",
        arm_failure: bool = False,
        merge_failure: bool = False,
        gate_mutates: bool = False,
        head_changes_after_ci: bool = False,
        path_batches: list[list[str]] | None = None,
        zero_commit_failures: int = 0,
    ):
        self.root = root
        self.issue_payload = issue_payload or issue()
        self.gate_failure = gate_failure
        self.red_output = red_output
        self.arm_failure = arm_failure
        self.merge_failure = merge_failure
        self.gate_mutates = gate_mutates
        self.head_changes_after_ci = head_changes_after_ci
        self.path_batches = list(path_batches or [])
        self.zero_commit_failures = zero_commit_failures
        self.calls: list[str] = []
        self.remote_timeouts: list[tuple[str, int | None]] = []
        self.commit = 0

    def issue(self, _grant, _timeout=None):
        self.calls.append("issue")
        return self.issue_payload

    def validate_machine_accounts(self, _grant):
        self.calls.append("accounts")

    def validate_repository_wall(self, _grant, timeout=None):
        self.calls.append("branch-protection")
        self.remote_timeouts.append(("branch-protection", timeout))
        return {"credentialBoundary": "operator-ambient", "baseCommit": "a" * 40}

    def claim_issue(self, _grant, session_id, on_assigned=None):
        self.calls.append(f"claim:{session_id}")
        if on_assigned:
            on_assigned()

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
            return CommandResult(argv, 1, self.red_output, "", 0.1)
        if self.gate_failure and argv == grant()["definitionOfDone"]["test"]:
            return CommandResult(argv, 1, "1 failed", "", 0.1)
        return CommandResult(argv, 0, "366 passed", "", 0.1)

    def assert_machine_commit(self, _grant, _worktree, previous_commit=None):
        if self.zero_commit_failures:
            self.zero_commit_failures -= 1
            self.calls.append("commit:none")
            raise BenchError("local developer did not create an attributable commit")
        self.commit += 1
        value = f"commit-{self.commit}"
        self.calls.append(f"commit:{value}")
        assert value != previous_commit
        return value

    def assert_unchanged_machine_commit(
        self,
        _grant,
        _worktree,
        expected_commit,
        previous_commit=None,
    ):
        self.calls.append(f"revalidate:{expected_commit}:{previous_commit}")
        if self.gate_mutates:
            raise BenchError("repository gate changed the reviewed machine commit")

    def changed_paths(self, _grant, _worktree):
        self.calls.append("paths")
        if self.path_batches:
            return self.path_batches.pop(0)
        return ["tests/test_rehearsal.py"]

    def push(self, _grant, _worktree, timeout=None):
        self.calls.append("push")
        self.remote_timeouts.append(("push", timeout))

    def create_pr(self, _grant, _worktree, body_path, timeout=None):
        self.calls.append("create-pr")
        self.remote_timeouts.append(("create-pr", timeout))
        assert "failing test" in body_path.read_text().lower()
        return {"number": 321, "url": "https://github.test/pull/321"}

    def update_pr_body(self, _grant, _pr_number, body_path, timeout=None):
        body = body_path.read_text()
        self.calls.append("update-pr-body")
        self.remote_timeouts.append(("update-pr-body", timeout))
        assert "commit-1" in body
        assert "commit-2" in body
        assert "Codex correction rounds: 1" in body

    def post_codex_review(self, _grant, _pr_number, review, timeout=None):
        self.calls.append(f"codex-review:{review['verdict']}")
        self.remote_timeouts.append(("codex-review", timeout))

    def assert_pr_head(self, _grant, _pr_number, expected_commit, timeout=None):
        self.calls.append(f"pr-head:{expected_commit}:{timeout}")
        if self.head_changes_after_ci and any(call.startswith("ci:") for call in self.calls):
            raise BenchError("pull request head changed outside the granted mission")

    def arm_auto_merge(self, _grant, _pr_number, head_commit, timeout=None):
        self.calls.append(f"auto-merge:{head_commit}:{timeout}")
        if self.arm_failure:
            raise BenchError("auto-merge response was lost")

    def assert_auto_merge_waiting(self, _grant, _pr_number, head_commit, timeout=None):
        self.calls.append(f"auto-merge-waiting:{head_commit}:{timeout}")
        return {
            "state": "OPEN",
            "headRefOid": head_commit,
            "reviewDecision": "REVIEW_REQUIRED",
            "autoMergeRequest": {"enabledAt": "2026-08-21T09:00:00Z"},
        }

    def disable_auto_merge(self, _grant, _pr_number):
        self.calls.append("disable-auto-merge")

    def wait_for_ci(self, _grant, _pr_number, timeout):
        self.calls.append(f"ci:{timeout}")
        return "all checks passed"

    def approve(self, _grant, _pr_number, summary, head_commit, timeout=None):
        self.calls.append(f"approve:{head_commit}:{summary}:{timeout}")

    def wait_for_merge(self, _grant, _pr_number, timeout):
        self.calls.append(f"merged:{timeout}")
        if self.merge_failure:
            raise BenchError("auto-merge did not land within the acceptance budget")
        return {"state": "MERGED", "mergedAt": "2026-08-21T09:00:00Z"}


class FakeAgents:
    def __init__(self, plans=None, reviews=None, acceptances=None):
        self.plans = list(plans or [{"clean": True, "summary": "Clean plan", "steps": ["Add the test", "Make it pass"], "touchedPaths": ["tests/test_rehearsal.py"]}])
        self.reviews = list(reviews or [
            {"verdict": "changes-requested", "summary": "One correction", "findings": [{"priority": "P1", "body": "Cover the empty case."}]},
            {"verdict": "clean", "summary": "Correction verified", "findings": []},
        ])
        self.acceptances = list(acceptances or [{
            "verdict": "approve",
            "summary": "Definition of done verified.",
            "securityFindings": [],
        }])
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
        return self.acceptances.pop(0)


class FakeDecision:
    def __init__(self, answer=None):
        self.calls: list[str] = []
        self.answer = answer or {
            "selectedOptionId": "continue-narrow",
            "freeText": "Stay inside the grant.",
        }

    def request(self, context):
        self.calls.append("request")
        assert any(term in context.lower() for term in ("unclean", "human-owned", "security finding"))
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
    def __init__(self, protection, codeowners=None):
        self.protection = protection
        self.codeowners = codeowners

    def run(
        self, argv, cwd, timeout, *, input_text="", extra_env=None,
        inherit_env=True, complete_stdout=False,
    ):
        assert Path(argv[0]).name == "gh"
        assert argv[1] == "api"
        assert inherit_env is False
        assert "GH_TOKEN" not in extra_env
        assert all(
            extra_env[account["tokenEnv"]] == ""
            for account in grant()["machineAccounts"].values()
        )
        assert extra_env["PATH"]
        assert complete_stdout is True
        endpoint = argv[-1]
        if "/git/ref/heads/" in endpoint:
            output = json.dumps({"object": {"sha": "a" * 40}})
        elif "/contents/.github/CODEOWNERS?ref=" in endpoint:
            output = self.codeowners
            if output is None:
                output = (Path(cwd) / ".github" / "CODEOWNERS").read_text()
        else:
            output = json.dumps(self.protection)
        return CommandResult(argv, 0, output, "", 0.1)


class DiffRunner:
    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None, inherit_env=True):
        assert Path(argv[0]).name == "git"
        assert argv[1:4] == ["diff", "--no-ext-diff", "--no-textconv"]
        assert inherit_env is False
        return CommandResult(
            argv,
            0,
            "R100\tschemas/canonical/job-v2.json\tdocs/job-v2.json\nM\ttests/test_job.py\n",
            "",
            0.1,
        )


class AccountRunner:
    def __init__(self, accounts):
        self.accounts = accounts
        self.commands = []

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None, complete_stdout=False):
        login, account_id = self.accounts[extra_env["GH_TOKEN"]]
        command = [Path(argv[0]).name, *argv[1:]]
        self.commands.append(command)
        assert command == ["gh", "api", "user"]
        assert complete_stdout is True
        output = json.dumps({"login": login, "id": account_id})
        return CommandResult(argv, 0, output, "", 0.1)


class PushRunner:
    def __init__(self, expected_blank):
        self.expected_blank = expected_blank

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None, inherit_env=True):
        assert Path(argv[0]).name == "git"
        assert argv[1] == "push"
        assert inherit_env is False
        assert all(extra_env[name] == "" for name in self.expected_blank)
        assert extra_env["SM_BENCH_PUSH_TOKEN"] == "local-token"
        assert extra_env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert extra_env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert extra_env["GIT_CONFIG_KEY_3"] == "core.hooksPath"
        assert Path(extra_env["GIT_CONFIG_VALUE_3"]).is_dir()
        return CommandResult(argv, 0, "pushed", "", 0.1)


class MergePollRunner:
    def __init__(self, clock):
        self.clock = clock
        self.timeouts = []

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None, complete_stdout=False):
        assert Path(argv[0]).name == "gh"
        assert argv[1:3] == ["pr", "view"]
        assert complete_stdout is True
        self.timeouts.append(timeout)
        self.clock[0] += min(0.6, timeout)
        return CommandResult(argv, 0, '{"state":"OPEN"}', "", 0.1)


class AutoMergeWaitingRunner:
    def __init__(self, value):
        self.value = value

    def run(self, argv, cwd, timeout, *, input_text="", extra_env=None, complete_stdout=False):
        assert Path(argv[0]).name == "gh"
        assert argv[1:3] == ["pr", "view"]
        assert argv[-2:] == ["--json", "state,headRefOid,reviewDecision,autoMergeRequest"]
        assert extra_env["GH_TOKEN"] == "local-token"
        assert complete_stdout is True
        return CommandResult(argv, 0, json.dumps(self.value), "", 0.1)


def protected_repository():
    return {
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "require_code_owner_reviews": True,
            "dismiss_stale_reviews": True,
        },
        "required_conversation_resolution": {"enabled": True},
        "required_status_checks": {
            "strict": True,
            "checks": [
                {"context": "Python test suite (3.11)"},
                {"context": "Python test suite (3.12)"},
            ],
        },
    }


def protected_codeowners() -> str:
    return (
        "* @andrewHermann @sm-agent-claude\n"
        "/schemas/ @andrewHermann\n"
        "/schemas/canonical/ @andrewHermann\n"
        "/schemas/schema-registry.json @andrewHermann\n"
        "/bin/ @andrewHermann\n"
        "/steel-mission-chat/ @andrewHermann\n"
        "/adapters/ @andrewHermann\n"
        "/.github/CODEOWNERS @andrewHermann\n"
        "/.github/workflows/ @andrewHermann\n"
        "/Dockerfile.private-runner @andrewHermann\n"
        "/requirements-dev.txt @andrewHermann\n"
        "/package.json @andrewHermann\n"
        "/package-lock.json @andrewHermann\n"
        "/steel_core/ @andrewHermann @sm-agent-claude\n"
        "/plan/ @andrewHermann\n"
        "/docs/workplan.md @andrewHermann\n"
        "/tooling/ @andrewHermann @sm-agent-claude\n"
    )


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

    assert result["state"] == "queued"
    assert result["reviewCorrectionRounds"] == 1
    acceptance_prompt = next(prompt for kind, prompt in agents.prompts if kind == "acceptance")
    assert "report any actionable security finding you nonetheless observe" in acceptance_prompt
    evidence = json.loads(Path(result["evidencePath"]).read_text())
    assert evidence["stages"]["acceptance"]["securityReview"] == {
        "required": False,
        "status": "not-performed",
        "findings": [],
    }
    first_push = platform.calls.index("push")
    assert any(call.startswith("command:make test") for call in platform.calls[:first_push])
    assert any(call.startswith("command:make release-check") for call in platform.calls[:first_push])
    assert next(index for index, call in enumerate(platform.calls) if call.startswith("auto-merge:")) < next(
        index for index, call in enumerate(platform.calls) if call.startswith("auto-merge-waiting:")
    ) < next(
        index for index, call in enumerate(platform.calls) if call.startswith("approve:")
    )
    assert not any(call.startswith("merged:") for call in platform.calls)
    acceptance_calls = [
        call for call in platform.calls
        if call.startswith(("pr-head:", "auto-merge:", "auto-merge-waiting:", "approve:"))
    ]
    assert acceptance_calls
    assert all(not call.endswith(":None") for call in acceptance_calls)
    assert all(
        timeout is not None
        for operation, timeout in platform.remote_timeouts
        if operation in {"push", "create-pr"}
    )
    assert "update-pr-body" in platform.calls
    assert all(REQUIREMENT in prompt and ACCEPTANCE in prompt for _, prompt in agents.prompts)
    develop_prompt = next(prompt for kind, prompt in agents.prompts if kind == "develop")
    assert "invoke the apply_patch executable through the shell" in develop_prompt
    assert "does not support a direct apply_patch tool call" in develop_prompt

    feed = [json.loads(line) for line in Path(result["feedPath"]).read_text().splitlines()]
    assert feed[-1]["outcome"]["status"] == "succeeded"
    assert all(schema_check.validate(item, "canonical/agent-session-status-v1.json") == [] for item in feed)


def test_status_feed_bounds_model_summary_without_truncating_evidence(tmp_path):
    long_summary = "implementation detail " * 110
    agents = FakeAgents(plans=[{
        "clean": True,
        "summary": long_summary,
        "steps": ["Implement the granted change."],
        "touchedPaths": ["tests/test_rehearsal.py"],
    }])

    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=FakePlatform(tmp_path),
        agents=agents,
        decisions=FakeDecision(),
    ).run()

    evidence = json.loads(Path(result["evidencePath"]).read_text())
    feed = [json.loads(line) for line in Path(result["feedPath"]).read_text().splitlines()]
    plan_complete = next(
        item for item in feed
        if item["stage"] == "plan" and item["lastEvent"]["kind"] == "stage-completed"
    )
    assert evidence["stages"]["plan"]["summary"] == long_summary
    assert len(plan_complete["lastEvent"]["summary"]) == 2000
    assert plan_complete["lastEvent"]["summary"].endswith("...")


def test_develop_stage_retries_a_zero_commit_turn_within_its_budget(tmp_path):
    platform = FakePlatform(tmp_path, zero_commit_failures=1)
    agents = FakeAgents()

    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    ).run()

    develop_prompts = [prompt for kind, prompt in agents.prompts if kind == "develop"]
    assert result["state"] == "queued"
    assert len(develop_prompts) == 2
    assert "previous local turn returned without a commit" in develop_prompts[1]
    assert platform.calls.index("commit:none") < platform.calls.index("commit:commit-1")


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

    assert result["state"] == "queued"


def test_clean_security_review_issue_reaches_acceptance_without_escalation(tmp_path):
    platform = FakePlatform(tmp_path, issue_payload=issue(labels=["task", "security-review"]))
    agents = FakeAgents()
    decisions = FakeDecision()
    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=decisions,
    ).run()

    assert result["state"] == "queued"
    assert decisions.calls == []
    acceptance_prompt = next(prompt for kind, prompt in agents.prompts if kind == "acceptance")
    assert "security review" in acceptance_prompt.lower()
    assert "section 4.5" in acceptance_prompt
    evidence = json.loads(Path(result["evidencePath"]).read_text())
    assert evidence["stages"]["acceptance"]["securityReview"] == {
        "required": True,
        "status": "clean",
        "findings": [],
    }


def test_declared_security_surface_requires_acceptance_security_review(tmp_path):
    value = grant()
    value["surfaces"]["subprocessExecution"] = True
    agents = FakeAgents()
    platform = FakePlatform(tmp_path)
    result = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    ).run()

    assert result["state"] == "queued"
    acceptance_prompt = next(prompt for kind, prompt in agents.prompts if kind == "acceptance")
    assert "subprocessExecution" in acceptance_prompt


def test_partial_claim_failure_is_recorded_after_assignment(tmp_path):
    class PartialClaimPlatform(FakePlatform):
        def claim_issue(self, _grant, _session_id, on_assigned=None):
            self.calls.append("assignment-succeeded")
            if on_assigned:
                on_assigned()
            raise BenchError("issue claim comment failed: comment failed")

    state_root = tmp_path / "state"
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        state_root,
        platform=PartialClaimPlatform(tmp_path),
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="issue claim comment failed"):
        bench.run()

    evidence = json.loads((bench.session_dir / "evidence.json").read_text())
    feed = [
        json.loads(line)
        for line in (state_root / "agent-session-status.jsonl").read_text().splitlines()
    ]
    assert evidence["state"] == "failed"
    assert evidence["failure"]["reason"] == "issue claim comment failed: comment failed"
    assert feed[-1]["stage"] == "plan"
    assert feed[-1]["lastEvent"]["kind"] == "session-stopped"


def test_assignment_failure_does_not_emit_a_false_claim_timestamp(tmp_path):
    class AssignmentFailurePlatform(FakePlatform):
        def claim_issue(self, _grant, _session_id, on_assigned=None):
            self.calls.append("assignment-failed")
            raise BenchError("issue assignment failed: assignment failed")

    state_root = tmp_path / "state"
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        state_root,
        platform=AssignmentFailurePlatform(tmp_path),
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="issue assignment failed"):
        bench.run()

    evidence = json.loads((bench.session_dir / "evidence.json").read_text())
    assert evidence["state"] == "failed"
    assert evidence["failure"]["reason"] == "issue assignment failed: assignment failed"
    assert not (state_root / "agent-session-status.jsonl").exists()


def test_security_review_label_added_during_execution_is_reviewed_before_approval(tmp_path):
    changed_issue = issue(labels=["task", "security-review"])

    class RelabelledPlatform(FakePlatform):
        def issue(self, grant_value, timeout=None):
            if self.calls.count("issue"):
                self.issue_payload = changed_issue
            return super().issue(grant_value, timeout)

    platform = RelabelledPlatform(tmp_path)
    agents = FakeAgents()
    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    ).run()

    assert result["state"] == "queued"
    assert "push" in platform.calls
    acceptance_prompt = next(prompt for kind, prompt in agents.prompts if kind == "acceptance")
    assert "security review" in acceptance_prompt.lower()


def test_security_finding_escalates_and_blocks_approval(tmp_path):
    platform = FakePlatform(tmp_path, issue_payload=issue(labels=["task", "security-review"]))
    findings = [{
        "priority": "P1",
        "body": "Validate subprocess arguments before execution.",
    }]
    agents = FakeAgents(acceptances=[{
        "verdict": "reject",
        "summary": "Subprocess arguments cross the grant boundary unchecked.",
        "securityFindings": findings,
    }])
    decisions = FakeDecision()
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=decisions,
    )

    with pytest.raises(BenchError, match="security finding requires Founder resolution"):
        bench.run()

    assert decisions.calls[0] == "request"
    assert decisions.calls[1].startswith("wait:JOB-decision:")
    assert not any(call.startswith(("approve:", "auto-merge:")) for call in platform.calls)
    evidence = json.loads(bench.evidence_path.read_text())
    security = evidence["stages"]["acceptance"]["securityReview"]
    assert security["status"] == "escalated"
    assert security["findings"] == findings
    feed = [json.loads(line) for line in bench.feed_path.read_text().splitlines()]
    waiting = next(item for item in feed if item["state"] == "waiting-on-person")
    assert waiting["pendingDecision"]["kind"] == "blocked"


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

    assert result["state"] == "queued"
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


def test_red_test_runner_error_is_not_accepted_as_the_granted_assertion_failure(tmp_path):
    platform = FakePlatform(tmp_path, red_output="ERROR collecting tests/test_rehearsal.py")
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="did not match its granted failure signal"):
        bench.run()

    assert "push" not in platform.calls


def test_repository_gate_cannot_change_head_before_the_bench_pushes(tmp_path):
    platform = FakePlatform(tmp_path, gate_mutates=True)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="gate changed the reviewed machine commit"):
        bench.run()

    assert "push" not in platform.calls


def test_machine_checkable_path_abort_stops_before_every_push(tmp_path):
    value = grant()
    value["abortConditions"] = [{"kind": "path-changed", "paths": ["tests/"]}]
    platform = FakePlatform(tmp_path)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="abort condition matched"):
        bench.run()

    assert "push" not in platform.calls


def test_grant_requires_a_repository_relative_allowed_path_wall():
    value = grant()
    value.pop("allowedPaths")
    with pytest.raises(BenchError, match="allowedPaths"):
        mission_bench.validate_grant(value)

    value = grant()
    value["allowedPaths"] = ["../outside"]
    with pytest.raises(BenchError, match="repository-relative"):
        mission_bench.validate_grant(value)


def test_plan_outside_the_allowed_path_wall_stops_before_development(tmp_path):
    agents = FakeAgents(plans=[{
        "clean": True,
        "summary": "Touch an ungranted product path.",
        "steps": ["Edit the server."],
        "touchedPaths": ["steel-mission-chat/server.py"],
    }])
    platform = FakePlatform(tmp_path)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="outside the granted path wall"):
        bench.run()

    assert not any(call.startswith("commit:") for call in platform.calls)


def test_committed_path_outside_the_allowed_wall_stops_before_push(tmp_path):
    platform = FakePlatform(tmp_path, path_batches=[["README.md"]])
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="outside the granted path wall"):
        bench.run()

    assert "push" not in platform.calls


def test_grant_drift_abort_rechecks_the_issue_contract_before_push(tmp_path):
    changed_issue = issue()
    changed_issue["body"] = changed_issue["body"].replace(REQUIREMENT, "A changed requirement.")

    class DriftingPlatform(FakePlatform):
        def issue(self, grant_value, timeout=None):
            if self.calls.count("issue"):
                self.issue_payload = changed_issue
            return super().issue(grant_value, timeout)

    value = grant()
    value["abortConditions"] = [{"kind": "grant-drift"}]
    platform = DriftingPlatform(tmp_path)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="abort condition matched a changed issue contract"):
        bench.run()

    assert "push" not in platform.calls


def test_pipeline_returns_truthful_queued_state_without_waiting_for_merge(tmp_path):
    platform = FakePlatform(tmp_path, merge_failure=True)
    result = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    ).run()

    assert result["state"] == "queued"
    assert any(call.startswith("auto-merge:") for call in platform.calls)
    assert not any(call.startswith("merged:") for call in platform.calls)


def test_lost_auto_merge_response_is_treated_as_armed_and_cancelled(tmp_path):
    platform = FakePlatform(tmp_path, arm_failure=True)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="auto-merge response was lost"):
        bench.run()

    assert next(
        index for index, call in enumerate(platform.calls) if call.startswith("auto-merge:")
    ) < platform.calls.index("disable-auto-merge")
    assert not any(call.startswith("approve:") for call in platform.calls)


def test_pull_request_head_change_after_ci_stops_before_approval_or_auto_merge(tmp_path):
    platform = FakePlatform(tmp_path, head_changes_after_ci=True)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="pull request head changed"):
        bench.run()

    assert not any(call.startswith("auto-merge:") for call in platform.calls)
    assert not any(call.startswith("approve:") for call in platform.calls)


def test_branch_wall_is_revalidated_before_auto_merge_is_armed(tmp_path):
    class WeakeningWallPlatform(FakePlatform):
        def validate_repository_wall(self, grant_value, timeout=None):
            if any(operation == "branch-protection" for operation, _timeout in self.remote_timeouts):
                raise BenchError("branch protection changed before auto-merge")
            super().validate_repository_wall(grant_value, timeout)

    platform = WeakeningWallPlatform(tmp_path)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="branch protection changed"):
        bench.run()

    assert not any(call.startswith("auto-merge:") for call in platform.calls)


def test_frozen_base_is_revalidated_after_ci_before_acceptance(tmp_path):
    class BaseAdvancesDuringCIPlatform(FakePlatform):
        def validate_repository_wall(self, grant_value, timeout=None):
            super().validate_repository_wall(grant_value, timeout)
            if self.calls.count("branch-protection") == 3:
                raise BenchError("base branch advanced after mission validation")

    platform = BaseAdvancesDuringCIPlatform(tmp_path)
    agents = FakeAgents()
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json"),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="base branch advanced"):
        bench.run()

    assert any(call.startswith("ci:") for call in platform.calls)
    assert not any(kind == "acceptance" for kind, _prompt in agents.prompts)
    assert not any(call.startswith("auto-merge:") for call in platform.calls)


def test_develop_budget_expiry_after_push_prevents_pr_creation(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(mission_bench.time, "monotonic", lambda: clock[0])

    class SlowPushPlatform(FakePlatform):
        def push(self, grant_value, worktree, timeout=None):
            super().push(grant_value, worktree, timeout)
            clock[0] += timeout

    value = grant()
    value["budgets"]["develop"]["elapsedSeconds"] = 1
    platform = SlowPushPlatform(tmp_path)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="elapsed-time budget is exhausted"):
        bench.run()

    assert ("push", 1) in platform.remote_timeouts
    assert "create-pr" not in platform.calls


def test_review_posting_cannot_complete_after_the_review_budget(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(mission_bench.time, "monotonic", lambda: clock[0])

    class SlowReviewPlatform(FakePlatform):
        def post_codex_review(self, grant_value, pr_number, review, timeout=None):
            super().post_codex_review(grant_value, pr_number, review)
            self.remote_timeouts.append(("codex-review", timeout))
            clock[0] += timeout or 1

        def update_pr_body(self, _grant, _pr_number, _body_path, timeout=None):
            self.calls.append("update-pr-body")
            self.remote_timeouts.append(("update-pr-body", timeout))

    value = grant()
    value["budgets"]["review"]["elapsedSeconds"] = 1
    platform = SlowReviewPlatform(tmp_path)
    agents = FakeAgents(reviews=[
        {"verdict": "clean", "summary": "Clean review", "findings": []},
    ])
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=platform,
        agents=agents,
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="elapsed-time budget is exhausted"):
        bench.run()

    assert ("codex-review", 1) in platform.remote_timeouts
    assert not any(call.startswith("auto-merge:") for call in platform.calls)


def test_expired_acceptance_read_cannot_arm_auto_merge(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(mission_bench.time, "monotonic", lambda: clock[0])

    class SlowHeadPlatform(FakePlatform):
        def assert_pr_head(self, grant_value, pr_number, expected_commit, timeout=None):
            super().assert_pr_head(grant_value, pr_number, expected_commit, timeout)
            clock[0] += timeout

    value = grant()
    value["budgets"]["acceptance"]["elapsedSeconds"] = 1
    platform = SlowHeadPlatform(tmp_path)
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=platform,
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    with pytest.raises(BenchError, match="elapsed-time budget is exhausted"):
        bench.run()

    assert "pr-head:commit-2:1" in platform.calls
    assert not any(call.startswith("auto-merge:") for call in platform.calls)
    assert "disable-auto-merge" not in platform.calls


def test_review_correction_that_touches_authority_stops_before_another_push(tmp_path):
    platform = FakePlatform(
        tmp_path,
        path_batches=[
            ["tests/test_rehearsal.py"],
            ["tests/test_rehearsal.py"],
            ["docs/workplan.md"],
        ],
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


def test_successful_command_reaps_background_descendants_before_returning(tmp_path):
    marker = tmp_path / "successful-descendant-survived"
    child_code = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).write_text('survived')"
    )
    parent_code = (
        "import os, subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print('done')"
    )

    result = SubprocessRunner().run([sys.executable, "-c", parent_code], tmp_path, 5)

    assert result.returncode == 0
    time.sleep(1.2)
    assert not marker.exists()


def test_subprocess_output_is_drained_into_fixed_size_tail_buffers(tmp_path):
    result = SubprocessRunner().run([
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('a' * 100000 + 'stdout-end'); "
        "sys.stderr.write('b' * 100000 + 'stderr-end')",
    ], tmp_path, 5)

    assert len(result.stdout.encode()) <= 20000
    assert len(result.stderr.encode()) <= 20000
    assert result.stdout.endswith("stdout-end")
    assert result.stderr.endswith("stderr-end")


def test_structured_subprocess_preserves_complete_output_beyond_diagnostic_tail(tmp_path):
    payload = {"model": "claude-opus-5", "structured_output": {"summary": "x" * 50000}}
    result = SubprocessRunner().run(
        [sys.executable, "-c", f"import json; print(json.dumps({payload!r}))"],
        tmp_path,
        5,
        complete_stdout=True,
    )

    assert json.loads(result.stdout) == payload


def test_untrusted_subprocess_inherits_only_allowlisted_parent_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://unrelated-secret")
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        tmp_path,
        5,
        extra_env={"ANTHROPIC_API_KEY": "provider-only"},
        inherit_env=False,
    )
    environment = json.loads(result.stdout)

    assert "DATABASE_URL" not in environment
    assert environment["ANTHROPIC_API_KEY"] == "provider-only"


def test_privileged_commands_stay_pinned_after_untrusted_path_shadowing(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    attacker = tmp_path / "attacker"
    trusted.mkdir()
    attacker.mkdir()
    for directory in (trusted, attacker):
        for name in ("git", "gh"):
            executable = directory / name
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(trusted))

    class RecordingRunner:
        def __init__(self):
            self.commands = []

        def run(self, argv, cwd, timeout, **_kwargs):
            self.commands.append(argv)
            return CommandResult(argv, 0, "", "", 0.1)

    runner = RecordingRunner()
    platform = GitHubPlatform(tmp_path, runner)
    monkeypatch.setenv("PATH", f"{attacker}{os.pathsep}{trusted}")

    platform._run(["gh", "api", "user"])
    platform._run(["git", "status"])

    assert [Path(command[0]) for command in runner.commands] == [
        (trusted / "gh").resolve(),
        (trusted / "git").resolve(),
    ]


def test_claude_stages_require_reported_opus_major_version_five_or_newer():
    assert reported_opus_major('{"modelUsage":{"claude-opus-5":{}}}') == 5
    assert reported_opus_major('{"model":"claude-opus-6-1"}') == 6
    assert reported_opus_major('{"model":"claude-opus-4-1"}') == 4
    assert reported_opus_major('{"model":"opus"}') is None
    assert reported_opus_major(
        '{"modelUsage":{"claude-opus-4":{}},"structured_output":{"summary":"opus-5"}}'
    ) == 4
    assert reported_opus_major('{"modelUsage":{"claude-opus-5":{},"claude-opus-4":{}}}') == 4
    assert reported_opus_major('{"structured_output":{"summary":"opus-5"}}') is None


def test_claude_stages_preserve_single_result_object_compatibility():
    structured = {"clean": True, "summary": "single result", "steps": [], "touchedPaths": []}
    success = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "structured_output": structured,
        "modelUsage": {"claude-opus-5": {}},
    }

    assert structured_value(CommandResult(["claude"], 0, json.dumps(success), "", 0.1)) == structured
    assert reported_opus_major(json.dumps(success)) == 5

    failure = dict(success, subtype="error_during_execution", is_error=True)
    with pytest.raises(BenchError, match="model result reported an error"):
        structured_value(CommandResult(["claude"], 0, json.dumps(failure), "", 0.1))


def test_claude_stages_accept_the_current_json_event_array():
    structured = {"clean": True, "summary": "runtime preflight", "steps": [], "touchedPaths": []}
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "system", "subtype": "init", "model": "claude-opus-5"},
        {
            "type": "assistant",
            "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "Planning"}]},
        },
        {"type": "user", "message": {"role": "user", "content": []}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "api_error_status": None,
            "result": json.dumps(structured),
            "structured_output": structured,
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "permission_denials": [],
            "session_id": "session-1",
            "uuid": "result-1",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {},
                "claude-opus-5": {},
            },
        },
    ]
    result = CommandResult(["claude"], 0, json.dumps(events), "", 0.1)

    assert structured_value(result) == structured
    assert reported_opus_major(result.stdout) == 5


def test_claude_event_array_does_not_trust_configured_or_model_authored_opus_names():
    structured = {"summary": "model-authored claude-opus-5 text is not execution evidence"}
    events = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5"},
        {
            "type": "assistant",
            "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "opus-5"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(structured),
            "structured_output": structured,
            "modelUsage": {"claude-sonnet-5": {}},
        },
    ]
    result = CommandResult(["claude"], 0, json.dumps(events), "", 0.1)

    assert structured_value(result) == structured
    assert reported_opus_major(result.stdout) is None


def test_claude_error_result_event_fails_before_structured_output_is_used():
    events = [{
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "claude-opus-5",
        "modelUsage": {"claude-opus-5": {}},
    }]

    with pytest.raises(BenchError, match="model result reported an error"):
        structured_value(CommandResult(["claude"], 0, json.dumps(events), "", 0.1))


def test_model_subprocess_environment_allows_only_its_provider_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-only")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "must-not-leak")
    driver = AgentDriver(
        credential_envs={"MISSION_GITHUB_TOKEN", "DECISION_TOKEN"},
        developer_identity=("sm-agent-qwen", "qwen@example.invalid"),
    )
    environment = driver._agent_env(tmp_path, "claude")

    assert environment["MISSION_GITHUB_TOKEN"] == ""
    assert environment["DECISION_TOKEN"] == ""
    assert environment["GH_TOKEN"] == ""
    assert environment["ANTHROPIC_API_KEY"] == "claude-only"
    assert environment["UNRELATED_API_TOKEN"] == ""
    assert environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_1"] == ""
    assert environment["GIT_CONFIG_KEY_3"] == "core.hooksPath"
    local_environment = driver._agent_env(tmp_path, "local")
    assert local_environment["ANTHROPIC_API_KEY"] == ""
    assert local_environment["GIT_CONFIG_VALUE_6"] == "sm-agent-qwen"
    assert local_environment["GIT_CONFIG_VALUE_7"] == "qwen@example.invalid"


def test_local_developer_parks_the_platform_sandbox_inside_the_isolated_worktree(tmp_path):
    class RecordingRunner:
        def __init__(self):
            self.argv = []

        def run(self, argv, _cwd, _timeout, **_kwargs):
            self.argv = argv
            return CommandResult(argv, 0, "done", "", 0.1)

    runner = RecordingRunner()
    driver = AgentDriver(
        runner=runner,
        developer_identity=("sm-agent-qwen", "qwen@example.invalid"),
    )

    driver.develop("Implement and commit.", tmp_path, mission_bench.StageBudget({
        "elapsedSeconds": 30,
        "turns": 1,
    }), tmp_path)

    assert runner.argv[runner.argv.index("-m") + 1] == "qwen3-coder:30b"
    assert runner.argv[runner.argv.index("-s") + 1] == "workspace-write"


def test_runtime_state_must_live_outside_the_product_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    grant_path = write_grant(tmp_path / "grant.json")

    with pytest.raises(BenchError, match="outside the product repository"):
        PipelineBench(grant_path, repository / "runtime", repository_root=repository)


def test_grant_rejects_free_form_abort_conditions():
    value = grant()
    value["abortConditions"] = ["stop if adapters/ is touched"]

    with pytest.raises(BenchError, match="machine-checkable objects"):
        mission_bench.validate_grant(value)


def test_grant_requires_machine_checkable_security_surfaces():
    value = grant()
    value.pop("surfaces")

    with pytest.raises(BenchError, match="surfaces must declare exactly"):
        mission_bench.validate_grant(value)


def test_pr_body_renders_granted_security_surface_checkboxes(tmp_path):
    value = grant()
    value["surfaces"]["subprocessExecution"] = True
    bench = PipelineBench(
        write_grant(tmp_path / "grant.json", value),
        tmp_path / "state",
        platform=FakePlatform(tmp_path),
        agents=FakeAgents(),
        decisions=FakeDecision(),
    )

    body = bench._pr_body()

    assert "- [x] Subprocess or container execution" in body
    assert "- [ ] A network-listening service, or its bind address" in body
    assert "- [ ] None of the above" in body


def test_repository_wall_requires_claude_acceptance_without_authority_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    platform.validate_repository_wall(grant())


def test_repository_wall_uses_operator_credentials_not_non_admin_machine_token(
    tmp_path, monkeypatch
):
    for account in grant()["machineAccounts"].values():
        monkeypatch.setenv(account["tokenEnv"], f"secret-{account['login']}")
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-trusted-wall-check")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())

    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    evidence = platform.validate_repository_wall(grant())

    assert evidence == {
        "credentialBoundary": "operator-ambient",
        "baseCommit": "a" * 40,
    }


def test_repository_wall_reads_codeowners_from_the_authenticated_live_base(tmp_path, monkeypatch):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())
    remote_codeowners = codeowners.read_text().replace(
        "/schemas/canonical/ @andrewHermann",
        "/schemas/canonical/ @sm-agent-codex",
    )
    platform = GitHubPlatform(
        tmp_path,
        ProtectionRunner(protected_repository(), codeowners=remote_codeowners),
    )

    with pytest.raises(BenchError, match="Founder"):
        platform.validate_repository_wall(grant())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: "\n".join(
            [line for line in value.splitlines() if not line.startswith("*")]
            + ["* @andrewHermann @sm-agent-claude"]
        ) + "\n",
        lambda value: value + "/docs/ @sm-agent-claude\n",
        lambda value: value + "* @sm-agent-claude @andrewHermann\n",
    ],
)
def test_repository_wall_rejects_later_broader_authority_overrides(
    tmp_path, monkeypatch, mutate
):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(mutate(protected_codeowners()))
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    with pytest.raises(BenchError, match="effective CODEOWNERS rule must remain Founder-only"):
        platform.validate_repository_wall(grant())


def test_repository_wall_requires_codeowners_itself_to_remain_founder_owned(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(
        protected_codeowners().replace("/.github/CODEOWNERS @andrewHermann\n", "")
    )
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    with pytest.raises(BenchError, match="CODEOWNERS must remain Founder-owned"):
        platform.validate_repository_wall(grant())


def test_repository_wall_rejects_a_base_advance_after_initial_validation(
    tmp_path, monkeypatch
):
    value = grant()
    monkeypatch.setenv(value["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())

    class AdvancingBaseRunner(ProtectionRunner):
        def __init__(self):
            super().__init__(protected_repository())
            self.base_oids = ["a" * 40, "a" * 40, "b" * 40]

        def run(self, argv, cwd, timeout, **kwargs):
            if "/git/ref/heads/" in argv[-1]:
                oid = self.base_oids.pop(0)
                return CommandResult(argv, 0, json.dumps({"object": {"sha": oid}}), "", 0.1)
            return super().run(argv, cwd, timeout, **kwargs)

    platform = GitHubPlatform(tmp_path, AdvancingBaseRunner())
    platform.validate_repository_wall(value)

    with pytest.raises(BenchError, match="base branch advanced after mission validation"):
        platform.validate_repository_wall(value)


def test_repository_wall_refuses_auto_merge_without_a_required_approval(tmp_path, monkeypatch):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text("* @sm-agent-claude\n")
    protection = protected_repository()
    protection["required_pull_request_reviews"]["required_approving_review_count"] = 0
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protection))

    with pytest.raises(BenchError, match="require an approving review"):
        platform.validate_repository_wall(grant())


def test_repository_wall_refuses_more_approvals_than_the_pipeline_produces(tmp_path, monkeypatch):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())
    protection = protected_repository()
    protection["required_pull_request_reviews"]["required_approving_review_count"] = 2
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protection))

    with pytest.raises(BenchError, match="exactly one approving review"):
        platform.validate_repository_wall(grant())


def test_repository_wall_rejects_a_later_machine_override_of_an_authority_rule(tmp_path, monkeypatch):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(
        protected_codeowners() + "/schemas/canonical/job-v2.json @sm-agent-claude\n"
    )
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    with pytest.raises(BenchError, match="effective CODEOWNERS rule must remain Founder-only"):
        platform.validate_repository_wall(grant())


@pytest.mark.parametrize("machine_login", ["sm-agent-codex", "SM-AGENT-QWEN"])
def test_repository_wall_rejects_every_machine_account_as_authority_owner(
    tmp_path, monkeypatch, machine_login
):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(
        protected_codeowners().replace(
            "/schemas/canonical/ @andrewHermann",
            f"/schemas/canonical/ @{machine_login}",
        )
    )
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protected_repository()))

    with pytest.raises(BenchError, match="Founder"):
        platform.validate_repository_wall(grant())


def test_repository_wall_requires_checks_against_the_current_base(tmp_path, monkeypatch):
    monkeypatch.setenv(grant()["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())
    protection = protected_repository()
    protection["required_status_checks"]["strict"] = False
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protection))

    with pytest.raises(BenchError, match="current base branch"):
        platform.validate_repository_wall(grant())


def test_repository_wall_requires_both_interpreter_checks(tmp_path, monkeypatch):
    value = grant()
    monkeypatch.setenv(value["machineAccounts"]["local"]["tokenEnv"], "local-token")
    codeowners = tmp_path / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text(protected_codeowners())
    protection = protected_repository()
    protection["required_status_checks"]["checks"].pop()
    platform = GitHubPlatform(tmp_path, ProtectionRunner(protection))

    with pytest.raises(BenchError, match="Python 3.11 and 3.12"):
        platform.validate_repository_wall(value)


def test_machine_accounts_use_repo_scoped_identity_without_user_email_scope(tmp_path, monkeypatch):
    value = grant()
    accounts = {}
    for account_id, (worker, account) in enumerate(value["machineAccounts"].items(), start=101):
        token = f"token-{worker}"
        monkeypatch.setenv(account["tokenEnv"], token)
        accounts[token] = (account["login"], account_id)
    runner = AccountRunner(accounts)
    platform = GitHubPlatform(tmp_path, runner)

    platform.validate_machine_accounts(value)

    assert runner.commands == [["gh", "api", "user"]] * 3


def test_machine_commit_email_must_match_the_authenticated_account_noreply_identity(
    tmp_path, monkeypatch
):
    value = grant()
    value["machineAccounts"]["local"]["email"] = "wrong@example.invalid"
    accounts = {}
    for account_id, (worker, account) in enumerate(value["machineAccounts"].items(), start=101):
        token = f"token-{worker}"
        monkeypatch.setenv(account["tokenEnv"], token)
        accounts[token] = (account["login"], account_id)
    platform = GitHubPlatform(tmp_path, AccountRunner(accounts))

    with pytest.raises(BenchError, match="local commit email must match"):
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

    platform.push(value, worktree, 30)


def test_auto_merge_must_be_armed_and_waiting_on_approval_before_claude_approves(
    tmp_path, monkeypatch
):
    value = grant()
    monkeypatch.setenv(value["machineAccounts"]["local"]["tokenEnv"], "local-token")
    head = "a" * 40
    waiting = {
        "state": "OPEN",
        "headRefOid": head,
        "reviewDecision": "REVIEW_REQUIRED",
        "autoMergeRequest": {"enabledAt": "2026-08-21T09:00:00Z"},
    }
    platform = GitHubPlatform(tmp_path, AutoMergeWaitingRunner(waiting))

    assert platform.assert_auto_merge_waiting(value, 321, head, 30) == waiting


def test_auto_merge_refuses_to_approve_when_the_review_wall_is_not_holding(
    tmp_path, monkeypatch
):
    value = grant()
    monkeypatch.setenv(value["machineAccounts"]["local"]["tokenEnv"], "local-token")
    platform = GitHubPlatform(
        tmp_path,
        AutoMergeWaitingRunner({
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "reviewDecision": "APPROVED",
            "autoMergeRequest": {"enabledAt": "2026-08-21T09:00:00Z"},
        }),
    )

    with pytest.raises(BenchError, match="held by the required approval wall"):
        platform.assert_auto_merge_waiting(value, 321, "a" * 40, 30)


def test_merge_poll_and_sleep_never_exceed_the_remaining_acceptance_budget(tmp_path, monkeypatch):
    value = grant()
    monkeypatch.setenv(value["machineAccounts"]["local"]["tokenEnv"], "local-token")
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(mission_bench.time, "monotonic", lambda: clock[0])

    def advance_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(mission_bench.time, "sleep", advance_sleep)
    runner = MergePollRunner(clock)
    platform = GitHubPlatform(tmp_path, runner)

    with pytest.raises(BenchError, match="acceptance budget"):
        platform.wait_for_merge(value, 321, 1)

    assert runner.timeouts == [1.0]
    assert sleeps == [pytest.approx(0.4)]


def test_changed_paths_include_both_ends_of_an_authority_file_rename(tmp_path):
    platform = GitHubPlatform(tmp_path, DiffRunner())
    platform.worktree_bases[tmp_path.resolve()] = "base-commit"
    paths = platform.changed_paths(grant(), tmp_path)

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
