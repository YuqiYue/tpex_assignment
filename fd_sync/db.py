"""All SQL lives in this module.

The rest of the code calls methods like `upsert_ticket()` and never writes SQL
itself, so moving from SQLite to Azure SQL means changing this file only
(mainly `INSERT ... ON CONFLICT` becomes `MERGE`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .constants import CLOSED_STEPS

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# Used in queries as: current_step NOT IN (?, ?, ?)
_CLOSED_PLACEHOLDERS = ", ".join("?" for _ in CLOSED_STEPS)


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_PATH.read_text())

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Sync state (key/value)
    # ------------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str | None, now: str) -> None:
        self.conn.execute(
            """INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now),
        )

    # ------------------------------------------------------------------
    # Reference data
    # ------------------------------------------------------------------

    def upsert_building(self, row: dict, now: str) -> None:
        self._upsert("buildings", {**row, "synced_at": now}, key="building_id")

    def upsert_category(self, row: dict, now: str) -> None:
        self._upsert("categories", {**row, "synced_at": now}, key="id")

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    def upsert_ticket(self, row: dict, users: list[dict], now: str) -> bool:
        """Insert or update a ticket the API just returned. Returns True if it is new.

        Besides copying the fields this maintains our bookkeeping columns:
        last_seen_at = now, content_changed_at = now only if the payload
        changed, and deleted_at = NULL (a ticket the API returns is not deleted).
        """
        is_new = self.conn.execute("SELECT 1 FROM tickets WHERE id = ?", (row["id"],)).fetchone() is None
        values = {**row, "first_seen_at": now, "last_seen_at": now, "content_changed_at": now}
        columns = ", ".join(values)
        placeholders = ", ".join(f":{c}" for c in values)
        api_fields = ", ".join(f"{c} = excluded.{c}" for c in row if c not in ("id", "raw_json"))
        # In an UPDATE, every right-hand side sees the *old* row, so
        # `tickets.raw_json` below is the previously stored payload.
        self.conn.execute(
            f"""INSERT INTO tickets ({columns}) VALUES ({placeholders})
                ON CONFLICT (id) DO UPDATE SET
                    {api_fields},
                    content_changed_at = CASE WHEN tickets.raw_json = excluded.raw_json
                                              THEN tickets.content_changed_at
                                              ELSE excluded.content_changed_at END,
                    raw_json = excluded.raw_json,
                    last_seen_at = excluded.last_seen_at,
                    deleted_at = NULL""",
            values,
        )
        self.conn.execute("DELETE FROM ticket_users WHERE ticket_id = ?", (row["id"],))
        for user in users:
            self.conn.execute(
                """INSERT OR REPLACE INTO ticket_users (ticket_id, user_id, role, name, email)
                   VALUES (:ticket_id, :user_id, :role, :name, :email)""",
                user,
            )
        return is_new

    def max_ticket_id(self) -> int | None:
        return self.conn.execute("SELECT MAX(id) FROM tickets").fetchone()[0]

    def mark_deleted(self, ticket_id: int, now: str) -> None:
        self.conn.execute(
            "UPDATE tickets SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now, ticket_id)
        )

    def tickets_not_seen_since(self, since: str, limit: int) -> list[int]:
        """Non-deleted tickets the API has not returned since `since`: deletion suspects."""
        rows = self.conn.execute(
            """SELECT id FROM tickets
               WHERE deleted_at IS NULL AND last_seen_at < ?
               ORDER BY id DESC LIMIT ?""",
            (since, limit),
        )
        return [r["id"] for r in rows]

    def open_tickets_due_for_refresh(self, synced_before: str, limit: int) -> list[int]:
        """Open tickets whose activities/chats were never synced, were synced
        before `synced_before`, or whose ticket fields changed since (a status
        change usually comes with a new activity, so refresh right away).
        Never-synced and most stale first."""
        rows = self.conn.execute(
            f"""SELECT id FROM tickets
                WHERE deleted_at IS NULL
                  AND current_step NOT IN ({_CLOSED_PLACEHOLDERS})
                  AND (children_synced_at IS NULL
                       OR children_synced_at < ?
                       OR children_synced_at < content_changed_at)
                ORDER BY children_synced_at IS NOT NULL, children_synced_at, id DESC
                LIMIT ?""",
            (*CLOSED_STEPS, synced_before, limit),
        )
        return [r["id"] for r in rows]

    def closed_tickets_needing_sync(self, limit: int) -> list[int]:
        """Closed tickets whose activities/chats were never synced (the
        historical backfill) or that changed since their last sync (e.g. just
        closed: one last refresh picks up the final messages). Newest first."""
        rows = self.conn.execute(
            f"""SELECT id FROM tickets
                WHERE deleted_at IS NULL
                  AND current_step IN ({_CLOSED_PLACEHOLDERS})
                  AND (children_synced_at IS NULL OR children_synced_at < content_changed_at)
                ORDER BY id DESC
                LIMIT ?""",
            (*CLOSED_STEPS, limit),
        )
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------
    # Activities and chat messages
    # ------------------------------------------------------------------

    def upsert_activity(self, row: dict, emails: list[dict], now: str) -> None:
        self._upsert("activities", {**row, "synced_at": now}, key="id")
        for email in emails:
            self._upsert("activity_emails", email, key="id")

    def upsert_chat_message(self, row: dict, now: str) -> None:
        self._upsert("chat_messages", {**row, "synced_at": now}, key="id")

    def mark_children_synced(self, ticket_id: int, now: str) -> None:
        self.conn.execute("UPDATE tickets SET children_synced_at = ? WHERE id = ?", (now, ticket_id))

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        closed = CLOSED_STEPS

        def one(sql: str, *args: object) -> int:
            return self.conn.execute(sql, args).fetchone()[0]

        return {
            "buildings": one("SELECT COUNT(*) FROM buildings"),
            "categories": one("SELECT COUNT(*) FROM categories"),
            "tickets": one("SELECT COUNT(*) FROM tickets WHERE deleted_at IS NULL"),
            "open_tickets": one(
                f"SELECT COUNT(*) FROM tickets WHERE deleted_at IS NULL "
                f"AND current_step NOT IN ({_CLOSED_PLACEHOLDERS})", *closed),
            "deleted_tickets": one("SELECT COUNT(*) FROM tickets WHERE deleted_at IS NOT NULL"),
            "tickets_without_children_synced": one(
                "SELECT COUNT(*) FROM tickets WHERE deleted_at IS NULL AND children_synced_at IS NULL"),
            "activities": one("SELECT COUNT(*) FROM activities"),
            "chat_messages": one("SELECT COUNT(*) FROM chat_messages"),
        }

    # ------------------------------------------------------------------

    def _upsert(self, table: str, row: dict, key: str) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        updates = ", ".join(f"{c} = excluded.{c}" for c in row if c != key)
        self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT ({key}) DO UPDATE SET {updates}",
            row,
        )
