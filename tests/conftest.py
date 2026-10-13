from __future__ import annotations

import dataclasses

import httpx
import pytest

from fd_sync.api import FacilityDeskClient
from fd_sync.config import Settings
from fd_sync.db import Database
from fd_sync.sync import SyncJob
from tests.fakes import BASE_URL, FakeClock, FakeFacilityDesk


@pytest.fixture
def api() -> FakeFacilityDesk:
    return FakeFacilityDesk(ticket_page_size=2)  # tiny pages so pagination is exercised


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def make_job(api, clock, db):
    """Build a SyncJob wired to the fake API, the fake clock and an in-memory DB."""
    def _make(**overrides) -> SyncJob:
        settings = dataclasses.replace(Settings(base_url=BASE_URL), **overrides)
        client = FacilityDeskClient(
            settings.base_url, settings.client_id, settings.client_secret, clock=clock,
            rate_limit_per_minute=settings.rate_limit_per_minute,
            http=httpx.Client(transport=api.transport()),
        )
        return SyncJob(client, db, settings, clock)
    return _make


@pytest.fixture
def make_client(api, clock):
    def _make(rate_limit_per_minute: int = 55) -> FacilityDeskClient:
        return FacilityDeskClient(BASE_URL, "id", "secret", clock=clock,
                                  rate_limit_per_minute=rate_limit_per_minute,
                                  http=httpx.Client(transport=api.transport()))
    return _make
