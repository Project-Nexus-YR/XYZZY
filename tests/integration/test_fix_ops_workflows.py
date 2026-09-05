"""Static checks on the CI, publish, dependency and image configuration.

Each assertion pins a fix that round 2 made so a later edit cannot quietly
undo it: the workflows parse, both carry a concurrency group, the image
scanner and the signed attestation are wired in, the pip-audit run cannot
touch the gate interpreter, the tag publish path checks the version, the base
image has a Dependabot updater, and the health check honours XYZZY_PORT.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_every_workflow_parses() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        yaml.safe_load(path.read_text(encoding="utf-8"))


def test_gates_workflow_has_concurrency_group() -> None:
    doc = _load("ci.yml")
    assert doc["concurrency"]["group"] == "${{ github.workflow }}-${{ github.ref }}"
    assert doc["concurrency"]["cancel-in-progress"] is True


def test_image_workflow_has_non_cancelling_concurrency_group() -> None:
    doc = _load("docker-publish.yml")
    assert doc["concurrency"]["group"] == "image-latest"
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_gates_pip_audit_runs_isolated_via_pipx() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "pipx run pip-audit -r constraints.txt" in text
    assert "pip install pip-audit" not in text


def test_docker_build_job_scans_the_image() -> None:
    doc = _load("ci.yml")
    steps = doc["jobs"]["docker-build"]["steps"]
    scan = next(s for s in steps if s.get("uses", "").startswith("aquasecurity/trivy-action@"))
    assert scan["with"]["severity"] == "CRITICAL,HIGH"
    assert str(scan["with"]["exit-code"]) == "1"
    assert str(scan["with"]["ignore-unfixed"]) in {"True", "true"}
    # Pinned by commit with the version it stands for named beside it; the
    # commit itself moves with every dependabot bump, so the test asserts the
    # shape, not the value.
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"aquasecurity/trivy-action@[0-9a-f]{40} # v\d+\.\d+\.\d+", text)


def test_publish_job_checks_tag_against_pyproject_version() -> None:
    text = (WORKFLOWS / "docker-publish.yml").read_text(encoding="utf-8")
    assert "GITHUB_REF_NAME" in text
    assert "tomllib" in text


def test_publish_job_attests_provenance_instead_of_buildkit_metadata() -> None:
    doc = _load("docker-publish.yml")
    steps = doc["jobs"]["publish"]["steps"]
    build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
    assert "provenance" not in build["with"], "unsigned BuildKit provenance must not stand in"
    attest = next(
        s for s in steps if s.get("uses", "").startswith("actions/attest-build-provenance@")
    )
    assert attest["with"]["push-to-registry"] is True
    perms = doc["jobs"]["publish"]["permissions"]
    assert perms["id-token"] == "write"
    assert perms["attestations"] == "write"


def test_dependabot_watches_the_docker_base_image() -> None:
    doc = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {u["package-ecosystem"] for u in doc["updates"]}
    assert "docker" in ecosystems


def test_healthcheck_honours_xyzzy_port() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    healthcheck = text[text.index("HEALTHCHECK") :]
    assert "XYZZY_PORT" in healthcheck


def test_no_undocumented_console_script_or_empty_extra() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "scripts" not in project
    assert "nexus" not in project["optional-dependencies"]


def test_websockets_floor_is_not_redeclared() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert not any(d.split(">")[0].split("=")[0].strip() == "websockets" for d in deps)
