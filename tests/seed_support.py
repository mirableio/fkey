"""Generic YAML fixture loading through the public TasteDB operations."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from fkey.engine import TasteDB


def _endpoint(value: Any, field: str) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field} must be [collection, id]")
    return str(value[0]), str(value[1])


def _normalize_yaml(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _normalize_yaml(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_yaml(item) for item in value]
    return value


def load_seed(database: TasteDB, source: str | Path) -> dict[str, int]:
    """Load collections, records, and links from a normalized test fixture."""
    data = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("seed fixture must contain a mapping")

    counts = {"collections": 0, "records": 0, "links": 0}
    for spec in data.get("collections") or []:
        database.create_collection(
            str(spec["name"]),
            str(spec.get("description", "")),
            spec.get("fields") or [],
        )
        counts["collections"] += 1

    for spec in data.get("records") or []:
        record_id = spec.get("id")
        database.add_record(
            str(spec["collection"]),
            _normalize_yaml(spec.get("values") or {}),
            _normalize_yaml(spec.get("extra")),
            record_id=str(record_id) if record_id is not None else None,
        )
        counts["records"] += 1

    for spec in data.get("links") or []:
        from_collection, from_id = _endpoint(spec.get("from"), "from")
        to_collection, to_id = _endpoint(spec.get("to"), "to")
        database.add_link(
            from_collection,
            from_id,
            to_collection,
            to_id,
            spec.get("kind"),
            _normalize_yaml(spec.get("values")),
            _normalize_yaml(spec.get("props")),
        )
        counts["links"] += 1

    return counts
