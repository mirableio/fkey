from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from starlette.testclient import TestClient

from fkey.oauth import WEB_CLIENT_ID
from fkey.rate_limit import AUTH_LIMITS
from fkey.server import configure_accounts, http_app, oauth_provider, user_engine
from fkey.ui import AG_GRID
from fkey.web import SESSION_COOKIE

PASSWORD = "correct horse battery staple"


class BrowseUITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = configure_accounts(Path(self.temporary.name) / "data")
        owner = self.store.create_account("Dima", "dima@example.com", PASSWORD)
        self.store.create_account("Other", "other@example.com", PASSWORD)
        database = user_engine(owner["id"])
        self.barolo = database.add_record(
            "wines",
            {"name": "Бароло Bussia", "vintage": 2016, "country": "Italy"},
            {
                "grape": "Nebbiolo",
                "shop": "https://example.com/barolo",
                "pairing": ["lamb", "truffle risotto"],
            },
        )["id"]
        database.add_record("wines", {"name": "Chablis", "vintage": 2021})
        self.hostile = database.add_record(
            "wines",
            {"name": "<script>alert(1)</script>", "vintage": 2020},
            {"link": "javascript:alert(1)"},
        )["id"]
        database.add_record("profiles", {"name": "Anna", "relation": "wife"})
        database.add_link(
            "profiles", "self", "wines", self.barolo,
            values={"rating": 9, "at": "2026-07-02", "note": "with lamb"},
        )
        database.add_link("profiles", "anna", "wines", self.barolo, values={"rating": 8})
        database.add_record("people", {"name": "Giacomo Conterno", "kind": "winemaker"})
        database.add_link("people", "giacomo-conterno", "wines", self.barolo, kind="made")

        self.client = TestClient(http_app(), base_url="http://127.0.0.1")
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def sign_in(self, email: str = "dima@example.com", **headers: str):
        return self.client.post(
            "/app/login",
            data={"email": email, "password": PASSWORD, "next": "/app/c/wines"},
            headers=headers,
            follow_redirects=False,
        )

    def get(self, url: str):
        return self.client.get(url, follow_redirects=False)

    def test_signed_out_pages_redirect_to_sign_in(self) -> None:
        self.assertEqual(self.get("/").headers["location"], "/app")
        response = self.get("/app/c/wines?q=x")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"], "/app/login?next=%2Fapp%2Fc%2Fwines%3Fq%3Dx"
        )
        page = self.get("/app/login?next=//evil.example").text
        self.assertIn('name="next" value="/app"', page)

    def test_sign_in_session_and_sign_out(self) -> None:
        wrong = self.client.post(
            "/app/login",
            data={"email": "dima@example.com", "password": "wrong password"},
            follow_redirects=False,
        )
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn(SESSION_COOKIE, wrong.headers.get("set-cookie", ""))
        self.assertEqual(self.sign_in(origin="https://evil.example").status_code, 403)

        response = self.sign_in(origin="http://127.0.0.1")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/app/c/wines")
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("httponly", "samesite=lax", "path=/app"):
            self.assertIn(attribute, cookie)
        self.assertEqual(self.get("/app").headers["location"], "/app/c/wines")

        token = self.client.cookies.get(SESSION_COOKIE)
        mcp = self.client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(mcp.status_code, 401)
        self.assertIsNone(asyncio.run(oauth_provider.get_client(WEB_CLIENT_ID)))

        signed_out = self.client.post("/app/logout", follow_redirects=False)
        self.assertEqual(signed_out.headers["location"], "/app/login")
        self.assertIsNone(oauth_provider.session_subject(token))
        self.client.cookies.set(SESSION_COOKIE, token, path="/app")
        self.assertTrue(self.get("/app").headers["location"].startswith("/app/login"))

    def test_users_only_see_their_own_records(self) -> None:
        self.sign_in("other@example.com")

        self.assertEqual(self.get(f"/app/c/wines/{self.barolo}").status_code, 404)
        self.assertNotIn("Bussia", self.get("/app/c/wines").text)

    def grid_data(self, url: str) -> dict:
        page = self.get(url).text
        match = re.search(
            r'<script type="application/json" id="grid-data">(.*?)</script>', page, re.S
        )
        self.assertIsNotNone(match, url)
        return json.loads(match.group(1))

    def test_grid_hands_rows_and_columns_to_the_browser(self) -> None:
        self.sign_in()

        data = self.grid_data("/app/c/wines?q=бароло")
        self.assertEqual(
            [(column["key"], column["kind"]) for column in data["columns"]],
            [
                ("name", "primary"),
                ("vintage", "number"),
                ("country", "text"),
                ("_rating", "number"),
                ("_last_at", "date"),
                ("extra", "pills"),
            ],
        )
        rating = next(column for column in data["columns"] if column["key"] == "_rating")
        self.assertEqual(rating["description"], "Your rating, out of 10.")
        rows = {row["id"]: row for row in data["rows"]}
        self.assertEqual(len(rows), 3)
        barolo = rows[self.barolo]
        self.assertEqual(barolo["href"], f"/app/c/wines/{quote(self.barolo)}")
        self.assertEqual(barolo["values"]["_rating"], 9)
        self.assertEqual(barolo["values"]["_last_at"], "2026-07-02")
        self.assertIn(
            {"key": "shop", "value": "https://example.com/barolo",
             "url": "https://example.com/barolo"},
            barolo["values"]["extra"],
        )
        self.assertEqual(
            rows[self.hostile]["values"]["name"], "<script>alert(1)</script>"
        )
        self.assertIn('value="бароло"', self.get("/app/c/wines?q=бароло").text)

        people = [column["key"] for column in self.grid_data("/app/c/profiles")["columns"]]
        self.assertNotIn("_rating", people)
        self.assertIn("No books yet", self.get("/app/c/books").text)
        with patch("fkey.ui.GRID_ROW_LIMIT", 2):
            self.assertIn("Showing the 2 most recently updated", self.get("/app/c/wines").text)

    def test_record_pages_navigate_links_and_escape_values(self) -> None:
        self.sign_in()

        wine = self.get(f"/app/c/wines/{self.barolo}").text
        self.assertIn('href="/app/c/profiles/anna"', wine)
        self.assertIn("8 / 10", wine)
        self.assertIn("with lamb", wine)
        # Each extra key is its own full field row, not a truncated pill.
        self.assertIn("<span>pairing</span>", wine)
        self.assertIn(">lamb, truffle risotto</span>", wine)
        self.assertIn("← made", wine)
        self.assertIn('href="/app/c/people/giacomo-conterno"', wine)

        person = self.get("/app/c/people/giacomo-conterno").text
        self.assertIn("made →", person)
        self.assertIn(f'href="/app/c/wines/{quote(self.barolo)}"', person)

        anna = self.get("/app/c/profiles/anna").text
        self.assertRegex(anna, r"Wines <span class=\"count\">1</span>")

        hostile = self.get(f"/app/c/wines/{self.hostile}").text
        self.assertNotIn("<script>alert", hostile)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", hostile)
        self.assertNotIn('href="javascript:', hostile)
        shop = self.get(f"/app/c/wines/{self.barolo}").text
        self.assertIn('href="https://example.com/barolo"', shop)
        self.assertIn('rel="noopener noreferrer"', shop)

        self.assertEqual(self.get("/app/c/wines/missing").status_code, 404)
        self.assertEqual(self.get("/app/c/nope").status_code, 404)

    def test_pages_send_security_headers(self) -> None:
        self.sign_in()

        page = self.get("/app/c/wines")
        policy = page.headers["content-security-policy"]
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self';"):
            self.assertIn(directive, policy)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.headers["referrer-policy"], "same-origin")
        # Row data is JSON inside a script element; markup in it must stay inert.
        self.assertNotIn("<script>alert", page.text)

        self.assertEqual(self.get("/app/static/ui.js").headers["cache-control"], "no-cache")
        grid = self.get(f"/app/static/{AG_GRID}/ag-grid-community.min.noStyle.js")
        self.assertEqual(grid.status_code, 200)
        self.assertIn("immutable", grid.headers["cache-control"])
        self.assertIn(f"/app/static/{AG_GRID}/ag-grid-community.min.noStyle.js", page.text)
        for missing in ("other.js", f"{AG_GRID}/LICENSE.txt", "../ui.py"):
            self.assertEqual(self.get(f"/app/static/{missing}").status_code, 404, missing)
        self.assertIn(("POST", "/app/login"), AUTH_LIMITS)


if __name__ == "__main__":
    unittest.main()
