from __future__ import annotations

import base64
import datetime as dt
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_html_preview.github import GitHubApiClient, GitHubClientError, generate_app_jwt


def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_generate_app_jwt_has_bounded_claims() -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    token = generate_app_jwt(123, private_key(), now)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] == 510


@pytest.mark.asyncio
async def test_client_fetches_tree_blob_and_creates_check() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": [{"path": "docs/html/index.html"}], "truncated": False})
        if "/git/blobs/" in request.url.path:
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"hello").decode()})
        if "/pulls/" in request.url.path:
            return httpx.Response(
                200,
                json={"number": 1, "state": "open", "head": {"sha": "abc1234"}, "base": {}},
            )
        if "/commits/" in request.url.path and request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"id": 41, "external_id": "pr-1"}]})
        if request.method == "POST" and request.url.path.endswith("/check-runs"):
            body = json.loads(request.content)
            assert body["details_url"] == "https://preview.example.com/path/"
            return httpx.Response(201, json={"id": 42})
        if request.method == "PATCH" and "/check-runs/" in request.url.path:
            body = json.loads(request.content)
            assert "head_sha" not in body
            assert body["details_url"] == ""
            return httpx.Response(200, json={"id": 41})
        raise AssertionError(request.url)

    async_client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    client = GitHubApiClient(123, private_key(), 99, api_base="https://api.github.test", client=async_client)
    tree = await client.get_tree("octo-org", "example-repo", "abc1234")
    assert tree.entries[0]["path"] == "docs/html/index.html"
    assert await client.get_blob("octo-org", "example-repo", "blob") == b"hello"
    assert (await client.get_pull_request("octo-org", "example-repo", 1))["state"] == "open"
    assert await client.find_check_run("octo-org", "example-repo", "abc1234", "pr-1") == "41"
    check_id = await client.upsert_check_run(
        owner="octo-org",
        repo="example-repo",
        check_name="pr-html-preview",
        head_sha="abc1234",
        status="completed",
        conclusion="success",
        title="Ready",
        summary="Ready",
        details_url="https://preview.example.com/path/",
        external_id="pr-1",
    )
    assert check_id == "42"
    updated_id = await client.upsert_check_run(
        owner="octo-org",
        repo="example-repo",
        check_name="pr-html-preview",
        head_sha="abc1234",
        status="completed",
        conclusion="success",
        title="Updated",
        summary="Updated",
        details_url=None,
        external_id="pr-1",
        check_run_id="41",
    )
    assert updated_id == "41"
    assert sum(request.url.path.endswith("/access_tokens") for request in requests) == 1
    assert "installation-token" not in repr(client)
    await client.close()


@pytest.mark.asyncio
async def test_client_redacts_http_failures_and_rejects_bad_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(500, text="server included a secret")

    async_client = httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    client = GitHubApiClient(123, private_key(), 99, api_base="https://api.github.test", client=async_client)
    with pytest.raises(GitHubClientError) as error:
        await client.get_tree("octo-org", "example-repo", "abc1234")
    assert "server included" not in str(error.value)
    await client.close()


@pytest.mark.asyncio
async def test_client_rejects_invalid_blob_base64() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "token", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(200, json={"encoding": "base64", "content": "!!!"})

    client = GitHubApiClient(
        1,
        private_key(),
        1,
        api_base="https://api.github.test",
        client=httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GitHubClientError, match="base64"):
        await client.get_blob("octo-org", "example-repo", "blob")
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_payload,match",
    [
        ({"expires_at": "2099-01-01T00:00:00Z"}, "incomplete"),
        ({"token": "token", "expires_at": "not-a-date"}, "expiry"),
    ],
)
async def test_client_rejects_invalid_installation_token_payload(token_payload: dict[str, str], match: str) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(201, json=token_payload))
    client = GitHubApiClient(
        1,
        private_key(),
        1,
        api_base="https://api.github.test",
        client=httpx.AsyncClient(base_url="https://api.github.test", transport=transport),
    )
    with pytest.raises(GitHubClientError, match=match):
        await client.get_tree("owner", "repo", "abc1234")
    await client.close()


@pytest.mark.asyncio
async def test_client_rejects_invalid_tree_and_check_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "token", "expires_at": "2099-01-01T00:00:00Z"})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": "wrong"})
        return httpx.Response(200, json={})

    client = GitHubApiClient(
        1,
        private_key(),
        1,
        api_base="https://api.github.test",
        client=httpx.AsyncClient(base_url="https://api.github.test", transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GitHubClientError, match="tree response"):
        await client.get_tree("owner", "repo", "abc1234")
    with pytest.raises(GitHubClientError, match="identifier"):
        await client.upsert_check_run(
            owner="owner",
            repo="repo",
            check_name="preview",
            head_sha="abc1234",
            status="completed",
            conclusion="success",
            title="ready",
            summary="ready",
            details_url=None,
            external_id="pr-1",
        )
    await client.close()
