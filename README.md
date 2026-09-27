# fkey

A personal taste database managed by AI agents over MCP. It stores movies,
shows, books, music, wines, cocktails, public figures, and whatever collections
the user and their agent add later.

It can run locally over stdio with one SQLite file, or as an authenticated HTTP
server with one isolated SQLite file per account. The evolving design lives in
[`docs/PLAN.md`](docs/PLAN.md).

## Data model

- Each item type is a real SQLite table with typed fields plus explicit `extra`
  JSON for long-tail observations.
- `profiles` contains the user (`self`) and their family and friends.
- A bare profile-to-item `link` stores that person's optional numeric rating,
  first/latest activity date (`YYYY-MM-DD`), note, and explicit `props` JSON.
- Named links express relationships such as `part_of`, `pairs_with`,
  `directed`, and `acted_in`.
- The agent may evolve the database through raw SQLite migrations; every
  migration creates a restorable snapshot first.

New databases are seeded with `movies`, `shows`, `books`, `music`, `wines`,
`cocktails`, `people`, `profiles`, and `links`.

## Setup

Requires Python 3.13 or newer and [uv](https://docs.astral.sh/uv/). The
development runtime is Python 3.14:

```bash
uv sync
```

Run the MCP server over stdio:

```bash
uv run fkey
```

The default database is `data/local/db.sqlite`. Override it with either:

```bash
uv run fkey --db /path/to/db.sqlite
FKEY_DB=/path/to/db.sqlite uv run fkey
```

## MCP tools

| Tool | Purpose |
|---|---|
| `describe_schema` | Live collections, fields, profiles, link kinds, rating scales, and recent migrations |
| `add_record` / `get_record` / `update_record` / `delete_record` | Generic record CRUD |
| `add_multiple_records` | Atomically create a same-collection batch, with optional profile links |
| `find_records` | Exact field filters and text search |
| `add_link` / `remove_link` | Profile history and named relationships |
| `query` | Read-only SQLite SELECT for arbitrary questions, stopped after 30 seconds |
| `create_collection` | Add a new item type with a minimal typed schema |
| `migrate` | Apply transactional DDL/DML after taking a snapshot |
| `list_snapshots` / `restore_snapshot` | Inspect and restore point-in-time snapshots |

Write tools accept `minimal: true` when only identifiers and essential safety
details are needed. `add_multiple_records` defaults to minimal output and is
create-only; use `update_record` and `add_link` to enrich existing records.
Suffixed ids are returned with `possible_duplicate_of` so imports can review
potential collisions without blocking legitimately distinct records.

The tool schemas are static. Collection names and fields are validated against
the live SQLite schema, so newly created or migrated collections work without
refreshing the MCP tool catalog.

## Use with Claude Code

```bash
claude mcp add fkey -- uv run --directory /Users/kuchin/Work/Mirable/fkey fkey
```

## Use locally with Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fkey": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/kuchin/Work/Mirable/fkey", "fkey"]
    }
  }
}
```

This local stdio configuration does not use web accounts or OAuth. It continues
to read `data/local/db.sqlite` unless `FKEY_DB` is set.

## Authenticated HTTP

Streamable HTTP requires OAuth for every MCP request. For local testing:

```bash
uv run fkey --http --port 8000
```

Behind an HTTPS tunnel or reverse proxy, set its public origin in `.env` and add
`--public`. `FKEY_PUBLIC_URL` is the origin only; do not append `/mcp`.

```bash
FKEY_PUBLIC_URL=https://fkey.example.com
uv run fkey --http --port 8000 --public
```

The remote MCP URL is then `https://fkey.example.com/mcp`. The server exposes
OAuth protected-resource and authorization-server discovery, Dynamic Client
Registration for compatible connectors, Authorization Code + PKCE login,
one-hour access tokens, rotating 30-day refresh tokens, grant-family revocation
when a rotated token is reused, and explicit revocation.

### Invite-only signup

Copy the example environment file and replace the signup code with a long,
random value:

```bash
cp .env.example .env
uv run fkey --http --port 8000
```

The signup form is available at `<public-origin>/signup`. A successful signup
stores the account in `data/accounts.sqlite` and provisions its personal
database below `data/users/<user-id>/db.sqlite`. Passwords are stored as salted
scrypt hashes; the signup code is read from `FKEY_SIGNUP_CODE` and is never
stored in the database.

