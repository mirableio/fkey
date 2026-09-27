"""Read-only SQL, migrations, snapshots, and point-in-time restore."""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fkey.engine import (
    BACKBONE_FIELDS,
    IDENTIFIER_RE,
    json_dumps,
    quote_identifier,
    slugify,
    utc_now,
)

if TYPE_CHECKING:
    from fkey.engine import TasteDB


_DENIED_MIGRATION_PRAGMAS = {
    "data_store_directory",
    "foreign_keys",
    "ignore_check_constraints",
    "journal_mode",
    "locking_mode",
    "schema_version",
    "temp_store_directory",
    "trusted_schema",
    "writable_schema",
}
SNAPSHOT_RETENTION = 15
SNAPSHOT_LABEL_MAX_BYTES = 48
QUERY_TIMEOUT_SECONDS = 30


def _snapshot_label(value: str) -> str:
    slug = slugify(value)
    encoded = slug.encode("utf-8")
    if len(encoded) <= SNAPSHOT_LABEL_MAX_BYTES:
        return slug
    return encoded[:SNAPSHOT_LABEL_MAX_BYTES].decode("utf-8", "ignore").rstrip("-")


def query(
    db: TasteDB,
    sql: str,
    parameters: Sequence[Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, flags=re.IGNORECASE):
        raise ValueError("query accepts a SELECT or WITH statement only")
    connection = db.connect()
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    connection.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    try:
        connection.execute("PRAGMA query_only = ON")
        try:
            cursor = connection.execute(sql, list(parameters or []))
            rows = cursor.fetchmany(max(1, int(limit)) + 1)
        except sqlite3.OperationalError as error:
            if str(error) != "interrupted":
                raise
            raise ValueError(
                f"query stopped after {QUERY_TIMEOUT_SECONDS} seconds; narrow it "
                "or aggregate less."
            ) from error
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns: list[str] = []
        used_columns: set[str] = set()
        for item in cursor.description or []:
            raw_name = item[0]
            column = raw_name
            suffix = 2
            while column in used_columns:
                column = f"{raw_name}_{suffix}"
                suffix += 1
            columns.append(column)
            used_columns.add(column)
        return {
            "columns": columns,
            "rows": [dict(zip(columns, row, strict=True)) for row in rows],
            "truncated": truncated,
        }
    finally:
        connection.close()


def snapshot(db: TasteDB, trigger: str, label: str = "") -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S%f")
    suffix = f"--{_snapshot_label(label)}" if label else ""
    filename = f"{timestamp}--{trigger}{suffix}.sqlite"
    with db._lock:
        db.backup_dir.mkdir(parents=True, exist_ok=True)
        destination_path = db.backup_dir / filename
        with db.session() as source:
            destination = sqlite3.connect(destination_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        for old_snapshot in sorted(
            db.backup_dir.glob("*.sqlite"), reverse=True
        )[SNAPSHOT_RETENTION:]:
            old_snapshot.unlink()
    return filename


def _next_migration_id(db: TasteDB) -> int:
    with db.session() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM _migrations"
        ).fetchone()
        return int(row["next_id"])


def _check_link_endpoints(db: TasteDB, connection: sqlite3.Connection) -> None:
    collections = set(db.collection_names(connection))
    for row in connection.execute(
        "SELECT from_collection, from_id, to_collection, to_id FROM links"
    ):
        for side in ("from", "to"):
            collection = row[f"{side}_collection"]
            record_id = row[f"{side}_id"]
            if collection not in collections:
                raise ValueError(f"Link refers to missing collection {collection!r}.")
            found = connection.execute(
                f"SELECT 1 FROM {quote_identifier(collection)} WHERE id = ?", (record_id,)
            ).fetchone()
            if found is None:
                raise ValueError(
                    f"Link refers to missing record {collection}/{record_id}."
                )


def _check_required_structures(db: TasteDB, connection: sqlite3.Connection) -> None:
    required_columns = {
        "_meta": {"scope", "collection", "name", "description", "value_json"},
        "_migrations": {
            "id",
            "created_at",
            "description",
            "statements_json",
            "backup_file",
        },
        "profiles": {"id", "name", "relation", "created_at", "updated_at", "extra"},
        "links": {
            "from_collection",
            "from_id",
            "to_collection",
            "to_id",
            "kind",
            "rating",
            "first_at",
            "last_at",
            "note",
            "props",
            "created_at",
            "updated_at",
        },
    }
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table, expected in required_columns.items():
        if table not in tables:
            raise ValueError(f"Migration removed required table {table!r}.")
        actual = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM pragma_table_info(?)", (table,)
            )
        }
        missing = sorted(expected - actual)
        if missing:
            raise ValueError(
                f"Migration removed required columns from {table!r}: "
                f"{', '.join(missing)}."
            )

    for collection in db.collection_names(connection):
        fields = {row["name"] for row in db._table_info(connection, collection)}
        missing = sorted(BACKBONE_FIELDS - fields)
        if missing:
            raise ValueError(
                f"Collection {collection!r} is missing backbone columns: "
                f"{', '.join(missing)}."
            )
        invalid = sorted(field for field in fields if not IDENTIFIER_RE.fullmatch(field))
        if invalid:
            raise ValueError(
                f"Collection {collection!r} has invalid field names: "
                f"{', '.join(invalid)}; use lowercase letters, numbers, and underscores."
            )

    if connection.execute("SELECT 1 FROM profiles WHERE id = 'self'").fetchone() is None:
        raise ValueError("Migration removed the stable 'self' profile.")


