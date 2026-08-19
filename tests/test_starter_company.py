"""The bundled company is launch data, so its internal links are a contract.

These checks keep the first-start manifest, normalized runtime registries, and
the active project inventory aligned. A starter set that opens but points at
missing sources is worse than an empty setup because it looks authoritative.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent.parent
STARTER_DIR = REPO_DIR / "starter-company"
MANIFEST_PATH = STARTER_DIR / "steel-mission-first-start-knowledge-v1.json"
PORTFOLIO_PATH = STARTER_DIR / "portfolio.json"
ISSUE_CATALOG_PATH = REPO_DIR / "tooling" / "github-plan.json"

sys.path.insert(0, str(REPO_DIR))
from adapters import schema_check  # noqa: E402
from adapters import codex_adapter  # noqa: E402


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_first_start_manifest_validates_against_canonical_schema():
    errors = schema_check.validate(
        _load(MANIFEST_PATH),
        "canonical/enterprise-knowledge-first-start-v1.json",
    )
    assert errors == []


def test_first_start_sources_exist_and_stay_inside_read_only_locations():
    manifest = _load(MANIFEST_PATH)
    roots = {
        "steel-mission-starter-company": STARTER_DIR.resolve(),
        "steel-mission-repository": REPO_DIR.resolve(),
    }
    locations = {item["locationId"]: item for item in manifest["sourceLocations"]}
    assert set(locations) == set(roots)
    assert all(item["accessMode"] == "read-only" for item in locations.values())

    source_ids: set[str] = set()
    for source in manifest["sources"]:
        source_id = source["sourceId"]
        assert source_id not in source_ids, f"duplicate source id {source_id}"
        source_ids.add(source_id)

        root = roots[source["locationId"]]
        target = (root / source["relativePath"]).resolve()
        assert target == root or root in target.parents, f"{source_id} escapes {root}"
        if source["nodeType"] == "file":
            assert target.is_file(), f"{source_id} does not resolve to a file: {target}"
        else:
            assert target.is_dir(), f"{source_id} does not resolve to a directory: {target}"


def test_foundations_capabilities_owners_and_references_are_complete():
    manifest = _load(MANIFEST_PATH)
    source_ids = {item["sourceId"] for item in manifest["sources"]}
    user_ids = {item["id"] for item in _load(REPO_DIR / "config" / "users.json")["users"]}

    foundations = {item["category"]: item for item in manifest["coreKnowledge"]}
    assert set(foundations) == {
        "operating-context",
        "operating-doctrine",
        "organization-workflow",
    }
    for foundation in foundations.values():
        assert set(foundation["sourceIds"]) <= source_ids
        assert set(foundation.get("ownerIds", [])) <= user_ids

    capabilities = {item["key"]: item for item in manifest["capabilities"]}
    assert set(capabilities) == {f"DC{index:02d}" for index in range(1, 14)}
    for capability in capabilities.values():
        assert set(capability["ownerIds"]) <= user_ids
        for facet in ("role", "authority", "knowledge"):
            references = capability["sources"][facet]
            assert references, f"{capability['key']} has no {facet} coverage"
            assert set(references) <= source_ids

    for source in manifest["sources"]:
        assert source["ownerId"] in user_ids


def test_normalized_launch_registries_name_the_same_company_and_team():
    manifest = _load(MANIFEST_PATH)
    organizations = _load(REPO_DIR / "config" / "organizations.json")
    users = _load(REPO_DIR / "config" / "users.json")["users"]

    assert manifest["organization"]["id"] == "steel-mission"
    assert organizations["activeOrganizationId"] == "steel-mission"
    assert organizations["organizations"][0]["name"] == "Steel Mission"
    assert all(user["organizationIds"] == ["steel-mission"] for user in users)
    assert {user["id"] for user in users} >= {
        "andrew-hermann",
        "emma-h",
        "al-architect",
        "al-product",
        "al-designer",
        "al-trust",
        "al-intelligence",
        "al-legal",
        "al-comm",
        "al-ops",
    }


def test_durable_core_is_six_epics_and_fifty_eight_total_issues():
    portfolio = _load(PORTFOLIO_PATH)
    project = portfolio["projects"][0]
    project_record = _load(REPO_DIR / project["projectRecord"])
    catalog = _load(ISSUE_CATALOG_PATH)

    epic_count = len(catalog["epics"])
    child_issue_count = len(catalog["issues"])
    total_issue_count = epic_count + child_issue_count

    assert project_record["projectId"] == "PRJ-0001"
    assert project_record["title"] == "Durable Core"
    assert project["projectId"] == "PRJ-0001"
    assert project["epicIssueCount"] == epic_count == 6
    assert project["childIssueCount"] == child_issue_count == 52
    assert project["totalIssueCount"] == total_issue_count == 58
    assert len(project["milestoneRecords"]) == len(catalog["milestones"]) == 6


def test_launch_company_contains_only_steel_mission_branding():
    for path in STARTER_DIR.rglob("*"):
        if path.suffix not in {".json", ".md"}:
            continue
        text = path.read_text().casefold()
        assert "present" not in text, f"legacy brand remains in {path.relative_to(REPO_DIR)}"
        assert "northstar" not in text, f"placeholder company remains in {path.relative_to(REPO_DIR)}"


def test_plain_three_ring_logo_is_retained_as_the_steel_mission_mark():
    html = (REPO_DIR / "steel-mission-chat" / "index.html").read_text()
    assert '<div class="gauge-mark" aria-label="Steel Mission mark" role="img">' in html
    assert '<span class="gauge-circle outer"></span>' in html
    assert '<span class="gauge-circle middle"></span>' in html
    assert '<span class="gauge-circle inner"></span>' in html


def test_delivery_model_roles_bind_requested_providers():
    registry = _load(REPO_DIR / "config" / "model-role-registry.json")
    assert schema_check.validate(registry, "canonical/model-role-registry-v1.json") == []
    models = {item["id"]: item for item in registry["models"]}
    roles = {item["id"]: item for item in registry["roles"]}

    expected = {
        "delivery.planner": ("claude-sonnet-5", "claude", "claude-code"),
        "delivery.coder": ("qwen2.5-coder:14b", "glimmer", "ollama"),
        "delivery.reviewer": ("codex-cli-default", "codex", "codex-cli"),
        "delivery.acceptance": ("claude-sonnet-5", "claude", "claude-code"),
    }
    for role_id, (model_id, provider, transport) in expected.items():
        assert roles[role_id]["primaryModel"] == model_id
        assert models[model_id]["provider"] == provider
        assert models[model_id]["transport"] == transport
        assert role_id in models[model_id]["roles"]


def test_codex_review_adapter_is_read_only_and_schema_shaped_in_mock_mode():
    result = codex_adapter.review(
        "DEV-000001", "mock", "keep the boundary", "one step", "abc123", "diff", "tests pass")
    assert result["producer"] == "steel-mission review (codex)"
    assert result["verdict"] == "ACCEPTED"
    assert result["mock"] is True
    assert schema_check.validate(result, "canonical/review-v1.json") == []


def test_starter_company_docker_contract_exists():
    dockerfile = (REPO_DIR / "Dockerfile").read_text()
    compose = (REPO_DIR / "compose.yaml").read_text()
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in dockerfile
    assert "@openai/codex@${CODEX_CLI_VERSION}" in dockerfile
    assert "STEEL_MISSION_OLLAMA_BASE_URL=http://host.docker.internal:11434" in dockerfile
    assert "container_name: steel-mission" in compose
    assert "starter-company" in compose
    assert "/run/secrets/claude-token" in compose
    assert "/home/steelmission/.codex/auth.json" in compose
