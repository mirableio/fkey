"""Validated record and link operations for a personal taste database."""

from __future__ import annotations

import difflib
import sqlite3
from datetime import date
from typing import TYPE_CHECKING, Any

from fkey.engine import (
    BACKBONE_FIELDS,
    json_dumps,
    json_loads,
    quote_identifier,
    slugify,
    utc_now,
)

if TYPE_CHECKING:
    from fkey.engine import TasteDB


def _validate_values(
    db: TasteDB,
    connection: sqlite3.Connection,
    collection: str,
    values: dict[str, Any],
) -> None:
    allowed = {
        row["name"]
        for row in db._table_info(connection, collection)
        if row["name"] not in BACKBONE_FIELDS
    }
    unknown = sorted(set(values) - allowed)
    if not unknown:
        return
    field = unknown[0]
    suggestion = difflib.get_close_matches(field, sorted(allowed), n=1)
    hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
    raise ValueError(
        f"Unknown field {field!r} for collection {collection!r}.{hint} "
        "Put deliberate long-tail data in extra."
    )


def _merge_json(existing: str | None, patch: dict[str, Any] | None) -> str:
    merged = json_loads(existing, {})
    if not isinstance(merged, dict):
        merged = {}
    for key, value in (patch or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return json_dumps(merged)


def _unique_id(
    connection: sqlite3.Connection, collection: str, base: str
) -> str:
    candidate = base
    suffix = 2
    while connection.execute(
        f"SELECT 1 FROM {quote_identifier(collection)} WHERE id = ?", (candidate,)
    ).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _activity_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("at must be an ISO date in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("at must be an ISO date in YYYY-MM-DD format") from error
    if parsed.isoformat() != value:
        raise ValueError("at must be an ISO date in YYYY-MM-DD format")
    return value


def _get_record_row(
    connection: sqlite3.Connection, collection: str, record_id: str
) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT * FROM {quote_identifier(collection)} WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No record {record_id!r} in collection {collection!r}.")
    result = dict(row)
    result["extra"] = json_loads(result.get("extra"), {})
    return result


def decode_record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    if "extra" in result:
        result["extra"] = json_loads(result["extra"], {})
    return result


def decode_link(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["kind"] = result["kind"] or None
    result["props"] = json_loads(result.get("props"), {})
    return result


def add_record(
    db: TasteDB,
    collection: str,
    values: dict[str, Any],
    extra: dict[str, Any] | None = None,
    *,
    record_id: str | None = None,
) -> dict[str, Any]:
    with db._lock, db.session() as connection:
        db._require_collection(connection, collection)
        _validate_values(db, connection, collection, values)
        display = str(
            values.get("title")
            or values.get("name")
            or collection.removesuffix("s")
        )
        year = values.get("year") or values.get("vintage")
        base = slugify(f"{display}-{year}" if year is not None else display)
        if record_id is None:
            record_id = _unique_id(connection, collection, base)
        elif slugify(record_id) != record_id:
            raise ValueError("record_id must be a canonical lowercase slug")

        now = utc_now()
        columns = ["id", *values.keys(), "created_at", "updated_at", "extra"]
        params = [record_id, *values.values(), now, now, json_dumps(extra or {})]
        column_sql = ", ".join(quote_identifier(column) for column in columns)
        placeholders = ", ".join("?" for _ in params)
        connection.execute(
            f"INSERT INTO {quote_identifier(collection)} ({column_sql}) "
            f"VALUES ({placeholders})",
            params,
        )
        return _get_record_row(connection, collection, record_id)


def get_record(db: TasteDB, collection: str, record_id: str) -> dict[str, Any]:
    with db.session() as connection:
        db._require_collection(connection, collection)
        record = _get_record_row(connection, collection, record_id)
        rows = connection.execute(
            """
            SELECT * FROM links
            WHERE (from_collection = ? AND from_id = ?)
               OR (to_collection = ? AND to_id = ?)
            ORDER BY created_at
            """,
            (collection, record_id, collection, record_id),
        ).fetchall()
        return {"record": record, "links": [decode_link(row) for row in rows]}


def update_record(
    db: TasteDB,
    collection: str,
    record_id: str,
    values: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = values or {}
    with db._lock, db.session() as connection:
        db._require_collection(connection, collection)
        _validate_values(db, connection, collection, values)
        current = _get_record_row(connection, collection, record_id)
        assignments: list[str] = []
        params: list[Any] = []
        for field, value in values.items():
            assignments.append(f"{quote_identifier(field)} = ?")
            params.append(value)
        if extra is not None:
            assignments.append("extra = ?")
            params.append(_merge_json(json_dumps(current["extra"]), extra))
        assignments.append("updated_at = ?")
        params.append(utc_now())
        params.append(record_id)
        connection.execute(
            f"UPDATE {quote_identifier(collection)} "
            f"SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        return _get_record_row(connection, collection, record_id)


def delete_record(db: TasteDB, collection: str, record_id: str) -> dict[str, Any]:
    if collection == "profiles" and record_id == "self":
        raise ValueError("The stable self profile cannot be deleted.")
    with db._lock, db.session() as connection:
        db._require_collection(connection, collection)
        record = _get_record_row(connection, collection, record_id)
        connection.execute(
            """
            DELETE FROM links
            WHERE (from_collection = ? AND from_id = ?)
               OR (to_collection = ? AND to_id = ?)
            """,
            (collection, record_id, collection, record_id),
        )
        connection.execute(
            f"DELETE FROM {quote_identifier(collection)} WHERE id = ?", (record_id,)
        )
        return record


def find_records(
    db: TasteDB,
    collection: str,
    filters: dict[str, Any] | None = None,
    text: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    filters = filters or {}
    with db.session() as connection:
        db._require_collection(connection, collection)
        columns = {row["name"]: row for row in db._table_info(connection, collection)}
        unknown = set(filters) - set(columns)
        if unknown:
            raise ValueError(f"Unknown filter field {sorted(unknown)[0]!r}.")

        clauses: list[str] = []
        params: list[Any] = []
        for field, value in filters.items():
            if value is None:
                clauses.append(f"{quote_identifier(field)} IS NULL")
            else:
                clauses.append(f"{quote_identifier(field)} = ?")
                params.append(value)
        if text:
            text_columns = [
                name
                for name, row in columns.items()
                if "TEXT" in (row["type"] or "").upper()
            ]
            clauses.append(
                "("
                + " OR ".join(
                    f"LOWER(COALESCE({quote_identifier(name)}, '')) LIKE LOWER(?)"
                    for name in text_columns
                )
                + ")"
            )
            params.extend([f"%{text}%"] * len(text_columns))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM {quote_identifier(collection)}{where} "
            "ORDER BY updated_at DESC LIMIT ?",
            [*params, max(1, int(limit))],
        ).fetchall()
        return [decode_record(row) for row in rows]


def _record_exists(
    db: TasteDB,
    connection: sqlite3.Connection,
    collection: str,
    record_id: str,
) -> bool:
    db._require_collection(connection, collection)
    return (
        connection.execute(
            f"SELECT 1 FROM {quote_identifier(collection)} WHERE id = ?", (record_id,)
        ).fetchone()
        is not None
    )


def _rating_scale(connection: sqlite3.Connection, collection: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT value_json FROM _meta
        WHERE scope = 'rating_scale' AND collection = ? AND name = 'default'
        """,
        (collection,),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT value_json FROM _meta
            WHERE scope = 'rating_scale' AND collection = '' AND name = 'default'
            """
        ).fetchone()
    return json_loads(row["value_json"] if row else None, {"min": 1, "max": 10})


def add_link(
    db: TasteDB,
    from_collection: str,
    from_id: str,
    to_collection: str,
    to_id: str,
    kind: str | None = None,
    values: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = values or {}
    allowed_values = {"rating", "at", "note"}
    unknown = set(values) - allowed_values
    if unknown:
        raise ValueError(f"Unknown link field {sorted(unknown)[0]!r}.")
    stored_kind = kind or ""
    at = _activity_date(values["at"]) if values.get("at") is not None else None

    with db._lock, db.session() as connection:
        if not _record_exists(db, connection, from_collection, from_id):
            raise ValueError(f"No record {from_id!r} in {from_collection!r}.")
        if not _record_exists(db, connection, to_collection, to_id):
            raise ValueError(f"No record {to_id!r} in {to_collection!r}.")
        if not stored_kind and (
            from_collection != "profiles" or to_collection == "profiles"
        ):
            raise ValueError(
                "kind may be omitted only for a profile-to-non-profile link."
            )
        if stored_kind:
            known = connection.execute(
                """
                SELECT 1 FROM _meta
                WHERE scope = 'link_kind' AND collection = '' AND name = ?
                """,
                (stored_kind,),
            ).fetchone()
            if known is None:
                rows = connection.execute(
                    "SELECT name FROM _meta WHERE scope = 'link_kind' ORDER BY name"
                ).fetchall()
                choices = [row["name"] for row in rows]
                suggestion = difflib.get_close_matches(stored_kind, choices, n=1)
                hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
                raise ValueError(
                    f"Unknown link kind {stored_kind!r}.{hint} "
                    "Register a new kind in _meta via migrate."
                )

        if "rating" in values and values["rating"] is not None:
            rating = float(values["rating"])
            scale = _rating_scale(connection, to_collection)
            if not float(scale["min"]) <= rating <= float(scale["max"]):
                raise ValueError(
                    f"rating must be between {scale['min']} and {scale['max']} "
                    f"for {to_collection}"
                )

        key = (from_collection, from_id, to_collection, to_id, stored_kind)
        existing = connection.execute(
            """
            SELECT * FROM links
            WHERE from_collection = ? AND from_id = ?
              AND to_collection = ? AND to_id = ? AND kind = ?
            """,
            key,
        ).fetchone()
        now = utc_now()
        if existing is None:
            connection.execute(
                """
                INSERT INTO links (
                    from_collection, from_id, to_collection, to_id, kind,
                    rating, first_at, last_at, note, props, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *key,
                    values.get("rating"),
                    at,
                    at,
                    values.get("note"),
                    _merge_json(None, props),
                    now,
                    now,
                ),
            )
        else:
            assignments = ["updated_at = ?"]
            params: list[Any] = [now]
            for field in ("rating", "note"):
                if field in values:
                    assignments.append(f"{field} = ?")
                    params.append(values[field])
            if at is not None:
                first_at = existing["first_at"]
                last_at = existing["last_at"]
                assignments.extend(["first_at = ?", "last_at = ?"])
                params.extend(
                    [
                        min(first_at, at) if first_at else at,
                        max(last_at, at) if last_at else at,
                    ]
                )
            if props is not None:
                assignments.append("props = ?")
                params.append(_merge_json(existing["props"], props))
            connection.execute(
                f"UPDATE links SET {', '.join(assignments)} "
                "WHERE from_collection = ? AND from_id = ? "
                "AND to_collection = ? AND to_id = ? AND kind = ?",
                [*params, *key],
            )

        row = connection.execute(
            """
            SELECT * FROM links
            WHERE from_collection = ? AND from_id = ?
              AND to_collection = ? AND to_id = ? AND kind = ?
            """,
            key,
        ).fetchone()
        assert row is not None
        return decode_link(row)


def remove_link(
    db: TasteDB,
    from_collection: str,
    from_id: str,
    to_collection: str,
    to_id: str,
    kind: str | None = None,
) -> dict[str, Any]:
    key = (from_collection, from_id, to_collection, to_id, kind or "")
    with db._lock, db.session() as connection:
        row = connection.execute(
            """
            SELECT * FROM links
            WHERE from_collection = ? AND from_id = ?
              AND to_collection = ? AND to_id = ? AND kind = ?
            """,
            key,
        ).fetchone()
        if row is None:
            raise ValueError("No matching link exists.")
        connection.execute(
            """
            DELETE FROM links
            WHERE from_collection = ? AND from_id = ?
              AND to_collection = ? AND to_id = ? AND kind = ?
            """,
            key,
        )
        return decode_link(row)
