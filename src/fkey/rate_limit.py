"""Small Redis-backed HTTP rate limiter for public auth endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: int


AUTH_LIMITS: dict[tuple[str, str], Limit] = {
    ("POST", "/oauth/login"): Limit(10, 60),
    ("POST", "/app/login"): Limit(10, 60),
    ("POST", "/signup"): Limit(5, 300),
    ("GET", "/authorize"): Limit(30, 60),
    ("POST", "/authorize"): Limit(30, 60),
    ("POST", "/register"): Limit(10, 60),
    ("POST", "/token"): Limit(30, 60),
    ("POST", "/revoke"): Limit(30, 60),
}

_INCREMENT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class RateLimitUnavailable(RuntimeError):
    """Raised when configured rate-limit storage cannot be reached."""


class RedisRateLimiter:
    """Atomic fixed-window counters shared by every application process."""

    def __init__(self, redis_url: str):
        self._redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )

    def allow(self, key: str, limit: Limit) -> bool:
        try:
            count = int(
                self._redis.eval(
                    _INCREMENT,
                    1,
                    f"fkey:rate:{key}",
                    limit.window_seconds,
                )
            )
        except (RedisError, TypeError, ValueError) as error:
            raise RateLimitUnavailable from error
        return count <= limit.requests


def _header(scope: Scope, name: bytes) -> str | None:
    for header_name, value in scope.get("headers", []):
        if header_name == name:
            return value.decode("latin-1")
    return None


def _valid_ip(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _client_ip(scope: Scope, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        forwarded = _header(scope, b"cf-connecting-ip")
        if forwarded is None:
            chain = _header(scope, b"x-forwarded-for")
            forwarded = chain.split(",", 1)[0] if chain else None
        valid_forwarded = _valid_ip(forwarded)
        if valid_forwarded is not None:
            return valid_forwarded
    client = scope.get("client")
    return _valid_ip(client[0] if client else None) or "unknown"


def _limited_response(path: str, status_code: int) -> PlainTextResponse | JSONResponse:
    message = (
        "Too many requests. Try again later."
        if status_code == 429
        else "Authentication is temporarily unavailable."
    )
    headers = {"Cache-Control": "no-store"}
    if path in {"/oauth/login", "/app/login", "/signup"}:
        return PlainTextResponse(message, status_code=status_code, headers=headers)
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": message},
        status_code=status_code,
        headers=headers,
    )


class RateLimitMiddleware:
    """Limit auth routes by client IP; unrelated MCP traffic is untouched."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RedisRateLimiter,
        trust_proxy_headers: bool = False,
        limits: dict[tuple[str, str], Limit] | None = None,
    ):
        self.app = app
        self.limiter = limiter
        self.trust_proxy_headers = trust_proxy_headers
        self.limits = limits or AUTH_LIMITS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        path = scope["path"]
        limit = self.limits.get((method, path))
        if limit is None:
            await self.app(scope, receive, send)
            return

        client_ip = _client_ip(scope, self.trust_proxy_headers)
        key = f"{method}:{path}:{client_ip}"
        try:
            allowed = await asyncio.to_thread(self.limiter.allow, key, limit)
        except RateLimitUnavailable:
            response = _limited_response(path, 503)
        else:
            if allowed:
                await self.app(scope, receive, send)
                return
            response = _limited_response(path, 429)
        await response(scope, receive, send)
