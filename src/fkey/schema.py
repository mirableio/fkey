"""Database bootstrap, collection creation, and live schema descriptions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from fkey.engine import (
    BACKBONE_FIELDS,
    json_dumps,
    json_loads,
    quote_identifier,
    utc_now,
)

if TYPE_CHECKING:
    from fkey.engine import TasteDB


FIELD_TYPE_ALIASES = {
    "text": "TEXT",
    "string": "TEXT",
    "date": "TEXT",
    "datetime": "TEXT",
    "integer": "INTEGER",
    "int": "INTEGER",
    "boolean": "INTEGER",
    "bool": "INTEGER",
    "real": "REAL",
    "float": "REAL",
    "number": "REAL",
    "numeric": "NUMERIC",
    "blob": "BLOB",
}

SEED_COLLECTIONS: dict[str, dict[str, Any]] = {
    "movies": {
        "description": "Movies and films.",
        "fields": [
            ("title", "TEXT", True, "Movie title."),
            ("year", "INTEGER", False, "Release year."),
        ],
    },
    "shows": {
        "description": "TV series, seasons, and episodes.",
        "fields": [
            ("title", "TEXT", True, "Show, season, or episode title."),
            ("year", "INTEGER", False, "Initial release year."),
            ("kind", "TEXT", False, "series, season, or episode."),
        ],
    },
    "books": {
        "description": "Books and other written works.",
        "fields": [
            ("title", "TEXT", True, "Book title."),
            ("year", "INTEGER", False, "Publication year."),
        ],
    },
    "music": {
        "description": "Albums, tracks, and other musical works.",
        "fields": [
            ("title", "TEXT", True, "Work title."),
            ("year", "INTEGER", False, "Release year."),
            ("kind", "TEXT", False, "album, track, composition, or another form."),
        ],
    },
    "wines": {
        "description": "Wines the user wants to remember.",
        "fields": [
            ("name", "TEXT", True, "Wine name."),
            ("vintage", "INTEGER", False, "Vintage year."),
            ("country", "TEXT", False, "Country of origin."),
        ],
    },
    "cocktails": {
        "description": "Cocktails and mixed drinks.",
        "fields": [("name", "TEXT", True, "Cocktail name.")],
    },
    "people": {
        "description": "Public figures such as actors, authors, musicians, and bands.",
        "fields": [
            ("name", "TEXT", True, "Public name."),
            ("kind", "TEXT", False, "actor, director, author, musician, band, etc."),
        ],
    },
}

LINK_KINDS = {
    "part_of": "The source is a component of the target.",
    "pairs_with": "The source pairs well with the target.",
    "recommended": "The source recommended the target.",
    "directed": "The source person directed the target work.",
    "acted_in": "The source person acted in the target work.",
    "wrote": "The source person wrote the target work.",
    "performed": "The source person performed the target work.",
    "made": "The source person or organization made the target.",
}


def _normalize_field_type(value: str) -> str:
    normalized = FIELD_TYPE_ALIASES.get(value.strip().lower())
    if normalized is None:
        choices = ", ".join(sorted(set(FIELD_TYPE_ALIASES.values())))
        raise ValueError(f"Unsupported field type {value!r}. Use one of: {choices}.")
    return normalized


def _create_table(
    connection: sqlite3.Connection,
    name: str,
    fields: Sequence[tuple[str, str, bool, str]],
) -> None:
    columns = [
        "id TEXT PRIMARY KEY",
        *[
            f"{quote_identifier(field_name)} {field_type}"
            + (" NOT NULL" if required else "")
            for field_name, field_type, required, _ in fields
        ],
        "created_at TEXT NOT NULL",
        "updated_at TEXT NOT NULL",
        "extra TEXT NOT NULL DEFAULT '{}'",
    ]
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(name)} ({', '.join(columns)})"
    )


def _seed_meta(
    connection: sqlite3.Connection,
    collection: str,
    description: str,
    fields: Iterable[tuple[str, str]],
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO _meta
            (scope, collection, name, description)
        VALUES ('collection', ?, '', ?)
        """,
        (collection, description),
    )
    for field, field_description in fields:
        connection.execute(
            """
            INSERT OR IGNORE INTO _meta
                (scope, collection, name, description)
            VALUES ('field', ?, ?, ?)
            """,
            (collection, field, field_description),
        )


