"""Proofs that constrain the UI build before component migration begins."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent.parent
PROOF_PAGE = REPO_DIR / "tests" / "fixtures" / "ui-build-proof.html"


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
