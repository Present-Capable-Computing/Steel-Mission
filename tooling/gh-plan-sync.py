#!/usr/bin/env python3
"""Apply tooling/github-plan.json to the GitHub repository.

Idempotent by construction: every object is looked up by title before it is
created, and an existing object is updated in place. Re-running after an edit to
the manifest converges rather than duplicating, which matters because a duplicated
issue is how a plan goes stale without anyone noticing.

    python3 tooling/gh-plan-sync.py --dry-run
    python3 tooling/gh-plan-sync.py

Requires the `gh` command line, authenticated with repository scope. The Projects
v2 board is not created here: it needs a scope the repository scope does not
include. See tooling/gh-project-bootstrap.sh.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "tooling" / "github-plan.json"
PLAN_DIR = REPO_ROOT / "plan"

DRY_RUN = False


class GhError(RuntimeError):
    pass


def gh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    if check and proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def gh_json(*args: str):
    return json.loads(gh(*args) or "null")


def api(
    path: str,
    *,
    method: str = "GET",
    fields: dict | None = None,
    typed_fields: dict | None = None,
):
    """`fields` are sent as strings; `typed_fields` keep their JSON type.

    The sub-issue and dependency endpoints take an integer node id and reject a
    quoted one, so the distinction is not cosmetic.
    """
    args = ["api", "-X", method, path]
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    for key, value in (typed_fields or {}).items():
        args += ["-F", f"{key}={value}"]
    return json.loads(gh(*args) or "null")


def say(action: str, what: str) -> None:
    prefix = "would " if DRY_RUN else ""
    print(f"  {prefix}{action}: {what}")


# --------------------------------------------------------------------------- labels


def sync_labels(repo: str, labels: list[dict]) -> None:
    print("Labels")
    existing = {
        label["name"]: label
        for label in api(f"repos/{repo}/labels?per_page=100")
    }
    for spec in labels:
        name = spec["name"]
        current = existing.get(name)
        if current is None:
            say("create label", name)
            if not DRY_RUN:
                api(
                    f"repos/{repo}/labels",
                    method="POST",
                    fields={
                        "name": name,
                        "color": spec["color"],
                        "description": spec.get("description", ""),
                    },
                )
            continue
        drifted = current.get("color") != spec["color"] or (
            current.get("description") or ""
        ) != spec.get("description", "")
        if drifted:
            say("update label", name)
            if not DRY_RUN:
                api(
                    f"repos/{repo}/labels/{name}",
                    method="PATCH",
                    fields={
                        "new_name": name,
                        "color": spec["color"],
                        "description": spec.get("description", ""),
                    },
                )
        else:
            print(f"  ok: {name}")


# ----------------------------------------------------------------------- milestones


def sync_milestones(repo: str, milestones: list[dict]) -> dict[str, tuple[int, str]]:
    print("Milestones")
    existing = {
        ms["title"]: ms
        for ms in api(f"repos/{repo}/milestones?state=all&per_page=100")
    }
    numbers: dict[str, tuple[int, str]] = {}
    for spec in milestones:
        title = spec["title"]
        current = existing.get(title)
        fields = {
            "title": title,
            "description": spec["description"],
            "state": spec.get("state", "open"),
        }
        # An omitted due date means "no date", and must be sent as one. Leaving it
        # out of the payload leaves the server's previous value in place, so a
        # milestone whose date was deliberately cleared keeps showing the old one.
        typed = {}
        if spec.get("due_on"):
            fields["due_on"] = spec["due_on"]
        else:
            typed["due_on"] = "null"
        if current is None:
            say("create milestone", title)
            if DRY_RUN:
                continue
            created = api(f"repos/{repo}/milestones", method="POST", fields=fields,
                          typed_fields=typed)
            numbers[spec["recordId"]] = (created["number"], title)
        else:
            number = current["number"]
            numbers[spec["recordId"]] = (number, title)
            drifted = (current.get("description") != spec["description"]
                       or (current.get("due_on") or None) != (spec.get("due_on") or None)
                       or current.get("state") != spec.get("state", "open"))
            if drifted:
                say("update milestone", title)
                if not DRY_RUN:
                    api(
                        f"repos/{repo}/milestones/{number}",
                        method="PATCH",
                        fields=fields,
                        typed_fields=typed,
                    )
            else:
                print(f"  ok: {title} (#{number})")
    return numbers


# --------------------------------------------------------------------------- issues


_ISSUE_CACHE: dict[str, dict] | None = None


def load_issues(repo: str) -> dict[str, dict]:
    """Every issue in the repository, by title.

    Listed once and held. The alternative -- a lookup per manifest entry -- is
    quadratic in the number of issues and runs into the rate limit long before
    the plan finishes applying. Search is deliberately not used: it is eventually
    consistent, and a stale miss creates a duplicate.
    """
    global _ISSUE_CACHE
    if _ISSUE_CACHE is None:
        _ISSUE_CACHE = {}
        page = 1
        while True:
            batch = api(f"repos/{repo}/issues?state=all&per_page=100&page={page}")
            if not batch:
                break
            for issue in batch:
                if "pull_request" not in issue:
                    _ISSUE_CACHE[issue["title"]] = issue
            page += 1
    return _ISSUE_CACHE


def find_issue(repo: str, title: str) -> dict | None:
    return load_issues(repo).get(title)


def technical_plan_for(milestone: str) -> str:
    """The design document of the project that owns this milestone.

    Hardcoding one path sent every issue of a second project at the first
    project's design -- and on a paused project, at a design nobody should be
    working from.
    """
    record = PLAN_DIR / f"{milestone}.json"
    if record.exists():
        project = json.loads(record.read_text()).get("projectId")
        owner = PLAN_DIR / f"{project}.json"
        if owner.exists():
            plan = json.loads(owner.read_text()).get("metadata", {}).get("technicalPlan")
            if plan:
                return plan
    return "docs/workplan.md"


def epic_body(spec: dict) -> str:
    # No task list here. The tasks are real sub-issues, so GitHub renders them
    # with their state and a progress count. A checkbox list beside that is a
    # second copy of the same relationship, maintained by hand, that goes wrong.
    return (
        f"## Outcome\n\n{spec['outcome']}\n\n"
        f"## Explicitly not in scope\n\n{spec['notInScope']}\n\n"
        f"## How the milestone is judged complete\n\n{spec['done']}\n\n"
        "---\n\n"
        f"Milestone record: `plan/{spec['milestone']}.json` · "
        "Workplan: `docs/workplan.md` · Design: `" + technical_plan_for(spec["milestone"]) + "`\n\n"
        "_Generated from `tooling/github-plan.json`. Edit the manifest and re-run "
        "`tooling/gh-plan-sync.py`; edits made here are overwritten._"
    )


def task_body(spec: dict) -> str:
    # Neither the epic nor the blockers are written here. Both are real GitHub
    # relationships -- a parent issue and a blocked-by dependency -- so the issue
    # header shows them and the board can filter on them. Restating them in the
    # body would be a copy that drifts.
    parts = [
        f"## Requirement\n\n{spec['requirement']}\n",
        f"## Acceptance evidence\n\n{spec['acceptance']}\n",
        f"## Surface\n\n{spec['surface']}\n",
    ]
    parts.append(
        "---\n\n"
        f"Milestone record: `plan/{spec['milestone']}.json` · "
        "Workplan: `docs/workplan.md` · Design: `" + technical_plan_for(spec["milestone"]) + "`\n\n"
        "This task is done when the definition of done in `docs/workplan.md` §5 is "
        "satisfied in full. A `DEV-NNNNNN` id appears here only once the control "
        "plane mints one.\n\n"
        "_Generated from `tooling/github-plan.json`. Edit the manifest and re-run "
        "`tooling/gh-plan-sync.py`; edits made here are overwritten._"
    )
    return "\n".join(parts)


def ensure_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    milestone: tuple[int, str] | None,
) -> int | None:
    milestone_number = milestone[0] if milestone else None
    milestone_title = milestone[1] if milestone else None
    current = find_issue(repo, title)
    if current is None:
        say("create issue", title)
        if DRY_RUN:
            return None
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels:
            args += ["--label", label]
        if milestone_title is not None:
            # gh resolves a milestone by title, not by number.
            args += ["--milestone", milestone_title]
        url = gh(*args).strip().splitlines()[-1]
        number = int(url.rsplit("/", 1)[-1])
        # Keep the cache truthful, or the second pass creates this issue again.
        load_issues(repo)[title] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": label} for label in labels],
            "milestone": {"number": milestone_number} if milestone_number else None,
        }
        return number

    number = current["number"]
    current_labels = {label["name"] for label in current.get("labels", [])}
    current_milestone = (current.get("milestone") or {}).get("number")
    drifted = (
        (current.get("body") or "").strip() != body.strip()
        or current_labels != set(labels)
        or current_milestone != milestone_number
    )
    if drifted:
        say("update issue", f"#{number} {title}")
        if not DRY_RUN:
            api(f"repos/{repo}/issues/{number}", method="PATCH", fields={"body": body})
            edits: list[str] = []
            for label in set(labels) - current_labels:
                edits += ["--add-label", label]
            for label in current_labels - set(labels):
                edits += ["--remove-label", label]
            if milestone_title is not None and current_milestone != milestone_number:
                edits += ["--milestone", milestone_title]
            if edits:
                gh("issue", "edit", str(number), "--repo", repo, *edits)
            cached = load_issues(repo)[title]
            cached["body"] = body
            cached["labels"] = [{"name": label} for label in labels]
            cached["milestone"] = (
                {"number": milestone_number} if milestone_number else None
            )
    else:
        print(f"  ok: #{number} {title}")
    return number


# ------------------------------------------------------------------ relationships


def _issue_node_id(repo: str, number: int) -> str:
    return api(f"repos/{repo}/issues/{number}")["id"]


def sync_sub_issues(
    repo: str,
    manifest: dict,
    epic_numbers: dict[str, int],
    issue_numbers: dict[str, int],
) -> None:
    """Attach each task to its epic as a real sub-issue.

    GitHub then renders the epic's children with their state and a progress
    count, and a task shows its parent in the header. The alternative -- a
    checkbox list in the epic body -- is a second copy of the same relationship
    that nothing keeps true.
    """
    print("Sub-issues")
    for epic in manifest["epics"]:
        parent = epic_numbers.get(epic["key"])
        if parent is None:
            continue
        wanted = [
            issue_numbers[task["key"]]
            for task in manifest["issues"]
            if task.get("epic") == epic["key"] and task["key"] in issue_numbers
        ]
        if DRY_RUN:
            print(f"  would attach {len(wanted)} tasks to #{parent}")
            continue
        present = {
            child["number"]
            for child in api(f"repos/{repo}/issues/{parent}/sub_issues")
        }
        missing = [number for number in wanted if number not in present]
        if not missing:
            print(f"  ok: #{parent} has {len(present)} sub-issues")
            continue
        for number in missing:
            say("attach", f"#{number} under #{parent}")
            api(
                f"repos/{repo}/issues/{parent}/sub_issues",
                method="POST",
                typed_fields={"sub_issue_id": _issue_node_id(repo, number)},
            )


def sync_dependencies(
    repo: str, manifest: dict, issue_numbers: dict[str, int]
) -> None:
    """Record each declared dependency as a blocked-by relationship.

    The sequencing is already asserted in tests/test_plan_records.py, which
    refuses a dependency on work scheduled later. Recording it here is what makes
    it visible to the person picking up the task, rather than only to the test.
    """
    print("Dependencies")
    for spec in manifest["issues"]:
        blocked = issue_numbers.get(spec["key"])
        wanted = [
            issue_numbers[key]
            for key in spec.get("dependsOn", [])
            if key in issue_numbers
        ]
        if blocked is None or not wanted:
            continue
        if DRY_RUN:
            print(f"  would block #{blocked} on {wanted}")
            continue
        present = {
            blocker["number"]
            for blocker in api(
                f"repos/{repo}/issues/{blocked}/dependencies/blocked_by"
            )
        }
        missing = [number for number in wanted if number not in present]
        if not missing:
            print(f"  ok: #{blocked} blocked by {sorted(present)}")
            continue
        for number in missing:
            say("block", f"#{blocked} on #{number}")
            api(
                f"repos/{repo}/issues/{blocked}/dependencies/blocked_by",
                method="POST",
                typed_fields={"issue_id": _issue_node_id(repo, number)},
            )


# ---------------------------------------------------------------- plan record write


def write_back(milestone_numbers: dict[str, tuple[int, str]], board_url: str | None) -> None:
    print("Plan records")
    for record_id, (number, _title) in sorted(milestone_numbers.items()):
        path = PLAN_DIR / f"{record_id}.json"
        if not path.exists():
            print(f"  missing: {path.name}")
            continue
        record = json.loads(path.read_text())
        if record.get("metadata", {}).get("githubMilestone") == number:
            print(f"  ok: {record_id} -> milestone #{number}")
            continue
        say("write back", f"{record_id}.metadata.githubMilestone = {number}")
        if not DRY_RUN:
            record.setdefault("metadata", {})["githubMilestone"] = number
            path.write_text(json.dumps(record, indent=2) + "\n")

    if board_url:
        path = PLAN_DIR / "PRJ-0001.json"
        record = json.loads(path.read_text())
        if record["metadata"].get("githubProject") != board_url:
            say("write back", f"PRJ-0001.metadata.githubProject = {board_url}")
            if not DRY_RUN:
                record["metadata"]["githubProject"] = board_url
                path.write_text(json.dumps(record, indent=2) + "\n")


# ----------------------------------------------------------------------------- main


def main() -> int:
    global DRY_RUN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--board-url", help="record a Projects v2 board URL on PRJ-0001")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    manifest = json.loads(MANIFEST.read_text())
    repo = manifest["repository"]
    print(f"Repository: {repo}{' (dry run)' if DRY_RUN else ''}\n")

    sync_labels(repo, manifest["labels"])
    print()
    milestone_numbers = sync_milestones(repo, manifest["milestones"])
    print()

    print("Epics")
    epic_numbers: dict[str, int] = {}
    for spec in manifest["epics"]:
        number = ensure_issue(
            repo,
            spec["title"],
            epic_body(spec),
            spec["labels"],
            milestone_numbers.get(spec["milestone"]),
        )
        if number is not None:
            epic_numbers[spec["key"]] = number
    print()

    print("Tasks")
    issue_numbers: dict[str, int] = {}
    for spec in manifest["issues"]:
        number = ensure_issue(
            repo,
            spec["title"],
            task_body(spec),
            spec["labels"],
            milestone_numbers.get(spec["milestone"]),
        )
        if number is not None:
            issue_numbers[spec["key"]] = number
    print()

    sync_sub_issues(repo, manifest, epic_numbers, issue_numbers)
    print()
    sync_dependencies(repo, manifest, issue_numbers)
    print()

    write_back(milestone_numbers, args.board_url)
    print("\nDone." if not DRY_RUN else "\nDry run complete. Nothing changed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
