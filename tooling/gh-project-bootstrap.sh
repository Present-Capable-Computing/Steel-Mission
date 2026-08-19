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

number="$(gh project list --owner "$OWNER" --format json \
  | python3 -c "import json,sys;t=sys.argv[1];print(next((str(p['number']) for p in json.load(sys.stdin)['projects'] if p['title']==t),''))" "$TITLE")"

if [ -z "$number" ]; then
  echo "Creating board: $TITLE"
  number="$(gh project create --owner "$OWNER" --title "$TITLE" --format json \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['number'])")"
else
  echo "Reusing board #$number"
fi

url="$(gh project view "$number" --owner "$OWNER" --format json \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['url'])")"

echo "Adding planned issues to the board"
gh issue list --repo "$REPO" --state open --limit 200 \
  --label task --label epic --json number \
  | python3 -c "import json,sys;[print(i['number']) for i in json.load(sys.stdin)]" \
  | while read -r issue; do
      gh project item-add "$number" --owner "$OWNER" \
        --url "https://github.com/$REPO/issues/$issue" >/dev/null
    done

echo "Recording the board on the project record"
python3 "$ROOT/tooling/gh-plan-sync.py" --board-url "$url"

cat <<MSG

Board: $url

The board is a working surface. plan/PRJ-0001.json is the source of truth for the
shape of this work; where the two disagree, the record wins and the board is
corrected.
MSG
