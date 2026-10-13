-- Schema for the FacilityDesk mirror (SQLite dialect; see README for Azure SQL notes).
--
-- Conventions
--   * Primary keys are FacilityDesk's own ids, so every write is an idempotent upsert.
--   * Timestamps are UTC text 'YYYY-MM-DDTHH:MM:SSZ' (sortable and comparable as text).
--   * raw_json keeps the original payload: if we later need a field we did not
--     model, we can backfill it from our own database instead of the API.
--   * No foreign keys between FacilityDesk entities: the source is not
--     consistent (e.g. tickets reference category 14, which the category list
--     does not contain) and rows arrive in no particular order.
--   * Columns prefixed with sync bookkeeping (first_seen_at, last_seen_at, ...)
--     are ours, not FacilityDesk's.

CREATE TABLE IF NOT EXISTS buildings (
    building_id   INTEGER PRIMARY KEY,
    building_name TEXT,
    building_code TEXT,
    address       TEXT,       -- flattened to one line when the API sends an object
    currency      TEXT,
    company_id    INTEGER,
    raw_json      TEXT NOT NULL,
    synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER,       -- tree: NULL for root categories
    company_id INTEGER,
    name       TEXT,
    synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id                  INTEGER PRIMARY KEY,
    title               TEXT,
    description         TEXT,
    category_id         INTEGER,
    category_name       TEXT,  -- denormalised by the API; kept because category ids can dangle
    priority            TEXT,  -- normalised to upper case
    current_step        TEXT,  -- not constrained to the documented enum (ON_HOLD exists)
    building_id         INTEGER,
    building_name       TEXT,
    exact_location      TEXT,
    supplier_name       TEXT,
    tenant_company_name TEXT,
    origin              TEXT,
    deadline_at         TEXT,
    site_visit_at       TEXT,
    created_at          TEXT,
    updated_at          TEXT,
    raw_json            TEXT NOT NULL,

    -- Sync bookkeeping
    first_seen_at       TEXT NOT NULL,  -- when we first stored this ticket
    last_seen_at        TEXT NOT NULL,  -- last time the API returned it (list or detail)
    content_changed_at  TEXT NOT NULL,  -- last time its payload differed from what we had
    children_synced_at  TEXT,           -- last full refresh of activities + chats (NULL = never)
    deleted_at          TEXT            -- soft delete: set when the API confirms a 404
);

CREATE INDEX IF NOT EXISTS ix_tickets_step ON tickets (current_step, children_synced_at);
CREATE INDEX IF NOT EXISTS ix_tickets_last_seen ON tickets (last_seen_at);

-- Users on a ticket. Replaced as a whole every time the ticket is stored.
CREATE TABLE IF NOT EXISTS ticket_users (
    ticket_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    role      TEXT NOT NULL,
    name      TEXT,
    email     TEXT,
    PRIMARY KEY (ticket_id, user_id, role)
);

CREATE TABLE IF NOT EXISTS activities (
    id                   INTEGER PRIMARY KEY,  -- globally unique per the API docs
    ticket_id            INTEGER NOT NULL,     -- taken from the request path
    message              TEXT,
    data_json            TEXT,                 -- free-form, depends on the activity type
    creator_id           INTEGER,              -- NULL for system-generated activities
    creator_name         TEXT,
    creator_email        TEXT,
    creator_company_id   INTEGER,
    creator_company_name TEXT,
    created_at           TEXT,
    raw_json             TEXT NOT NULL,
    synced_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_activities_ticket ON activities (ticket_id, created_at);

CREATE TABLE IF NOT EXISTS activity_emails (
    id                 INTEGER PRIMARY KEY,
    activity_id        INTEGER NOT NULL,
    sender_email       TEXT,
    recipient_email    TEXT,
    carbon_copies_json TEXT,
    subject            TEXT,
    email_content      TEXT
);

CREATE INDEX IF NOT EXISTS ix_activity_emails_activity ON activity_emails (activity_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id               INTEGER PRIMARY KEY,  -- globally unique per the API docs
    ticket_id        INTEGER NOT NULL,     -- taken from the request path
    chat_type        TEXT NOT NULL,        -- taken from the request path
    ticket_chat_id   INTEGER,
    message          TEXT,
    author_name      TEXT,
    author_photo_url TEXT,                 -- `profile_photo` or `thumbnail`
    sent_at          TEXT,
    files_json       TEXT,                 -- attachments are out of scope: metadata only
    mentions_json    TEXT,
    raw_json         TEXT NOT NULL,
    synced_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chat_messages_ticket ON chat_messages (ticket_id, chat_type, sent_at);

-- Key/value store for state that must survive between invocations
-- (e.g. where the full ticket scan stopped).
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
