#!/usr/bin/env python3
"""Apply the board's custom fields, item values and views from the manifest.

Fifty-eight items under a single default Status field is a list, not a board.
This adds the three dimensions the work actually has -- which phase, which area,
what kind of item -- and the views that make them readable.

Field values are *derived* from the labels already on each issue rather than
declared a second time in the manifest. A phase stated in two places is a phase
that will eventually disagree with itself. An item whose label does not map is
left unset and reported, never guessed.

    python3 tooling/gh-project-fields.py --dry-run
    python3 tooling/gh-project-fields.py

Requires `gh` authenticated with the `project` scope. Run after
tooling/gh-plan-sync.py, which creates the issues this reads.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "tooling" / "github-plan.json"

DRY_RUN = False


class GhError(RuntimeError):
    pass


def gh(*args: str) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def graphql(query: str, _typed: dict | None = None, **variables: str):
    """`-f` sends a variable as a string; a GraphQL Int! rejects a quoted one."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-f", f"{key}={value}"]
    for key, value in (_typed or {}).items():
        args += ["-F", f"{key}={value}"]
    result = json.loads(gh(*args) or "null")
    if result and result.get("errors"):
        raise GhError(json.dumps(result["errors"]))
    return result["data"]


def say(action: str, what: str) -> None:
    print(f"  {'would ' if DRY_RUN else ''}{action}: {what}")


def unwrap(doc):
    if isinstance(doc, dict):
        for key in ("fields", "items", "projects"):
            if key in doc:
                return doc[key]
    return doc


# --------------------------------------------------------------------------- fields


def sync_fields(number: str, owner: str, specs: list[dict]) -> dict[str, dict]:
    """Ensure every declared field exists. Returns them by name."""
    print("Fields")
    existing = {
        field["name"]: field
        for field in unwrap(
            json.loads(gh("project", "field-list", number, "--owner", owner, "--format", "json"))
        )
    }
    for spec in specs:
        name = spec["name"]
        if name in existing:
            # A single-select that has gained options in the manifest is updated in
            # place. Skipping it here used to be silent: the field stayed as it was,
            # and every item wanting a new option was then edited with a null option
            # id, which fails. A field that exists is not the same as a field that
            # is correct.
            missing = [
                option for option in spec.get("options", [])
                if option not in {o["name"] for o in existing[name].get("options", [])}
            ]
            if not missing:
                print(f"  ok: {name}")
                continue
            say("add options", f"{name}: {', '.join(missing)}")
            if DRY_RUN:
                continue
            kept = [o["name"] for o in existing[name].get("options", [])]
            args = ["api", "graphql", "-f", f"query={UPDATE_FIELD}",
                    "-f", f"field={existing[name]['id']}"]
            for option in kept + missing:
                args += ["-f", "opts[][name]=" + option, "-f", "opts[][color]=GRAY",
                         "-f", "opts[][description]="]
            gh(*args)
            existing[name] = json.loads(gh(
                "project", "field-list", number, "--owner", owner, "--format", "json"
            ))
            existing = {f["name"]: f for f in unwrap(existing[name])}
            continue
        say("create field", f"{name} ({spec['dataType']})")
        if DRY_RUN:
            continue
        args = [
            "project", "field-create", number, "--owner", owner,
            "--name", name, "--data-type", spec["dataType"], "--format", "json",
        ]
        if spec["dataType"] == "SINGLE_SELECT":
            args += ["--single-select-options", ",".join(spec["options"])]
        existing[name] = json.loads(gh(*args))
    return existing


def option_id(field: dict, value: str) -> str | None:
    for option in field.get("options", []):
        if option["name"] == value:
            return option["id"]
    return None


def derive(spec: dict, labels: list[str]) -> str | None:
    """The value this field takes for an item, read off its labels."""
    source = spec["from"]
    if source == "label":
        for option in spec["options"]:
            if option in labels:
                return option
        return None
    if source.startswith("label:"):
        prefix = source.split(":", 1)[1]
        for label in labels:
            if label.startswith(prefix):
                candidate = label[len(prefix):]
                return candidate if candidate in spec["options"] else None
        return None
    if source == "kind":
        # Most specific first: an acceptance criterion is also a task, and the
        # criterion is the more useful thing to see.
        if "acceptance-criterion" in labels:
            return "acceptance criterion"
        if "epic" in labels:
            return "epic"
        if "task" in labels:
            return "task"
        return None
    raise GhError(f"unknown field source {source!r}")


# ---------------------------------------------------------------------- item values


