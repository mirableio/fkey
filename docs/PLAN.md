# Plan: A Personal Taste Database, Managed by Agents

## 1. Why

Everyone accumulates taste: movies you loved, wines you'd buy again, books you
abandoned, the cocktail your friend orders every time. Today that knowledge is
scattered — half-remembered, spread across apps that each own one vertical
(Letterboxd for movies, Vivino for wine, Goodreads for books), none of which
talk to each other and none of which know about *your people*.

Meanwhile, AI assistants (Claude, ChatGPT) are becoming the natural interface
for this: you just *tell* them "we watched Dune with the kids, everyone loved
it except Misha who fell asleep" — no forms, no apps. But assistants have no
durable, structured place to put that knowledge, and their built-in memory is
flat text they can't reliably query.

This project gives every user a personal database that their AI assistant
reads and writes on their behalf:

- **One place for everything.** Movies and TV, books, music, wine, cocktails —
  and anything else, because the assistant can extend it.
- **Your people are first-class.** The database knows your family and friends
  and what each of them loves or hates, across all of it. "What should we watch
  tonight that Anna hasn't seen and Dad won't hate?" becomes an answerable
  query instead of a memory exercise.
- **The assistant is the librarian, not just a note-taker.** It doesn't only
  add rows — it reorganizes. When it notices you keep recording grape variety
  for wines, it adds a proper field for it. When series/seasons/episodes need
  structure, it builds it. The schema grows to fit *your* life, safely, with
  backups and undo.
- **Works everywhere you talk to an assistant.** Claude on web, desktop and
  phone; ChatGPT on web. One server, one account, same data.

The bet behind the design: with LLM agents in the loop, a rigid
one-size-fits-all schema is the wrong foundation. What's needed is a small,
safe set of primitives (records, links, profiles of your people, migrations)
plus good starting templates — and the agent does the rest, differently for
each user.

## 2. What we're building

### Design ethos: simple, intuitive, no over-engineering

This principle outranks every other consideration in this document and applies
to everything — data model, tool surface, code, auth, docs:

- **Prefer the simplest thing that works.** Every mechanism must pay rent
  today, not in a speculative future. When a feature idea arrives, the first
  question is "what happens if we just don't build this?"
- **Intuitive beats clever.** A tool an agent calls correctly without reading
  twice beats a more powerful one it fumbles. Same for a schema a human can
  read over their assistant's shoulder.
- **Fewer concepts.** Every noun in the design (collection, link, migration)
  must justify its existence; anything derivable from something that already
  exists should not get its own name or mechanism.
- **Boring technology, used plainly.** SQLite files, human-readable data,
  file-copy backups. No abstraction layers around things that are already
  simple.

When reviewing any part of design or code, "this could be simpler" is a
finding, not a nitpick.

A single multi-tenant MCP server ("one connector, many collections") where:

1. **Each user gets their own SQLite database file.** Complete isolation,
   trivial backup/restore, no shared-schema coupling between users.
2. **Everything is a collection, and the standard ones come ready.** A new
   database is seeded at creation with the collections most people want —
   `movies`, `shows`, `books`, `music`, `wines`, `cocktails`, and `people`
   (public figures: actors, directors, authors, musicians, bands — the
   IMDb/TMDB sense of "People", deliberately separate from the user's own
   `profiles`) — each with a handful of sensible typed fields. The user's
   first interaction is adding a record, never designing a schema. New types
   (boardgames, restaurants, perfumes, ...) are added by the agent via
   `create_collection`. After creation the schemas belong to the user —
   reshape, extend, or drop them freely; two users' wine schemas may
   diverge. Seeding happens once, at creation; existing databases are never
   touched by seed updates.
