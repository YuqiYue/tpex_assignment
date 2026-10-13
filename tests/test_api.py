from __future__ import annotations

import httpx
import pytest

from fd_sync.api import NotFound, OutOfTime


def test_expired_token_is_refreshed_and_the_request_retried(api, make_client):
    api.add_ticket(1)
    client = make_client()
    client.get_ticket(1)
    api.expire_tokens()

    assert client.get_ticket(1)["id"] == 1
    assert api.count_requests("/auth/token") == 2


def test_429_waits_for_retry_after(api, clock, make_client):
    api.add_ticket(1)
    client = make_client()
    client.get_ticket(1)
    api.queued_errors.append(httpx.Response(429, headers={"Retry-After": "23"}))

    before = clock.monotonic()
    assert client.get_ticket(1)["id"] == 1
    assert clock.monotonic() - before >= 23


def test_rate_limiter_never_exceeds_the_limit(api, clock, make_client):
    api.add_ticket(1)
    client = make_client(rate_limit_per_minute=10)
    for _ in range(25):  # +1 token request
        client.get_ticket(1)

    # 26 requests at 10/minute = one every 6s: the last one goes at t=150s.
    assert clock.monotonic() >= 150


def test_out_of_time_instead_of_sleeping_past_the_deadline(api, clock, make_client):
    api.add_ticket(1)
    client = make_client(rate_limit_per_minute=5)
    client.deadline = clock.monotonic() + 30
    with pytest.raises(OutOfTime):
        for _ in range(10):
            client.get_ticket(1)


def test_404_raises_not_found(api, make_client):
    with pytest.raises(NotFound):
        make_client().get_ticket(12345)


def test_pagination_reapplies_limit_on_every_page(api, make_client):
    api.add_ticket(1)
    for _ in range(150):
        api.add_activity(1)

    assert len(make_client().list_activities(1)) == 150
    activity_requests = [r for r in api.requests if "/activities" in r]
    assert len(activity_requests) == 2 and all("limit=100" in r for r in activity_requests)
