# PR HTML Preview

A self-hosted GitHub App that publishes HTML from pull requests to one password-protected website.

Each preview is served from one configured origin:

```text
https://preview.example.com/<owner>/<repository>/<branch-slug>/
```

The service receives GitHub webhooks directly. Installed repositories do **not** need workflows, actions, deployment manifests, or configuration files. One application process and one persistent volume serve every preview.

## What it does

- Reacts to pull request `opened`, `reopened`, `synchronize`, and `closed` events.
- Reads only a configured static directory at the exact PR head commit.
- Never runs code from the repository.
- Persists and deduplicates webhook deliveries before returning HTTP 202, then processes them serially from a SQLite queue on the PVC.
- Reconciles each queued event with the live pull request state, stages files with rollback-safe replacement, and requires `index.html`.
- Creates or updates a GitHub Check with the authenticated preview URL.
- Deletes the preview when the PR closes.
- Protects the index and every preview with HTTP Basic Authentication.
- Generates the preview password during Helm installation and preserves it across upgrades.
- Rejects pull requests from forks.

## Security boundary

> [!IMPORTANT]
> Every preview shares one origin and may contain arbitrary HTML and JavaScript. JavaScript in one preview can make same-origin requests to another preview. Install the App only on mutually trusted repositories, and allow only trusted contributors to create branches and pull requests. Fork previews are not supported. Basic Authentication prevents unauthorised visitors; it does not isolate installed repositories from each other.

The webhook and health endpoints are public by design. Webhooks require a valid HMAC-SHA256 signature. Preview content is bounded by configurable webhook, file-count, per-file, per-preview, aggregate-storage, preview-count, and minimum-free-space limits. Startup reconciliation removes abandoned staging trees and restores interrupted backups. Symlinks, submodules, unsafe paths, truncated Git trees, and malformed blobs are rejected.

The durable queue stores only the installation ID, repository identity, PR number, branch ref, head SHA, and repository identities required to process the event; it does not retain PR titles, bodies, sender profiles, or other unused webhook fields. Completed delivery IDs are retained for seven days for deduplication, with their processing payload erased. Transient failures remain pending with capped backoff rather than being marked complete or discarded.

## URL and branch mapping

Every branch name receives a bounded readable prefix plus a 16-character SHA-256 digest:

```text
main -> main-<digest>
release-2026.08 -> release-2026.08-<digest>
feature/report -> feature-report-<digest>
```

The digest is always present, including for already-safe branch names. This prevents a transformed branch such as `feature/report` from colliding with a literal branch that happens to resemble its slug. Slugs are capped at 100 characters to stay within route and filesystem-component limits.

The GitHub Check contains the exact URL; users do not need to calculate it.

## Prerequisites

- A Kubernetes cluster with a default or explicitly configured `StorageClass`.
- Helm 3 or 4.
- An ingress controller and working HTTPS certificate automation or a pre-created TLS Secret.
- A DNS record for the preview hostname.
- A container registry accessible by the cluster.
- Permission to create and install a GitHub App.

## 1. Build and publish the image

Use an image name controlled by your installation:

```bash
docker build -t registry.example/pr-html-preview:0.1.0 .
docker push registry.example/pr-html-preview:0.1.0
```

Do not place registry credentials in this repository. Configure `imagePullSecrets` in Helm values when the registry is private.

## 2. Register the GitHub App

Open **GitHub Settings → Developer settings → GitHub Apps → New GitHub App**.

Use these settings:

| Setting | Value |
|---|---|
| Homepage URL | This repository or your own documentation page |
| Webhook URL | `https://preview.example.com/api/github/webhook` |
| Webhook secret | Generate in the next section |
| Webhook active | Enabled |
| Where can this GitHub App be installed? | The accounts appropriate for your deployment |

Repository permissions:

| Permission | Access |
|---|---|
| Contents | Read-only |
| Pull requests | Read-only |
| Checks | Read and write |
| Metadata | Read-only (GitHub grants this automatically) |

Subscribe only to **Pull request** events.

After creating the App:

1. Record its numeric App ID.
2. Generate and download a private key.
3. Do not commit either credential.

## 3. Create the Kubernetes credential Secret

Generate the webhook secret locally and create the Kubernetes Secret from files so secret values are not placed in Helm values or shell arguments:

