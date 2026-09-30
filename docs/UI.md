# UI: Browse Your Database

## 1. Why

Your assistant is the librarian: it adds, organizes, and reshapes your data.
But sometimes you want to walk the shelves yourself — scan every wine at a
glance, find that one bottle from last summer, or follow a record to the
people and things linked to it. Chat is bad at that. A grid you can search and
click through is good at it.

Seeing your data directly also builds trust. You can check what the assistant
actually stored, spot duplicates or misfiled facts early, and ask it to fix
them.

The first version is **read-only**. All writing stays with the assistant,
which already knows the conventions: records versus links, ids, and snapshots
before schema changes. Adding a second way to write would mean a second set of
rules to keep consistent, and nothing about browsing needs it.

## 2. What

The design ethos in [PLAN.md](PLAN.md) applies here too: simple, intuitive,
no over-engineering. The UI adds no new concepts. It shows the same
collections, records, and links the assistant works with, looking and feeling
like Airtable or Baserow.

### Screens

**1. Sign in.** Email and password, same account as the connector.

**2. Collection grid** — the home of the app.

- **Sidebar**: the fkey mark, then every collection with an icon and record
  count. `profiles` is pinned first as "Your people"; the rest follow
  alphabetically. Seeded collections get fitting icons (film, TV, book, music,
  wine glass, cocktail, star); collections the assistant creates later get a
  generic table icon. The current collection is highlighted.
- **Toolbar**: collection name and description, record count, and one search
  box. That box is the only control. It matches every column of the
  collection — text, numbers, `extra`, and the id — ignoring case in every
  script, and filters the grid instantly as you type.
