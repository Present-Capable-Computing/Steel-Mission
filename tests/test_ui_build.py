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
