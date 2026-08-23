"""MCP surface for the local fkey database."""

from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from fkey.accounts import AccountStore
from fkey.engine import TasteDB
from fkey.oauth import MCP_SCOPE, OFFLINE_SCOPE, OAuthProvider
from fkey.rate_limit import RateLimitMiddleware, RedisRateLimiter
from fkey.web import oauth_login_page, signup_page

DEFAULT_DB = Path("data/local/db.sqlite")
DEFAULT_PUBLIC_URL = "http://127.0.0.1:8000"
STATIC_INSTRUCTIONS = (
    "Records hold intrinsic facts; profile links hold personal ratings, dates, "
    "and reactions. The user's own profile id is 'self'. Use describe_schema "
    "for current collections, fields, profiles, link kinds, and rating scales. "
    "Never invent a rating. Put deliberate long-tail data in extra or props. "
    "Use add_multiple_records for create-only backfills, minimal=true when full "
    "write results are unnecessary, and migrate only for schema evolution."
)

# Database content stays out of MCP instructions on purpose: instructions are
# a high-trust channel, and stored values (profile names, kind names) must
# never be promoted into agent guidance. Live state comes from describe_schema.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ADDITIVE = ToolAnnotations(destructive_hint=False, open_world_hint=False)
_OVERWRITE = ToolAnnotations(open_world_hint=False)
_DESTRUCTIVE = ToolAnnotations(destructive_hint=True, open_world_hint=False)


def _record_result(result: dict[str, Any], minimal: bool) -> dict[str, Any]:
    return {"id": result["id"]} if minimal else result


def _link_result(result: dict[str, Any], minimal: bool) -> dict[str, Any]:
    if not minimal:
        return result
    return {
        key: result[key]
        for key in (
            "from_collection",
            "from_id",
            "to_collection",
            "to_id",
            "kind",
        )
    }


def _multiple_record_receipt(item: dict[str, Any]) -> dict[str, Any]:
    receipt = {"id": item["record"]["id"]}
    if "possible_duplicate_of" in item:
        receipt["possible_duplicate_of"] = item["possible_duplicate_of"]
    return receipt