```bash
umask 077
mkdir -p .secrets
openssl rand -hex 32 > .secrets/webhook-secret
cp /path/to/downloaded-github-app.private-key.pem .secrets/private-key

kubectl create namespace pr-html-preview
kubectl -n pr-html-preview create secret generic github-app-credentials \
  --from-file=private-key=.secrets/private-key \
  --from-file=webhook-secret=.secrets/webhook-secret
```

Copy `.secrets/webhook-secret` into the GitHub App's **Webhook secret** field. Securely delete the local temporary directory afterwards:

```bash
rm -rf .secrets
```

The chart reads the private key and webhook secret from the existing Kubernetes Secret. It never stores them in Helm release values.

## 4. Configure Helm values

Create a local file outside this repository, for example `installation-values.yaml`:

```yaml
image:
  repository: registry.example/pr-html-preview
  tag: 0.1.0
  digest: ""

github:
  appId: "REPLACE_WITH_GITHUB_APP_ID"
  existingSecret: github-app-credentials

preview:
  baseUrl: https://preview.example.com
  username: preview
  existingAuthSecret: ""
  defaultSourceDir: docs/html
  sourceDirOverrides:
    octo-org/example-repo: site
  maxStorageBytes: 786432000
  maxPreviewCount: 100
  minFreeBytes: 104857600

ingress:
  enabled: true
  className: your-ingress-class
  host: preview.example.com
  annotations: {}
  tls:
    enabled: true
    secretName: pr-html-preview-tls

persistence:
  size: 1Gi
  storageClass: ""
  retain: true
```

Keep installation-specific values outside the public repository. Ingress annotations are controller-specific; configure the certificate issuer or TLS integration required by your cluster.

`sourceDirOverrides` is central configuration. Participating repositories do not need their own config file. Keys are `owner/repository`; values are relative POSIX directories.

## 5. Install

```bash
helm upgrade --install pr-html-preview ./charts/pr-html-preview \
  --namespace pr-html-preview \
  --values /path/to/installation-values.yaml
```

The chart creates exactly one replica using a `Recreate` deployment strategy, one Service, one PVC, and optionally one Ingress. The container runs as a non-root user with all Linux capabilities dropped and a read-only root filesystem. Only the PVC and `/tmp` are writable.

## 6. Retrieve the generated preview password

Normal Helm install NOTES do not print the credential value. A chart-generated password is present in the rendered Secret manifest and Helm release storage, as is normal for a Helm-managed Secret. If your CI or GitOps system archives rendered manifests and must never generate credentials, pre-create an auth Secret containing `username` and `password`, then set `preview.existingAuthSecret` to its name.

Example external auth Secret creation without placing the password in Helm values or a command-line argument:

```bash
umask 077
mkdir -p .auth-secret
printf '%s' 'preview' > .auth-secret/username
openssl rand -base64 36 > .auth-secret/password
kubectl -n pr-html-preview create secret generic preview-auth \
  --from-file=username=.auth-secret/username \
  --from-file=password=.auth-secret/password
rm -rf .auth-secret
```

Then set `preview.existingAuthSecret: preview-auth`.

For a chart-generated credential, the default commands are below. If `preview.existingAuthSecret` is set, replace the Secret name with that value:

```bash
kubectl -n pr-html-preview get secret pr-html-preview-pr-html-preview-preview-auth \
  -o jsonpath='{.data.username}' | base64 --decode; echo

kubectl -n pr-html-preview get secret pr-html-preview-pr-html-preview-preview-auth \
  -o jsonpath='{.data.password}' | base64 --decode; echo
```

Store the password in a password manager. The chart reuses the existing password during upgrades. The server reads the mounted Secret files for each request, so Kubernetes-propagated password rotations do not leave an environment-variable credential split. To rotate a chart-generated password, delete only the generated preview-auth Secret and run `helm upgrade` again. After changing the separate GitHub App private-key/webhook Secret, restart the Deployment because those credentials are startup environment variables.

## 7. Install the App on repositories

From the GitHub App settings page, select **Install App** and choose the account and repositories to preview. Prefer **Only select repositories**.

A participating repository needs:

```text
docs/html/index.html
```

All files below `docs/html/` are copied. Relative CSS, JavaScript, image, and document links continue to work. If a repository uses another directory, add a central `sourceDirOverrides` entry and upgrade the Helm release.

No workflow file is required in that repository.

## Release and deployment CI draft

`.github/workflows/release.yml` contains a safe-by-default draft for publishing and deploying the central service. It runs only after a push to `main`, and every release job is skipped until the repository variable `DEPLOYMENT_ENABLED` is exactly `true`.

