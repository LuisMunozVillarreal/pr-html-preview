from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def load_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_release_workflow_is_main_only_and_safe_by_default() -> None:
    workflow = load_workflow()
    triggers = workflow["on"]
    assert triggers == {"push": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "read"}

    jobs = workflow["jobs"]
    assert jobs["configuration"]["if"] == "${{ vars.DEPLOYMENT_ENABLED == 'true' }}"
    assert jobs["publish"]["needs"] == "configuration"
    assert jobs["deploy"]["needs"] == "publish"


def test_every_secret_consuming_job_uses_the_production_environment() -> None:
    jobs = load_workflow()["jobs"]
    for name, job in jobs.items():
        if "secrets." in yaml.safe_dump(job):
            assert job.get("environment") == "production", name


def test_release_workflow_dispatches_digest_and_checks_exact_health_status() -> None:
    text = WORKFLOW.read_text()
    assert ":sha-${{ github.sha }}" in text
    assert "push: true" in text
    assert 'image_ref:($image + "@" + $digest)' in text
    assert 'if [ "$status" = "200" ]' in text
    assert "--location" not in text
    assert "KUBECONFIG" not in text
    assert "kubectl" not in text
    assert "secrets.REGISTRY_PASSWORD" in text
    assert "GITOPS_TOKEN" in text
    assert "DEPLOYMENT_HEALTH_URL" in text


def test_deployment_and_smoke_test_share_one_environment_job() -> None:
    jobs = load_workflow()["jobs"]
    deploy = jobs["deploy"]
    assert deploy["environment"] == "production"
    step_names = [step["name"] for step in deploy["steps"]]
    assert step_names == [
        "Validate GitOps configuration",
        "Request deployment from the private GitOps repository",
        "Wait for the deployed health endpoint",
    ]
