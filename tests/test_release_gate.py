"""Proofs that keep the local and continuous-integration release gates aligned."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_DIR / "Makefile"
CI_WORKFLOW = REPO_DIR / ".github" / "workflows" / "ci.yml"
PYTHON_ENTRYPOINTS = (
    "steel-mission-chat/server.py",
    "bin/present-worker",
    "bin/present-control-plane",
    "bin/present-evidence-signer",
    "bin/present-private-runner",
    "bin/present-lease-broker",
)


def test_local_and_ci_compile_gates_share_every_entrypoint():
    makefile = MAKEFILE.read_text()
    workflow = CI_WORKFLOW.read_text()
    declaration = "PYTHON_ENTRYPOINTS := " + " ".join(PYTHON_ENTRYPOINTS)

    assert declaration in makefile
    assert "compile-entrypoints:" in makefile
    assert "$(PYTHON) -m py_compile $(PYTHON_ENTRYPOINTS)" in makefile
    assert "$(MAKE) compile-entrypoints" in makefile
    assert "run: make compile-entrypoints PYTHON=python" in workflow


def test_compile_gate_rejects_a_broken_lease_broker(tmp_path):
    shutil.copy2(MAKEFILE, tmp_path / "Makefile")
    for relative in PYTHON_ENTRYPOINTS:
        source = REPO_DIR / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    clean = subprocess.run(
        ["make", "compile-entrypoints", f"PYTHON={sys.executable}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert clean.returncode == 0, clean.stderr

    lease_broker = tmp_path / "bin" / "present-lease-broker"
    lease_broker.write_text("def deliberate_syntax_error(:\n")
    broken = subprocess.run(
        ["make", "compile-entrypoints", f"PYTHON={sys.executable}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert broken.returncode != 0
    assert "SyntaxError" in broken.stderr
    assert "present-lease-broker" in broken.stderr