def sync_values(
    number: str, owner: str, project_id: str, specs: list[dict], fields: dict[str, dict]
) -> None:
    print("Item values")
    items = unwrap(
        json.loads(
            gh("project", "item-list", number, "--owner", owner,
               "--limit", "500", "--format", "json")
        )
    )
    unset: list[str] = []
    changes = 0
    for item in items:
        labels = item.get("labels") or []
        title = item.get("title", "?")
        for spec in specs:
            field = fields.get(spec["name"])
            if field is None:
                continue
            wanted = derive(spec, labels)
            if wanted is None:
                unset.append(f"{spec['name']} on {title[:48]}")
                continue
            # gh reports a set single-select value under the lowercased field name.
            if item.get(spec["name"].lower()) == wanted:
                continue
            changes += 1
            say("set", f"{spec['name']}={wanted} on {title[:48]}")
            if DRY_RUN:
                continue
            gh(
                "project", "item-edit",
                "--id", item["id"],
                "--project-id", project_id,
                "--field-id", field["id"],
                "--single-select-option-id", option_id(field, wanted),
            )
    if not changes:
        print(f"  ok: {len(items)} items already carry every derived value")
    if unset:
        # Reported, never guessed. An item with no area label has no area.
        print(f"  unset ({len(unset)}), left as-is rather than inferred:")
        for entry in unset[:10]:
            print(f"    {entry}")
        if len(unset) > 10:
            print(f"    ... and {len(unset) - 10} more")


# ---------------------------------------------------------------------------- views


VIEWS_QUERY = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      views(first: 20) { nodes { id name filter } }
      fields(first: 50) { nodes { ... on ProjectV2FieldCommon { id name } } }
    }
  }
}
"""

UPDATE_FIELD = """
mutation($field: ID!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $opts}) {
    projectV2Field { ... on ProjectV2SingleSelectField { id name } }
  }
}
"""

CREATE_VIEW = """
mutation($project: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {
  createProjectV2View(input: {projectId: $project, name: $name, layout: $layout}) {
    projectV2View { id name }
  }
}
"""

UPDATE_VIEW = """
mutation($view: ID!, $filter: String!, $visible: [ID!]!) {
  updateProjectV2View(input: {viewId: $view, filter: $filter,
                              configuration: {visibleFieldIds: $visible}}) {
    projectV2View { id name }
  }
}
"""


def sync_views(owner: str, number: str, specs: list[dict]) -> str:
    print("Views")
    data = graphql(VIEWS_QUERY, {"number": number}, owner=owner)[
        "organization"
    ]["projectV2"]
    project_id = data["id"]
    field_ids = {field["name"]: field["id"] for field in data["fields"]["nodes"] if field}
    views = {view["name"]: view for view in data["views"]["nodes"]}

    for spec in specs:
        name = spec["name"]
        view = views.get(name)
        if view is None:
            say("create view", f"{name} ({spec['layout']})")
            if DRY_RUN:
                continue
            view = graphql(
                CREATE_VIEW, project=project_id, name=name, layout=spec["layout"]
            )["createProjectV2View"]["projectV2View"]
        elif view.get("filter") == spec.get("filter", ""):
            print(f"  ok: {name}")
            continue
        else:
            say("update view", name)
            if DRY_RUN:
                continue

        missing = [f for f in spec["visibleFields"] if f not in field_ids]
        if missing:
            print(f"    note: not shown, no such field: {missing}")
        visible = [field_ids[f] for f in spec["visibleFields"] if f in field_ids]
        args = ["api", "graphql", "-f", f"query={UPDATE_VIEW}",
                "-f", f"view={view['id']}", "-f", f"filter={spec.get('filter', '')}"]
        for field_id in visible:
            args += ["-f", f"visible[]={field_id}"]
        gh(*args)

    # Undeclared views are reported, never deleted. A view someone built for
    # themselves is not drift, and losing it would be worse than the clutter of
    # naming it here.
    extra = [name for name in views if name not in {s["name"] for s in specs}]
    if extra:
        print(f"  not declared in the manifest, left alone: {', '.join(extra)}")
    return project_id


# ----------------------------------------------------------------------------- main


def main() -> int:
    global DRY_RUN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--number", default="1", help="project number")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    manifest = json.loads(MANIFEST.read_text())
    owner = manifest["repository"].split("/")[0]
    project = manifest["project"]
    print(f"Board: {project['boardTitle']}{' (dry run)' if DRY_RUN else ''}\n")

    # Fields before views: a view names the fields it shows, so creating it first
    # silently drops any column whose field does not exist yet.
    fields = sync_fields(args.number, owner, project["fields"])
    print()
    project_id = sync_views(owner, args.number, project["views"])
    print()
    sync_values(args.number, owner, project_id, project["fields"], fields)
    print("\nDone." if not DRY_RUN else "\nDry run complete. Nothing changed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
