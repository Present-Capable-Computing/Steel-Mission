"""The plan layer is checked, not merely written down.

A project or milestone record that has drifted from its schema, from the other
records, or from the GitHub manifest is worse than no record: it reads as
authoritative while describing work that no longer exists. These tests are what
stop that happening quietly.
"""
from __future__ import annotations

import json
import re
import runpy
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
PLAN_DIR = REPO_DIR / "plan"
MANIFEST = REPO_DIR / "tooling" / "github-plan.json"
WORKPLAN = REPO_DIR / "docs" / "workplan.md"
AGENT_GUIDE = REPO_DIR / "AGENTS.md"

sys.path.insert(0, str(REPO_DIR))
from adapters import schema_check  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _projects() -> list[Path]:
    return sorted(PLAN_DIR.glob("PRJ-*.json"))


def _milestones() -> list[Path]:
    return sorted(PLAN_DIR.glob("MS-*.json"))


def test_plan_directory_is_tracked_not_ignored():
    # tasks/ is ignored, so plan records deliberately do not live there. A record
    # that does not commit is a record that goes stale without anyone noticing.
    ignore = (REPO_DIR / ".gitignore").read_text().splitlines()
    assert "plan/" not in [line.strip() for line in ignore]
    assert _projects(), "no project record found"
    assert _milestones(), "no milestone records found"


@pytest.mark.parametrize("path", _projects(), ids=lambda p: p.name)
def test_project_record_validates(path: Path):
    errors = schema_check.validate(_load(path), "canonical/project-v1.json")
    assert errors == [], f"{path.name}: {errors}"


@pytest.mark.parametrize("path", _milestones(), ids=lambda p: p.name)
def test_milestone_record_validates(path: Path):
    errors = schema_check.validate(_load(path), "canonical/milestone-v1.json")
    assert errors == [], f"{path.name}: {errors}"


def test_project_and_milestones_reference_each_other():
    for project_path in _projects():
        project = _load(project_path)
        declared = set(project.get("milestoneIds", []))
        owned = {
            _load(p)["milestoneId"]
            for p in _milestones()
            if _load(p).get("projectId") == project["projectId"]
        }
        assert declared == owned, (
            f"{project_path.name} declares {sorted(declared)} but owns {sorted(owned)}; "
            "a milestone listed in neither direction is work nobody is tracking"
        )


def test_every_milestone_names_its_boundary():
    # Scope grows by omission. A milestone without a stated boundary has no way
    # to refuse work, and a milestone without completion evidence cannot be closed
    # on anything but a feeling.
    for path in _milestones():
        record = _load(path)
        assert record.get("outcome"), f"{path.name}: no outcome"
        assert record.get("notInScope"), f"{path.name}: no stated boundary"
        assert record.get("completionEvidence"), f"{path.name}: no completion evidence"


def test_no_milestone_state_reads_as_a_verification_outcome():
    # A milestone schedules work. If one of its states could be read as saying the
    # work is correct, membership in the milestone would silently upgrade every
    # task in it.
    forbidden = {"VERIFIED", "PASSED", "PASS", "APPROVED", "ACCEPTED", "PROVEN"}
    schema = schema_check.load_schema("canonical/milestone-v1.json")
    states = set(schema["properties"]["state"]["enum"])
    assert not states & forbidden
    for path in _milestones():
        assert _load(path)["state"] in states


def test_documents_the_records_point_at_exist():
    for project_path in _projects():
        metadata = _load(project_path).get("metadata", {})
        for key in ("planDocument", "technicalPlan"):
            target = metadata.get(key)
            assert target, f"{project_path.name}: {key} not recorded"
            assert (REPO_DIR / target).exists(), f"{project_path.name}: {target} missing"


def test_manifest_is_internally_consistent():
    manifest = _load(MANIFEST)
    label_names = {label["name"] for label in manifest["labels"]}
    milestone_ids = {ms["recordId"] for ms in manifest["milestones"]}
    epic_keys = {epic["key"] for epic in manifest["epics"]}
    issue_keys = {issue["key"] for issue in manifest["issues"]}

    assert len(issue_keys) == len(manifest["issues"]), "duplicate issue key"

    for item in manifest["epics"] + manifest["issues"]:
        assert item["milestone"] in milestone_ids, f"{item['key']}: unknown milestone"
        unknown = set(item["labels"]) - label_names
        assert not unknown, f"{item['key']}: labels not declared: {sorted(unknown)}"

    for issue in manifest["issues"]:
        assert issue["epic"] in epic_keys, f"{issue['key']}: unknown epic"
        missing = set(issue.get("dependsOn", [])) - issue_keys
        assert not missing, f"{issue['key']}: depends on unknown {sorted(missing)}"


def test_manifest_milestones_match_the_records():
    manifest = _load(MANIFEST)
    from_manifest = {ms["recordId"] for ms in manifest["milestones"]}
    from_records = {_load(p)["milestoneId"] for p in _milestones()}
    assert from_manifest == from_records, (
        "the GitHub manifest and the milestone records disagree about which "
        "milestones exist; the records are the source of truth and the manifest "
        "is corrected"
    )


