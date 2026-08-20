from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from fkey.accounts import AccountStore
from fkey.engine import TasteDB
from fkey.server import configure_accounts, http_app


class AccountStoreTest(unittest.TestCase):
    def test_account_is_normalized_hashed_and_provisioned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AccountStore(Path(temporary) / "data")
            account = store.create_account(
                "Dima", "  DIMA@Example.COM ", "correct horse battery staple"
            )

            self.assertEqual(account["email"], "dima@example.com")
            self.assertEqual(
                store.authenticate("DIMA@example.com", "correct horse battery staple"),
                account,
            )
            self.assertIsNone(store.authenticate(account["email"], "wrong password"))
            database = TasteDB(store.database_path(account["id"]))
            self.assertEqual(
                database.get_record("profiles", "self")["record"]["name"], "Dima"
            )

            with self.assertRaisesRegex(ValueError, "already exists"):
                store.create_account(
                    "Someone else", "dima@example.com", "another secure password"
                )

    def test_successful_login_upgrades_older_scrypt_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AccountStore(Path(temporary) / "data")
            with patch("fkey.accounts.SCRYPT_P", 1):
                account = store.create_account(
                    "Dima", "dima@example.com", "correct horse battery staple"
                )

            authenticated = store.authenticate(
                account["email"], "correct horse battery staple"
            )
            self.assertEqual(authenticated, account)
            with store.session() as connection:
                password_hash = connection.execute(
                    "SELECT password_hash FROM users WHERE id = ?", (account["id"],)
                ).fetchone()[0]
            self.assertEqual(password_hash.split("$")[3], "5")


class SignupPageTest(unittest.TestCase):
    def test_signup_requires_configured_code_and_creates_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = configure_accounts(Path(temporary) / "data")
            app = http_app()
            with (
                TestClient(app, base_url="http://127.0.0.1") as client,
                patch.dict(os.environ, {"FKEY_SIGNUP_CODE": "invite-123"}),
            ):
                page = client.get("/signup")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Create your account", page.text)
                nonce = re.search(r'<script nonce="([a-f0-9]+)">', page.text)
                self.assertIsNotNone(nonce)
                self.assertIn(
                    f"script-src 'nonce-{nonce.group(1)}'",
                    page.headers["content-security-policy"],
                )
                self.assertIn(
                    'data-submitting-label="Creating account…"', page.text
                )
                self.assertIn("button.disabled = true", page.text)

                rejected = client.post(
                    "/signup",
                    data={
                        "name": "Dima",
                        "email": "dima@example.com",
                        "password": "correct horse battery staple",
                        "signup_code": "wrong",
                    },
                )
                self.assertEqual(rejected.status_code, 403)
                self.assertEqual(store.account_count(), 0)

                created = client.post(
                    "/signup",
                    data={
                        "name": "Dima",
                        "email": "dima@example.com",
                        "password": "correct horse battery staple",
                        "signup_code": "invite-123",
                    },
                )
                self.assertEqual(created.status_code, 201)
                self.assertIn("Account created", created.text)
                self.assertEqual(store.account_count(), 1)

    def test_signup_is_disabled_without_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configure_accounts(Path(temporary) / "data")
            app = http_app()
            with (
                patch.dict(os.environ, {}, clear=True),
                TestClient(app, base_url="http://127.0.0.1") as client,
            ):
                response = client.get("/signup")

            self.assertEqual(response.status_code, 503)
            self.assertIn("not configured", response.text)


if __name__ == "__main__":
    unittest.main()
