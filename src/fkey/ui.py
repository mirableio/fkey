"""Read-only browse UI: collection grids and record pages with their links."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from fkey.engine import BACKBONE_FIELDS, TasteDB, json_loads, quote_identifier
from fkey.oauth import OAuthProvider
from fkey.web import session_user

PAGE_SIZE = 100
GRID_ROW_LIMIT = 5000
RELATIONSHIP_LIMIT = 500
STATIC_DIR = Path(__file__).parent / "static"
AG_GRID = "vendor/ag-grid-36.2.0"
STATIC_FILES = {
    "ui.css",
    "ui.js",
    f"{AG_GRID}/ag-grid-community.min.noStyle.js",
    f"{AG_GRID}/ag-grid.min.css",
    f"{AG_GRID}/ag-theme-quartz.min.css",
}
COLLECTION_ICONS = {
    "books": "book",
    "cocktails": "cocktail",
    "movies": "movie",
    "music": "music",
    "people": "star",
    "profiles": "users",
    "shows": "tv",
    "wines": "wine",
}
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        # AG Grid's theme embeds its icon font and a few images as data: URLs.
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; font-src data:; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

Page = Callable[[TasteDB, Request], Response]


class NotFound(ValueError):
    """A collection or record that does not exist for this user."""


# Routing ---------------------------------------------------------------------


async def render(
    request: Request,
    provider: OAuthProvider,
    open_database: Callable[[str], TasteDB],
    page: Page,
) -> Response:
    """Run a signed-in page off the event loop, or send the browser to sign in."""
    user_id = session_user(request, provider)
    if user_id is None:
        target = request.url.path
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(
            f"/app/login?{urlencode({'next': target})}", status_code=302
        )
    database = open_database(user_id)
    return await asyncio.to_thread(page, database, request)


async def static_file(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in STATIC_FILES:
        return Response("Not found", status_code=404)
    # Vendored files carry their version in the path, so they never change.
    cache = "public, max-age=31536000, immutable" if name.startswith("vendor/") else "no-cache"
    return FileResponse(STATIC_DIR / name, headers={"Cache-Control": cache})


def home_page(database: TasteDB, request: Request) -> Response:
    counts = _collection_counts(database)
    largest = max(counts, key=lambda name: (counts[name], name == "profiles"))
    return RedirectResponse(_collection_href(largest), status_code=302)


def grid_page(database: TasteDB, request: Request) -> Response:
    collection = request.path_params["collection"]
    try:
        return _html("grid.html", _grid_context(database, collection, request.query_params))
    except NotFound as error:
        return _not_found(database, collection, str(error))


def record_page(database: TasteDB, request: Request) -> Response:
    collection = request.path_params["collection"]
    try:
        return _html(
            "record.html",
            _record_context(
                database,
                collection,
                request.path_params["record_id"],
                _page_number(request.query_params),
            ),
        )
    except NotFound as error:
        return _not_found(database, collection, str(error))


def _html(template: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _templates.get_template(template).render(**context),
        status_code=status_code,
        headers=PAGE_HEADERS,
    )


def _not_found(database: TasteDB, collection: str, message: str) -> HTMLResponse:
    context = _shell(database, collection)
    context.update(title="Not found", message=message)
    return _html("not_found.html", context, status_code=404)


# Shared shell ----------------------------------------------------------------


def _collection_href(collection: str) -> str:
    return f"/app/c/{quote(collection, safe='')}"


def _record_href(collection: str, record_id: str) -> str:
    return f"{_collection_href(collection)}/{quote(record_id, safe='')}"


def _collection_label(collection: str) -> str:
    if collection == "profiles":
        return "Your people"
    return collection.replace("_", " ").capitalize()


def _collection_icon(collection: str) -> str:
    return COLLECTION_ICONS.get(collection, "table")


def _collection_counts(database: TasteDB) -> dict[str, int]:
    names = database.collection_names()
    sql = " UNION ALL ".join(
        f"SELECT ? AS name, COUNT(*) AS count FROM {quote_identifier(name)}"
        for name in names
    )
    rows = database.query(sql, names, limit=len(names))["rows"]
    return {row["name"]: row["count"] for row in rows}


def _shell(database: TasteDB, current: str | None) -> dict[str, Any]:
    counts = _collection_counts(database)
    ordered = sorted(counts, key=lambda name: (name != "profiles", _collection_label(name)))
    self_rows = database.query("SELECT name FROM profiles WHERE id = 'self'")["rows"]
    return {
        "nav": [
            {
                "label": _collection_label(name),
                "icon": _collection_icon(name),
                "href": _collection_href(name),
                "count": counts[name],
                "current": name == current,
            }
            for name in ordered
        ],
        "user_name": self_rows[0]["name"] if self_rows else "",
    }


# Values ----------------------------------------------------------------------


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _is_url(value: str) -> bool:
    return value.startswith(("https://", "http://")) and " " not in value


def _compact(value: Any) -> str:
    """Readable text for a stored value; only deeply nested data stays JSON."""
    scalar = (str, int, float, bool, type(None))
    if isinstance(value, list) and all(isinstance(item, scalar) for item in value):
        return ", ".join(_compact(item) for item in value)
    if isinstance(value, dict) and all(isinstance(item, scalar) for item in value.values()):
        return ", ".join(f"{key}: {_compact(item)}" for key, item in value.items())
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return _format_number(value)
    return str(value)


def _cell(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {"kind": "empty"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"kind": "number", "value": _format_number(value)}
    if isinstance(value, str) and _is_url(value):
        return {"kind": "url", "value": value}
    return {"kind": "text", "value": _compact(value)}


def _pills(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "value": _compact(value),
            "url": value if isinstance(value, str) and _is_url(value) else None,
        }
        for key, value in values.items()
        if value is not None
    ]


def _field_icon(declared_type: str) -> str:
    upper = (declared_type or "").upper()
    if any(name in upper for name in ("INT", "REAL", "NUM", "FLOA", "DOUB")):
        return "hash"
    return "text"


def _label_column(columns: list[str]) -> str:
    for name in ("title", "name"):
        if name in columns:
            return name
    return "id"


def _describe(database: TasteDB, collection: str) -> dict[str, Any]:
    try:
        return database.describe_schema(collection)
    except ValueError as error:
        raise NotFound(str(error)) from error


def _rating_scales(database: TasteDB) -> dict[str, str]:
    rows = database.query(
        """
        SELECT collection, value_json FROM _meta
        WHERE scope = 'rating_scale' AND name = 'default'
        """
    )["rows"]
    scales = {}
    for row in rows:
        value = json_loads(row["value_json"], {})
        if isinstance(value, dict) and "max" in value:
            scales[row["collection"]] = _format_number(value["max"])
    return scales


def _rating(value: Any, collection: str, scales: dict[str, str]) -> str:
    if value is None:
        return ""
    scale = scales.get(collection) or scales.get("")
    number = _format_number(value)
    return f"{number} / {scale}" if scale else number


# Grid ------------------------------------------------------------------------


def _page_number(params: Mapping[str, str]) -> int:
    try:
        return max(1, int(params.get("page", "1")))
    except ValueError:
        return 1


def _column(
    key: str, label: str, kind: str, icon: str, description: str = ""
) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "icon": icon,
        "description": description,
    }


def _grid_value(value: Any) -> Any:
    if isinstance(value, (bool, dict, list)):
        return _compact(value)
    return value


def _grid_context(
    database: TasteDB, collection: str, params: Mapping[str, str]
) -> dict[str, Any]:
    schema = _describe(database, collection)
    fields = [field for field in schema["fields"] if field["name"] not in BACKBONE_FIELDS]
    primary = _label_column([field["name"] for field in fields])
    personal = collection != "profiles"

    columns = []
    if primary == "id":
        columns.append(_column("id", "id", "primary", "text"))
    for field in sorted(fields, key=lambda field: field["name"] != primary):
        icon = _field_icon(field["type"])
        kind = "primary" if field["name"] == primary else "number" if icon == "hash" else "text"
        columns.append(_column(field["name"], field["name"], kind, icon, field["description"]))
    if personal:
        scales = _rating_scales(database)
        scale = scales.get(collection) or scales.get("")
        rating = f"Your rating, out of {scale}." if scale else "Your rating."
        columns += [
            _column("_rating", "Your rating", "number", "star", rating),
            _column("_last_at", "Last", "date", "calendar", "The last date you logged it."),
        ]
    columns.append(
        _column("extra", "extra", "pills", "braces", "Details kept outside the typed fields.")
    )

    select, join, join_params = "r.*", "", []
    if personal:
        select += ", l.rating AS _rating, l.last_at AS _last_at"
        join = (
            "LEFT JOIN links AS l ON l.from_collection = 'profiles' "
            "AND l.from_id = 'self' AND l.kind = '' "
            "AND l.to_collection = ? AND l.to_id = r.id"
        )
        join_params = [collection]
    table = quote_identifier(collection)
    rows = database.query(
        f"SELECT {select} FROM {table} AS r {join} "
        "ORDER BY r.updated_at DESC, r.id LIMIT ?",
        [*join_params, GRID_ROW_LIMIT + 1],
        limit=GRID_ROW_LIMIT + 1,
    )["rows"]
    truncated = len(rows) > GRID_ROW_LIMIT
    rows = rows[:GRID_ROW_LIMIT]
    total = len(rows)
    if truncated:
        total = database.query(f"SELECT COUNT(*) AS count FROM {table}")["rows"][0]["count"]

    grid_rows = []
    for row in rows:
        values = {
            column["key"]: _grid_value(row[column["key"]])
            for column in columns
            if column["kind"] != "pills"
        }
        values[columns[0]["key"]] = values[columns[0]["key"]] or row["id"]
        values["extra"] = _pills(json_loads(row["extra"], {}))
        grid_rows.append(
            {"id": row["id"], "href": _record_href(collection, row["id"]), "values": values}
        )

    label = _collection_label(collection)
    noun = "people" if collection == "profiles" else label.lower()
    context = _shell(database, collection)
    context.update(
        title=label,
        label=label,
        noun=noun,
        collection_icon=_collection_icon(collection),
        description=schema["description"],
        q=params.get("q", ""),
        total=total,
        shown=len(grid_rows),
        truncated=truncated,
        ag_grid=AG_GRID,
        grid_data={
            "collection": collection,
            "noun": noun,
            "columns": columns,
            "rows": grid_rows,
        },
    )
    return context


# Record page -----------------------------------------------------------------


def _labels(
    database: TasteDB, endpoints: set[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    by_collection: dict[str, list[str]] = {}
    for collection, record_id in endpoints:
        by_collection.setdefault(collection, []).append(record_id)
    labels: dict[tuple[str, str], str] = {}
    for collection, ids in by_collection.items():
        columns = [
            row["name"]
            for row in database.query(
                "SELECT name FROM pragma_table_info(?)", [collection]
            )["rows"]
        ]
        column = quote_identifier(_label_column(columns))
        placeholders = ", ".join("?" for _ in ids)
        rows = database.query(
            f"SELECT id, {column} AS label FROM {quote_identifier(collection)} "
            f"WHERE id IN ({placeholders})",
            ids,
            limit=len(ids),
        )["rows"]
        for row in rows:
            labels[(collection, row["id"])] = row["label"] or row["id"]
    return labels


def _link_pill(
    collection: str, record_id: str, labels: dict[tuple[str, str], str]
) -> dict[str, str]:
    return {
        "label": labels.get((collection, record_id), record_id),
        "href": _record_href(collection, record_id),
        "icon": "user" if collection == "profiles" else _collection_icon(collection),
    }


def _record_context(
    database: TasteDB, collection: str, record_id: str, page: int
) -> dict[str, Any]:
    schema = _describe(database, collection)
    table = quote_identifier(collection)
    rows = database.query(f"SELECT * FROM {table} WHERE id = ?", [record_id], limit=1)["rows"]
    if not rows:
        raise NotFound(f"No record {record_id!r} in {_collection_label(collection)}.")
    record = rows[0]
    fields = [field for field in schema["fields"] if field["name"] not in BACKBONE_FIELDS]
    title = record[_label_column([field["name"] for field in fields])] or record["id"]

    is_profile = collection == "profiles"
    if is_profile:
        history_where = "from_collection = 'profiles' AND from_id = ? AND kind = ''"
        history_params = [record_id]
    else:
        history_where = "to_collection = ? AND to_id = ? AND kind = ''"
        history_params = [collection, record_id]
    history_total = database.query(
        f"SELECT COUNT(*) AS count FROM links WHERE {history_where}", history_params
    )["rows"][0]["count"]
    pages = max(1, math.ceil(history_total / PAGE_SIZE))
    page = min(page, pages)
    history = database.query(
        f"SELECT * FROM links WHERE {history_where} "
        "ORDER BY last_at DESC NULLS LAST, updated_at DESC LIMIT ? OFFSET ?",
        [*history_params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        limit=PAGE_SIZE,
    )["rows"]
    relationships = database.query(
        """
        SELECT * FROM links
        WHERE kind != ''
          AND ((from_collection = ? AND from_id = ?) OR (to_collection = ? AND to_id = ?))
        ORDER BY kind, updated_at DESC
        LIMIT ?
        """,
        [collection, record_id, collection, record_id, RELATIONSHIP_LIMIT],
        limit=RELATIONSHIP_LIMIT,
    )["rows"]

    def other_end(link: dict[str, Any]) -> tuple[str, str]:
        if link["from_collection"] == collection and link["from_id"] == record_id:
            return link["to_collection"], link["to_id"]
        return link["from_collection"], link["from_id"]

    labels = _labels(database, {other_end(link) for link in [*history, *relationships]})
    scales = _rating_scales(database)

    history_groups: list[dict[str, Any]] = []
    group_counts: dict[str, int] = {}
    if is_profile:
        group_counts = {
            row["to_collection"]: row["count"]
            for row in database.query(
                f"SELECT to_collection, COUNT(*) AS count FROM links "
                f"WHERE {history_where} GROUP BY to_collection",
                history_params,
            )["rows"]
        }
    for link in history:
        other_collection, other_id = other_end(link)
        group_key = other_collection if is_profile else ""
        group = next((item for item in history_groups if item["key"] == group_key), None)
        if group is None:
            group = {
                "key": group_key,
                "label": _collection_label(other_collection) if is_profile else "",
                "icon": _collection_icon(other_collection),
                "count": group_counts.get(other_collection, 0),
                "rows": [],
            }
            history_groups.append(group)
        group["rows"].append(
            {
                "pill": _link_pill(other_collection, other_id, labels),
                "rating": _rating(link["rating"], link["to_collection"], scales),
                "first_at": link["first_at"] or "",
                "last_at": link["last_at"] or "",
                "note": link["note"] or "",
                "props": _pills(json_loads(link["props"], {})),
            }
        )
    history_groups.sort(key=lambda group: group["label"])

    relationship_groups: list[dict[str, Any]] = []
    for link in relationships:
        outgoing = link["from_collection"] == collection and link["from_id"] == record_id
        heading = f"{link['kind']} →" if outgoing else f"← {link['kind']}"
        group = next((item for item in relationship_groups if item["heading"] == heading), None)
        if group is None:
            group = {"heading": heading, "pills": []}
            relationship_groups.append(group)
        group["pills"].append(_link_pill(*other_end(link), labels))

    label = _collection_label(collection)
    context = _shell(database, collection)
    context.update(
        title=f"{title} · {label}",
        collection=collection,
        collection_label=label,
        collection_href=_collection_href(collection),
        collection_icon=_collection_icon(collection),
        record_title=title,
        record_id=record["id"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        fields=[
            {
                "label": field["name"],
                "icon": _field_icon(field["type"]),
                "description": field["description"],
                "cell": _cell(record[field["name"]]),
            }
            for field in fields
        ],
        more=[
            {"label": key, "cell": _cell(value)}
            for key, value in json_loads(record["extra"], {}).items()
            if value is not None
        ],
        is_profile=is_profile,
        history_groups=history_groups,
        history_total=history_total,
        relationship_groups=relationship_groups,
        previous_href=f"?page={page - 1}" if page > 1 else None,
        next_href=f"?page={page + 1}" if page < pages else None,
    )
    return context
