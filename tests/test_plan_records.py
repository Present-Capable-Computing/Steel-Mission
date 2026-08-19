"""The plan layer is checked, not merely written down.

A project or milestone record that has drifted from its schema, from the other
records, or from the GitHub manifest is worse than no record: it reads as
authoritative while describing work that no longer exists. These tests are what
stop that happening quietly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
PLAN_DIR = REPO_DIR / "plan"
MANIFEST = REPO_DIR / "tooling" / "github-plan.json"

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
