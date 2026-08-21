from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "pr-html-preview"


def render(*extra_args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(CHART),
            "--set-string",
            "github.appId=123456",
            "--set",
            "ingress.enabled=true",
            "--set",
            "ingress.host=preview.example.com",
            *extra_args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if isinstance(document, dict)]


def one(documents: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [document for document in documents if document.get("kind") == kind]
    assert len(matches) == 1, f"expected one {kind}, found {len(matches)}"
    return matches[0]


def main() -> None:
    documents = render()
    deployment = one(documents, "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert pod_spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["ports"] == [{"name": "http", "containerPort": 8080}]
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    env = {item["name"]: item for item in container["env"]}
    assert env["GITHUB_PRIVATE_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "github-app-credentials"
    assert env["PREVIEW_PASSWORD_FILE"]["value"].endswith("/password")
    assert env["PREVIEW_USERNAME_FILE"]["value"].endswith("/username")
    for name in (
        "GITHUB_WEBHOOK_BODY_MAX_BYTES",
        "PREVIEW_MAX_FILE_COUNT",
        "PREVIEW_MAX_FILE_SIZE_BYTES",
        "PREVIEW_MAX_TOTAL_BYTES",
        "PREVIEW_MAX_STORAGE_BYTES",
        "PREVIEW_MAX_PREVIEW_COUNT",
        "PREVIEW_MIN_FREE_BYTES",
    ):
        assert env[name]["value"].isdigit()

    secret = one(documents, "Secret")
    password = secret["stringData"]["password"]
    assert len(password) == 40 and password.isalnum()
    assert secret["stringData"]["username"] == "preview"

    pvc = one(documents, "PersistentVolumeClaim")
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    ingress = one(documents, "Ingress")
    assert ingress["spec"]["rules"][0]["host"] == "preview.example.com"
    assert one(documents, "Service")["spec"]["ports"][0]["targetPort"] == "http"

    external = render("--set-string", "preview.existingAuthSecret=provided-auth")
    assert not [document for document in external if document.get("kind") == "Secret"]
    external_deployment = one(external, "Deployment")
    volumes = external_deployment["spec"]["template"]["spec"]["volumes"]
    auth_volume = next(volume for volume in volumes if volume["name"] == "preview-auth")
    assert auth_volume["secret"]["secretName"] == "provided-auth"
    print("Helm contract: OK")


if __name__ == "__main__":
    main()
