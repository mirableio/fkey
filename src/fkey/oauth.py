"""Contained OAuth authorization provider for the remote MCP server."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from fkey.accounts import AccountStore

ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 60 * 60 * 24 * 30
AUTHORIZATION_REQUEST_SECONDS = 10 * 60
AUTHORIZATION_CODE_SECONDS = 5 * 60
MCP_SCOPE = "fkey"
OFFLINE_SCOPE = "offline_access"


class LoginError(ValueError):
    """A safe message to display during the browser authorization flow."""


class FkeyRefreshToken(RefreshToken):
    grant_id: str


class FkeyAccessToken(AccessToken):
    grant_id: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class OAuthProvider:
    """SQLite-backed OAuth code flow with PKCE and rotating refresh tokens."""

    def __init__(
        self,
        account_store: Callable[[], AccountStore],
        issuer_url: str,
        resource_url: str,
    ):
        self._account_store = account_store
        self._lock = threading.RLock()
        self._initialized_path: Path | None = None
        self.configure(issuer_url, resource_url)

    def configure(self, issuer_url: str, resource_url: str) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url.rstrip("/")

    def _store(self) -> AccountStore:
        store = self._account_store()
        self._initialize(store)
        return store

    def _initialize(self, store: AccountStore) -> None:
        if self._initialized_path == store.path:
            return
        with self._lock:
            if self._initialized_path == store.path:
                return
            with store.session() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_clients (
                        client_id TEXT PRIMARY KEY,
                        client_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS oauth_pending (
                        request_id TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        state TEXT,
                        scopes_json TEXT NOT NULL,
                        code_challenge TEXT NOT NULL,
                        redirect_uri TEXT NOT NULL,
                        redirect_uri_explicit INTEGER NOT NULL,
                        resource TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id)
                    );

                    CREATE TABLE IF NOT EXISTS oauth_codes (
                        code_hash TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        code_challenge TEXT NOT NULL,
                        redirect_uri TEXT NOT NULL,
                        redirect_uri_explicit INTEGER NOT NULL,
                        resource TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id),
                        FOREIGN KEY (subject) REFERENCES users(id)
                    );

                    CREATE TABLE IF NOT EXISTS oauth_tokens (
                        token_hash TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        grant_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id),
                        FOREIGN KEY (subject) REFERENCES users(id)
                    );
                    CREATE INDEX IF NOT EXISTS oauth_tokens_grant_idx
                        ON oauth_tokens (grant_id);

                    CREATE TABLE IF NOT EXISTS oauth_rotated_refresh_tokens (
                        token_hash TEXT PRIMARY KEY,
                        grant_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id)
                    );
                    """
                )
            self._initialized_path = store.path

    @staticmethod
    def _validate_client(client: OAuthClientInformationFull) -> None:
        if not client.redirect_uris or len(client.redirect_uris) > 10:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Provide between one and ten redirect URIs.",
            )
        for uri in client.redirect_uris:
            parsed = urlparse(str(uri))
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.fragment or parsed.username or parsed.password:
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="Redirect URIs may not contain credentials or fragments.",
                )
            if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="Redirect URIs must use HTTPS or a loopback HTTP address.",
                )
        if client.token_endpoint_auth_method not in {
            "none",
            "client_secret_basic",
            "client_secret_post",
        }:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="Unsupported token endpoint authentication method.",
            )
        scopes = set((client.scope or "").split())
        if MCP_SCOPE not in scopes or not scopes.issubset({MCP_SCOPE, OFFLINE_SCOPE}):
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=f"OAuth clients must register the {MCP_SCOPE!r} scope.",
            )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        store = self._store()
        with store.session() as connection:
            row = connection.execute(
                "SELECT client_json FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["client_json"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # MCP SDK 2.0 requires a duplicate form-body client_id for Basic auth.
        # Echo the interoperable form-based method until its token handler is fixed.
        if client_info.token_endpoint_auth_method == "client_secret_basic":
            client_info.token_endpoint_auth_method = "client_secret_post"
        self._validate_client(client_info)
        store = self._store()
        try:
            with store.session() as connection:
                connection.execute(
                    """
                    INSERT INTO oauth_clients (client_id, client_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        client_info.client_id,
                        client_info.model_dump_json(),
                        int(time.time()),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="This OAuth client is already registered.",
            ) from error

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        resource = params.resource or self.resource_url
        if resource.rstrip("/") != self.resource_url:
            raise AuthorizeError(
                error="invalid_target",
                error_description="The requested resource does not match this MCP server.",
            )
        scopes = params.scopes or (client.scope or MCP_SCOPE).split()
        if MCP_SCOPE not in scopes:
            raise AuthorizeError(
                error="invalid_scope",
                error_description=f"The {MCP_SCOPE!r} scope is required.",
            )

        request_id = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + AUTHORIZATION_REQUEST_SECONDS
        store = self._store()
        with store.session() as connection:
            self._remove_expired(connection)
            connection.execute(
                """
                INSERT INTO oauth_pending
                    (request_id, client_id, state, scopes_json, code_challenge,
                     redirect_uri, redirect_uri_explicit, resource, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    client.client_id,
                    params.state,
                    json.dumps(scopes),
                    params.code_challenge,
                    str(params.redirect_uri),
                    int(params.redirect_uri_provided_explicitly),
                    self.resource_url,
                    expires_at,
                ),
            )
        return f"{self.issuer_url}/oauth/login?{urlencode({'request': request_id})}"

    def pending_authorization(self, request_id: str) -> dict[str, str] | None:
        store = self._store()
        with store.session() as connection:
            row = connection.execute(
                """
                SELECT p.expires_at, p.redirect_uri, c.client_json
                FROM oauth_pending AS p
                JOIN oauth_clients AS c ON c.client_id = p.client_id
                WHERE p.request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None or row["expires_at"] < int(time.time()):
                connection.execute(
                    "DELETE FROM oauth_pending WHERE request_id = ?", (request_id,)
                )
                return None
        client = OAuthClientInformationFull.model_validate_json(row["client_json"])
        callback = urlparse(row["redirect_uri"])
        callback_host = callback.hostname or "the connector"
        return {
            "client_name": client.client_name or "An MCP connector",
            "callback_host": callback_host,
            "callback_origin": f"{callback.scheme}://{callback.netloc}",
        }

    def complete_authorization(
        self,
        request_id: str,
        email: str,
        password: str,
    ) -> str:
        store = self._store()
        account = store.authenticate(email, password)
        if account is None:
            raise LoginError("Email or password is incorrect.")

        code = secrets.token_urlsafe(32)
        now = int(time.time())
        with store.session() as connection:
            pending = connection.execute(
                "SELECT * FROM oauth_pending WHERE request_id = ?", (request_id,)
            ).fetchone()
            if pending is None or pending["expires_at"] < now:
                connection.execute(
                    "DELETE FROM oauth_pending WHERE request_id = ?", (request_id,)
                )
                raise LoginError(
                    "This authorization request expired. Return to your connector and try again."
                )
            connection.execute(
                "DELETE FROM oauth_pending WHERE request_id = ?", (request_id,)
            )
            connection.execute(
                """
                INSERT INTO oauth_codes
                    (code_hash, client_id, subject, scopes_json, code_challenge,
                     redirect_uri, redirect_uri_explicit, resource, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _token_hash(code),
                    pending["client_id"],
                    account["id"],
                    pending["scopes_json"],
                    pending["code_challenge"],
                    pending["redirect_uri"],
                    pending["redirect_uri_explicit"],
                    pending["resource"],
                    now + AUTHORIZATION_CODE_SECONDS,
                ),
            )
        return construct_redirect_uri(
            pending["redirect_uri"],
            code=code,
            state=pending["state"],
            iss=self.issuer_url,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        store = self._store()
        with store.session() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_codes
                WHERE code_hash = ? AND client_id = ?
                """,
                (_token_hash(authorization_code), client.client_id),
            ).fetchone()
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            subject=row["subject"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        now = int(time.time())
        store = self._store()
        with store.session() as connection:
            removed = connection.execute(
                """
                DELETE FROM oauth_codes
                WHERE code_hash = ? AND client_id = ? AND expires_at >= ?
                """,
                (_token_hash(authorization_code.code), client.client_id, now),
            ).rowcount
            if removed != 1 or authorization_code.subject is None:
                raise TokenError(
                    error="invalid_grant",
                    error_description="The authorization code is invalid or expired.",
                )
            return self._issue_tokens(
                connection,
                client_id=client.client_id,
                subject=authorization_code.subject,
                scopes=authorization_code.scopes,
                resource=authorization_code.resource or self.resource_url,
            )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> FkeyRefreshToken | None:
        row = self._load_token(refresh_token, "refresh", client.client_id)
        if row is None:
            self._revoke_reused_refresh_token(refresh_token, client.client_id)
            return None
        return FkeyRefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            subject=row["subject"],
            grant_id=row["grant_id"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: FkeyRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        store = self._store()
        now = int(time.time())
        with store.session() as connection:
            valid = connection.execute(
                """
                SELECT resource, expires_at FROM oauth_tokens
                WHERE token_hash = ? AND kind = 'refresh'
                  AND client_id = ? AND expires_at >= ?
                """,
                (_token_hash(refresh_token.token), client.client_id, now),
            ).fetchone()
            if valid is None or refresh_token.subject is None:
                reused = connection.execute(
                    """
                    SELECT grant_id FROM oauth_rotated_refresh_tokens
                    WHERE token_hash = ? AND client_id = ? AND expires_at >= ?
                    """,
                    (_token_hash(refresh_token.token), client.client_id, now),
                ).fetchone()
                if reused is not None:
                    connection.execute(
                        "DELETE FROM oauth_tokens WHERE grant_id = ? AND client_id = ?",
                        (reused["grant_id"], client.client_id),
                    )
                raise TokenError(
                    error="invalid_grant",
                    error_description="The refresh token is invalid or expired.",
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO oauth_rotated_refresh_tokens
                    (token_hash, grant_id, client_id, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    _token_hash(refresh_token.token),
                    refresh_token.grant_id,
                    client.client_id,
                    valid["expires_at"],
                ),
            )
            connection.execute(
                "DELETE FROM oauth_tokens WHERE grant_id = ?",
                (refresh_token.grant_id,),
            )
            return self._issue_tokens(
                connection,
                client_id=client.client_id,
                subject=refresh_token.subject,
                scopes=scopes,
                resource=valid["resource"],
                grant_id=refresh_token.grant_id,
            )

    async def load_access_token(self, token: str) -> FkeyAccessToken | None:
        row = self._load_token(token, "access")
        if row is None or row["resource"].rstrip("/") != self.resource_url:
            return None
        return FkeyAccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes_json"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=row["subject"],
            claims={"iss": self.issuer_url},
            grant_id=row["grant_id"],
        )

    async def revoke_token(
        self,
        token: FkeyAccessToken | FkeyRefreshToken,
    ) -> None:
        store = self._store()
        with store.session() as connection:
            row = connection.execute(
                "SELECT grant_id FROM oauth_tokens WHERE token_hash = ?",
                (_token_hash(token.token),),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "DELETE FROM oauth_tokens WHERE grant_id = ?", (row["grant_id"],)
                )

    def _load_token(
        self,
        token: str,
        kind: str,
        client_id: str | None = None,
    ) -> sqlite3.Row | None:
        store = self._store()
        now = int(time.time())
        sql = """
            SELECT t.* FROM oauth_tokens AS t
            JOIN users AS u ON u.id = t.subject
            WHERE t.token_hash = ? AND t.kind = ? AND t.expires_at >= ?
        """
        parameters: list[str | int] = [_token_hash(token), kind, now]
        if client_id is not None:
            sql += " AND t.client_id = ?"
            parameters.append(client_id)
        with store.session() as connection:
            return connection.execute(sql, parameters).fetchone()

    def _revoke_reused_refresh_token(self, token: str, client_id: str) -> None:
        store = self._store()
        now = int(time.time())
        with store.session() as connection:
            reused = connection.execute(
                """
                SELECT grant_id FROM oauth_rotated_refresh_tokens
                WHERE token_hash = ? AND client_id = ? AND expires_at >= ?
                """,
                (_token_hash(token), client_id, now),
            ).fetchone()
            if reused is not None:
                connection.execute(
                    "DELETE FROM oauth_tokens WHERE grant_id = ? AND client_id = ?",
                    (reused["grant_id"], client_id),
                )

    def _issue_tokens(
        self,
        connection: sqlite3.Connection,
        *,
        client_id: str,
        subject: str,
        scopes: list[str],
        resource: str,
        grant_id: str | None = None,
    ) -> OAuthToken:
        now = int(time.time())
        grant_id = grant_id or secrets.token_hex(16)
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        scopes_json = json.dumps(scopes)
        connection.executemany(
            """
            INSERT INTO oauth_tokens
                (token_hash, kind, grant_id, client_id, subject, scopes_json,
                 resource, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _token_hash(access_token),
                    "access",
                    grant_id,
                    client_id,
                    subject,
                    scopes_json,
                    resource,
                    now + ACCESS_TOKEN_SECONDS,
                    now,
                ),
                (
                    _token_hash(refresh_token),
                    "refresh",
                    grant_id,
                    client_id,
                    subject,
                    scopes_json,
                    resource,
                    now + REFRESH_TOKEN_SECONDS,
                    now,
                ),
            ],
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    @staticmethod
    def _remove_expired(connection: sqlite3.Connection) -> None:
        now = int(time.time())
        connection.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        connection.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
        connection.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
        connection.execute(
            "DELETE FROM oauth_rotated_refresh_tokens WHERE expires_at < ?", (now,)
        )