3. **Two primitives exist in every database, above any collection:**
   - `profiles` — the user's own people: family, friends, and a seeded
     `self` record for the user (so "I love Tarantino" has a valid subject).
     Virtual profiles, no accounts; distinct from `people`, the
     public-figures collection.
   - `links` — a generic edge table connecting any record to any record. A
     bare link from a profile to an item records that person's history with
     it, through three optional fields: numeric `rating` (numbers, not
     stance words), `first_at`/`last_at` dates (so "we rewatched it last
     night" is an update, not a new row), and a note. No verb or concept
     name needed — the target collection already implies what engaging means
     (a link to a movie can only mean watching). `kind` is reserved for
     relationships the endpoints don't imply: `part_of`, `pairs_with`,
     `recommended`, credits like `directed`.
4. **The agent can evolve the schema through a migration tool** that always
   snapshots the database file first, applies changes transactionally,
   integrity-checks the result, and records what it did. `restore_snapshot`
   returns to any snapshot, point-in-time.
5. **The agent can query with read-only SQL**, so arbitrary cross-collection
   questions don't require us to predict them.
6. **The agent is oriented by stable conventions plus one discovery call.**
   Both modes use safe generic instructions; multi-user HTTP initialization
   happens before a user is authenticated, so per-database instructions would
   risk leaking the wrong account's schema. `describe_schema` is the
   authoritative, on-demand view of the current database.
7. **Auth is a contained OAuth 2.1 provider.** Code-gated email/password
   signup supplies identity; Authorization Code + PKCE, dynamic client
   registration, opaque tokens, and per-token user routing supply the remote
   connector flow (see 3.7).

### Explicitly decided (and why)

| Decision | Choice | Why |
|---|---|---|
| One MCP server vs one per collection type | **One server** | Profiles/links must span collections (separate servers can't join); ~14 generic tools beat 30+ near-duplicates in the agent's context; one connector per user per platform. Type-specific tailored tools would go stale anyway once schemas evolve at runtime. |
| Storage model | **Real tables + `extra` JSON column per collection** | Typed columns give indexes, constraints and natural SQL; the JSON column absorbs long-tail observations without ceremony. The agent's playbook: new facts start in `extra`, recurring keys get *promoted* to real columns via migration. Structure is earned, not guessed. A pure document store would forfeit SQL and enforce nothing; a pure relational model would demand a migration for every stray observation. |
| Agent SQL access | **Yes, read-only** | Enforced at the connection level (`PRAGMA query_only` + authorizer), so it physically cannot write. Agents are good at SQL; this replaces an unbounded family of bespoke query tools. |
| Auth | **Contained OAuth 2.1 provider for now** | A shared signup code gates email/password accounts; Authorization Code + PKCE and DCR match current connector behavior. The provider boundary stays replaceable as clients move from DCR toward Client ID Metadata Documents. |
| Personal vs intrinsic facts | **Item tables hold intrinsic/shared facts only; all personal facts live on profile links** | `movies.rating` on the record *and* rating on a `self` link would be two homes for the same fact — the agent would have to choose, and choice is where inconsistency breeds. Item tables carry what's true of the thing itself (title, year, ...); every person's relationship to it — including the owner's, via `self` — is a link with rating/dates/note. One representation for everyone. A single entry remains `add_record` + `add_link`; create-only backfills use their atomic `add_multiple_records` composition. |
| Backfill writes | **One narrow create-only batch tool** | `add_multiple_records` creates records from one collection with optional profile links in one transaction. It is not a generic operation DSL and never updates existing records; enrichment stays explicit through `update_record` and `add_link`. Minimal output is the default so batching reduces context as well as round trips; suffixed ids carry `possible_duplicate_of` rather than treating every slug collision as the same real-world item. |
| Opinions | **Numeric `rating` on links, not stance words** | Stance vocabularies drift under agent use (`loves`, `loved`, `favorite`, `really_likes`, ...); a number can't. Ratings are `REAL` (decimals like 4.8 welcome); default scale 1–10 with documented meaning (1–2 hate … 9–10 love), per-collection overrides in `_meta` (e.g. 1–5 for books) — the user renegotiates semantics with their agent, not with us. |
| Dated activity | **`first_at`/`last_at` on links; no events table** | A separate events table forces a classification decision (durable opinion vs dated event) on every write — two plausible homes for the same fact is how agent-written data goes inconsistent. Two typed date columns answer the real queries ("when first/last"). Escalation, if full history ever matters to a user: an activity collection via `create_collection` — no intermediate JSON-list convention (that would be duplicated truth with awkward reads). |
| Link identity | **`(from, to, kind)` unique; `add_link` upserts** | Makes repeat events idempotent updates (bump `last_at`, keep `first_at`, overwrite `rating`, merge `props`) instead of silently accumulating duplicate edges across sessions. This is what makes the dates design work at all. |
| Link kinds | **Bare links for person→item; closed vocabulary for the rest** | Consumption verbs (`watched/read/drank`) would be 100% redundant with the target collection — one extra naming decision per write, pure drift surface. A bare profile→item link records the person's history with the item via its optional fields (rating, dates, note); the edge itself has no concept name, because naming it after any one field would push agents to fabricate that field or skip logging when it's absent. Kinds exist only where endpoints don't imply the meaning (`part_of`, `pairs_with`, `recommended`, credits — real information: people→movies could be `directed` or `acted_in`), validated against the `_meta` registry with suggestions; new kinds created deliberately, with a description. |
| Schema source of truth | **SQLite itself** | Structure is derived live from `sqlite_master` / `PRAGMA table_info`; a small `_meta` table adds only human/agent-facing descriptions and semantics. The schema registry can never drift from reality. |
| Restore semantics | **File-snapshot restore, point-in-time** | Honest and bulletproof at this scale (the tool is named `restore_snapshot`, not "rollback", because that's what it does). True down-migrations are a rabbit hole. The snapshot catalog lives *outside* the user DB — restoring a file must not erase the history needed to undo the restore. |
| Tool schemas | **Static; `collection` is a plain string** | Clients cache tool lists and refresh unevenly (ChatGPT may need manual refresh); a per-user collection enum would go stale and *block* writes to new collections. Server-side validation with did-you-mean suggestions gives the same safety without the fragility. |
| Starting schemas | **Seed all standard collections at DB creation** | A regular user's first interaction should be adding a record, not designing a schema — and seeded collections in the session summary teach humans what the system can hold. Seeding is part of the one-time bootstrap, not an install/versioning mechanism; only *adding new types* needs a tool. |
| `extra` keys | **Explicit parameter, never implicit routing** | A typo like `Year` or `tittle` must be an error ("did you mean `title`?"), not permanent data. The agent states its intent: typed fields at top level, long-tail observations in an explicit `extra` dict. |

## 3. How

### 3.1 Architecture

```
┌────────────────────────────────────────────────┐
│ MCP server (streamable HTTP; stdio for dev)    │
│  ~14 generic tools                             │
├────────────────────────────────────────────────┤
│ auth: contained OAuth 2.1 provider             │
│  token → user_id                               │
├────────────────────────────────────────────────┤
│ engine                                         │
│  schema: derive from SQLite + _meta overlay    │
│  records/links: validated CRUD, link registry  │
│  migrator: snapshot → transact → verify → log  │
│  snapshots: external catalog + restore         │
│  bootstrap: new-DB seeding (profiles, links,   │
│    standard collections, _meta, conventions)   │
│  store: user_id → data/{user_id}/db.sqlite     │
│         read-only connection for query tool    │
└────────────────────────────────────────────────┘
```

Layered so the engine is fully testable without MCP or auth (phase 1 runs it
over stdio with a single local user).

### 3.2 Per-user database layout

Every database contains:

- **System tables** (underscore-prefixed):
  - `_meta` — descriptions and semantics for collections, fields, link kinds,
    and rating scales (default 1–10 with decimals; per-collection overrides).
  - `_migrations` — id, timestamp, description, statements applied, backup
    file reference. Logical history for the agent; the *snapshot catalog*
    lives outside the DB (see 3.5), so restores can't erase it.
- **Built-in collections:** `profiles`, `links` (see 3.3).
- **Standard collections, seeded at creation:** `movies`, `shows`, `books`,
  `music`, `wines`, `cocktails`, `people` — each a real table with a
  mandatory backbone — `id` (slug), `created_at`, `updated_at`, `extra`
  (JSON) — plus a few typed columns for **intrinsic facts only** (e.g.
  movies: `title`, `year`; people: `name`, `kind`). Item tables never carry
  personal fields like a rating or a watch date — those are links (3.3),
  including the owner's own, via `self`. These seed schemas are a maintained
  part of the server.
- **Agent-created collections:** any new type the user's life turns out to
  need, added via `create_collection`; same backbone, same rules.

Conventions the server enforces:

- Record ids are human-readable slugs (`inception-2010`), unique per
  collection, suffixed on collision.
- `extra` is an explicit parameter: typed fields at top level, everything
  else deliberately placed in `extra`. Unknown top-level keys error with
  suggestions — a typo never silently becomes data. On update, `extra` is
  merged, never replaced; a null value deletes a key.
- All writes go through exactly two doors: the record/link tools (known
  shapes, validated) or `migrate` (anything, but always behind a snapshot).

### 3.3 Profiles, people, and links

`profiles` — the user's own people, seeded in every DB: `name`, `relation`
(self, wife, son, friend, ...), `birthday?`, `notes`, `extra`. One record
with the stable id `self` is created at bootstrap (its name may change; the
id never does), so the owner's tastes are stored exactly like everyone
else's ("I love Tarantino" links from `self`). Virtual profiles: these are
*the user's* notes about their people; they don't have accounts and are
never contacted.

`people` — public figures, in the sense IMDb/TMDB use "People": actors,
directors, authors, musicians, bands (`kind: band`), winemakers. `name`,
`kind`, `notes`, `extra`. Deliberately separate from `profiles`: profile
entries are private observations (relations, tastes, notes about your kids),
people are shared public facts (filmographies, discographies) — different
privacy weight, different agent etiquette (ask before adding someone's
profile; freely add Villeneuve when logging a movie). "My friend Lev hated
it" → `profiles` (it has the `relation` field), "add Zendaya to the cast" →
`people`. Public figures make "what else by this director that Anna would
like" a join, and let taste attach to makers, not just works — *self loves
Tarantino* is often more durable than any one movie rating. The rare
friend-who-is-also-an-artist is a link between the two records, not a merged
table.

`links` — one edge table for all relationships, durable and dated alike:

```
from_collection, from_id, to_collection, to_id, kind,
rating (REAL, nullable), first_at, last_at (nullable),
note, props (JSON), created_at, updated_at
```

- **A bare link records a person's history with an item.** `kind` may be
  omitted only when `from` is a profile and the target is not another
  profile — everywhere else it's required (a bare movie→movie or
  profile→profile edge would mean nothing). The target collection already
  says what engaging means (a link to a movie can only mean watching — a
  verb would be redundant, and redundant naming decisions are drift
  surface). The edge is defined by its optional fields, which are
  independent: rating without dates ("Anna loves it, unclear when she saw
  it"), dates without rating ("watched, jury's out"), just a note ("Misha
  fell asleep"). "Dad hates subtitles" is a bare link to a tag record with
  rating 1. Absence of a link means *no recorded history* — "Dad hasn't
  seen it" queries answer from what's logged, not from omniscience.
- **Identity:** `(from, to, kind)` is unique; `add_link` upserts. Patch
  semantics are explicit: omitted fields are preserved, an explicit `null`
  clears, `props` merges. (Bare kind is stored as `''` so uniqueness holds —
  SQLite treats NULLs as distinct in unique indexes.)
- **Dates are maintained by the server, not computed by agents.** `add_link`
  takes a single optional canonical ISO date `at` (`YYYY-MM-DD`): new link
  sets `first_at = last_at = at`; an
  existing link sets `first_at = min(first_at, at)`, `last_at =
  max(last_at, at)`; no `at` leaves both untouched. "We rewatched it last
  night" is one idempotent call — the agent states what happened, invariants
  are the server's job. `first_at`/`last_at` answer the real queries ("when
  did we first/last watch"); if full history ever matters to a user, the
  step is an activity collection via `create_collection` — no intermediate
  JSON-list convention, and never a second built-in table.
- **Opinions are ratings, not stance words.** A nullable numeric `rating`:
  `anna → dune (rating 8.5, last_at ...)`. Ratings are `REAL` — decimals
  like 4.8 are fine. The default scale is 1–10 with documented meaning (1–2
  hate ... 9–10 love); `_meta` holds per-collection overrides (a user who
  thinks in 1–5 for books tells their agent once). Rating-scale rows use
  `scope='rating_scale'`, the target collection (or empty for the global
  default), `name='default'`, and a `value_json` object containing `min` and
  `max`. Item tables carry no
  rating fields at all — every rating, the owner's included, is somebody's
  link, so "whose opinion" is always explicit.
- **Kinds exist only where the endpoints don't imply the meaning**, as a
  closed, extendable vocabulary. Seeded: `part_of` (hierarchies),
  `pairs_with`, `recommended`, and credit kinds (`directed`, `acted_in`,
  `wrote`, `performed`, `made` — real information: people→movies could be
  either `directed` or `acted_in`, and Eastwood is both on the same film).
  `add_link` validates `kind` against the `_meta` registry and errors with
  suggestions on anything unknown; a new kind (a future `wants`, say) is
  registered by updating the registry via `migrate` — deliberate, described,
  and no dedicated tool needed.

This one table is what makes the promised queries possible: "movies Anna
rated 8+ that Dad hasn't seen" is a join over `movies`, `links`, and
`profiles`; "what did we watch last month" is `to_collection = 'movies' AND
last_at >= ...` — no special-purpose code.

### 3.4 Tools (the whole surface, ~14)

| Tool | Purpose | Notes |
|---|---|---|
| `describe_schema` | No argument: full schema — all collections, fields with descriptions, link kinds, rating scales, row counts, recent migrations. With `collection`: deep detail on one — fields, common `extra` keys seen so far, relevant link kinds, a sample record | One tool, two zoom levels. The no-arg output is the authoritative version of the summary session `instructions` deliver at connect time (see 3.6); the per-collection view is what an agent checks before writing records well. |
| `add_record` | Insert into a collection | Validates against live schema; `extra` is an explicit dict parameter; unknown top-level keys error with suggestions. Unknown *collection* is an error pointing at `create_collection` (no silent table creation from typos). `collection` is a plain string — tool schemas are static; validation lives server-side. Returns stored record with id. |
| `add_multiple_records` | Atomically insert multiple records from one collection | Each item contains `values`, optional `extra`, and an optional bare profile link to the new record. Create-only: existing records are enriched through `update_record` / `add_link`. All items commit or all roll back; compact id-and-warning output is the default. |
| `get_record` | Full record + its links | |
| `update_record` | Partial update; `extra` merged | |
| `delete_record` | Remove record (+ its links) | |
| `find_records` | Structured filter (collection, field=value, text contains, limit) | The 80% case without SQL. |
| `add_link` / `remove_link` | Manage edges | `kind` optional only for profile→non-profile (the common case; rating/dates/note carry the meaning), required otherwise. Upserts on `(from, to, kind)`; single optional `at` maintains `first_at`/`last_at` as min/max server-side; omitted fields preserved, explicit `null` clears, `props` merges. Named kinds validated against the registry. Link listing is folded into `get_record` / `query`. |
| `query` | **Read-only** SQL SELECT | Separate connection with `PRAGMA query_only` + authorizer rejecting non-SELECT; row/size limits. |
| `create_collection` | Create a new collection: name, fields with types + descriptions | For genuinely new types only (standard ones are pre-seeded): backbone columns (`id`, timestamps, `extra`) added automatically, `_meta` populated. Description teaches "start minimal — a few fields plus `extra`". |
| `migrate` | Apply agent-authored DDL/DML with automatic snapshot | The single door for all schema evolution: promoting `extra` keys or `props` data to columns, reshaping, backfills, updating `_meta` descriptions and the kind registry. Deliberately one powerful tool rather than a suite of constrained schema operations — snapshots plus readable errors make mistakes recoverable, and helpers can be added later if dogfooding proves agents fumble. Contained to the user's own DB (see 3.5). |
| `restore_snapshot` | Restore the DB to a named snapshot | Honest name: a point-in-time restore, not a logical rollback. Data written after the snapshot is lost (stated in the description); a safety snapshot is taken first, so restores are themselves restorable. |
| `list_snapshots` | Available restore targets — scanned from snapshot filenames, each showing timestamp and trigger (migration / restore-safety) | The restore-target picker. Migration *history* isn't a separate tool: recent migrations appear in `describe_schema`, and `_migrations` is queryable via `query`. |

Design rule: server instructions carry the short global model; individual tool
descriptions carry only behavior specific to that operation. `describe_schema`
owns live, user-specific state such as fields, profiles, link kinds, and rating
scales. One critical sentence about records versus links is intentionally
repeated on the relevant write tools for clients that surface instructions
poorly.

Write tools accept `minimal: true` to return identifiers and essential safety
details instead of full stored objects or schemas. Existing single-write tools
default to full results; `add_multiple_records` defaults to minimal so a batch
does not echo its entire input back into agent context.

Second design rule: **schema tools and data tools don't mix.**
`describe_schema` returns structure, never records (the sample record in its
per-collection view is the one deliberate exception — it exists to teach
conventions); records flow only through `get_record` / `find_records` /
`query`. The profiles roster in session instructions is a convenience copy —
the truth is always a `find_records` away.

### 3.5 Migrations, snapshots, restore

`migrate(description, statements[])` executes:

1. **Snapshot** the user's DB file via the SQLite backup API (consistent even
   during concurrent activity) into `data/{user_id}/backups/`.
2. **Apply** all statements in a single transaction, on a **contained
   connection**: an SQLite authorizer rejects anything that escapes the user
   DB or defeats the snapshot/transaction guarantees — `ATTACH`/`DETACH`,
   `VACUUM`, explicit `BEGIN`/`COMMIT`/`ROLLBACK`, `load_extension`,
   dangerous PRAGMAs (`writable_schema`, ...), and writes to `_migrations`.
   Raw DDL/DML freedom stays intact *inside* the database (`_meta` is
   writable — that's how descriptions, rating scales, and the kind registry
   evolve with the schema).
3. **Verify**: `PRAGMA integrity_check`, `PRAGMA foreign_key_check`; a
   **link-integrity scan** (polymorphic links aren't covered by SQLite FKs —
   an orphan check over `links` against the live collections is app-level);
   required structures still exist (`profiles`, `links`, `_meta`,
   `_migrations`, backbone columns). Any of these failing rolls the
   transaction back with a readable error (snapshot kept regardless).
   **`_meta` reconciliation** runs last: descriptions for dropped
   columns/tables are pruned, new columns missing descriptions come back as
   *warnings* — they never fail an otherwise valid migration.
4. **Record** the migration in `_migrations` with the statements and backup
   reference.
5. **Return** the fresh `describe_schema` summary so the agent sees the
   result.

**The backup directory is the catalog.** Snapshot filenames are
self-describing —

```
2026-08-19T143200--migration--42--add-vintage.sqlite
2026-08-19T151000--restore-safety.sqlite
```

— and `list_snapshots` scans them. No sidecar index to maintain or recover,
nothing to drift when snapshots are pruned. The catalog lives outside the
database on purpose: `_migrations` is *inside* the file being restored, so it
alone could never safely describe the restore's own undo path.

**Restore procedure** (`restore_snapshot`): acquire the per-user write lock →
copy and validate the candidate snapshot without touching the live database →
close cached connections → take a safety snapshot of the current state
(cataloged) → replace the DB file atomically → reopen → run the same
integrity, structure, and link checks as step 3. Point-in-time, honestly
documented: data written after the snapshot is lost, but the safety snapshot
makes the restore itself reversible.

Notes:

- SQLite `ALTER TABLE` is limited (no type changes, constrained drops); the
  standard workaround (create new table → copy → rename) is just statements,
  and the `migrate` tool description teaches the pattern.
- The engine keeps the newest 15 migration/restore snapshots per user. The
  filename directory remains the restore catalog; older `_migrations`
  references are historical and may outlive their restore files.
- A Compose backup sidecar uses the SQLite backup API to create one complete
  daily set containing `accounts.sqlite`, every user database, and their
  retained agent snapshots. It keeps 7 local daily sets. Encrypted off-site
  restic retention is a separate deployment step; it consumes completed sets,
  never live databases.

### 3.6 Orientation and bootstrapping

**Session-start orientation.** The MCP `instructions` field contains generic
conventions. Multi-user HTTP initialization occurs before an authenticated
request identifies a user, so instructions must not contain any account's
schema:

> Manage the user's personal taste database. Use `describe_schema` to inspect
> the live collections before unfamiliar writes. Personal opinions and dates
> belong on profile-to-item links; intrinsic facts belong on item records.

Instructions are only a hint: clients cache initialize results and surface them
unevenly. `describe_schema` is the authoritative source on demand, after schema
changes, and for every authenticated HTTP user — nothing database-specific
lives in instructions.

**Seeded start, extendable forever.** A fresh database is bootstrapped in one
shot: `profiles` (with the `self` record), `links`, `_meta` (descriptions,
link-kind registry, rating scales), conventions, and the standard collections
(`movies`, `shows`, `books`, `music`, `wines`, `cocktails`, `people`) with
their seed fields and link kinds. The user's very first message ("add Dune,
we loved it") is an `add_record` plus an `add_link` from `self` — simple
calls that work on every client, regardless of how capable its model is. Genuinely new types arrive later via `create_collection`;
unused seeded collections cost nothing (one quiet line in the summary) and
the agent may drop or reshape them like anything else. The seed schemas are
the one place type-specific knowledge lives in the server, and they're
maintained as a first-class artifact — but they apply only at creation time:
existing user databases are never touched by seed updates.

### 3.7 Auth and multi-tenancy

The first multi-user implementation is deliberately self-contained:

- A shared signup code from `FKEY_SIGNUP_CODE` protects the web signup form;
  accounts use email/password with salted scrypt hashes.
- Every account gets a server-generated UUID and
  `data/users/{user_id}/db.sqlite`. The UUID comes only from a validated access
  token and is never an MCP argument.
- The isolated in-process authorization provider implements Authorization Code
  + PKCE, opaque one-hour access tokens, rotating 30-day refresh tokens,
  grant-family revocation when a rotated refresh token is reused, explicit
  revocation, audience/resource validation, and OAuth discovery.
- Dynamic Client Registration is enabled for current Claude compatibility.
  The provider is kept separate from the engine and tool modules so it can be
  replaced by a dedicated authorization server without changing data routing.
- HTTP MCP requests require the `fkey` scope. Stdio remains an explicitly local,
  single-database mode without OAuth.

Redis-backed IP limits protect signup, login, registration, authorization,
token exchange, and revocation when `FKEY_REDIS_URL` is configured. Deliberately
still missing: Client ID Metadata Documents (DCR is being phased out by the
protocol), password reset, email verification, an account/admin UI, and live
connector verification against every target client. Those should be driven by
actual deployment needs rather than expanding the account system in advance.

### 3.8 Deployment

- Docker image; single small VPS.
- TLS via platform or Caddy; both connector platforms require HTTPS.
- Persistent bind mounts for `data/` and completed daily `backups/`; the
  backup sidecar mounts primary data read-only and uses the SQLite backup API.
  Off-site backup will use encrypted restic over completed sets only.
- Structured logs; per-user request counts as the first metric.

## 4. Implementation plan

### Phase 1 — Engine + tools, local (the bulk of the value)

1. Engine: store, schema derivation + `_meta` overlay, records CRUD with
   validation, slugging, explicit-`extra` semantics; links with upsert
   identity, kind registry, ratings, server-maintained `first_at`/`last_at`
   from `at`, patch semantics.
2. Migrator + snapshots: snapshot / contained transaction (authorizer) /
   verify (integrity + link scan + required structures + `_meta`
   reconciliation) / log; filename-based catalog; restore procedure.
3. Bootstrap: new-DB seeding — profiles (with `self`), links, `_meta`
   (descriptions, kind registry, rating scales), and the standard collections
   with their seed schemas; `create_collection` for new types; normalized
   generic YAML fixtures for integration tests, loaded through the same
   record/link operations used by MCP tools.
4. MCP layer over stdio with a single local user; all ~14 tools (static
   schemas); generic instructions plus authoritative `describe_schema` output.
5. Test pyramid: unit tests on engine and migrator (including failure paths:
   bad DDL, integrity failures, orphaned links, restore-of-restore);
   protocol-level smoke test.
6. Dogfood from Claude Code/Desktop.

Exit criteria: the "movie night" query works end-to-end locally — profiles
added, links with ratings and dates, cross-collection SELECT through
`query`; a schema promotion (`extra` key → real column) executed by the
agent via `migrate` and undone via `restore_snapshot` successfully.

### Phase 2 — Auth + multi-tenancy

Implemented: code-gated signup, the contained OAuth provider, protected
Streamable HTTP, token → `user_id` → DB routing, refresh-token rotation,
reuse detection and grant-family revocation, explicit revocation, Redis-backed
auth limits, and an end-to-end two-user isolation test.

Remaining:

1. Verify the full connector dance against **claude.ai** (web → syncs to
   Desktop/mobile) and **ChatGPT developer mode** through the real public URL.
2. Tune the existing Redis-backed auth limits from real connector traffic before
   wider access; add edge limits when deployment has a stable proxy boundary.
3. Add CIMD when target-client support makes it the practical replacement for
   DCR.
4. Add only the account recovery/admin operations that dogfooding proves are
   necessary.

Exit criteria: two real users using the same server from Claude mobile with
separate databases.

### Phase 3 — Deploy + polish

1. Local Dockerfile/Compose, persistent data, TLS, and beta deployment are
   implemented.
2. Daily complete backup sets and 15-snapshot engine retention are implemented;
   add encrypted off-site restic storage.
3. Tune seed schemas and instruction wording from dogfooding across more
   collections (wine, cocktails, music).
4. README/docs: setup for both platforms.

### Deferred (explicitly not now)

- Real identity (passkeys/Google) behind the same OAuth front.
- Shared/household databases (two accounts, one DB) and per-collection
  permissions.
- Shareable schema templates (export a proven schema so another user's agent
  can adapt it).
- Full-text search (SQLite FTS5) and vector similarity for "more like this".
- Export tool (dump any collection to readable YAML).
- Wishlist/status tracking (a `wants` link kind when someone actually asks).
- MCP resources/prompts surface (tools-only covers both platforms today).

## 5. Risks and open questions

- **Moving auth target.** The MCP auth spec (DCR → CIMD) and the SDK's
  preferred AS/RS split are both in motion; that's exactly why the AS shape
  is a phase-2 spike, not a decision made in this document. Budget real
  tunnel-and-retry days against both clients' current behavior.
- **Agent discipline.** The design leans on generated instructions and tool
  descriptions to steer agent behavior (promote-don't-hoard,
  link-don't-duplicate, sane schemas for new types). Expect iteration on
  wording after dogfooding; treat instruction text as a first-class artifact.
  Nothing critical may live *only* in instructions — `describe_schema` must
  always suffice.
- **Migration safety vs. freedom.** The agent can author destructive DML.
  Snapshots make this recoverable, not impossible. Acceptable for personal
  data; revisit if databases become precious.
- **SQLite concurrency.** One process, per-user write serialization, and a
  30-second SQLite busy timeout are plenty here; revisit only if this outgrows
  "friends and family".