def _reconcile_meta(db: TasteDB, connection: sqlite3.Connection) -> list[str]:
    warnings: list[str] = []
    collections = set(db.collection_names(connection)) | {"links"}
    collection_rows = connection.execute(
        "SELECT collection FROM _meta WHERE scope = 'collection'"
    ).fetchall()
    for row in collection_rows:
        if row["collection"] not in collections:
            connection.execute(
                "DELETE FROM _meta WHERE scope = 'collection' AND collection = ?",
                (row["collection"],),
            )
    rating_scale_rows = connection.execute(
        """
        SELECT collection FROM _meta
        WHERE scope = 'rating_scale' AND collection != ''
        """
    ).fetchall()
    for row in rating_scale_rows:
        if row["collection"] not in collections:
            connection.execute(
                "DELETE FROM _meta WHERE scope = 'rating_scale' AND collection = ?",
                (row["collection"],),
            )
    rows = connection.execute(
        "SELECT collection, name FROM _meta WHERE scope = 'field'"
    ).fetchall()
    for row in rows:
        collection = row["collection"]
        field = row["name"]
        if collection not in collections:
            connection.execute(
                "DELETE FROM _meta WHERE scope = 'field' AND collection = ? AND name = ?",
                (collection, field),
            )
            continue
        fields = {item["name"] for item in db._table_info(connection, collection)}
        if field not in fields:
            connection.execute(
                "DELETE FROM _meta WHERE scope = 'field' AND collection = ? AND name = ?",
                (collection, field),
            )
    for collection in collections:
        described = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM _meta WHERE scope = 'field' AND collection = ?",
                (collection,),
            )
        }
        for field in db._table_info(connection, collection):
            if field["name"] not in described and field["name"] not in BACKBONE_FIELDS:
                warnings.append(
                    f"{collection}.{field['name']} has no description in _meta"
                )
    return warnings


def _validate_database(db: TasteDB, connection: sqlite3.Connection) -> None:
    _check_required_structures(db, connection)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"SQLite integrity_check failed: {integrity}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError(f"foreign_key_check failed: {foreign_key_errors[0]}")
    _check_link_endpoints(db, connection)


def migrate(
    db: TasteDB, description: str, statements: list[str]
) -> dict[str, Any]:
    if not statements:
        raise ValueError("migrate requires at least one SQL statement")
    with db._lock:
        return _migrate(db, description, statements)


