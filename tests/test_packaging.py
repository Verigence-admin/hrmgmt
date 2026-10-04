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


def test_railway_config_builds_our_dockerfile_and_migrates_before_start() -> None:
    """The service was inherited from the old attendance service, whose settings point at another
    Dockerfile. The config file must name ours explicitly so those settings cannot win."""
    config = tomllib.loads((ROOT / "railway.toml").read_text())
    assert config["build"]["builder"] == "DOCKERFILE"
    assert config["build"]["dockerfilePath"] == "Dockerfile"
    assert (ROOT / "Dockerfile").is_file()
    deploy = config["deploy"]
    assert "alembic upgrade head" in deploy["preDeployCommand"][0]
    assert deploy["healthcheckPath"] == "/health"


def test_dockerfile_starts_the_app_factory_and_ships_migrations() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "hrmgmt.main:app_factory" in dockerfile and "--factory" in dockerfile
    for needed in ("COPY migrations", "COPY src", "COPY alembic.ini", "requirements.txt"):
        assert needed in dockerfile


def test_stopgap_dockerfile_mirrors_the_real_one() -> None:
    """Dockerfile.attendance exists only because the Railway service still points at that name."""
    real = (ROOT / "Dockerfile").read_text()
    stopgap = (ROOT / "Dockerfile.attendance").read_text()
    body = "\n".join(line for line in stopgap.splitlines() if not line.startswith("#")) + "\n"
    assert body == real
