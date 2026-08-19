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
    "Manage the user's personal taste database: movies, shows, books, music, "
    "wines, cocktails, people (public figures such as directors and bands), "
    "profiles (the user's own family and friends), plus any collections added "
    "later. Two rules matter most. 1) Records hold intrinsic facts only "
    "(title, year, ...); every personal rating, date, or reaction is a link "
    "from a profile to the item — and the user themself is the profile with "
    "id 'self'. 2) Ratings are numeric (default 1-10, decimals fine; "
    "per-collection scales in describe_schema) — never invent one the user "
    "didn't express. Long-tail observations go in the explicit extra/props "
    "objects; when a key keeps recurring, promote it to a real column via "
    "migrate. describe_schema is the authority whenever the live schema is "
    "unclear."
)

# Database content stays out of MCP instructions on purpose: instructions are
# a high-trust channel, and stored values (profile names, kind names) must
# never be promoted into agent guidance. Live state comes from describe_schema.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ADDITIVE = ToolAnnotations(destructive_hint=False, open_world_hint=False)
_OVERWRITE = ToolAnnotations(open_world_hint=False)
_DESTRUCTIVE = ToolAnnotations(destructive_hint=True, open_world_hint=False)


def _auth_settings(issuer_url: str, resource_url: str) -> AuthSettings:
    return AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_url,
        required_scopes=[MCP_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[MCP_SCOPE, OFFLINE_SCOPE],
            default_scopes=[MCP_SCOPE],
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
    """Describe the live database schema and conventions.

    With no collection, returns every collection with fields, row counts, the
    profiles roster (who the user's people are), link kinds, rating scales, and
    recent migrations. Pass a collection for deeper detail, common extra keys,
    and a sample row to imitate. Call this at the start of a session and
    whenever the live schema is unclear — it is the authority.
    """
    return engine().describe_schema(collection)


@mcp.tool(annotations=_ADDITIVE)
def add_record(
    collection: str,
    values: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a record to a collection and return its generated id and stored fields.

    Records hold intrinsic facts only. Ratings, watch/read dates, and reactions
    never go in a record — put them on add_link from a profile (the user
    themself is the profile 'self'). Example:
    add_record("movies", values={"title": "Dune", "year": 2021}).
    The user's people are records too:
    add_record("profiles", values={"name": "Anna", "relation": "wife"}).
    values must use the collection's schema fields (unknown keys are errors
    with suggestions); deliberate long-tail observations go in the explicit
    extra object. Use create_collection first if the collection doesn't exist.
    """
    return engine().add_record(collection, values, extra)


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
) -> dict[str, Any]:
    """Partially update a record.

    Only values keys supplied are changed; an explicit null clears an optional
    field (required fields reject null). extra is merged into the current
    object; a null extra value deletes that key.
    """
    return engine().update_record(collection, record_id, values, extra)


@mcp.tool(annotations=_DESTRUCTIVE)
def delete_record(collection: str, record_id: str) -> dict[str, Any]:
    """Delete a record and its incoming and outgoing links."""
    return engine().delete_record(collection, record_id)


@mcp.tool(annotations=_READ_ONLY)
def find_records(
    collection: str,
    filters: dict[str, Any] | None = None,
    text: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find records by exact field values and/or free text.

    filters is {"field": value} matched by equality (a null value matches
    records where the field is unset). text is a case-insensitive substring
    search across all text columns. Returns newest-updated first, up to limit.
    Example: find_records("movies", filters={"year": 2021}, text="dune").
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
) -> dict[str, Any]:
    """Record a relationship, or a person's history with an item (upserts).

    This is where all personal taste lives. The user themself is the profile
    'self' — "I'd give Dune an 8.5, watched it on 2026-08-18" is:
    add_link("profiles", "self", "movies", "dune-2021",
             values={"rating": 8.5, "at": "2026-08-18"}).
    Omit kind for these profile-to-item history links. values may contain
    rating (numeric; scales in describe_schema — never invent a rating the
    person didn't express), at (YYYY-MM-DD), and note. Calling again updates
    the same link: at maintains first_at/last_at server-side, omitted
    rating/note are preserved, explicit null clears them, and props (explicit
    long-tail JSON) merges with null deleting a key.
    For any other relationship pass a registered kind — part_of, pairs_with,
    recommended, directed, acted_in, wrote, performed, made — and register new
    kinds via migrate first.
    """
    return engine().add_link(
        from_collection, from_id, to_collection, to_id, kind, values, props
    )


@mcp.tool(annotations=_DESTRUCTIVE)
def remove_link(
    from_collection: str,
    from_id: str,
    to_collection: str,
    to_id: str,
    kind: str | None = None,
) -> dict[str, Any]:
    """Remove the exact relationship identified by its endpoints and optional kind."""
    return engine().remove_link(from_collection, from_id, to_collection, to_id, kind)


@mcp.tool(name="query", annotations=_READ_ONLY)
def query_database(
    sql: str,
    parameters: list[Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Run a read-only SQLite SELECT for arbitrary cross-collection questions.

    Important links-table encoding: bare personal-history links store
    kind = '' (empty string, NOT NULL) — filter with kind = ''. Example,
    "movies Anna rated 8+ that Dad hasn't seen": join movies to links on
    to_collection = 'movies' AND to_id = movies.id AND kind = '' with
    from_id for Anna, excluding ids Dad has links to. Use describe_schema
    first when table or field names are unclear. Parameters use SQLite ?
    placeholders. Results are capped by limit.
    """
    return engine().query(sql, parameters, limit)


@mcp.tool(annotations=_ADDITIVE)
def create_collection(
    name: str,
    description: str,
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a genuinely new collection (item type) with a few typed fields.

    For new kinds of things the user tracks (boardgames, restaurants, ...);
    the standard collections already exist. Each field is {"name", "type",
    "description", optional "required"}; types: text, integer, real, boolean,
    date, datetime. Fields are for intrinsic facts only — personal history
    stays on links. The id, timestamps, and extra backbone are added
    automatically. Start minimal; long-tail observations earn structure later.
    Example: create_collection("restaurants", "Restaurants worth remembering",
    fields=[{"name": "name", "type": "text", "required": true},
            {"name": "city", "type": "text"}]).
    """
    return engine().create_collection(name, description, fields)


@mcp.tool(annotations=_DESTRUCTIVE)
def migrate(description: str, statements: list[str]) -> dict[str, Any]:
    """Apply agent-authored SQLite DDL/DML after taking a restorable snapshot.

    Statements run in one transaction. On error, the transaction is rolled back
    and the snapshot is kept. Use this for schema evolution (promoting recurring
    extra/props keys to real columns, backfills, reshaping) and for deliberate
    _meta updates:
    - new link kind: INSERT INTO _meta (scope, collection, name, description)
      VALUES ('link_kind', '', '<kind>', '<meaning>')
    - rating scale: scope='rating_scale', collection='<target>',
      name='default', value_json='{"min":1,"max":5}'
    Returns the resulting live schema.
    """
    return engine().migrate(description, statements)


@mcp.tool(annotations=_DESTRUCTIVE)
def restore_snapshot(snapshot: str) -> dict[str, Any]:
    """Restore the database to a named point-in-time snapshot.

    Data written after that snapshot is lost. A safety snapshot of the current state
    is taken first, so this restore can itself be reversed. Use list_snapshots to
    obtain an exact snapshot filename.
    """
    return engine().restore_snapshot(snapshot)


@mcp.tool(annotations=_READ_ONLY)
def list_snapshots() -> list[dict[str, str]]:
    """List available point-in-time restore targets, newest first."""
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