def test_manifest_milestone_states_match_the_records():
    manifest = _load(MANIFEST)
    manifest_states = {
        item["recordId"]: item.get("state", "open")
        for item in manifest["milestones"]
    }
    for path in _milestones():
        record = _load(path)
        expected = "closed" if record["state"] in {"COMPLETE", "ABANDONED"} else "open"
        assert manifest_states[record["milestoneId"]] == expected, (
            f"{record['milestoneId']} is {record['state']} in its record but "
            f"{manifest_states[record['milestoneId']]} in the GitHub projection"
        )


def test_plan_sync_repairs_milestone_state_only_drift():
    sync = runpy.run_path(str(REPO_DIR / "tooling" / "gh-plan-sync.py"))
    calls = []

    def fake_api(path, *, method="GET", fields=None, typed_fields=None):
        if method == "GET":
            return [{
                "number": 12,
                "title": "U5 - Review findings",
                "description": "same description",
                "due_on": "2026-08-22T23:59:59Z",
                "state": "open",
            }]
        calls.append((path, method, fields, typed_fields))
        return {}

    globals_ = sync["sync_milestones"].__globals__
    globals_["api"] = fake_api
    sync["sync_milestones"]("owner/repository", [{
        "recordId": "MS-0012",
        "title": "U5 - Review findings",
        "description": "same description",
        "due_on": "2026-08-22T23:59:59Z",
        "state": "closed",
    }])

    assert len(calls) == 1
    assert calls[0][0] == "repos/owner/repository/milestones/12"
    assert calls[0][1] == "PATCH"
    assert calls[0][2]["state"] == "closed"


def test_project_state_is_consistent_on_every_working_surface():
    projects = {_load(path)["projectId"]: _load(path) for path in _projects()}
    active = [project_id for project_id, record in projects.items() if record["state"] == "ACTIVE"]
    assert len(active) == 1, f"expected one active project, found {active}"

    active_project = active[0]
    agent_guide = AGENT_GUIDE.read_text()
    workplan = WORKPLAN.read_text()
    manifest = _load(MANIFEST)
    labels = {item["name"]: item["description"] for item in manifest["labels"]}
    board_description = manifest["project"]["boardDescription"]

    assert f"active project is {active_project}".lower() in agent_guide.lower()
    assert f"Active project: `{active_project}`." in workplan
    for project_id, record in projects.items():
        state = record["state"].lower()
        assert labels[f"prj:{project_id}"].endswith(f"({state})")
        assert re.search(rf"{project_id}[^.;]*\({state}\)", board_description)


def test_ms_0012_epic_carries_all_four_review_findings():
    manifest = _load(MANIFEST)
    issues = [item for item in manifest["issues"] if item["milestone"] == "MS-0012"]
    epic = next(item for item in manifest["epics"] if item["key"] == "epic-u5")
    body_source = " ".join(epic[key] for key in ("outcome", "notInScope", "done"))

    assert len(issues) == 4
    assert epic["outcome"].startswith("The four findings")
    for finding in (
        "front-end toolchain",
        "domain capability",
        "version-controlled configuration",
        "loopback",
    ):
        assert finding in body_source


def test_every_issue_states_a_requirement_and_its_evidence():
    # The control plane refuses a requirement no human has written. A planned task
    # that carries no outcome and no acceptance evidence cannot enter the pipeline,
    # so it is not a task yet.
    for issue in _load(MANIFEST)["issues"]:
        assert issue.get("requirement"), f"{issue['key']}: no requirement"
        assert issue.get("acceptance"), f"{issue['key']}: no acceptance evidence"
        assert issue.get("surface"), f"{issue['key']}: no surface named"


def test_dependencies_do_not_run_backwards_across_milestones():
    # A task may depend on an earlier milestone's task. It must never depend on a
    # later one: that is a sequencing error that only shows up as a blocked task
    # halfway through the phase.
    manifest = _load(MANIFEST)
    order = {ms["recordId"]: index for index, ms in enumerate(manifest["milestones"])}
    by_key = {issue["key"]: issue for issue in manifest["issues"]}
    for issue in manifest["issues"]:
        for dependency in issue.get("dependsOn", []):
            assert order[by_key[dependency]["milestone"]] <= order[issue["milestone"]], (
                f"{issue['key']} depends on {dependency}, which is scheduled later"
            )


def test_workplan_records_the_frontend_dependency_budget_and_audit_threshold():
    workplan = WORKPLAN.read_text()

    assert "exactly three direct front-end packages" in workplan
    assert "exact version" in workplan
    assert "npm audit --audit-level=high" in workplan