- **Grid** — [AG Grid Community](https://www.ag-grid.com/) (MIT), rather than
  a hand-built table, so resizing, sizing to content, pinning, sorting, and
  keyboard navigation behave like the products we're emulating:
  - A row-number column, then the primary column (`title` or `name`), both
    pinned while scrolling sideways on wider screens. The primary cell links
    to the record.
  - The collection's fields in schema order. Headers carry a small type icon
    (text, number) and the field name; hovering shows the field's
    description from `_meta`.
  - Columns start sized to their content, within a limit (320px; 360px for
    the name, 520px for `extra`, 200px for the name on phones). Drag a header
    edge to resize; dragged widths are remembered per collection in your
    browser, while the other columns keep fitting their content.
  - **Your rating** and **Last** columns, read from your own (`self`) link
    to each row. Ratings live on links, not records, so without these a
    wine grid would show no ratings at all. They are ordinary columns, not a
    control; other people's ratings appear on the record page. Collections
    with no `self` links simply show them empty, and `profiles` omits them.
  - An `extra` column last, rendering keys as compact pills
    (`grape: Nebbiolo`); pills holding a web address are links.
  - Clicking a header sorts by it (ascending, descending, off); empty cells
    stay last either way. The default is most recently updated first.
  - Clicking anywhere on a row, or pressing Enter on it, opens the record.
  - The page carries up to 5,000 rows, most recently updated first, so
    search and sort need no round trip; the footer shows "12 of 109 wines"
    while searching and notes when a collection exceeds the limit.
- **Cells**: numbers right-aligned with tabular figures, dates as stored
  (`2026-08-18`), empty values left blank. Only `http(s)` values become
  links, opening in a new tab.
- **Tooltips**: a cell that doesn't fit shows its full value after a short
  pause — wrapped to a readable width, line breaks kept, and `extra` one key
  per line. You can move into the tooltip to select and copy. Cells that fit
  show nothing. (We measure overflow ourselves: AG Grid's built-in
  "when truncated" mode ignores cells with custom renderers.)
- **Empty states**: "No wines yet. Ask your assistant to add one." and "No
  wines match “бароло”."

**3. Record page** — a full page, like Airtable's expanded record.

- Breadcrumb (`Wines / Barolo Bussia`), the display name as title, created and
  updated times in small muted text.
- **Fields**: a vertical list, label beside the value; empty fields show a
  muted dash. Each `extra` key follows as its own field, marked with `{}`.
  Unlike the grid, nothing is truncated: long text wraps and keeps its line
  breaks, lists read as comma-separated text, and simple objects as
  `key: value` pairs; only deeply nested data stays JSON.
- **History**: bare profile links. On an item it lists each person who has
  history with it: person, rating on the collection's scale (`8.5 / 10`), first
  and last dates, note, and `props` as pills. On a profile it lists everything
  that person has logged, grouped by collection with a count per group, newest
  first.
- **Relationships**: named links grouped by kind, showing direction —
  outgoing as `made → Barolo Bussia`, incoming as `← directed: Denis
  Villeneuve`.
- Every linked record is a pill; clicking it opens that record's page. This is
  how you move around: wine → Anna → a movie she loved → its director.
- History is paged 100 at a time, most recent activity first; relationships
  show up to 500.

### URLs

Every view has a stable URL, so back, bookmarks, and sharing a link with
yourself all work:

| URL | View |
|---|---|
| `/` | Redirects to `/app` |
| `/app` | Redirects to your largest collection |
| `/app/login`, `/app/logout` | Sign in, sign out |
| `/app/c/{collection}?q=` | Grid (the search text is kept in the URL) |
| `/app/c/{collection}/{id}?page=` | Record page |
| `/app/static/…` | CSS, JavaScript, icons |

Collections live under `/app/c/` so a collection named `login` or `static`
can never collide with an app route.

### Visual design

Airtable and Baserow share a recognizable language; we follow it:

- **Content first, quiet chrome.** White canvas, light gray sidebar and grid
  header, 1px light gray grid lines, no shadows except a subtle one on the
  record card.
- **Readable before dense.** 15px text in the grid (42px rows) and chrome,
  16px on the record page, 22px collection titles and 32px record titles;
  system font stack (Inter when available) with tabular figures for numbers.
- **The grid is themed, not overridden.** AG Grid's Quartz theme is
  restyled only through its documented `--ag-*` variables: fkey colors,
  vertical cell lines, subtle header separators that turn green on hover as
  resize handles.
- **One accent color**, the existing fkey green from the signup pages, used
  for the current collection, links, pills, and focus rings. Nothing else is
  colored.
- **Consistent with account pages.** Sign-in reuses the account-page shell in
  `web.py`, so signup, connector login, and app login look like one product.
- **Icons** from a small SVG sprite of outline icons (adapted from Tabler,
  MIT) inlined into each page — no icon fonts or CDNs.
- **Phones** (under 760px): the sidebar folds behind a menu button; in the
  grid nothing is pinned and row numbers are hidden, so a sideways swipe
  moves whole rows and each column gets the full screen width (pinning ate
  most of a 375px screen); the record page becomes one column.
- **Keyboard**: `/` focuses search and `Esc` clears it; arrow keys move
  through the grid and Enter opens the row; pills are real links reachable
  with Tab.
- Colors are CSS custom properties from the start, so dark mode later is a
  small change.

### Sign-in and sessions

Web sessions reuse the MCP token mechanism rather than adding a second one.
A signed-in browser holds a token stored exactly like OAuth tokens: a row in
the existing `oauth_tokens` table, hashed with SHA-256, bound to the user,
with an expiry. That brings the existing behavior for free:

- validation through the same lookup, which rejects expired tokens and tokens
  whose account no longer exists;
- cleanup through the same expired-token sweep, which also runs at each
  sign-in.

Sign-out deletes the token's row.

What differs is only how the token is minted. The OAuth redirect flow
(authorize → login → callback → code exchange) exists to let a *third-party*
client prove who it is; for our own same-origin pages it would be pure
ceremony. It would also bring a real hazard: browsers load pages in
parallel, and parallel refreshes of a rotating refresh token look exactly
like token theft to our reuse detection, which would revoke the session. So
the sign-in form checks the password with the same `AccountStore.authenticate`
used by connector login and mints one token directly:

- `kind = 'session'`, a reserved first-party client id that the OAuth
  endpoints refuse, 30-day expiry like refresh tokens.
- Session tokens are never accepted as MCP bearer tokens (that lookup only
  accepts `kind = 'access'`), and MCP tokens are never accepted as sessions.
- Cookie `fkey_session`: `HttpOnly`, `SameSite=Lax`, `Path=/app`, and
  `Secure` whenever the public URL is HTTPS.
- Sign-in is rate-limited like the other auth forms; it redirects only to
  `/app` paths.
- The session decides which database is opened. URLs never contain user ids.

### Security

- Record values are written by agents, often from web content, so they are
  untrusted: templates escape everything automatically, and only `http(s)`
  URLs become links (`rel="noopener noreferrer"`).
- Content Security Policy: `default-src 'none'`; scripts, styles, images,
  and fetches from `'self'` only, plus `data:` fonts and images because the
  grid theme embeds its icon font that way; `form-action 'self'`,
  `frame-ancestors 'none'`, `base-uri 'none'`. No inline scripts or styles:
  we use AG Grid's build without runtime-injected styles and its static
  theme files.
- Grid rows reach the browser as JSON inside a
  `<script type="application/json">` element, escaped so markup in values
  stays inert, and every cell is built with DOM text nodes — never HTML
  strings.
- Pages send `Cache-Control: no-store`; our static files revalidate, and the
  vendored grid files, versioned in their path, are cached permanently.
- Everything is GET except sign-in and sign-out; both POSTs check the
  `Origin` header, and `SameSite=Lax` keeps the cookie off cross-site POSTs.
- `Referrer-Policy: same-origin`, not `no-referrer`: the latter makes browsers
  send `Origin: null` on our own form POSTs, which the check must reject.
- All data reads go through the existing read-only query path (`query_only`,
  30-second limit).

## 3. How

### Code layout

| Piece | Where | Notes |
|---|---|---|
| Browse routes and data shaping | `src/fkey/ui.py` | Grid, record page, redirects |
| Templates | `src/fkey/templates/` | `base`, `grid`, `record`, `not_found`, shared `macros`, and the `icons` sprite (Jinja2, autoescape on) |
| Styles and script | `src/fkey/static/` | `ui.css`, `ui.js` (grid setup, search, remembered widths, `/` shortcut, phone menu) |
| Grid library | `src/fkey/static/vendor/ag-grid-36.2.0/` | AG Grid Community's no-style browser build, its base and Quartz theme CSS, and its license, served from our own origin |
| Sessions | `src/fkey/oauth.py` | Mint, load, and revoke session tokens next to the OAuth token code |
| Sign-in page | `src/fkey/web.py` | Beside signup and connector login, same shell |
| Wiring | `src/fkey/server.py`, `rate_limit.py` | Routes in the HTTP app; `POST /app/login` limit |

One Python dependency: `uv add jinja2`. AG Grid is vendored rather than loaded
from a CDN, so pages make no third-party requests; upgrading means replacing
the versioned folder. There is no JavaScript build step. The UI exists only in
HTTP mode; stdio stays a local, UI-less mode.

### Data access

- **Sidebar**: collection names and counts from the live schema.
- **Grid**: one SQL statement built from the live schema, with every
  identifier passed through `quote_identifier`, run through `TasteDB.query`
  (read-only, time-limited): all columns plus a `LEFT JOIN` to `self`'s bare
  link for Your rating and Last, newest first, capped at 5,000 rows. The
  server sends columns (key, label, kind, icon, description) and rows (id,
  record URL, values) as JSON; the browser does search and sort.
- **Case folding** lives in `TasteDB.connect()`, so `find_records` and
  agents' own `query` SQL can call `casefold()`; grid search folds case with
  JavaScript's Unicode-aware lowercasing.
- **Record page**: the record row plus a paged links query in both
  directions. Linked records' display names are resolved in one query per
  collection: `title`, else `name`, else the id — the same order the id
  generator uses.
- **Rating scales** come from `_meta`, per collection with the global default
  as fallback, for display like `8.5 / 10`.

### Tests

- Signed-out requests redirect to sign-in; a bad password, rate limit, and
  sign-out all behave; a session token is rejected by `/mcp` and an access
  token by `/app`.
- Two users: each sees only their own records, even with a guessed record URL.
- A record value containing `<script>` renders as text; a `javascript:` URL
  value is not a link.
- Grid: the JSON handed to the browser — column kinds and order, Your rating
  and Last, link pills, the row cap, markup kept inert; the vendored files
  are served with long caching and nothing else under `/app/static/` is.
  Search, sort, resizing, and remembered widths are browser behavior,
  checked in a real browser.
- Record page: history on an item and on a profile, relationships in both
  directions, and every pill leads to the right record.

## 4. Implementation plan

Each step is small enough to ship to beta on its own.

1. **Sessions and shell.** Session tokens, sign-in and sign-out, the base
   template with sidebar, static assets, security headers, `/` and `/app`
   redirects.
2. **Grid.** AG Grid with content-sized, resizable columns, search, sort,
   cell rendering, Your rating and Last, empty states.
3. **Record page.** Fields, More, History, Relationships, link pills.
4. **Polish.** Phone layout, keyboard shortcuts, hover descriptions.

Exit criteria: on `beta.fkey.app`, sign in, open Wines, type `бароло`, open
the bottle, click Anna, and land on a movie she loved — each step one click,
each page fast.

### Deferred (explicitly not now)

- Editing of any kind.
- Filter builder, grouping, hiding or reordering columns, saved views.
- Gallery, kanban, and calendar views; images and attachments.
- Search across all collections at once.
- Relationship pills inside grid cells; other people's ratings as grid
  columns.
- CSV export.
- Dark mode.
