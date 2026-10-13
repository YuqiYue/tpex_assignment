"""Settings and the clock.

Every tunable number of the pipeline lives in `Settings`, so changing the
schedule or the API budget never requires touching the sync logic.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone


@dataclass(frozen=True)
class Settings:
    # FacilityDesk API. Defaults point at the local Prism mock.
    base_url: str = "http://127.0.0.1:4010"
    client_id: str = "tpex-ingestion"
    client_secret: str = "s3cr3t-value"

    # Local SQLite file (in Azure this would be the Azure SQL connection string).
    db_path: str = "data/facilitydesk.db"

    # The API allows 60 requests/minute for everything using our credentials.
    # We keep 2/minute of headroom for retries and ad-hoc calls.
    rate_limit_per_minute: int = 58

    # How long one invocation may keep calling the API. The timer fires every
    # 5 minutes; 285s leaves a margin before the next tick and is far below
    # the 10-minute Consumption plan timeout.
    cycle_seconds: int = 285

    # Activities and chats of an open ticket are re-fetched when their last
    # sync is older than this. 50 minutes + up to one 5-minute timer tick
    # keeps us within the ~1 hour freshness requirement.
    open_ticket_refresh_minutes: int = 50

    # The full ticket list (~170 pages) is walked a few pages per cycle.
    # 15 pages x 12 cycles/hour = 180 pages/hour => one full pass per hour.
    scan_pages_per_cycle: int = 15

    # Max "is this ticket really deleted?" checks (GET /tickets/{id}) per cycle.
    deletion_checks_per_cycle: int = 50

    # Buildings and categories change rarely; refresh them every few hours.
    reference_refresh_hours: int = 6

    @classmethod
    def from_env(cls) -> Settings:
        """Read every field from an upper-case environment variable, e.g.
        `FD_BASE_URL`, `FD_RATE_LIMIT_PER_MINUTE`. Unset variables keep the default."""
        overrides = {}
        for field in fields(cls):
            raw = os.environ.get(f"FD_{field.name.upper()}")
            if raw is not None:
                overrides[field.name] = int(raw) if field.type == "int" else raw
        return cls(**overrides)


class Clock:
    """Wall-clock and sleeping in one place, so tests can swap in a fake clock
    and run an hour of syncing in milliseconds."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
