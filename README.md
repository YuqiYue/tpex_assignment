# FacilityDesk → SQL sync

A pipeline that keeps a SQL database in sync with the FacilityDesk ticket API:
buildings, ticket categories, tickets, ticket activities and chat messages.
It picks up new tickets, changes to existing tickets (including new activities
and chat messages), and tickets that were deleted in FacilityDesk.

**In short:** one timer-triggered Azure Function runs a *sync cycle* every
5 minutes. Each cycle spends a fixed time budget on a prioritised list of work,
writes everything as idempotent upserts, and stores its progress in the
database, so the next cycle continues where the previous one stopped. The
real constraint is the API rate limit (60 requests/minute), so most of the
design is about spending requests well.

---

## Contents

1. [How to run](#how-to-run)
2. [How it works](#how-it-works)
3. [The request budget](#the-request-budget)
4. [Data model](#data-model)
5. [Key decisions and trade-offs](#key-decisions-and-trade-offs)
6. [Initial backfill](#initial-backfill)
7. [Running on Azure](#running-on-azure)
8. [Surprises in the API and assumptions](#surprises-in-the-api-and-assumptions)
9. [Known limitations and next steps](#known-limitations-and-next-steps)
10. [Project layout and where to change what](#project-layout-and-where-to-change-what)

---

## How to run

Requirements: [uv](https://docs.astral.sh/uv/) and, for the mock API, Node.js.

```bash
uv sync                                          # creates .venv with Python 3.11+ and dependencies

# Terminal 1: the mock API from the assignment
npx @stoplight/prism-cli@5 mock openapi.yaml     # http://127.0.0.1:4010

# Terminal 2
uv run python -m fd_sync cycle                   # one sync cycle (what the Azure timer runs)
uv run python -m fd_sync status                  # row counts in data/facilitydesk.db
uv run python -m fd_sync loop                    # a cycle every 5 minutes, like production

uv run pytest                                    # tests (the Prism test is skipped if the mock is not running)
uv run python scripts/simulate_production.py --hours 24   # budget/freshness simulation, see below
```

The database is a SQLite file at `data/facilitydesk.db`; open it with
`sqlite3 data/facilitydesk.db` or any SQLite browser.

Configuration is read from environment variables (all optional; defaults
point at the mock). Every field of `Settings` in
[`fd_sync/config.py`](fd_sync/config.py) can be overridden as `FD_<NAME>`:

| Variable | Default | Meaning |
|---|---|---|
| `FD_BASE_URL` | `http://127.0.0.1:4010` | API base URL |
| `FD_CLIENT_ID` / `FD_CLIENT_SECRET` | mock values | API credentials |
| `FD_DB_PATH` | `data/facilitydesk.db` | SQLite file |
| `FD_RATE_LIMIT_PER_MINUTE` | `58` | our own request pace (API limit is 60) |
| `FD_CYCLE_SECONDS` | `285` | time budget of one cycle (timer fires every 300 s) |
| `FD_OPEN_TICKET_REFRESH_MINUTES` | `50` | re-fetch activities/chats of open tickets after this |
| `FD_SCAN_PAGES_PER_CYCLE` | `15` | pages of the full ticket list read per cycle |
| `FD_DELETION_CHECKS_PER_CYCLE` | `50` | max deletion confirmations per cycle |
| `FD_REFERENCE_REFRESH_HOURS` | `6` | how often buildings and categories are refreshed |

Note: against the mock, one cycle takes ~30 seconds. That is the rate limiter
pacing requests at 58/minute, not slowness.

---

## How it works

### The constraints that shape the design

* **60 requests/minute** for everything using our credentials: at most
  3,600 requests/hour. This is the bottleneck, not compute.
* **`updated_at` does not change when activities or chat messages are added.**
  There is no cheap "what changed?" signal for the things that change most
  often. Every open ticket's activities and 5 chats must be re-read regularly:
  400 open tickets × 6 requests = 2,400 requests per refresh round. That alone
  is two thirds of the hourly ceiling.
* **`GET /tickets` has no filters**, but it is sorted newest first and ids only
  increase. New tickets are always on page 1.
* **Deleted tickets simply disappear** from the list (and return 404).
* Invocations are **short-lived** (≤10 min) and **share no memory**.

### One cycle

Every 5 minutes, `SyncJob.run_cycle()` ([`fd_sync/sync.py`](fd_sync/sync.py))
runs these steps in priority order, until its 285-second budget is used up:

| # | Step | What | Typical cost |
|---|---|---|---|
| 1 | `sync_reference_data` | Buildings + categories, if older than 6 h | 2 requests, 4×/day |
| 2 | `discover_new_tickets` | Read `GET /tickets` from page 1 until we reach an id we already have | 1 request |
| 3 | `advance_full_scan` | Read the next 15 pages of the full ticket list. At the end of a pass, verify tickets that were not seen (deletion detection) | 15 requests |
| 4 | `refresh_open_tickets` | Re-fetch activities + all 5 chats of open tickets not refreshed in the last 50 minutes (or whose ticket fields changed since) | 6 requests/ticket |
| 5 | `backfill_closed_tickets` | Leftover budget: activities + chats of closed tickets never synced (historical backfill) or changed since their last sync (e.g. just closed) | 6 requests/ticket |

When the budget runs out mid-step, the API client raises `OutOfTime` instead
of sleeping past the deadline, the cycle ends cleanly, and the next cycle
picks up the remaining work. Nothing is lost, because:

* every write is an **upsert keyed on FacilityDesk's own id**, so redoing work is harmless;
* all progress lives in the **database** (`sync_state` table plus bookkeeping
  columns on `tickets`), never in memory;
* a ticket's `children_synced_at` is only set after **all** its activities and
  chats were fetched and stored.

### How each requirement is met

**New tickets.** Step 2 reads page 1 of the ticket list every 5 minutes. New
tickets have never-synced activities/chats, so step 4 handles them first.
A new ticket is fully in the database within ~5 minutes.

**Changes to tickets.** The full scan (step 3) reads the whole list, 50 complete
tickets per page, about once an hour. That is cheaper than `GET /tickets/{id}`
for each of the 400 open tickets (168 vs. 400 requests), covers closed tickets
too (so re-opened tickets are noticed), and doubles as deletion detection.
We keep the original payload in `raw_json`; when it differs from what we had,
`content_changed_at` is bumped, which makes step 4 refresh that ticket's
activities/chats right away (a status change usually comes with a new activity).

**Changes to activities and chat messages.** Because `updated_at` does not tell
us about them, step 4 re-reads every open ticket's activities and 5 chats at
least every ~55 minutes (50-minute threshold + up to one 5-minute timer tick).
Unknown steps such as `ON_HOLD` count as open (see
[`constants.py`](fd_sync/constants.py)): polling too much costs a few requests,
polling too little breaks freshness.

**Deleted tickets.** During a full-scan pass every ticket we see gets
`last_seen_at = now`. When the pass ends, tickets not seen since the pass
started are *suspects*. A suspect is only marked deleted after
`GET /tickets/{id}` returns 404. That second check matters: while we page
through the list (over an hour), a deletion shifts every later ticket one
position up, so a ticket can slip between two pages unseen. Deletions are
**soft** (`deleted_at`), so history is kept and a ticket that comes back (e.g.
access restored) is simply un-deleted on the next upsert. Open tickets are
also caught earlier: their sub-resources return 404 during step 4.

---

## The request budget

Steady state, per hour (58 requests/minute × 285 s of each 300 s ≈ 3,300 requests/hour):

| Work | Requests / hour |
|---|---:|
| Auth token (once per cycle: invocations share no memory) | 12 |
| New tickets (page 1 every cycle) | 12 |
| Full scan (8,400 tickets ÷ 50 = 168 pages per pass, 15 pages per cycle) | 180 |
| Deletion confirmations | ~1–5 |
| Buildings + categories (every 6 h) | <1 |
| Open tickets: 400 × 6 requests every ~55 min | ~2,600 |
| **Left for backfill of closed tickets** | **~450** (≈ 70 tickets/hour) |

The simulation script checks this with the fake API and a fake clock (8,000
closed + 400 open tickets, new tickets, chat messages, status changes and
deletions every cycle; a simulated day runs in under a minute):

```
$ uv run python scripts/simulate_production.py --hours 6
hour requests  open  max staleness open (min)  closed not yet backfilled deleted
   1     3312   400                        55                       7929       0
   2     3312   401                        54                       7898       0
   3     3312   403                        55                       7825       1
   ...
```

So: ~3,300 requests/hour (92% of the hard ceiling), every open ticket's
activities and chats are at most 55 minutes old, and the backfill progresses
at ~70 tickets/hour.

**This is tight, on purpose.** The freshness requirement for activities and chats
forces ~2,400–2,600 requests/hour no matter how clever the rest is, so there
is not much room. If the number of open tickets grows, the options are, in order
of preference: ask FacilityDesk for an `updated_since` filter, a "last activity"
timestamp, webhooks or a higher limit; poll chat types that were always empty
less often; poll quiet tickets (no activity for N days) less often. All of these
trade freshness or complexity for budget, so I did not build them now.

---

## Data model

See [`schema.sql`](schema.sql) (SQLite dialect).

| Table | Key | Notes |
|---|---|---|
| `buildings` | `building_id` | `address` flattened to one line (the API sometimes sends an object) |
| `categories` | `id` | tree via `parent_id` |
| `tickets` | `id` | API fields + sync bookkeeping (below) |
| `ticket_users` | `(ticket_id, user_id, role)` | replaced as a whole on every ticket upsert |
| `activities` | `id` | `ticket_id` from the request path; `data` as JSON |
| `activity_emails` | `id` | emails embedded in activities |
| `chat_messages` | `id` | `ticket_id` and `chat_type` from the request path; files and mentions as JSON |
| `sync_state` | `key` | key/value progress, e.g. where the full scan is |

Choices worth calling out:

* **FacilityDesk ids as primary keys.** Every write is an idempotent
  `INSERT ... ON CONFLICT DO UPDATE`, which is what makes retries and cut-off
  cycles safe.
* **UTC everywhere**, stored as `YYYY-MM-DDTHH:MM:SSZ` text, so values compare
  and sort correctly as strings in any dialect.
* **`raw_json` on every entity.** The examples show the schema cannot be fully
  trusted. Keeping the payload means a field we did not model can be
  backfilled from our own database (`json_extract`) instead of spending API budget.
* **Soft deletes** (`deleted_at`) rather than `DELETE`: reporting and SLA
  history should not silently lose tickets.
* **No foreign keys between FacilityDesk entities.** The source is not
  consistent (tickets reference category 14, which is not in the category
  list) and entities arrive in no particular order. Rejecting a ticket because
  of a dangling category would be worse than storing it.
* **Ticket bookkeeping columns:** `first_seen_at`, `last_seen_at` (deletion
  detection), `content_changed_at` (payload changed), `children_synced_at`
  (last full refresh of activities + chats), `deleted_at`.
* **Chat files and mentions as JSON** rather than tables: files are out of
  scope, and mentions are rarely queried. Easy to normalise later from `raw_json`.

---

## Key decisions and trade-offs

**One singleton timer function instead of queue fan-out.** With a 60/minute
limit, parallelism buys nothing: one process sending one request per second
already uses the whole budget. Several workers would need a distributed rate
limiter (e.g. a token bucket in Table storage) and coordination about who
does what. Timer triggers run as a singleton and never overlap, so there is
exactly one API caller and the rate limiter can live in memory. Simple and
sufficient. A queue becomes worth it only if processing (not the API) becomes
the bottleneck.

**Even request spacing instead of "60, then wait".** The limiter spaces requests
60/58 ≈ 1.03 s apart. Because invocations share no memory, a burst-style
limiter in a new invocation cannot know how many requests the previous
invocation sent in its last minute and could trigger 429s. With even spacing
and a gap between cycles, no 60-second window ever exceeds the limit, without
any shared state. 429s (from someone else using our credentials) are still
handled by honouring `Retry-After`.

**Work by priority within a time budget, not a fixed plan.** Each cycle just does
the most important work first until time runs out. If the API is slow, or a
burst of new tickets arrives, the backfill slows down first and the open
tickets stay fresh. No schedule to tune per step.

**Full list scan for field changes and deletions.** Discussed above: cheaper than
per-ticket GETs, and one mechanism covers three needs (field changes,
re-opened tickets, deletions).

**Re-read all pages of a ticket's activities/chats on each refresh** (with
`limit=100`, almost always a single page). Simpler than tracking per-chat
cursors, and it also picks up edited messages. Only tickets with more than 100
messages in one chat would benefit from a cursor.

**Closed tickets are not polled for new messages.** After a ticket is closed it
gets one final refresh (its payload changed, so `content_changed_at` is newer
than `children_synced_at`). After that we only look again if the ticket itself
changes (e.g. re-opened). A message posted on a closed ticket without any
status change would be missed. Polling 8,000 closed tickets is not affordable;
I think this is the right trade-off, but it is an assumption to validate with
the business.

**SQLite locally, one module for all SQL.** All SQL lives in
[`fd_sync/db.py`](fd_sync/db.py), so moving to Azure SQL means changing that file
only (mostly `ON CONFLICT` → `MERGE`, `?` parameters stay with `pyodbc`).

**Sync state in the database, not in Table storage.** The progress markers live
in the same database as the data, so they are committed together with it. One
less service to depend on and no way for state and data to disagree.

**Lenient parsing.** Payloads that deviate from the schema are normalised where
the intent is obvious (`medium` → `MEDIUM`, `"48213"` → `48213`) and never crash
the sync. One broken ticket (persistent 5xx) is logged and skipped so it cannot
block the others.

**Stateful fake API for tests.** The Prism mock is stateless, so it cannot show
new, changed or deleted tickets. The tests use an in-memory FacilityDesk
([`tests/fakes.py`](tests/fakes.py)) served through `httpx.MockTransport` (so the
real HTTP client code runs), plus a fake clock, so an hour of syncing runs in
milliseconds. There is also one smoke test against Prism to check that we
handle the real, production-captured examples.

---

## Initial backfill

Nothing special is needed: the backfill is just the normal cycle running
against an empty database.

1. **Ticket rows:** the first full-scan pass inserts all ~8,400 tickets in about
   an hour (168 pages).
2. **Open tickets' activities and chats** come first (step 4): ~2,400 requests,
   done within the first hour.
3. **Closed tickets' activities and chats** use whatever budget is left (step 5),
   newest first, because recent history is the most useful for reporting.

In steady state there are ~450 spare requests/hour, i.e. ~70 closed tickets/hour,
so 8,000 historical tickets take about **5 days**. That is acceptable for
history, it is resumable at any point, and it never hurts the freshness of
open tickets.

If it must go faster, it is a configuration change, not a code change: during the
initial load set `FD_OPEN_TICKET_REFRESH_MINUTES=240`. Open tickets are then
refreshed every 4 hours instead of every hour, and most of the budget goes to
the backfill: ~500 tickets/hour between refresh rounds, ~400/hour on average,
so roughly 20 hours (one weekend before go-live). Check it with
`scripts/simulate_production.py --open-refresh-minutes 240`.
The theoretical minimum is 8,000 × 6 = 48,000 requests ≈ 14 hours at the full limit.

---

## Running on Azure

The repository is also an Azure Functions (Python v2 model) project:

* [`function_app.py`](function_app.py): a timer trigger (`0 */5 * * * *`) that calls
  the same `run_cycle()` as the CLI.
* [`host.json`](host.json): `functionTimeout` 10 minutes (our cycles stop after 285 s).
* [`requirements.txt`](requirements.txt): dependencies for deployment.
* [`local.settings.example.json`](local.settings.example.json): copy to
  `local.settings.json` to run with `func start` (Azure Functions Core Tools + Azurite).

For production I would change:

* **Database:** Azure SQL instead of SQLite: a `pyodbc` version of `db.py`,
  `schema.sql` translated to T-SQL (`NVARCHAR`, `DATETIME2`, `MERGE` for upserts).
* **Secrets:** `FD_CLIENT_SECRET` and the connection string as Key Vault
  references in the app settings.
* **Storage account:** only used by the Functions runtime itself (timer lease
  and schedule monitor). The pipeline does not need queues or tables.
* **Observability (out of scope, but cheap):** each cycle logs a one-line summary
  (`requests`, `tickets_new`, `tickets_deleted`, `stopped_at_deadline`, ...) that
  can feed an Application Insights alert, e.g. "oldest open-ticket refresh > 70 min".

---

## Surprises in the API and assumptions

The spec says the examples come from production and the schemas were written
by the FacilityDesk team. They disagree in several places; in each case I
trusted the examples (what production actually sends) over the schema.

| Observation | How it is handled |
|---|---|
| `priority: medium` (lower case), schema enum is upper case | upper-cased |
| `current_step: ON_HOLD`, not in the enum | stored as-is; anything not explicitly closed counts as **open** |
| Activity `ticket_id: "48213"` (string) | parsed to int; the ticket id from the **request path** is used regardless |
| Activity `data: []` instead of an object | stored as `{}` |
| `site_visit_at: "2026-09-28 13:30:00"`, no offset | **assumed Europe/Amsterdam** local time, converted to UTC (all example buildings are in NL/DE) |
| Timestamps mixing `+02:00` and `+00:00` | everything converted to UTC |
| `tenant_company_name` and `deadline_at` omitted entirely (sparse ticket) | all optional fields read with `.get()` |
| Building `address` is an object for the Berlin building, a string elsewhere | flattened to `"Stralauer Allee 2, 10245 Berlin, DE"`; original in `raw_json` |
| Chat author has `thumbnail` instead of `profile_photo` | either one is stored as `author_photo_url` |
| Ticket references category 14 "Air quality", which is not in the category list | no foreign key; ticket keeps the denormalised `category_name` |
| Chat messages contain neither ticket id nor chat type | both taken from the request path |
| A `404` may mean deleted **or** no longer accessible | both are treated as deleted (soft, reversible if the ticket reappears) |

Assumptions:

* Activities are an audit trail and are not deleted. Deleted chat messages are
  not detected (we only add/update child rows). Edits are picked up because we
  re-read all pages.
* Removed buildings/categories are not detected (they are only upserted). They
  change rarely, and a full refresh is cheap if needed.
* The rate limit counts `POST /auth/token` too, so it goes through the limiter.
* The documented token lifetime (30 min) is trusted over the returned
  `expires_at`, measured on our own monotonic clock (immune to clock skew; the
  mock even returns an `expires_at` in the past). A 401 triggers one refresh and retry.
* `links.next` may not carry our `limit` parameter along, so the client merges
  its own parameters into every page URL.

Mock-specific behaviour (not expected in production):

* The mock returns the same activities and chat messages for every ticket and
  chat type. Because ids are globally unique, each ends up as one row assigned
  to the last ticket/chat type that returned it, and the client logs a
  warning when an activity's `ticket_id` disagrees with the path.
* With `Prefer: example=first_of_many`, page 2 links to page 2 again. Both
  pagination loops stop when a `next` link points at a page already fetched.

---

## Known limitations and next steps

* **Budget headroom is small** (see [The request budget](#the-request-budget)).
  Growth beyond ~450 open tickets needs one of the trade-offs listed there.
* **No history of ticket changes.** We store the latest state only. If SLA
  reporting needs "when did this ticket enter step X?", the activities
  (status-change entries) already contain it; otherwise a `ticket_versions`
  table written when `content_changed_at` changes would be a small addition.
* **Messages on closed tickets** without any status change are missed (see trade-offs).
* **Single region / single caller assumption:** if another system starts
  using the same API credentials heavily, our 429 handling keeps us correct
  but freshness degrades. The rate limit is then a shared resource that needs
  an owner.

---

## Project layout and where to change what

```
fd_sync/
  config.py      Settings (all tunables) and the Clock
  constants.py   chat types, which steps count as closed, default time zone
  api.py         HTTP client: auth, rate limiting, retries, pagination
  normalize.py   raw payload -> table rows (all the leniency lives here)
  db.py          all SQL: upserts, queries, sync state
  sync.py        the cycle: what to fetch, in which order
  __main__.py    CLI: cycle / loop / status
schema.sql       table definitions
function_app.py  Azure Functions timer trigger
host.json, requirements.txt, local.settings.example.json
tests/           fake API + fake clock, sync behaviour, client, normalisation, Prism smoke test
scripts/simulate_production.py   budget and freshness simulation
```

| If you want to... | Change |
|---|---|
| store a new ticket field | column in `schema.sql` + one line in `normalize.ticket()` (old rows can be backfilled from `raw_json`) |
| sync a new endpoint/entity | a method in `api.py`, a function in `normalize.py`, an upsert in `db.py`, a step in `SyncJob.run_cycle()` |
| change freshness, pace or scan speed | `Settings` in `config.py` (or `FD_*` environment variables) |
| change what counts as an open ticket | `CLOSED_STEPS` in `constants.py` |
| move to another database | `db.py` and `schema.sql` only |
