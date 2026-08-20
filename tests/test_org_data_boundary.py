"""The shipped demo company stays shipped, and stays a demo.

`starter-company/` is the synthetic organisation every user of this product gets
on a fresh clone. It is product data, distributed to everyone.

An installation runs on its own organisation by pointing `STEEL_MISSION_ORG_DIR`
at a directory outside this tree. The failure this file exists to prevent is the
other approach: making an installation work by overwriting the shipped directory
in place. That destroys the demo data for every other user, and puts one
organisation's real operating data -- its roster, its clients, its decisions --
into a tree that is published under an open-source licence.

The check is deliberately mechanical. "Remember not to" does not survive a busy
week; a failing test does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
ORG_DIR = REPO_DIR / "starter-company"
CANON_DIR = ORG_DIR / "canon"

sys.path.insert(0, str(REPO_DIR))
from adapters import common  # noqa: E402

# The shipped organisation. Changing this constant is a product decision about
# what every user receives -- not a step in setting up an installation.
DEMO_COMPANY = "Northstar Forge"


def _canon_documents() -> list[Path]:
    return sorted(CANON_DIR.glob("*.md"))


def test_the_shipped_directory_exists():
    assert ORG_DIR.is_dir(), "starter-company/ is missing from the product tree"
    assert _canon_documents(), "starter-company/canon holds no documents"


@pytest.mark.parametrize("path", _canon_documents(), ids=lambda p: p.name)
def test_every_canon_document_declares_the_demo_company(path: Path):
    text = path.read_text(encoding="utf-8")
    assert f"Company: {DEMO_COMPANY}" in text, (
        f"{path.name} no longer declares '{DEMO_COMPANY}'.\n\n"
        "If an installation's own data was written here, move it out and point\n"
        "STEEL_MISSION_ORG_DIR at it instead. The shipped directory is product\n"
        "data: replacing it publishes one organisation's operating data to\n"
        "everyone and removes the demo company every other user starts from."
    )


def test_the_demo_company_is_named_throughout_its_knowledge_base():
    missing = [
        path.name
        for path in sorted((ORG_DIR / "knowledge").rglob("*.md"))
        if DEMO_COMPANY not in path.read_text(encoding="utf-8")
        and "Company:" in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"knowledge documents no longer naming {DEMO_COMPANY}: {missing}"


def test_readme_still_promises_a_synthetic_starter_company():
    # The promise and the data are one claim. If the data stops being synthetic,
    # the README stops being true, and a user who trusted it has real data in a
    # place they believed was a demo.
    readme = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    assert "synthetic" in readme.lower(), "README no longer describes the data as synthetic"
    assert DEMO_COMPANY in readme, f"README no longer names {DEMO_COMPANY}"


def test_org_dir_defaults_to_the_shipped_company():
    assert common.ORG_DIR == REPO_DIR / "starter-company"


def test_org_dir_is_redirectable_without_touching_the_tree():
    """The seam this whole file depends on: an installation can point elsewhere.

    Asserted by running the worker in a subprocess, because a module-level path
    constant is read once at import and an in-process monkeypatch would prove
    nothing about how the binary actually behaves.
    """
    probe = (
        "import sys; sys.path.insert(0, %r);"
        "from adapters import common; print(common.ORG_DIR)" % str(REPO_DIR)
    )
    environment = dict(os.environ, STEEL_MISSION_ORG_DIR="/tmp/some-installation")
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, env=environment, cwd=str(REPO_DIR),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/tmp/some-installation"


APPLICATION_DOCKERFILE = REPO_DIR / "Dockerfile"

# What must never be inside a published application image. Secrets because the
# image is pushed to a registry and pulled by strangers; the plan layer because
# effort figures and internal scheduling are not product; the browser toolchain
# because its committed self-contained page is the runtime artifact.
MUST_NOT_SHIP = (".env", "plan", "tooling", "node_modules", "steel-mission-ui")


@pytest.mark.skipif(
    not APPLICATION_DOCKERFILE.exists(),
    reason="no application Dockerfile in this tree yet",
)
def test_the_application_image_excludes_secrets_plans_and_the_frontend_toolchain():
    """Guards every local-only path omitted from the broad application copy.

    The exclusion list decides whether a secret, planning data or the browser
    build toolchain ends up inside something published to a registry. Naming each
    required exclusion here makes a missing entry fail instead of remaining
    invisible because the test only checked the entries already present.
    """
    dockerfile = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")
    copies_tree = any(
        line.strip().startswith(("COPY", "ADD")) and " . " in line
        for line in dockerfile.splitlines()
    )
    if not copies_tree:
        pytest.skip("the image does not copy the tree wholesale")

    ignore_path = REPO_DIR / ".dockerignore"
    assert ignore_path.exists(), (
        "the image copies the whole tree and there is no .dockerignore; a local "
        ".env would be built into a published image"
    )
    entries = {
        line.strip()
        for line in ignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = [
        name
        for name in MUST_NOT_SHIP
        if not any(entry.rstrip("/") == name for entry in entries)
    ]
    assert not missing, (
        f".dockerignore does not exclude {missing}. The image copies the tree "
        "wholesale, so anything not excluded is published to whoever pulls it. "
        "Keep '!.env.example' if the sample file should still ship."
    )


def test_config_dir_defaults_to_the_shipped_config():
    assert common.CONFIG_DIR == REPO_DIR / "config"


def test_config_dir_is_redirectable_without_touching_the_tree():
    """An installation supplies its own organisation registry, users and
    capability map. Without this, redirecting only ORG_DIR serves the
    installation's documents under the shipped company's identity -- which reads
    as working, and is the failure that is hardest to notice."""
    probe = (
        "import sys; sys.path.insert(0, %r);"
        "from adapters import common; print(common.CONFIG_DIR)" % str(REPO_DIR)
    )
    environment = dict(os.environ, STEEL_MISSION_CONFIG_DIR="/tmp/some-config")
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, env=environment, cwd=str(REPO_DIR),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/tmp/some-config"


def test_no_module_reads_the_config_directory_around_the_seam():
    # A path built from the product root ignores an installation's redirect, and
    # does it silently: the file exists, so nothing errors, and the wrong
    # organisation's configuration is served.
    offenders = []
    for relative in ("steel-mission-chat/server.py", "bin/present-worker", "adapters/common.py"):
        text = (REPO_DIR / relative).read_text(encoding="utf-8")
        # One use is legitimate, and only in a file that defines the seam: the
        # default on the end of its own CONFIG_DIR definition.
        allowed = 1 if "CONFIG_DIR = Path(" in text else 0
        if text.count('WORKER_DIR / "config"') > allowed:
            offenders.append(relative)
    assert not offenders, (
        f"{offenders} build a config path from the product root; use common.CONFIG_DIR "
        "so STEEL_MISSION_CONFIG_DIR is honoured"
    )


@pytest.mark.skipif(
    not APPLICATION_DOCKERFILE.exists(),
    reason="no application Dockerfile in this tree yet",
)
def test_the_image_does_not_point_its_data_directories_at_empty_state():
    """The shipped image must read its own bundled config and company.

    This is here because the image did the opposite: it set the config directory
    to a path under the state volume, which does not exist in the image at all.
    Nothing failed -- the application came up and reported the shipped company --
    so the only symptom was a declared location that was never read. An
    installation overrides these at run time; the image must not.
    """
    for line in APPLICATION_DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().rstrip("\\").strip()
        for variable in ("STEEL_MISSION_CONFIG_DIR", "STEEL_MISSION_ORG_DIR"):
            if stripped.startswith(f"{variable}="):
                pytest.fail(
                    f"the image sets {variable} ({stripped}). Leave it unset so the "
                    "bundled config and starter company are used; an installation "
                    "sets it at run time."
                )


@pytest.mark.skipif(
    not APPLICATION_DOCKERFILE.exists(),
    reason="no application Dockerfile in this tree yet",
)
def test_the_image_does_not_hardcode_one_cpu_architecture():
    # A hardcoded vendor path builds on the maintainer's laptop and fails on
    # every other architecture -- and fails by creating a broken symlink rather
    # than by stopping the build, so the container starts without the tool.
    for line in APPLICATION_DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "arm64" not in line and "aarch64" not in line and "x86_64" not in line, (
            f"Dockerfile names a CPU architecture: {line.strip()}; discover the "
            "packaged path instead, and fail the build when it is not found"
        )


def test_no_config_file_hardcodes_a_path_into_the_shipped_directory():
    # Config refers to ${ORG_DIR}. A literal path under the product tree would
    # silently ignore an installation's redirect, which is what made overwriting
    # the shipped data the only thing that worked.
    offenders = []
    for path in sorted((REPO_DIR / "config").glob("*.json")):
        text = path.read_text(encoding="utf-8")
        if "${WORKER_DIR}/starter-company" in text:
            offenders.append(path.name)
        json.loads(text)
    assert not offenders, (
        f"config files still resolve into the shipped directory: {offenders}; "
        "use ${ORG_DIR} so an installation's redirect is honoured"
    )
