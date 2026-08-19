from __future__ import annotations

import unittest

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from fkey.rate_limit import Limit, RateLimitMiddleware, RateLimitUnavailable


class FakeLimiter:
    def __init__(self, *, allowed: bool = True, unavailable: bool = False):
        self.allowed = allowed
        self.unavailable = unavailable
        self.calls: list[tuple[str, Limit]] = []

    def allow(self, key: str, limit: Limit) -> bool:
        self.calls.append((key, limit))
        if self.unavailable:
            raise RateLimitUnavailable
        return self.allowed


def _app(limiter: FakeLimiter, *, trust_proxy_headers: bool = False) -> Starlette:
    async def endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/oauth/login", endpoint, methods=["POST"]),
            Route("/mcp", endpoint, methods=["POST"]),
        ]
    )
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        trust_proxy_headers=trust_proxy_headers,
        limits={("POST", "/oauth/login"): Limit(2, 60)},
    )
    return app


class RateLimitMiddlewareTest(unittest.TestCase):
    def test_limits_auth_route_using_trusted_proxy_ip(self) -> None:
        limiter = FakeLimiter(allowed=False)
        with TestClient(_app(limiter, trust_proxy_headers=True)) as client:
            response = client.post(
                "/oauth/login", headers={"CF-Connecting-IP": "203.0.113.8"}
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            limiter.calls,
            [("POST:/oauth/login:203.0.113.8", Limit(2, 60))],
        )

    def test_ignores_forwarded_ip_unless_explicitly_trusted(self) -> None:
        limiter = FakeLimiter()
        with TestClient(_app(limiter)) as client:
            response = client.post(
                "/oauth/login", headers={"CF-Connecting-IP": "203.0.113.8"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("203.0.113.8", limiter.calls[0][0])

    def test_does_not_limit_regular_mcp_requests(self) -> None:
        limiter = FakeLimiter(allowed=False)
        with TestClient(_app(limiter)) as client:
            response = client.post("/mcp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(limiter.calls, [])

    def test_fails_closed_when_redis_is_unavailable(self) -> None:
        limiter = FakeLimiter(unavailable=True)
        with TestClient(_app(limiter)) as client:
            response = client.post("/oauth/login")

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()