def bootstrap(db: TasteDB) -> None:
    """Create a new database's system primitives and seed collections."""
    with db._lock, db.session() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS _meta (
                scope TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                value_json TEXT,
                PRIMARY KEY (scope, collection, name)
            );

            CREATE TABLE IF NOT EXISTS _migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                description TEXT NOT NULL,
                statements_json TEXT NOT NULL,
                backup_file TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                relation TEXT,
                birthday TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                extra TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS links (
                from_collection TEXT NOT NULL,
                from_id TEXT NOT NULL,
                to_collection TEXT NOT NULL,
                to_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                rating REAL,
                first_at TEXT,
                last_at TEXT,
                note TEXT,
                props TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (from_collection, from_id, to_collection, to_id, kind)
            );

            CREATE INDEX IF NOT EXISTS links_from_idx
                ON links (from_collection, from_id);
            CREATE INDEX IF NOT EXISTS links_to_idx
                ON links (to_collection, to_id);
            """
        )

        now = utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO profiles
                (id, name, relation, created_at, updated_at, extra)
            VALUES ('self', 'Me', 'self', ?, ?, '{}')
            """,
            (now, now),
        )

        _seed_meta(
            connection,
            "profiles",
            "The user's own virtual profiles, including the stable self record.",
            [
                ("name", "Profile name."),
                ("relation", "Relationship to self."),
                ("birthday", "Birthday when known."),
                ("notes", "Free-form profile notes."),
            ],
        )
        _seed_meta(
            connection,
            "links",
            "Relationships between records; bare profile links carry ratings and dates.",
            [
                ("rating", "Numeric opinion on the target collection's rating scale."),
                ("first_at", "First recorded engagement date."),
                ("last_at", "Most recent recorded engagement date."),
                ("note", "A note about the relationship or experience."),
                ("props", "Explicit long-tail relationship properties as JSON."),
            ],
        )

        for name, spec in SEED_COLLECTIONS.items():
            _create_table(connection, name, spec["fields"])
            _seed_meta(
                connection,
                name,
                spec["description"],
                [(field[0], field[3]) for field in spec["fields"]],
            )

        for kind, description in LINK_KINDS.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO _meta
                    (scope, collection, name, description)
                VALUES ('link_kind', '', ?, ?)
                """,
                (kind, description),
            )

        connection.execute(
            """
            INSERT OR IGNORE INTO _meta
                (scope, collection, name, description, value_json)
            VALUES ('rating_scale', '', 'default',
                    'Default numeric opinion scale.', ?)
            """,
            (json_dumps({"min": 1, "max": 10, "anchors": "1-2 hate; 9-10 love"}),),
        )


def create_collection(
    db: TasteDB,
    name: str,
    description: str,
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    quote_identifier(name)
    if name in {"links", "profiles"} or name.startswith("_"):
        raise ValueError(f"Collection name {name!r} is reserved.")
    parsed_fields: list[tuple[str, str, bool, str]] = []
    seen = set(BACKBONE_FIELDS)
    for spec in fields:
        field_name = str(spec.get("name", ""))
        quote_identifier(field_name)
        if field_name in seen:
            raise ValueError(f"Duplicate or reserved field {field_name!r}.")
        seen.add(field_name)
        parsed_fields.append(
            (
                field_name,
                _normalize_field_type(str(spec.get("type", "text"))),
                bool(spec.get("required", False)),
                str(spec.get("description", "")),
            )
        )

    with db._lock, db.session() as connection:
        existing = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"Collection {name!r} already exists.")
        _create_table(connection, name, parsed_fields)
        _seed_meta(
            connection,
            name,
            description,
            [(field[0], field[3]) for field in parsed_fields],
        )
    return describe_schema(db, name)


def describe_schema(db: TasteDB, collection: str | None = None) -> dict[str, Any]:
    with db.session() as connection:
        collections = db.collection_names(connection)
        if collection is not None:
            if collection == "links":
                return _collection_description(db, connection, "links")
            db._require_collection(connection, collection)
            return _collection_description(db, connection, collection)

        collection_summaries = [
            _collection_description(db, connection, name, include_sample=False)
            for name in collections
        ]
        profiles = connection.execute(
            "SELECT id, name, relation FROM profiles ORDER BY id != 'self', name"
        ).fetchall()
        link_kinds = connection.execute(
            """
            SELECT name, description FROM _meta
            WHERE scope = 'link_kind' ORDER BY name
            """
        ).fetchall()
        rating_rows = connection.execute(
            """
            SELECT collection, name, description, value_json FROM _meta
            WHERE scope = 'rating_scale' ORDER BY collection, name
            """
        ).fetchall()
        migrations = connection.execute(
            """
            SELECT id, created_at, description, backup_file
            FROM _migrations ORDER BY id DESC LIMIT 5
            """
        ).fetchall()
        return {
            "collections": collection_summaries,
            "profiles": [dict(row) for row in profiles],
            "links": _collection_description(
                db, connection, "links", include_sample=False
            ),
            "link_kinds": [dict(row) for row in link_kinds],
            "rating_scales": [
                {
                    "collection": row["collection"] or "default",
                    "name": row["name"],
                    "description": row["description"],
                    "value": json_loads(row["value_json"], {}),
                }
                for row in rating_rows
            ],
            "recent_migrations": [dict(row) for row in migrations],
            "meta_table": {
                "columns": [
                    "scope",
                    "collection",
                    "name",
                    "description",
                    "value_json",
                ],
                "note": (
                    "migrate may update _meta to describe fields, link kinds, "
                    "and rating scales."
                ),
                "rating_scale_convention": {
                    "scope": "rating_scale",
                    "collection": "target collection, or empty for the global default",
                    "name": "default",
                    "value_json": {"min": 1, "max": 10, "anchors": "optional text"},
                },
            },
        }


def _collection_description(
    db: TasteDB,
    connection: sqlite3.Connection,
    collection: str,
    include_sample: bool = True,
) -> dict[str, Any]:
    from fkey.records import decode_link, decode_record

    meta_rows = connection.execute(
        """
        SELECT scope, name, description FROM _meta
        WHERE collection = ? AND scope IN ('collection', 'field')
        """,
        (collection,),
    ).fetchall()
    collection_description = next(
        (row["description"] for row in meta_rows if row["scope"] == "collection"),
        "",
    )
    field_descriptions = {
        row["name"]: row["description"]
        for row in meta_rows
        if row["scope"] == "field"
    }
    fields = [
        {
            "name": row["name"],
            "type": row["type"],
            "required": bool(row["notnull"]),
            "primary_key": bool(row["pk"]),
            "description": field_descriptions.get(row["name"], ""),
        }
        for row in db._table_info(connection, collection)
    ]
    count = connection.execute(
        f"SELECT COUNT(*) AS count FROM {quote_identifier(collection)}"
    ).fetchone()["count"]
    result: dict[str, Any] = {
        "name": collection,
        "description": collection_description,
        "row_count": count,
        "fields": fields,
    }
    if include_sample:
        row = connection.execute(
            f"SELECT * FROM {quote_identifier(collection)} LIMIT 1"
        ).fetchone()
        if row is not None:
            result["sample"] = (
                decode_link(row) if collection == "links" else decode_record(row)
            )
        if collection != "links" and "extra" in {field["name"] for field in fields}:
            keys: set[str] = set()
            extra_rows = connection.execute(
                f"SELECT extra FROM {quote_identifier(collection)} "
                "WHERE extra != '{}' LIMIT 100"
            ).fetchall()
            for extra_row in extra_rows:
                value = json_loads(extra_row["extra"], {})
                if isinstance(value, dict):
                    keys.update(value)
            result["common_extra_keys"] = sorted(keys)
    return result
