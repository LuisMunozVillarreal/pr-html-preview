from __future__ import annotations

import base64
import binascii
import datetime as dt
from typing import Any

import httpx
import jwt

from .preview import TreeResponse


class GitHubClientError(RuntimeError):
    """A redacted GitHub API failure safe to record in service logs."""


def generate_app_jwt(app_id: int, private_key_pem: str, now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "iat": int((current - dt.timedelta(seconds=30)).timestamp()),
            "exp": int((current + dt.timedelta(minutes=8)).timestamp()),
            "iss": str(app_id),
        },
        private_key_pem,
        algorithm="RS256",
    )


class GitHubApiClient:
    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        installation_id: int,
        *,
        api_base: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.installation_id = installation_id
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self.api_base, timeout=httpx.Timeout(30.0))
        self._installation_token: str | None = None
        self._token_expires_at: dt.datetime | None = None

    async def close(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return f"GitHubApiClient(app_id={self.app_id}, installation_id={self.installation_id}, api_base={self.api_base!r})"

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-html-preview",
        }

    async def _token(self) -> str:
        now = dt.datetime.now(dt.UTC)
        if self._installation_token and self._token_expires_at and now < self._token_expires_at - dt.timedelta(minutes=5):
            return self._installation_token
        response = await self._client.post(
            f"/app/installations/{self.installation_id}/access_tokens",
            headers=self._headers(generate_app_jwt(self.app_id, self.private_key_pem)),
        )
        payload = self._json_object(response, "installation token request")
        token = payload.get("token")
        expires_at = payload.get("expires_at")
        if not isinstance(token, str) or not isinstance(expires_at, str):
            raise GitHubClientError("installation token response was incomplete")
        try:
            parsed_expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubClientError("installation token expiry was invalid") from exc
        self._installation_token = token
        self._token_expires_at = parsed_expiry
        return token

    async def get_tree(self, owner: str, repo: str, ref: str) -> TreeResponse:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/git/trees/{ref}",
            headers=self._headers(await self._token()),
            params={"recursive": "1"},
        )
        payload = self._json_object(response, "tree fetch")
        tree = payload.get("tree")
        if not isinstance(tree, list) or not all(isinstance(entry, dict) for entry in tree):
            raise GitHubClientError("tree response was invalid")
        return TreeResponse(entries=tree, truncated=payload.get("truncated") is True)

    async def get_blob(self, owner: str, repo: str, blob_sha: str) -> bytes:
        response = await self._client.get(f"/repos/{owner}/{repo}/git/blobs/{blob_sha}", headers=self._headers(await self._token()))
        payload = self._json_object(response, "blob fetch")
        content = payload.get("content")
        if payload.get("encoding") != "base64" or not isinstance(content, str):
            raise GitHubClientError("blob response was invalid")
        try:
            return base64.b64decode("".join(content.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GitHubClientError("blob response contained invalid base64") from exc

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        response = await self._client.get(f"/repos/{owner}/{repo}/pulls/{number}", headers=self._headers(await self._token()))
        payload = self._json_object(response, "pull request fetch")
        required = ("number", "state", "head", "base")
        if any(key not in payload for key in required):
            raise GitHubClientError("pull request response was incomplete")
        return payload

    async def find_check_run(self, owner: str, repo: str, head_sha: str, external_id: str) -> str | None:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            headers=self._headers(await self._token()),
            params={"check_name": "pr-html-preview", "filter": "all", "per_page": 100},
        )
        payload = self._json_object(response, "check run list")
        runs = payload.get("check_runs")
        if not isinstance(runs, list):
            raise GitHubClientError("check run list response was invalid")
        for run in runs:
            if isinstance(run, dict) and run.get("external_id") == external_id and isinstance(run.get("id"), int | str):
                return str(run["id"])
        return None

    async def upsert_check_run(
        self,
        *,
        owner: str,
        repo: str,
        check_name: str,
        head_sha: str,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
        details_url: str | None,
        external_id: str,
        check_run_id: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "name": check_name,
            "head_sha": head_sha,
            "status": status,
            "external_id": external_id,
            "output": {"title": title, "summary": summary},
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        if details_url is not None:
            body["details_url"] = details_url
        headers = self._headers(await self._token())
        if check_run_id is None:
            response = await self._client.post(f"/repos/{owner}/{repo}/check-runs", headers=headers, json=body)
        else:
            body.pop("head_sha", None)  # GitHub's update endpoint does not accept head_sha.
            body["details_url"] = details_url or ""
            response = await self._client.patch(f"/repos/{owner}/{repo}/check-runs/{check_run_id}", headers=headers, json=body)
        payload = self._json_object(response, "check run update")
        check_id = payload.get("id")
        if not isinstance(check_id, int | str):
            raise GitHubClientError("check run response omitted its identifier")
        return str(check_id)

    @staticmethod
    def _json_object(response: httpx.Response, context: str) -> dict[str, Any]:
        if response.status_code >= 300:
            raise GitHubClientError(f"{context} failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubClientError(f"{context} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubClientError(f"{context} returned an invalid response")
        return payload
