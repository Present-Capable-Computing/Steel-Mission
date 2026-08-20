"""Proofs that constrain the UI build before component migration begins."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent.parent
PROOF_PAGE = REPO_DIR / "tests" / "fixtures" / "ui-build-proof.html"
PACKAGE_MANIFEST = REPO_DIR / "package.json"
PACKAGE_LOCK = REPO_DIR / "package-lock.json"
APP_PAGE = REPO_DIR / "steel-mission-chat" / "app.html"
CI_WORKFLOW = REPO_DIR / ".github" / "workflows" / "ci.yml"
CODEOWNERS = REPO_DIR / ".github" / "CODEOWNERS"
DEPENDABOT = REPO_DIR / ".github" / "dependabot.yml"


def test_real_iife_bundle_satisfies_the_legacy_script_constraints(tmp_path):
    html = PROOF_PAGE.read_text()

    assert "<!-- esbuild 0.28.2 · --bundle · --format=iife -->" in html
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    script = tmp_path / "steel-mission-ui-build-proof.js"
    script.write_text(scripts[0])

    result = subprocess.run(
        ["node", "--check", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_existing_legacy_page_parse_test_remains_unchanged():
    worker_tests = (REPO_DIR / "tests" / "test_worker.py").read_text()

    assert 're.findall(r"<script>(.*?)</script>", html, flags=re.S)' in worker_tests
    assert '["node", "--check", str(script)]' in worker_tests


def test_ui_toolchain_has_exactly_three_exact_pinned_direct_packages():
    manifest = json.loads(PACKAGE_MANIFEST.read_text())
    direct = {
        **manifest.get("dependencies", {}),
        **manifest.get("devDependencies", {}),
    }

    assert manifest.get("private") is True
    assert direct == {
        "esbuild": "0.28.2",
        "preact": "10.29.8",
        "typescript": "7.0.2",
    }


def test_ui_lockfile_carries_the_exact_direct_pins_and_integrity_hashes():
    manifest = json.loads(PACKAGE_MANIFEST.read_text())
    lock = json.loads(PACKAGE_LOCK.read_text())

    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == manifest["devDependencies"]
    installed = {
        path: package
        for path, package in lock["packages"].items()
        if path.startswith("node_modules/") and not package.get("link")
    }
    assert installed
    assert all(package.get("integrity", "").startswith("sha512-") for package in installed.values())


def test_ui_build_emits_one_self_contained_unminified_page(tmp_path):
    html = APP_PAGE.read_text()

    assert '<script src=' not in html
    assert '<script type="module"' not in html
    assert '<link rel="stylesheet"' not in html
    assert "https://" not in html
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    assert "// node_modules/preact/" in scripts[0]
    script = tmp_path / "steel-mission-app.js"
    script.write_text(scripts[0])
    result = subprocess.run(
        ["node", "--check", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_ui_build_and_drift_check_are_make_targets():
    makefile = (REPO_DIR / "Makefile").read_text()
    manifest = json.loads(PACKAGE_MANIFEST.read_text())

    assert "ui-build:" in makefile
    assert "ui-check:" in makefile
    assert manifest["scripts"]["ui:build"] == "node tooling/build-ui.mjs"
    assert manifest["scripts"]["ui:check"] == "node tooling/build-ui.mjs --check"


def test_ui_builder_is_an_unminified_iife_with_no_external_assets():
    builder = (REPO_DIR / "tooling" / "build-ui.mjs").read_text()

    assert 'format: "iife"' in builder
    assert "minify: false" in builder
    assert "write: false" in builder
    assert "<style>" in builder
    assert "<script>" in builder


def test_ci_has_an_isolated_reproducible_ui_job():
    workflow = CI_WORKFLOW.read_text()
    python_job, ui_job = workflow.split("  ui-build:", 1)

    assert "npm ci" not in python_job
    assert "name: UI build is reproducible" in ui_job
    assert "node-version: 24.14.0" in ui_job
    assert "npm ci" in ui_job
    assert "npm audit --audit-level=high" in ui_job
    assert "npm run ui:typecheck" in ui_job
    assert "npm run ui:test" in ui_job
    assert "npm run ui:check" in ui_job


def test_ci_watches_an_edited_committed_page_fail_then_restores_it():
    workflow = CI_WORKFLOW.read_text()

    assert "deliberate rebuild drift" in workflow
    assert "must reject an edited committed page" in workflow
    assert workflow.count("npm run ui:check") >= 3


def test_ui_package_files_are_codeowned_supply_chain_surfaces():
    codeowners = CODEOWNERS.read_text().splitlines()

    assert "/package.json                   @andrewHermann" in codeowners
    assert "/package-lock.json              @andrewHermann" in codeowners


def test_dependabot_covers_the_locked_npm_ecosystem_weekly():
    config = DEPENDABOT.read_text()
    _before, npm_and_after = config.split('- package-ecosystem: "npm"', 1)
    npm_block = npm_and_after.split("\n  - package-ecosystem:", 1)[0]

    assert 'directory: "/"' in npm_block
    assert 'interval: "weekly"' in npm_block
    assert 'labels: ["dependencies", "security"]' in npm_block
    assert 'prefix: "npm"' in npm_block
