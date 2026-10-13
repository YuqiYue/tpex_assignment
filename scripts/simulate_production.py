"""Simulate a day of production load in a few seconds, to check the request
budget and the freshness claims in the README with numbers instead of hope.

    uv run python scripts/simulate_production.py --hours 24

Uses the fake API and fake clock from the tests: 8,000 historical (closed)
tickets, 400 open tickets, ~40 new tickets per day, a steady trickle of new
chat messages and status changes, a few deletions. Prints one line per hour.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_sync.api import FacilityDeskClient  # noqa: E402
from fd_sync.config import Settings  # noqa: E402
from fd_sync.constants import CHAT_TYPES, CLOSED_STEPS  # noqa: E402
from fd_sync.db import Database  # noqa: E402
from fd_sync.sync import SyncJob  # noqa: E402
from tests.fakes import BASE_URL, FakeClock, FakeFacilityDesk  # noqa: E402

CYCLE_MINUTES = 5


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--open-refresh-minutes", type=int, default=Settings.open_ticket_refresh_minutes,
                        help="e.g. 240 to see how fast the initial backfill goes when open tickets wait")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    api = FakeFacilityDesk(ticket_page_size=50)
    for ticket_id in range(1, 8001):
        api.add_ticket(ticket_id, current_step=rng.choice(CLOSED_STEPS))
    for ticket_id in range(8001, 8401):
        api.add_ticket(ticket_id, current_step="VISIT_SCHEDULED")
    next_id = 8401

    clock = FakeClock()
    settings = Settings(base_url=BASE_URL, open_ticket_refresh_minutes=args.open_refresh_minutes)
    db = Database(":memory:")
    client = FacilityDeskClient(BASE_URL, "id", "secret", clock=clock,
                                rate_limit_per_minute=settings.rate_limit_per_minute,
                                http=httpx.Client(transport=api.transport()))
    job = SyncJob(client, db, settings, clock)

    print(f"{'hour':>4} {'requests':>8} {'open':>5} {'max staleness open (min)':>25} "
          f"{'closed not yet backfilled':>26} {'deleted':>7}")
    cycles_per_hour = 60 // CYCLE_MINUTES
    next_tick = 0.0
    for hour in range(1, args.hours + 1):
        requests_before = client.requests_made
        max_staleness = 0.0
        for _ in range(cycles_per_hour):
            # Production-like changes between two cycles (5 minutes).
            if rng.random() < 40 / (24 * cycles_per_hour):  # ~40 new tickets/day
                api.add_ticket(next_id)
                next_id += 1
            open_ids = [t["id"] for t in api.tickets.values() if t["current_step"] not in CLOSED_STEPS]
            for _ in range(3):  # new chat messages on random open tickets
                api.add_chat_message(rng.choice(open_ids), rng.choice(CHAT_TYPES))
            if rng.random() < 0.3:  # status change, sometimes closing the ticket
                api.tickets[rng.choice(open_ids)]["current_step"] = rng.choice(
                    ["REVIEWING_FIX", "ISSUE_CLOSED"])
            if rng.random() < 0.02:  # a user deletes a ticket
                api.delete_ticket(rng.choice(list(api.tickets)))

            job.run_cycle()
            max_staleness = max(max_staleness, oldest_open_sync_minutes(db, clock))
            next_tick += CYCLE_MINUTES * 60  # the timer fires every 5 minutes
            clock.sleep(next_tick - clock.monotonic())
            # Just before the next cycle runs is when data is the most stale.
            max_staleness = max(max_staleness, oldest_open_sync_minutes(db, clock))

        backlog = db.conn.execute(
            f"""SELECT COUNT(*) FROM tickets WHERE deleted_at IS NULL AND children_synced_at IS NULL
                AND current_step IN ({",".join("?" * len(CLOSED_STEPS))})""", CLOSED_STEPS,
        ).fetchone()[0]
        summary = db.summary()
        print(f"{hour:>4} {client.requests_made - requests_before:>8} {summary['open_tickets']:>5} "
              f"{max_staleness:>25.0f} {backlog:>26} {summary['deleted_tickets']:>7}")


def oldest_open_sync_minutes(db: Database, clock: FakeClock) -> float:
    """Age of the least recently refreshed open ticket (never synced counts from its first sighting)."""
    oldest = db.conn.execute(
        f"""SELECT MIN(COALESCE(children_synced_at, first_seen_at)) FROM tickets
            WHERE deleted_at IS NULL AND current_step NOT IN ({",".join("?" * len(CLOSED_STEPS))})""",
        CLOSED_STEPS,
    ).fetchone()[0]
    if oldest is None:
        return 0.0
    synced = datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (clock.now() - synced).total_seconds() / 60


if __name__ == "__main__":
    main()
