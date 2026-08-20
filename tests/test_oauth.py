from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from fkey.engine import TasteDB
from fkey.server import configure_accounts, configure_oauth, http_app

BASE_URL = "http://127.0.0.1:8000"
CALLBACK_URL = "https://claude.ai/api/mcp/auth_callback"


def _sse_payload(response) -> dict:
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"No SSE data in response: {response.text}")


class OAuthFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = configure_accounts(Path(self.temporary.name) / "data")
        self.alice = self.store.create_account(
            "Alice", "alice@example.com", "correct horse battery staple"
        )
        self.bob = self.store.create_account(
            "Bob", "bob@example.com", "another correct horse battery"
        )
        TasteDB(self.store.database_path(self.alice["id"])).add_record(
            "movies", {"title": "Arrival", "year": 2016}
        )
        TasteDB(self.store.database_path(self.bob["id"])).add_record(
            "movies", {"title": "Dune", "year": 2021}
        )
        configure_oauth(BASE_URL)
        self.app = http_app()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _register(client: TestClient) -> dict:
        response = client.post(
            "/register",
            json={
                "client_name": "Test connector",
                "redirect_uris": [CALLBACK_URL],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "fkey",
                "application_type": "web",
            },
        )
        if response.status_code != 201:
            raise AssertionError(response.text)
        return response.json()

    @staticmethod
    def _authorization_code(
        client: TestClient, oauth_client: dict, email: str, password: str
    ) -> tuple[str, str]:
        verifier = "test-verifier-" * 5
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        authorization = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": oauth_client["client_id"],
                "redirect_uri": CALLBACK_URL,
                "scope": "fkey",
                "state": "test-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": f"{BASE_URL}/mcp",
            },
        )
        if authorization.status_code != 302:
            raise AssertionError(authorization.text)
        request_id = parse_qs(urlparse(authorization.headers["location"]).query)[
            "request"
        ][0]
        login_page = client.get(f"/oauth/login?request={request_id}")
        if login_page.status_code != 200:
            raise AssertionError(login_page.text)
        expected_form_action = "form-action 'self' https://claude.ai"
        if expected_form_action not in login_page.headers["content-security-policy"]:
            raise AssertionError(login_page.headers["content-security-policy"])
        nonce = re.search(r'<script nonce="([a-f0-9]+)">', login_page.text)
        if nonce is None:
            raise AssertionError("Login form is missing its submission script")
        if (
            f"script-src 'nonce-{nonce.group(1)}'"
            not in login_page.headers["content-security-policy"]
        ):
            raise AssertionError(login_page.headers["content-security-policy"])
        if 'data-submitting-label="Connecting…"' not in login_page.text:
            raise AssertionError("Login form is missing its submitting label")
        login = client.post(
            "/oauth/login",
            data={"request": request_id, "email": email, "password": password},
        )
        if login.status_code != 303:
            raise AssertionError(login.text)
        callback = parse_qs(urlparse(login.headers["location"]).query)
        if callback["state"] != ["test-state"] or callback["iss"] != [BASE_URL]:
            raise AssertionError(callback)
        return callback["code"][0], verifier

    @classmethod
    def _authorize(
        cls,
        client: TestClient,
        oauth_client: dict,
        email: str,
        password: str,
    ) -> dict:
        code, verifier = cls._authorization_code(
            client, oauth_client, email, password
        )
        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": oauth_client["client_id"],
                "client_secret": oauth_client["client_secret"],
                "code": code,
                "redirect_uri": CALLBACK_URL,
                "code_verifier": verifier,
                "resource": f"{BASE_URL}/mcp",
            },
        )
        if token.status_code != 200:
            raise AssertionError(token.text)
        result = token.json()
        result["authorization_code"] = code
        result["code_verifier"] = verifier
        return result

    @staticmethod
    def _query_movies(client: TestClient, access_token: str) -> list[dict]:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "query",
                    "arguments": {"sql": "SELECT title FROM movies ORDER BY title"},
                },
            },
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        payload = _sse_payload(response)
        result = json.loads(payload["result"]["content"][0]["text"])
        return result["rows"]

    def test_discovery_oauth_rotation_and_user_isolation(self) -> None:
        with TestClient(self.app, base_url=BASE_URL, follow_redirects=False) as client:
            unauthorized = client.post("/mcp", json={})
            self.assertEqual(unauthorized.status_code, 401)
            self.assertIn("resource_metadata", unauthorized.headers["www-authenticate"])

            resource = client.get("/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(resource.status_code, 200)
            self.assertEqual(resource.json()["resource"], f"{BASE_URL}/mcp")
            self.assertEqual(resource.json()["authorization_servers"], [BASE_URL])

            oauth_client = self._register(client)
            alice_tokens = self._authorize(
                client,
                oauth_client,
                "alice@example.com",
                "correct horse battery staple",
            )
            bob_tokens = self._authorize(
                client,
                oauth_client,
                "bob@example.com",
                "another correct horse battery",
            )

            self.assertEqual(
                self._query_movies(client, alice_tokens["access_token"]),
                [{"title": "Arrival"}],
            )
            self.assertEqual(
                self._query_movies(client, bob_tokens["access_token"]),
                [{"title": "Dune"}],
            )

            revoked = client.post(
                "/revoke",
                data={
                    "client_id": oauth_client["client_id"],
                    "client_secret": oauth_client["client_secret"],
                    "token": bob_tokens["access_token"],
                    "token_type_hint": "access_token",
                },
            )
            self.assertEqual(revoked.status_code, 200)
            rejected_after_revoke = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {bob_tokens['access_token']}"},
                json={},
            )
            self.assertEqual(rejected_after_revoke.status_code, 401)

            refreshed = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": oauth_client["client_id"],
                    "client_secret": oauth_client["client_secret"],
                    "refresh_token": alice_tokens["refresh_token"],
                },
            )
            self.assertEqual(refreshed.status_code, 200)
            refreshed_tokens = refreshed.json()
            self.assertNotEqual(
                refreshed_tokens["refresh_token"], alice_tokens["refresh_token"]
            )
            reused = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": oauth_client["client_id"],
                    "client_secret": oauth_client["client_secret"],
                    "refresh_token": alice_tokens["refresh_token"],
                },
            )
            self.assertEqual(reused.status_code, 400)
            self.assertEqual(reused.json()["error"], "invalid_grant")
            rejected_after_reuse = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {refreshed_tokens['access_token']}"},
                json={},
            )
            self.assertEqual(rejected_after_reuse.status_code, 401)

    def test_token_endpoint_rejects_wrong_pkce_and_client_secret(self) -> None:
        with TestClient(self.app, base_url=BASE_URL, follow_redirects=False) as client:
            oauth_client = self._register(client)
            code, verifier = self._authorization_code(
                client,
                oauth_client,
                "alice@example.com",
                "correct horse battery staple",
            )
            token_data = {
                "grant_type": "authorization_code",
                "client_id": oauth_client["client_id"],
                "client_secret": oauth_client["client_secret"],
                "code": code,
                "redirect_uri": CALLBACK_URL,
                "resource": f"{BASE_URL}/mcp",
            }

            wrong_pkce = client.post(
                "/token", data={**token_data, "code_verifier": "wrong-verifier"}
            )
            self.assertEqual(wrong_pkce.status_code, 400)
            self.assertEqual(wrong_pkce.json()["error"], "invalid_grant")

            issued = client.post(
                "/token", data={**token_data, "code_verifier": verifier}
            )
            self.assertEqual(issued.status_code, 200)
            wrong_secret = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": oauth_client["client_id"],
                    "client_secret": "wrong-secret",
                    "refresh_token": issued.json()["refresh_token"],
                },
            )
            self.assertEqual(wrong_secret.status_code, 401)
            self.assertEqual(wrong_secret.json()["error"], "invalid_client")

    def test_login_rejects_wrong_password_without_consuming_request(self) -> None:
        with TestClient(self.app, base_url=BASE_URL, follow_redirects=False) as client:
            oauth_client = self._register(client)
            verifier = "another-test-verifier" * 3
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            authorization = client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": oauth_client["client_id"],
                    "redirect_uri": CALLBACK_URL,
                    "scope": "fkey",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            request_id = parse_qs(urlparse(authorization.headers["location"]).query)[
                "request"
            ][0]

            rejected = client.post(
                "/oauth/login",
                data={
                    "request": request_id,
                    "email": "alice@example.com",
                    "password": "wrong password",
                },
            )
            self.assertEqual(rejected.status_code, 401)
            accepted = client.post(
                "/oauth/login",
                data={
                    "request": request_id,
                    "email": "alice@example.com",
                    "password": "correct horse battery staple",
                },
            )
            self.assertEqual(accepted.status_code, 303)


if __name__ == "__main__":
    unittest.main()
