"""Test doubles: a *stateful* in-memory FacilityDesk and a fake clock.

The Prism mock is stateless (it always returns the same examples), so it
cannot show that we pick up new, changed or deleted tickets. This fake keeps
real state that tests can mutate between sync cycles, and it is served
through httpx.MockTransport so the real HTTP client code is exercised.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx

BASE_URL = "http://fake.facilitydesk"


class FakeClock:
    """Time only moves when someone sleeps or a test calls advance()."""

    def __init__(self, start: datetime = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)):
        self._start = start
        self._elapsed = 0.0

    def monotonic(self) -> float:
        return self._elapsed

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def sleep(self, seconds: float) -> None:
        self._elapsed += seconds

    def advance(self, minutes: float) -> None:
        self._elapsed += minutes * 60


class FakeFacilityDesk:
    def __init__(self, ticket_page_size: int = 50):
        self.ticket_page_size = ticket_page_size
        self.tickets: dict[int, dict] = {}
        self.activities: dict[int, list[dict]] = defaultdict(list)
        self.chats: dict[tuple[int, str], list[dict]] = defaultdict(list)
        self.buildings = [{"building_id": 101, "building_name": "Harbour Offices", "company_id": 7}]
        self.categories = [{"id": 1, "parent_id": None, "company_id": 7, "name": "HVAC"}]
        self.hidden_from_list: set[int] = set()  # simulate a ticket slipping between pages
        self.requests: list[str] = []
        self.queued_errors: list[httpx.Response] = []  # returned before handling normally
        self._valid_tokens: set[str] = set()
        self._ids = itertools.count(900_000)

    # ---- helpers for tests ------------------------------------------------

    def add_ticket(self, ticket_id: int, **fields) -> dict:
        ticket = {
            "id": ticket_id,
            "title": f"Ticket {ticket_id}",
            "current_step": "ISSUE_REPORTED",
            "building_id": 101,
            "created_at": "2026-10-01T08:00:00+02:00",
            "updated_at": "2026-10-01T08:00:00+02:00",
            "users": [],
            **fields,
        }
        self.tickets[ticket_id] = ticket
        return ticket

    def add_activity(self, ticket_id: int, message: str = "Something happened") -> dict:
        activity = {"id": next(self._ids), "ticket_id": ticket_id, "message": message,
                    "data": [], "creator": None, "created_at": "2026-10-01T09:00:00+00:00", "emails": []}
        self.activities[ticket_id].append(activity)
        return activity

    def add_chat_message(self, ticket_id: int, chat_type: str, message: str = "Hello") -> dict:
        msg = {"id": next(self._ids), "ticket_chat_id": 1, "message": message,
               "author": {"name": "Someone"}, "sent_at": "2026-10-01T09:00:00+00:00",
               "files": [], "mentions": []}
        self.chats[(ticket_id, chat_type)].append(msg)
        return msg

    def delete_ticket(self, ticket_id: int) -> None:
        del self.tickets[ticket_id]

    def expire_tokens(self) -> None:
        self._valid_tokens.clear()

    def count_requests(self, fragment: str) -> int:
        return sum(fragment in r for r in self.requests)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- the API ----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.requests.append(f"{request.method} {path}?{request.url.query.decode()}")

        if self.queued_errors:
            return self.queued_errors.pop(0)

        if path == "/auth/token":
            token = f"token-{len(self._valid_tokens) + len(self.requests)}"
            self._valid_tokens.add(token)
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer",
                                             "expires_at": "2026-10-05T10:30:00+00:00"})

        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self._valid_tokens:
            return httpx.Response(401, json={"message": "Unauthenticated."})

        parts = path.strip("/").split("/")
        if parts == ["buildings"]:
            return self._page(path, self.buildings, query, default_size=50)
        if parts == ["tickets", "categories"]:
            return httpx.Response(200, json={"data": self.categories})
        if parts == ["tickets"]:
            visible = [t for tid, t in sorted(self.tickets.items(), reverse=True)
                       if tid not in self.hidden_from_list]
            return self._page(path, visible, query, default_size=self.ticket_page_size, fixed=True)

        ticket_id = int(parts[1])
        if ticket_id not in self.tickets:
            return httpx.Response(404, json={"message": "Ticket not found"})
        if len(parts) == 2:
            return httpx.Response(200, json={"data": self.tickets[ticket_id]})
        if parts[2] == "activities":
            return self._page(path, self.activities[ticket_id], query, default_size=20)
        if parts[2] == "chats":
            return self._page(path, self.chats[(ticket_id, parts[3])], query, default_size=20)
        return httpx.Response(404, json={"message": "Not found"})

    def _page(self, path: str, items: list, query: dict, default_size: int, fixed: bool = False):
        page = int(query.get("page", 1))
        size = default_size if fixed else min(int(query.get("limit", default_size)), 100)
        chunk = items[(page - 1) * size: page * size]
        has_next = page * size < len(items)
        # Like many real APIs, `next` only carries the page number and drops
        # `limit`: the client has to re-apply its own query parameters.
        return httpx.Response(200, json={
            "data": chunk,
            "links": {"first": f"{BASE_URL}{path}?page=1", "prev": None,
                      "next": f"{BASE_URL}{path}?page={page + 1}" if has_next else None},
            "meta": {"current_page": page, "per_page": size},
        })
