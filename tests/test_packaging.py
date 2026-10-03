from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _names(lines: list[str]) -> set[str]:
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


def test_requirements_match_pyproject_dependencies() -> None:
    """Railway installs from requirements.txt; it must list exactly what pyproject declares."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = _names(pyproject["project"]["dependencies"])
    pinned = _names((ROOT / "requirements.txt").read_text().splitlines())
    assert declared == pinned


def test_railway_config_runs_migrations_before_start_and_checks_health() -> None:
    config = tomllib.loads((ROOT / "railway.toml").read_text())
    deploy = config["deploy"]
    assert "alembic upgrade head" in deploy["preDeployCommand"][0]
    assert "hrmgmt.main:app_factory" in deploy["startCommand"]
    assert deploy["healthcheckPath"] == "/health"
