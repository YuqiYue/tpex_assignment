"""The sync logic: what to fetch, in which order, within the API budget.

One invocation = one *cycle* (`SyncJob.run_cycle`). A cycle runs these steps
in order until its time budget is used up:

  1. sync_reference_data      buildings + categories, every few hours
  2. discover_new_tickets     first page(s) of GET /tickets, every cycle
  3. advance_full_scan        walk a slice of the full ticket list; at the end
                              of a pass, verify tickets that were not seen
                              (deletion detection)
  4. refresh_open_tickets     re-fetch activities + chats of open tickets
                              that were not refreshed in the last 45 minutes
  5. backfill_closed_tickets  leftover budget: activities + chats of closed
                              tickets never synced (the historical backfill)
                              or changed since their last sync

Every write is an idempotent upsert and all progress is stored in the
database, so a cycle can be cut off at any point and the next one continues.
See the README for the reasoning and the request budget.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

import httpx

from . import normalize
from .api import ApiError, FacilityDeskClient, NotFound, OutOfTime
from .config import Clock, Settings
from .constants import CHAT_TYPES
from .db import Database

log = logging.getLogger(__name__)

# Keys in the sync_state table
REFERENCE_SYNCED_AT = "reference.synced_at"
SCAN_PHASE = "scan.phase"  # NULL (no pass running) | "listing" | "verifying"
SCAN_STARTED_AT = "scan.started_at"
SCAN_NEXT_URL = "scan.next_url"
SCAN_COMPLETED_AT = "scan.completed_at"

# ~40 new tickets per day: one page (50 tickets) per 5-minute cycle is plenty.
# More pages only matter after an outage; the full scan covers anything older.
MAX_DISCOVERY_PAGES = 5

# How many ticket ids to read from the database at a time in steps 4 and 5.
BATCH_SIZE = 20


class SyncJob:
    def __init__(self, client: FacilityDeskClient, db: Database, settings: Settings, clock: Clock):
        self.client = client
        self.db = db
        self.settings = settings
        self.clock = clock
        self.stats: Counter[str] = Counter()

    def run_cycle(self) -> dict:
        self.stats = Counter()
        requests_before = self.client.requests_made
        self.client.deadline = self.clock.monotonic() + self.settings.cycle_seconds
        try:
            for step in (
                self.sync_reference_data,
                self.discover_new_tickets,
                self.advance_full_scan,
                self.refresh_open_tickets,
                self.backfill_closed_tickets,
            ):
                step()
                self.db.commit()
        except OutOfTime:
            # Expected under load: the next cycle continues where we stopped.
            self.stats["stopped_at_deadline"] = 1
        finally:
            self.db.commit()
        self.stats["requests"] = self.client.requests_made - requests_before
        log.info("Cycle finished: %s", dict(self.stats))
        return dict(self.stats)

    # ------------------------------------------------------------------
    # 1. Buildings and categories
    # ------------------------------------------------------------------

    def sync_reference_data(self) -> None:
        last_synced = self.db.get_state(REFERENCE_SYNCED_AT)
        refresh_after = self._now_minus(hours=self.settings.reference_refresh_hours)
        if last_synced and last_synced > refresh_after:
            return
        now = self._now()
        for raw in self.client.list_buildings():
            self.db.upsert_building(normalize.building(raw), now)
        for raw in self.client.list_categories():
            self.db.upsert_category(normalize.category(raw), now)
        self.db.set_state(REFERENCE_SYNCED_AT, now, now)
        self.stats["reference_data_synced"] = 1

    # ------------------------------------------------------------------
    # 2. New tickets
    # ------------------------------------------------------------------

    def discover_new_tickets(self) -> None:
        """GET /tickets is newest first and ids only go up, so new tickets are
        on the first page(s): read until we reach an id we already have."""
        known_max_id = self.db.max_ticket_id()
        url: str | None = self.client.tickets_first_page_url()
        for _ in range(MAX_DISCOVERY_PAGES):
            tickets, next_url = self.client.get_tickets_page(url)
            self._store_tickets(tickets)
            if known_max_id is None or not tickets or next_url is None:
                # known_max_id is None on the very first run: the full scan
                # loads the ~8,000 existing tickets, not this step.
                return
            if min(normalize.to_int(t["id"]) for t in tickets) <= known_max_id:
                return
            url = next_url

    # ------------------------------------------------------------------
    # 3. Full scan: ticket field changes everywhere + deletion detection
    # ------------------------------------------------------------------

    def advance_full_scan(self) -> None:
        """Walk the whole ticket list, `scan_pages_per_cycle` pages per cycle.

        Why walk everything instead of fetching each open ticket?
          * One page returns 50 complete tickets, so the whole list (~170
            pages) is cheaper than GET /tickets/{id} for 400 open tickets.
          * It also covers closed tickets, so re-opened tickets are noticed.
          * Every ticket we see gets last_seen_at = now. When the pass is
            done, a ticket that was not seen since the pass started is a
            deletion suspect.
        """
        phase = self.db.get_state(SCAN_PHASE)
        if phase is None:
            now = self._now()
            self.db.set_state(SCAN_STARTED_AT, now, now)
            self.db.set_state(SCAN_NEXT_URL, self.client.tickets_first_page_url(), now)
            self.db.set_state(SCAN_PHASE, "listing", now)
            phase = "listing"

        if phase == "listing":
            if not self._scan_pages():
                return  # more pages left, continue next cycle
            self.db.set_state(SCAN_PHASE, "verifying", self._now())

        self._verify_unseen_tickets()

    def _scan_pages(self) -> bool:
        """Fetch the next slice of pages. Returns True when the list is exhausted."""
        url = self.db.get_state(SCAN_NEXT_URL)
        for _ in range(self.settings.scan_pages_per_cycle):
            tickets, next_url = self.client.get_tickets_page(url)
            self._store_tickets(tickets)
            self.stats["scan_pages"] += 1
            # `next_url == url`: the stateless mock keeps pointing at the same page.
            if not tickets or next_url is None or next_url == url:
                return True
            url = next_url
            self.db.set_state(SCAN_NEXT_URL, url, self._now())
            self.db.commit()  # progress survives if we get cut off
        return False

    def _verify_unseen_tickets(self) -> None:
        """Confirm deletion suspects one by one with GET /tickets/{id}.

        Not being in the list is not enough to call a ticket deleted: while we
        page through the list, a deletion elsewhere shifts every later ticket
        one position up, so one ticket can slip between two pages unseen.
        Only a 404 on the ticket itself counts. Deleted tickets are a handful
        per hour, so this costs few requests."""
        started_at = self.db.get_state(SCAN_STARTED_AT)
        limit = self.settings.deletion_checks_per_cycle
        suspects = self.db.tickets_not_seen_since(started_at, limit)
        for ticket_id in suspects:
            try:
                raw = self.client.get_ticket(ticket_id)
            except NotFound:
                log.info("Ticket %s is gone from FacilityDesk, marking it deleted", ticket_id)
                self.db.mark_deleted(ticket_id, self._now())
                self.stats["tickets_deleted"] += 1
            else:
                self._store_tickets([raw])  # still exists, it slipped through the pages
            self.db.commit()

        if len(suspects) < limit:  # every suspect checked: the pass is complete
            now = self._now()
            self.db.set_state(SCAN_PHASE, None, now)
            self.db.set_state(SCAN_COMPLETED_AT, now, now)
            self.stats["scan_passes_completed"] += 1

    # ------------------------------------------------------------------
    # 4 + 5. Activities and chat messages
    # ------------------------------------------------------------------

    def refresh_open_tickets(self) -> None:
        """Ticket.updated_at does not change when activities or chat messages
        are added, so there is no cheap "what changed?" signal: every open
        ticket has to be re-fetched regularly. Most stale first."""
        def due(limit: int) -> list[int]:
            synced_before = self._now_minus(minutes=self.settings.open_ticket_refresh_minutes)
            return self.db.open_tickets_due_for_refresh(synced_before, limit)

        self._refresh_children_of(due, stat="open_tickets_refreshed")

    def backfill_closed_tickets(self) -> None:
        self._refresh_children_of(self.db.closed_tickets_needing_sync, stat="closed_tickets_synced")

    def _refresh_children_of(self, next_ids, stat: str) -> None:
        """Refresh tickets returned by `next_ids(limit)` until there are none
        left or the time budget runs out (OutOfTime propagates to run_cycle)."""
        failed: set[int] = set()
        while True:
            ids = [i for i in next_ids(BATCH_SIZE + len(failed)) if i not in failed]
            if not ids:
                return
            for ticket_id in ids:
                try:
                    self.refresh_ticket_children(ticket_id)
                    self.stats[stat] += 1
                except ApiError:
                    # Don't let one broken ticket block all the others.
                    log.exception("Could not refresh ticket %s, skipping it this cycle", ticket_id)
                    failed.add(ticket_id)
                    self.stats["errors"] += 1

    def refresh_ticket_children(self, ticket_id: int) -> None:
        """Fetch all activities and all 5 chats of one ticket, then store them.

        Everything is fetched before anything is written, and
        children_synced_at is only set at the end, so a ticket is either fully
        refreshed or retried later."""
        try:
            activities = self.client.list_activities(ticket_id)
            chats = {chat_type: self.client.list_chat_messages(ticket_id, chat_type)
                     for chat_type in CHAT_TYPES}
        except NotFound:
            # Sub-resources of a deleted ticket return 404 as well.
            log.info("Ticket %s returned 404, marking it deleted", ticket_id)
            self.db.mark_deleted(ticket_id, self._now())
            self.stats["tickets_deleted"] += 1
            return

        now = self._now()
        for raw in activities:
            row, emails = normalize.activity(raw, ticket_id)
            self.db.upsert_activity(row, emails, now)
        for chat_type, messages in chats.items():
            for raw in messages:
                self.db.upsert_chat_message(normalize.chat_message(raw, ticket_id, chat_type), now)
        self.db.mark_children_synced(ticket_id, now)
        self.db.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _store_tickets(self, raw_tickets: list[dict]) -> None:
        now = self._now()
        for raw in raw_tickets:
            row, users = normalize.ticket(raw)
            if self.db.upsert_ticket(row, users, now):
                self.stats["tickets_new"] += 1
            self.stats["tickets_upserted"] += 1

    def _now(self) -> str:
        return normalize.format_utc(self.clock.now())

    def _now_minus(self, **delta: float) -> str:
        return normalize.format_utc(self.clock.now() - timedelta(**delta))


def run_cycle(settings: Settings, clock: Clock | None = None, http: httpx.Client | None = None) -> dict:
    """Entry point used by both the CLI and the Azure Function."""
    clock = clock or Clock()
    db = Database(settings.db_path)
    client = FacilityDeskClient(
        settings.base_url, settings.client_id, settings.client_secret,
        clock=clock, rate_limit_per_minute=settings.rate_limit_per_minute, http=http,
    )
    try:
        return SyncJob(client, db, settings, clock).run_cycle()
    finally:
        db.close()
