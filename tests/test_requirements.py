"""Proofs that runtime, PostgreSQL, and development dependencies stay separate."""
from __future__ import annotations

from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent.parent
RUNTIME_REQUIREMENTS = REPO_DIR / "requirements.txt"
POSTGRES_REQUIREMENTS = REPO_DIR / "requirements-postgres.txt"
DEVELOPMENT_REQUIREMENTS = REPO_DIR / "requirements-dev.txt"
CI_WORKFLOW = REPO_DIR / ".github" / "workflows" / "ci.yml"
RUNTIME_INSTALL_GUIDES = (
    REPO_DIR / "README.md",
    REPO_DIR / "INSTALL.md",
    REPO_DIR / "RELEASE_NOTES.md",
    REPO_DIR / "docs" / "quickstart-local.md",
)


def requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_runtime_postgres_and_development_requirements_are_separate():
    runtime = requirement_lines(RUNTIME_REQUIREMENTS)
    postgres = requirement_lines(POSTGRES_REQUIREMENTS)
    development = requirement_lines(DEVELOPMENT_REQUIREMENTS)

    assert runtime == ["cryptography>=50.0.0,<51"]
    assert development == ["-r requirements.txt", "pytest>=9.1.1,<10"]
    assert postgres == ["-r requirements.txt", "psycopg[binary]>=3,<4"]
    assert not any("psycopg" in requirement for requirement in runtime + development)


def test_ci_installs_runtime_and_development_requirements():
    workflow = CI_WORKFLOW.read_text()

    assert "python -m pip install -r requirements.txt -r requirements-dev.txt" in workflow


def test_product_and_private_runner_images_install_their_required_groups():
    product_image = (REPO_DIR / "Dockerfile").read_text()
    runner_image = (REPO_DIR / "Dockerfile.private-runner").read_text()

    assert "pip install --no-cache-dir -r /tmp/steel-mission-requirements.txt" in product_image
    assert "requirements-dev" not in product_image
    assert "COPY requirements.txt requirements-dev.txt /tmp/" in runner_image
    assert "pip install --no-cache-dir -r /tmp/requirements-dev.txt" in runner_image
    assert "requirements-postgres" not in runner_image


def test_runtime_install_guides_do_not_require_test_dependencies():
    for guide in RUNTIME_INSTALL_GUIDES:
        text = guide.read_text()
        assert "pip install -r requirements.txt" in text, guide
        assert "pip install -r requirements-dev.txt" not in text, guide
