#!/usr/bin/env bash
# Create the Projects v2 board for PRJ-0001 and add every planned issue to it.
#
# Projects v2 needs a token scope that the repository scope does not include.
# This script checks for it first and tells you exactly what to run, rather than
# failing halfway through with a partially populated board.
#
#   tooling/gh-project-bootstrap.sh
#
# Idempotent: an existing board with the same title is reused, and adding an
# issue that is already on the board is a no-op on GitHub's side.

set -euo pipefail

REPO="Present-Capable-Computing/Steel-Mission"
OWNER="Present-Capable-Computing"
TITLE="Steel-Mission — Durable Core (PRJ-0001)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! gh auth status >/dev/null 2>&1; then
  echo "error: gh is not authenticated. Run: gh auth login" >&2
  exit 1
fi

if ! gh api graphql -f query='query{viewer{login}}' >/dev/null 2>&1; then
  echo "error: cannot reach the GitHub GraphQL API." >&2
  exit 1
fi

if ! gh project list --owner "$OWNER" >/dev/null 2>&1; then
  cat >&2 <<'MSG'
error: this token cannot read or write Projects v2.

The repository scope does not cover project boards. Grant the scope and retry:

    gh auth refresh -s project,read:project

If that reports that the token cannot be refreshed, it is a classic personal
access token rather than an OAuth one. Add the `project` scope at
https://github.com/settings/tokens, then re-authenticate:

    gh auth login --with-token < token.txt

Everything else in the plan — labels, milestones, issues, branch protection —
is applied by tooling/gh-plan-sync.py and does not need this scope.
MSG
  exit 2
fi

# gh has wrapped project listings in a {"projects": [...]} envelope in some
# versions and returned a bare array in others. Read either rather than pinning
# a gh version this script cannot check for.
unwrap() {
  python3 -c '
import json, sys
doc = json.load(sys.stdin)
items = doc["projects"] if isinstance(doc, dict) and "projects" in doc else doc
if isinstance(items, dict):
    items = [items]
key, want = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
for item in items:
    if want is None or item.get("title") == want:
        print(item[key])
        break
' "$@"
}

number="$(gh project list --owner "$OWNER" --format json | unwrap number "$TITLE")"

if [ -z "$number" ]; then
  echo "Creating board: $TITLE"
  number="$(gh project create --owner "$OWNER" --title "$TITLE" --format json | unwrap number)"
else
  echo "Reusing board #$number"
fi

url="$(gh project view "$number" --owner "$OWNER" --format json | unwrap url)"

# The board carries its record id, so that someone who arrives at the board first
# can find the record that governs it. plan/README.md states this link; setting it
# here is what makes the statement true.
description="$(python3 -c "import json;print(json.load(open('$ROOT/tooling/github-plan.json'))['project']['boardDescription'])")"
gh project edit "$number" --owner "$OWNER" --description "$description" >/dev/null

echo "Adding planned issues to the board"
# One request per label. Repeated --label flags are an AND, not an OR, and no
# issue carries both task and epic, so filtering on both at once returns nothing.
{
  gh issue list --repo "$REPO" --state open --limit 200 --label task --json number --jq '.[].number'
  gh issue list --repo "$REPO" --state open --limit 200 --label epic --json number --jq '.[].number'
} | sort -un | while read -r issue; do
      [ -n "$issue" ] || continue
      gh project item-add "$number" --owner "$OWNER" \
        --url "https://github.com/$REPO/issues/$issue" >/dev/null
    done

# An organisation project does not appear in a repository's Projects tab until it
# is linked. Creating the board and adding every issue to it is not enough: without
# this, someone looking for the project where it belongs does not find it.
echo "Linking the board to the repository"
project_id="$(gh api graphql -F n="$number" \
  -f query='query($n:Int!){organization(login:"'"$OWNER"'"){projectV2(number:$n){id}}}' \
  -q .data.organization.projectV2.id)"
repo_id="$(gh api "repos/$REPO" -q .node_id)"
gh api graphql -f query='mutation($p:ID!,$r:ID!){linkProjectV2ToRepository(input:{projectId:$p,repositoryId:$r}){repository{name}}}' \
  -f p="$project_id" -f r="$repo_id" >/dev/null 2>&1 || echo "  already linked"

# A board created through the API is private by default. Every item on this one is
# a public issue on a public repository, so a private board hides a view of public
# information without protecting anything. Declared in the manifest, not assumed:
# a board for a private repository would set this to private.
visibility="$(python3 -c "import json;print(json.load(open('$ROOT/tooling/github-plan.json'))['project'].get('boardVisibility','private'))")"
echo "Setting board visibility: $visibility"
gh api graphql -F public="$([ "$visibility" = public ] && echo true || echo false)" \
  -f p="$project_id" \
  -f query='mutation($p:ID!,$public:Boolean!){updateProjectV2(input:{projectId:$p,public:$public}){projectV2{public}}}' >/dev/null

echo "Applying fields, item values and views"
python3 "$ROOT/tooling/gh-project-fields.py" --number "$number"

echo "Recording the board on the project record"
python3 "$ROOT/tooling/gh-plan-sync.py" --board-url "$url"

cat <<MSG

Board: $url

The board is a working surface. plan/PRJ-0001.json is the source of truth for the
shape of this work; where the two disagree, the record wins and the board is
corrected.
MSG