def _migrate(
    db: TasteDB, description: str, statements: list[str]
) -> dict[str, Any]:
    migration_id = _next_migration_id(db)
    backup_file = snapshot(db, "migration", f"{migration_id}-{description}")
    connection = db.connect()
    rejected_operation: list[str] = []

    def migration_authorizer(
        action: int,
        argument: str | None,
        _argument_2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_TRANSACTION:
            rejected_operation.append(
                "Migration statements may not manage transactions "
                f"({argument or 'transaction control'}); migrate does that automatically."
            )
            return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
            operation = "ATTACH" if action == sqlite3.SQLITE_ATTACH else "DETACH"
            rejected_operation.append(
                f"{operation} is not allowed; migrations are confined to the fkey database."
            )
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            pragma = (argument or "").lower()
            if _argument_2 is not None and pragma in _DENIED_MIGRATION_PRAGMAS:
                rejected_operation.append(
                    f"PRAGMA {pragma} may not be changed during a migration."
                )
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            function = (_argument_2 or argument or "").lower()
            if function == "load_extension":
                rejected_operation.append(
                    "load_extension is not allowed during a migration."
                )
                return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            table = (argument or "").lower()
            if table == "_migrations":
                rejected_operation.append(
                    f"Writes to {table} are managed by the migration system."
                )
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        connection.isolation_level = None
        connection.execute("BEGIN IMMEDIATE")
        connection.set_authorizer(migration_authorizer)
        try:
            for statement in statements:
                connection.execute(statement)
        finally:
            connection.set_authorizer(None)
        _validate_database(db, connection)
        warnings = _reconcile_meta(db, connection)
        cursor = connection.execute(
            """
            INSERT INTO _migrations
                (created_at, description, statements_json, backup_file)
            VALUES (?, ?, ?, ?)
            """,
            (utc_now(), description, json_dumps(statements), backup_file),
        )
        connection.execute("COMMIT")
    except Exception as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        # Nothing changed, so the snapshot would only crowd out real restore points.
        (db.backup_dir / backup_file).unlink(missing_ok=True)
        detail: Exception = error
        if rejected_operation:
            detail = ValueError(rejected_operation[0])
        raise ValueError(
            "Migration failed; no changes were committed. "
            f"{type(detail).__name__}: {detail}"
        ) from error
    finally:
        connection.close()
    return {
        "migration_id": cursor.lastrowid,
        "snapshot": backup_file,
        "warnings": warnings,
        "schema": db.describe_schema(),
    }


def list_snapshots(db: TasteDB) -> list[dict[str, str]]:
    snapshots: list[dict[str, str]] = []
    with db._lock:
        for path in sorted(db.backup_dir.glob("*.sqlite"), reverse=True):
            parts = path.stem.split("--")
            snapshots.append(
                {
                    "file": path.name,
                    "timestamp": parts[0],
                    "trigger": parts[1] if len(parts) > 1 else "unknown",
                    "label": parts[2] if len(parts) > 2 else "",
                }
            )
    return snapshots


def restore_snapshot(db: TasteDB, filename: str) -> dict[str, Any]:
    with db._lock:
        available = {item["file"] for item in list_snapshots(db)}
        if filename not in available:
            raise ValueError(f"Unknown snapshot {filename!r}. Use list_snapshots first.")
        source = db.backup_dir / filename
        temporary = db.path.with_suffix(".restore.tmp")
        shutil.copy2(source, temporary)
        candidate = sqlite3.connect(f"{temporary.resolve().as_uri()}?mode=ro", uri=True)
        candidate.row_factory = sqlite3.Row
        try:
            candidate.execute("PRAGMA query_only = ON")
            _validate_database(db, candidate)
        except Exception as error:
            candidate.close()
            temporary.unlink(missing_ok=True)
            raise ValueError(
                f"Snapshot {filename!r} failed validation; the live database was "
                f"not changed. {type(error).__name__}: {error}"
            ) from error
        finally:
            candidate.close()

        safety = snapshot(db, "restore-safety")
        for suffix in ("-wal", "-shm"):
            auxiliary = Path(f"{db.path}{suffix}")
            if auxiliary.exists():
                auxiliary.unlink()
        temporary.replace(db.path)
        with db.session() as connection:
            _validate_database(db, connection)
    return {
        "restored": filename,
        "safety_snapshot": safety,
        "schema": db.describe_schema(),
    }