When enabled, the workflow:

1. validates the required variables and secrets;
2. builds and publishes a traceable `sha-<full-commit>` container tag;
3. sends the immutable `repository@sha256:...` image reference and source SHA to a private GitOps repository using `repository_dispatch`;
4. optionally waits for the deployment health endpoint to return exactly HTTP 200.

The workflow deliberately has no kubeconfig, `kubectl`, personal domain, registry name, or installation-specific Helm values. The private GitOps repository is responsible for setting `image.repository` and `image.digest` in the deployment values and allowing Flux to reconcile them. A digest takes precedence over `image.tag` in the chart.

Configuration that remains to be added later:

| Type | Name | Purpose |
|---|---|---|
| Variable | `DEPLOYMENT_ENABLED` | Master gate; leave unset or `false` until the complete path is configured. |
| Variable | `CONTAINER_REGISTRY` | Registry hostname only. |
| Variable | `IMAGE_REPOSITORY` | Repository path inside the registry. |
| Variable | `GITOPS_REPOSITORY` | Private deployment repository in `owner/repository` form. |
| Variable | `SMOKE_TEST_ENABLED` | Enables the optional health probe when exactly `true`. |
| Variable | `DEPLOYMENT_HEALTH_URL` | Full private deployment health URL, stored outside source control. |
| Secret | `REGISTRY_USERNAME` | Registry publishing identity. |
| Secret | `REGISTRY_PASSWORD` | Registry publishing credential. |
| Secret | `GITOPS_TOKEN` | Fine-grained credential allowed to dispatch only to the private GitOps repository. |

Create a protected GitHub Environment named `production` before enabling the workflow, restrict its deployment branch to `main`, and store all three deployment secrets as environment secrets rather than repository secrets. Add required reviewers there if image publication and deployment should require approval. Both secret-consuming jobs use this environment. The dispatch and optional smoke test run in the same environment job, so a failed health check fails that deployment job. The private GitOps repository still needs a receiver for the `update-pr-html-preview` repository-dispatch event; this will be added when the registry and deployment repository are selected.

## Operations

### Health

```bash
kubectl -n pr-html-preview port-forward service/pr-html-preview-pr-html-preview 8080:80
curl http://127.0.0.1:8080/healthz
```

Expected response:

```json
{"status":"ok"}
```

### Upgrade

```bash
helm upgrade pr-html-preview ./charts/pr-html-preview \
  --namespace pr-html-preview \
  --values /path/to/installation-values.yaml
```

### Uninstall

```bash
helm uninstall pr-html-preview --namespace pr-html-preview
```

When `persistence.retain=true`, the PVC carries `helm.sh/resource-policy: keep` and remains after uninstall. Delete it explicitly only after confirming the previews are no longer needed:

```bash
kubectl -n pr-html-preview delete pvc pr-html-preview-pr-html-preview-data
```

### Troubleshooting

| Symptom | Check |
|---|---|
| GitHub reports webhook delivery failure | Confirm DNS, TLS, the exact `/api/github/webhook` path, and matching webhook secrets. |
| Check says preview failed | Confirm the configured source directory exists at the PR head and contains `index.html`; inspect redacted server logs. |
| Preview receives 401 | Retrieve the current generated credentials from the Kubernetes Secret. |
| Static assets return 404 | Use relative URLs and ensure assets are inside the configured source directory. Missing assets do not fall back to `index.html`. |
| App cannot read a repository | Confirm the App is installed on that repository and has Contents read permission. |
| Check is absent | Confirm Pull request event subscription and Checks write permission. |
| Fork PR is skipped | Fork previews are intentionally unsupported because all previews share one authenticated origin. |
| Password changed unexpectedly | Ensure the generated preview-auth Secret was not deleted between Helm upgrades. |

## Local development

```bash
cp .env.example .env
# Replace placeholders locally; never commit .env.
set -a; . ./.env; set +a
uv sync --dev
uv run pr-html-preview
```

For local HTTP only, set `PREVIEW_ALLOW_HTTP=true` and use a localhost `PREVIEW_BASE_URL`. Production configuration requires HTTPS.

## Verification

```bash
uv run ruff check src tests
uv run mypy
uv run pytest
uv run pip-audit --skip-editable --progress-spinner off
helm lint charts/pr-html-preview --set-string github.appId=123456
docker build -t pr-html-preview:test .
```

## Licence

GPL-3.0-only. See [LICENSE](LICENSE).