def _auth_settings(issuer_url: str, resource_url: str) -> AuthSettings:
    return AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_url,
        required_scopes=[MCP_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[MCP_SCOPE, OFFLINE_SCOPE],
            default_scopes=[MCP_SCOPE, OFFLINE_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )


_engine: TasteDB | None = None
_account_store: AccountStore | None = None
_user_engines: dict[str, TasteDB] = {}
_user_engines_lock = threading.RLock()
_http_mode = False

oauth_provider = OAuthProvider(
    lambda: accounts(),
    DEFAULT_PUBLIC_URL,
    f"{DEFAULT_PUBLIC_URL}/mcp",
)

mcp = MCPServer(
    name="fkey",
    title="Personal Taste Database",
    description="A personal, extensible taste database managed by AI agents.",
    instructions=STATIC_INSTRUCTIONS,
    auth_server_provider=oauth_provider,
    auth=_auth_settings(DEFAULT_PUBLIC_URL, f"{DEFAULT_PUBLIC_URL}/mcp"),
)


def configure(path: str | Path) -> TasteDB:
    """Configure the process-wide Phase 1 database used by MCP tools."""
    global _engine, _http_mode
    _http_mode = False
    _engine = TasteDB(path)
    return _engine


def engine() -> TasteDB:
    access_token = get_access_token()
    if access_token is not None:
        if access_token.subject is None:
            raise PermissionError("The access token has no user identity.")
        return user_engine(access_token.subject)
    if _http_mode:
        raise PermissionError("An authenticated user identity is required in HTTP mode.")
    if _engine is None:
        path = Path(os.environ.get("FKEY_DB", DEFAULT_DB))
        return configure(path)
    return _engine


def configure_accounts(data_dir: str | Path) -> AccountStore:
    """Configure the central account store and per-user data directory."""
    global _account_store
    _account_store = AccountStore(data_dir)
    with _user_engines_lock:
        _user_engines.clear()
    return _account_store


def accounts() -> AccountStore:
    if _account_store is None:
        return configure_accounts(os.environ.get("FKEY_DATA_DIR", "data"))
    return _account_store


def user_engine(user_id: str) -> TasteDB:
    with _user_engines_lock:
        existing = _user_engines.get(user_id)
        if existing is not None:
            return existing
        store = accounts()
        if not store.has_account(user_id):
            raise PermissionError("The authenticated account no longer exists.")
        database = TasteDB(store.database_path(user_id))
        _user_engines[user_id] = database
        return database


def configure_oauth(public_url: str) -> OAuthProvider:
    """Configure the public OAuth issuer and MCP resource identifiers."""
    parsed = urlparse(public_url)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("FKEY_PUBLIC_URL must use HTTPS, except on localhost.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("FKEY_PUBLIC_URL must be an origin without a path or query.")
    issuer_url = public_url.rstrip("/")
    resource_url = f"{issuer_url}/mcp"
    oauth_provider.configure(issuer_url, resource_url)
    mcp.settings.auth = _auth_settings(issuer_url, resource_url)
    return oauth_provider


def http_app(
    *,
    host: str = "127.0.0.1",
    transport_security: TransportSecuritySettings | None = None,
) -> Starlette:
    """Build the HTTP app, enabling shared auth limits when Redis is configured."""
    global _http_mode
    _http_mode = True
    app = mcp.streamable_http_app(
        stateless_http=True,
        host=host,
        transport_security=transport_security,
    )
    redis_url = os.environ.get("FKEY_REDIS_URL")
    if redis_url:
        trust_proxy_headers = os.environ.get(
            "FKEY_TRUST_PROXY_HEADERS", ""
        ).casefold() in {"1", "true", "yes", "on"}
        app.add_middleware(
            RateLimitMiddleware,
            limiter=RedisRateLimiter(redis_url),
            trust_proxy_headers=trust_proxy_headers,
        )
    return app


@mcp.custom_route("/signup", methods=["GET", "POST"], include_in_schema=False)
async def signup(request: Request) -> Response:
    """Create an invited account and provision its personal database."""
    return await signup_page(request, accounts(), os.environ.get("FKEY_SIGNUP_CODE"))


@mcp.custom_route("/oauth/login", methods=["GET", "POST"], include_in_schema=False)
async def oauth_login(request: Request) -> Response:
    """Authenticate the resource owner during an OAuth authorization request."""
    return await oauth_login_page(request, oauth_provider)


@mcp.tool(annotations=_READ_ONLY)
def describe_schema(collection: str | None = None) -> dict[str, Any]:
    """Describe the live schema and conventions.

    Without collection, returns collection summaries, profiles, links, kinds,
    rating scales, and recent migrations. With collection, returns detailed
    fields, common extra keys, and a sample.
    """
    return engine().describe_schema(collection)


@mcp.tool(annotations=_ADDITIVE)
def add_record(
    collection: str,
    values: dict[str, Any],
    extra: dict[str, Any] | None = None,
    minimal: bool = False,
) -> dict[str, Any]:
    """Add one record to an existing collection.

    Records contain intrinsic facts; personal ratings, dates, and reactions
    belong on profile links. values accepts live schema fields and extra holds
    deliberate long-tail data. minimal returns only the generated id.
    """
    return _record_result(engine().add_record(collection, values, extra), minimal)


@mcp.tool(annotations=_ADDITIVE)
def add_multiple_records(
    collection: str,
    records: list[dict[str, Any]],
    minimal: bool = True,
) -> dict[str, Any]:
    """Atomically create multiple records in one collection.

    Each item has values, optional extra, and an optional profile_link with
    profile_id (default self), values, and props. This always creates new
    records; a matching slug is suffixed and flagged as a possible duplicate.
    Shape: {"values": {...}, "extra": {...}, "profile_link": {"profile_id":
    "self", "values": {...}, "props": {...}}}. Use update_record and add_link
    to enrich existing records. All items succeed or none do. minimal returns
    generated ids and duplicate warnings, and defaults to true.
    """
    results = engine().add_multiple_records(collection, records)
    if minimal:
        return {"items": [_multiple_record_receipt(item) for item in results]}
    return {"items": results}


@mcp.tool(annotations=_READ_ONLY)
def get_record(collection: str, record_id: str) -> dict[str, Any]:
    """Get one complete record and all its incoming and outgoing links."""
    return engine().get_record(collection, record_id)


@mcp.tool(annotations=_OVERWRITE)
def update_record(
    collection: str,
    record_id: str,
    values: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    minimal: bool = False,
) -> dict[str, Any]:
    """Partially update a record.

    Omitted fields stay unchanged; null clears optional fields. extra merges,
    with null values deleting keys. minimal returns only the record id.
    """
    return _record_result(
        engine().update_record(collection, record_id, values, extra), minimal
    )


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_record(
    collection: str, record_id: str, minimal: bool = False
) -> dict[str, Any]:
    """Delete a record and its links. minimal returns only the record id."""
    return _record_result(engine().delete_record(collection, record_id), minimal)


@mcp.tool(annotations=_READ_ONLY)
def find_records(
    collection: str,
    filters: dict[str, Any] | None = None,
    text: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find records using exact filters and/or case-insensitive text search.

    Returns newest-updated first, up to limit. A null filter matches an unset
    field.
    """
    return engine().find_records(collection, filters, text, limit)


@mcp.tool(annotations=_OVERWRITE)
def add_link(
    from_collection: str,
    from_id: str,
    to_collection: str,
    to_id: str,
    kind: str | None = None,
    values: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
    minimal: bool = False,
) -> dict[str, Any]:
    """Upsert a link by endpoints and optional kind.

    Omit kind only for profile-to-item history; for the user's own history,
    from_id is 'self'. values accepts rating, at (YYYY-MM-DD), and note; at
    maintains first_at/last_at. Omitted values stay unchanged, null clears, and
    props merges with null deleting keys. Other relationships require a kind
    registered in the live schema. minimal returns only the link identity.
    """
    return _link_result(
        engine().add_link(
            from_collection, from_id, to_collection, to_id, kind, values, props
        ),
        minimal,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
def remove_link(
    from_collection: str,
    from_id: str,
    to_collection: str,
    to_id: str,
    kind: str | None = None,
    minimal: bool = False,
) -> dict[str, Any]:
    """Remove an exact link. minimal returns only its identity."""
    return _link_result(
        engine().remove_link(
            from_collection, from_id, to_collection, to_id, kind
        ),
        minimal,
    )


@mcp.tool(name="query", annotations=_READ_ONLY)
def query_database(
    sql: str,
    parameters: list[Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Run a read-only parameterized SQLite SELECT or WITH query.

    Bare profile-history links store kind = ''. Parameters use ? placeholders;
    results are capped by limit. Use describe_schema when names are uncertain.
    """
    return engine().query(sql, parameters, limit)


@mcp.tool(annotations=_ADDITIVE)
def create_collection(
    name: str,
    description: str,
    fields: list[dict[str, Any]],
    minimal: bool = False,
) -> dict[str, Any]:
    """Create a new collection with a few typed intrinsic fields.

    Field types are text, integer, real, boolean, date, or datetime. id,
    timestamps, and extra are added automatically. minimal returns its name.
    """
    result = engine().create_collection(name, description, fields)
    return {"name": result["name"]} if minimal else result


@mcp.tool(annotations=_DESTRUCTIVE)
def migrate(
    description: str, statements: list[str], minimal: bool = False
) -> dict[str, Any]:
    """Apply schema evolution after taking a restorable snapshot.

    Never use this for ordinary record creation or updates: use add_record or
    create-only add_multiple_records, then update_record and add_link to enrich
    existing records. Reserve migrate for schema changes, related backfills,
    and _meta updates. Statements run in one transaction; failures roll back
    and keep the snapshot. minimal omits the resulting full schema.
    """
    result = engine().migrate(description, statements)
    if not minimal:
        return result
    return {
        key: result[key] for key in ("migration_id", "snapshot", "warnings")
    }


@mcp.tool(annotations=_DESTRUCTIVE)
def restore_snapshot(snapshot: str, minimal: bool = False) -> dict[str, Any]:
    """Restore a validated point-in-time snapshot after taking a safety snapshot.

    Data written after the target is lost. Use list_snapshots for exact names.
    minimal omits the resulting full schema.
    """
    result = engine().restore_snapshot(snapshot)
    if not minimal:
        return result
    return {key: result[key] for key in ("restored", "safety_snapshot")}


@mcp.tool(annotations=_READ_ONLY)
def list_snapshots() -> list[dict[str, str]]:
    """List restore targets, newest first."""
    return engine().list_snapshots()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Personal taste database MCP server")
    parser.add_argument(
        "--db",
        default=os.environ.get("FKEY_DB", str(DEFAULT_DB)),
        help="Local stdio SQLite database path (default: data/local/db.sqlite)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve authenticated multi-user Streamable HTTP instead of local stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("FKEY_DATA_DIR", "data"),
        help="Account database and per-user data directory (default: data)",
    )
    parser.add_argument(
        "--public-url",
        default=os.environ.get("FKEY_PUBLIC_URL"),
        help="Public HTTPS origin used for OAuth discovery (for example https://fkey.example)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help=(
            "Accept any Host header (required behind a tunnel or reverse proxy, "
            "which forwards its own hostname). Default is localhost-only "
            "DNS-rebinding protection."
        ),
    )
    args = parser.parse_args()

    if args.http:
        if args.public and not args.public_url:
            parser.error("--public requires FKEY_PUBLIC_URL or --public-url")
        public_url = args.public_url or f"http://{args.host}:{args.port}"
        configure_accounts(args.data_dir)
        try:
            configure_oauth(public_url)
        except ValueError as error:
            parser.error(str(error))
        transport_security = None
        if args.public:
            transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        app = http_app(
            host=args.host,
            transport_security=transport_security,
        )
        import uvicorn

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=mcp.settings.log_level.lower(),
        )
    else:
        configure(args.db)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