Each authenticated `/mcp` request is routed from the access token's user
identity to that account's database. User IDs are never MCP tool arguments and
accounts are not exposed through MCP.

### Browse UI

Signed-in users can browse their database read-only at `<public-origin>/app`:
an Airtable-style grid per collection with search and sorting, and record
pages whose links lead to the related records. It uses the same account as the
connector. See [`docs/UI.md`](docs/UI.md).

### Local Docker Compose

The local Compose stack runs fkey with Redis-backed limits on signup, login,
OAuth registration, authorization, token exchange, and revocation. It mounts
the existing `data/` directory, so local accounts and databases are preserved.

```bash
cp .env.example .env  # if needed; set FKEY_SIGNUP_CODE
make up
```

The app listens only on `127.0.0.1:8000`; Redis is reachable only inside the
Compose network and stores disposable rate-limit counters. Stop any host-run
fkey process on port 8000 before starting Compose. To stop the stack:

```bash
make down
```

For a Cloudflare tunnel, set `FKEY_PUBLIC_URL` in `.env` before starting the
stack, then continue pointing the tunnel at `http://localhost:8000`.

### Deploy beta.fkey.app

Production runs on `beta.fkey.app` behind the host's Caddy service. The app is
bound to host loopback on port 9040; only Caddy exposes it publicly. SQLite
account and user databases persist in `/opt/apps/fkey/data`, while Redis holds
only disposable rate-limit counters. The containers run as UID 10001;
`make deploy` gives that user ownership of `data/` and `backups/` before
starting them.

Create the production environment and set a real `FKEY_SIGNUP_CODE`, then
deploy the current working tree:

```bash
cp .env.prod.example .env.prod
make deploy
```

Production Compose reads `.env.prod`; local Compose continues to read `.env`.
For the first deploy only, `make deploy` falls back to `.env` if `.env.prod`
does not exist, so an existing local signup code can bootstrap the server.

The target defaults to `appuser@beta.fkey.app:/opt/apps/fkey` and uses normal
SSH configuration and agent resolution. Override `DEPLOY_HOST` or
`DEPLOY_PATH` on the `make` command when needed. The remote MCP URL is
`https://beta.fkey.app/mcp` and signup is at `https://beta.fkey.app/signup`.

### Backups

The production Compose stack takes one SQLite-safe backup per UTC day. Its
small backup service checks every six hours and creates a set only when that
day's set does not exist. Each set contains `accounts.sqlite`, every user
database, and each user's available agent-created migration/restore snapshots.
The engine keeps the newest 15 agent snapshots per user, and the backup service
keeps the newest 7 daily sets in `/opt/apps/fkey/backups`. Its primary-data
mount is read-only.

Run an extra backup at any time with:

```bash
make backup
```

To restore a complete set, replace `<timestamp>` with a directory from
`backups/`. Stop both data users, preserve the current directory as a safety
copy, install the selected set as the whole new `data/`, then restart:

```bash
cd /opt/apps/fkey
sudo docker compose -p fkey --project-directory . --env-file .env.prod \
  -f infra/docker-compose.prod.yaml stop app backup
sudo mv data data.before-restore
sudo cp -a backups/<timestamp> data
sudo docker compose -p fkey --project-directory . --env-file .env.prod \
  -f infra/docker-compose.prod.yaml up -d app backup
```

Confirm login and user data before removing `data.before-restore`. Restoring
`accounts.sqlite` also rolls OAuth state back: newer tokens require connector
reauthorization, and token revocations made after the backup are undone.

These local sets protect against application mistakes but remain on the same
server disk. Encrypted off-site restic storage is still required for recovery
from complete server or disk loss.

### Use remotely with Claude

Remote connectors are configured in Claude under **Customize → Connectors →
Add custom connector**, not in `claude_desktop_config.json`. Enter the public
MCP URL and leave the advanced OAuth client fields empty so Claude can use
Dynamic Client Registration. Choose **Connect**, then sign in through the fkey
authorization page. The remote connector becomes available in Claude Desktop,
web, and mobile.

If the local stdio entry remains in `claude_desktop_config.json`, Claude Desktop
will show both local and remote fkey tools. Remove or disable the local entry
after confirming the remote connector works to avoid that duplication.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

Integration scenarios use the normalized YAML fixture in
`tests/fixtures/seed.yaml`. The test-only loader applies its collections,
records, and links through the same `TasteDB` operations used by MCP tools.
