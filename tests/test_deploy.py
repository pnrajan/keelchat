"""Deploying from GitHub: the Fly.io config and workflow, and the Render Blueprint, agree with the image."""

from __future__ import annotations

import re
import tomllib

import yaml

from tests.conftest import ROOT

PORT = int(re.search(r"^EXPOSE (\d+)", (ROOT / "Dockerfile").read_text(), re.M).group(1))  # type: ignore[union-attr]


def test_fly_runs_the_image_on_its_port_and_checks_health() -> None:
    for path in (ROOT / "deploy" / "fly.toml", ROOT / "deploy" / "demo" / "fly.toml"):
        cfg = tomllib.loads(path.read_text())
        assert (path.parent / cfg["build"]["dockerfile"]).resolve() == ROOT / "Dockerfile", path
        assert cfg["http_service"]["internal_port"] == PORT, path
        assert cfg["http_service"]["checks"][0]["path"] == "/health", path


def test_the_workflow_deploys_the_default_config_only_with_its_api_key_set() -> None:
    flow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    default = re.search(r"vars\.FLY_CONFIG \|\| '([^']+)'", flow)
    assert default and (ROOT / default.group(1)).is_file()
    assert "YACHAT_API_KEY" in flow.split("flyctl deploy")[0]  # checked before deploying: the console stays locked
    assert yaml.safe_load(flow)[True]["workflow_run"]["workflows"] == ["CI"]  # `on:` reads as True; deploys after CI


def test_render_builds_the_image_on_its_port_with_a_generated_api_key() -> None:
    (web,) = yaml.safe_load((ROOT / "render.yaml").read_text())["services"]
    env = {v["key"]: v for v in web["envVars"]}
    assert web["runtime"] == "docker" and (ROOT / web["dockerfilePath"]).is_file()
    assert web["healthCheckPath"] == "/health" and env["PORT"]["value"] == str(PORT)
    assert env["YACHAT_API_KEY"].get("generateValue") is True
    assert all("value" not in v for k, v in env.items() if k.endswith("_API_KEY"))  # keys are never in the file
