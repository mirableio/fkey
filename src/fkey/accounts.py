"""Small account store and per-user database provisioning."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fkey.engine import TasteDB, utc_now

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 5


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hash_password(password: str) -> str:
    salt = uuid.uuid4().bytes
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_encode(salt)}${_encode(digest)}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = _decode(raw_salt)
        expected = _decode(raw_digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _password_needs_rehash(encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, _, _ = encoded.split("$")
        parameters = (int(raw_n), int(raw_r), int(raw_p))
    except (TypeError, ValueError):
        return True
    return algorithm != "scrypt" or parameters != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


class AccountStore:
    """Central identities plus one provisioned TasteDB file per account."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.path = self.data_dir / "accounts.sqlite"
        self.user_data_dir = self.data_dir / "users"
        self._lock = threading.RLock()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """Open a serialized transaction against the central account database."""
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self.session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def database_path(self, user_id: str) -> Path:
        try:
            normalized = uuid.UUID(user_id).hex
        except ValueError as error:
            raise ValueError("Invalid internal user id.") from error
        return self.user_data_dir / normalized / "db.sqlite"

    def create_account(self, name: str, email: str, password: str) -> dict[str, str]:
        name = name.strip()
        email = email.strip().casefold()
        if not name or len(name) > 100:
            raise ValueError("Name must be between 1 and 100 characters.")
        if len(email) > 254 or EMAIL_RE.fullmatch(email) is None:
            raise ValueError("Enter a valid email address.")
        if len(password) < 10:
            raise ValueError("Password must contain at least 10 characters.")
        if len(password) > 1024:
            raise ValueError("Password is too long.")

        user_id = uuid.uuid4().hex
        password_hash = _hash_password(password)
        with self.session() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (id, email, name, password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, email, name, password_hash, utc_now()),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "An account with this email already exists."
                ) from error

            database = TasteDB(self.database_path(user_id))
            database.update_record("profiles", "self", {"name": name})

        return {"id": user_id, "name": name, "email": email}

    def authenticate(self, email: str, password: str) -> dict[str, str] | None:
        normalized_email = email.strip().casefold()
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT id, email, name, password_hash
                FROM users WHERE email = ?
                """,
                (normalized_email,),
            ).fetchone()
        if row is None or not _password_matches(password, row["password_hash"]):
            return None
        if _password_needs_rehash(row["password_hash"]):
            upgraded_hash = _hash_password(password)
            with self.session() as connection:
                connection.execute(
                    """
                    UPDATE users SET password_hash = ?
                    WHERE id = ? AND password_hash = ?
                    """,
                    (upgraded_hash, row["id"], row["password_hash"]),
                )
        return {"id": row["id"], "name": row["name"], "email": row["email"]}

    def account_count(self) -> int:
        with self.session() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def has_account(self, user_id: str) -> bool:
        with self.session() as connection:
            row = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return row is not None
