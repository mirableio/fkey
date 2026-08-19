"""SQLite connection layer and stable facade for the fkey database."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BACKBONE_FIELDS = {"id", "created_at", "updated_at", "extra"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def quote_identifier(name: str) -> str:
    if not IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Invalid identifier {name!r}; use lowercase letters, numbers, and underscores."
        )
    return f'"{name}"'


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.casefold())
    slug: list[str] = []
    separator_pending = False
    for character in normalized:
        if unicodedata.category(character).startswith("M"):
            continue
        if character.isalnum():
            if separator_pending and slug:
                slug.append("-")
            slug.append(character)
            separator_pending = False
        else:
            separator_pending = True
    return "".join(slug) or "record"


class TasteDB:
    """A single personal taste database exposed through a stable facade."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.backup_dir = self.path.parent / "backups"
        self._lock = threading.RLock()
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if is_new:
            self.bootstrap()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def collection_names(self, connection: sqlite3.Connection | None = None) -> list[str]:
        owns_connection = connection is None
        connection = connection or self.connect()
        try:
            rows = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE '\\_%' ESCAPE '\\'
                  AND name NOT LIKE 'sqlite_%'
                  AND name != 'links'
                ORDER BY name
                """
            ).fetchall()
            return [row["name"] for row in rows]
        finally:
            if owns_connection:
                connection.close()

    def _require_collection(
        self, connection: sqlite3.Connection, collection: str
    ) -> None:
        if collection not in self.collection_names(connection):
            choices = self.collection_names(connection)
            suggestion = difflib.get_close_matches(collection, choices, n=1)
            hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
            raise ValueError(
                f"Unknown collection {collection!r}.{hint} "
                "Use create_collection for a new type."
            )

    @staticmethod
    def _table_info(
        connection: sqlite3.Connection, collection: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            f"PRAGMA table_info({quote_identifier(collection)})"
        ).fetchall()

    def bootstrap(self) -> None:
        schema_ops.bootstrap(self)

    def create_collection(
        self,
        name: str,
        description: str,
        fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return schema_ops.create_collection(self, name, description, fields)

    def describe_schema(self, collection: str | None = None) -> dict[str, Any]:
        return schema_ops.describe_schema(self, collection)

    def add_record(
        self,
        collection: str,
        values: dict[str, Any],
        extra: dict[str, Any] | None = None,
        *,
        record_id: str | None = None,
    ) -> dict[str, Any]:
        return record_ops.add_record(
            self, collection, values, extra, record_id=record_id
        )

    def get_record(self, collection: str, record_id: str) -> dict[str, Any]:
        return record_ops.get_record(self, collection, record_id)

    def update_record(
        self,
        collection: str,
        record_id: str,
        values: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return record_ops.update_record(self, collection, record_id, values, extra)

    def delete_record(self, collection: str, record_id: str) -> dict[str, Any]:
        return record_ops.delete_record(self, collection, record_id)

    def find_records(
        self,
        collection: str,
        filters: dict[str, Any] | None = None,
        text: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return record_ops.find_records(self, collection, filters, text, limit)

    def add_link(
        self,
        from_collection: str,
        from_id: str,
        to_collection: str,
        to_id: str,
        kind: str | None = None,
        values: dict[str, Any] | None = None,
        props: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return record_ops.add_link(
            self,
            from_collection,
            from_id,
            to_collection,
            to_id,
            kind,
            values,
            props,
        )

    def remove_link(
        self,
        from_collection: str,
        from_id: str,
        to_collection: str,
        to_id: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        return record_ops.remove_link(
            self, from_collection, from_id, to_collection, to_id, kind
        )

    def query(
        self,
        sql: str,
        parameters: Sequence[Any] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return migration_ops.query(self, sql, parameters, limit)

    def snapshot(self, trigger: str, label: str = "") -> str:
        return migration_ops.snapshot(self, trigger, label)

    def migrate(self, description: str, statements: list[str]) -> dict[str, Any]:
        return migration_ops.migrate(self, description, statements)

    def list_snapshots(self) -> list[dict[str, str]]:
        return migration_ops.list_snapshots(self)

    def restore_snapshot(self, filename: str) -> dict[str, Any]:
        return migration_ops.restore_snapshot(self, filename)


# Operation modules depend on the shared helpers and TasteDB type above.
from fkey import migrations as migration_ops  # noqa: E402
from fkey import records as record_ops  # noqa: E402
from fkey import schema as schema_ops  # noqa: E402