def test_prj_0001_rescope_is_dated_and_its_estimate_delta_is_explicit():
    project = _load(PLAN_DIR / "PRJ-0001.json")
    estimate = project["metadata"]["estimate"]
    expected_targets = {
        "MS-0001": "2026-08-27",
        "MS-0013": "2026-09-03",
        "MS-0014": "2026-09-17",
        "MS-0015": "2026-09-24",
        "MS-0002": "2026-10-15",
        "MS-0003": "2026-11-12",
        "MS-0004": "2026-11-26",
        "MS-0005": "2026-12-10",
        "MS-0006": "2026-12-23",
    }

    # The invariant is that the rescope was dated and the estimate re-defended with
    # its delta printed -- not that the project is ACTIVE right now. A project may
    # pause again, and pinning the current state would make an honest pause look
    # like a regression.
    assert project["state"] in {"ACTIVE", "PAUSED"}
    if project["state"] == "PAUSED":
        assert project["metadata"].get("pauseReason"), (
            "a paused project must record why, or the pause reads as drift"
        )
    assert project["metadata"]["rescopedAt"] == "2026-08-20"
    assert estimate["redefendedAt"] == "2026-08-20"
    assert estimate["baselineFocusedDays"] == 16.1
    assert estimate["focusedDays"] == 33.3
    assert estimate["deltaFocusedDays"] == 17.2
    assert "17.2" in estimate["redefense"], "the delta is printed, never absorbed"
    assert "+1.8" in estimate["redefense"], (
        "the founder-identity correction is printed as its own delta, not absorbed"
    )
    for milestone_id, target_date in expected_targets.items():
        milestone = _load(PLAN_DIR / f"{milestone_id}.json")
        assert milestone["targetDate"] == target_date
        if milestone_id in {"MS-0013", "MS-0014", "MS-0015"}:
            # Created at the rescope; their dates were set, not reset.
            assert milestone["createdAt"].startswith("2026-08-20")
        else:
            assert milestone["metadata"]["targetDateResetAt"] == "2026-08-20"


def test_prj_0001_mission_pipeline_is_recorded_on_every_surface():
    # D8 delegates in-mission authority; D9 makes review provenance real. A rule
    # this consequential that lives in only one document is a rule that drifts.
    project = _load(PLAN_DIR / "PRJ-0001.json")
    decisions = {item["id"]: item for item in project["metadata"]["decisions"]}
    workplan = WORKPLAN.read_text()

    for decision_id in ("PRJ-0001-D7", "PRJ-0001-D8", "PRJ-0001-D9"):
        assert decisions[decision_id]["status"] == "BINDING"
    assert "mission grant" in decisions["PRJ-0001-D8"]["decision"]
    assert "machine account" in decisions["PRJ-0001-D9"]["decision"]
    assert "granted mission" in workplan, "the workplan carries the mission rules"
    assert "machine account" in workplan
    # The pipeline's stages are recorded, and the schema authority boundary holds:
    # missions never merge changes to authority-owned schema paths on their own.
    assert "schemas/canonical" in decisions["PRJ-0001-D9"]["decision"]
    manifest = _load(MANIFEST)
    bench = next(i for i in manifest["issues"] if i["key"] == "c1-mission-pipeline-bench")
    assert "security-review" in bench["labels"]


def test_session_guardrails_are_recorded_on_every_surface():
    # The #185 spiral: a session found its blocker, kept building for five
    # hours, crossed a trust boundary, and grew a thin bench past four thousand
    # lines. Each guardrail below exists because prose alone did not stop it.
    workplan = WORKPLAN.read_text()
    for phrase in (
        "Reachability before work",
        "Session ceilings end the session",
        "An issue names its paths",
        "Reviews converge or escalate",
        "Acceptance runs where the code runs",
    ):
        assert phrase in workplan, f"working agreement lost the rule: {phrase}"

    manifest = _load(MANIFEST)
    by_key = {issue["key"]: issue for issue in manifest["issues"]}

    # Every task issue carries a wall, not a hand-picked subset: the rule says
    # "each agent-workable issue", and a wall that exists only where someone
    # remembered to add one is not a wall. Walls are refined per issue at claim
    # time through a manifest change, never silently widened by a session.
    for key, issue in by_key.items():
        if "epic" in issue["labels"]:
            continue
        wall = issue.get("allowedPaths")
        assert isinstance(wall, list) and wall, f"{key}: no path wall declared"

    bench = by_key["c1-mission-pipeline-bench"]
    assert "c1-founder-machine-accounts" in bench["dependsOn"], (
        "the bench must be blocked on the Founder's machine-account act, "
        "or a session starts work whose acceptance is unreachable"
    )
    ceiling = re.search(r"stays under (\d+) lines excluding tests", bench["acceptance"])
    assert ceiling and ceiling.group(1) == "2200", (
        "the active ceiling is pinned by its operative sentence, not by a "
        "substring the history paragraph would also satisfy"
    )
    assert "700" in bench["acceptance"], (
        "the original ceiling stays printed; a threshold history that vanishes "
        "is a threshold that was never really revised"
    )
    assert set(bench["allowedPaths"]) == {"tooling/", "tests/"}, (
        "the bench never enters product code; that is the D7 boundary"
    )
    # The Founder's operating model of 2026-08-21: every issue runs the
    # pipeline; a security-review label routes the acceptance stage into a
    # security review, and only a finding reaches the Founder.
    workplan = WORKPLAN.read_text()
    assert "escalates to the Founder only on a finding" in workplan
    assert "implementation-grade plan" in workplan
    assert "only on a finding" in bench["requirement"]
    assert "refuses security-review" not in bench["requirement"]
