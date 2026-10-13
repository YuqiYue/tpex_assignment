"""HTTP client for the FacilityDesk Client API.

Everything about talking to the API lives here: authentication, the rate
limit, retries and pagination. The sync logic only sees plain Python dicts.
"""

from __future__ import annotations

import logging
import httpx

from .config import Clock

log = logging.getLogger(__name__)

TOKEN_LIFETIME_SECONDS = 30 * 60  # documented token lifetime
TOKEN_REFRESH_MARGIN_SECONDS = 60  # refresh a minute early
MAX_ATTEMPTS = 4  # per request, across 401 / 429 / 5xx / network errors
PAGE_LIMIT = 100  # max `limit` for activities and chat messages
MAX_PAGES = 1000  # safety net against pagination loops


class NotFound(Exception):
    """404: the ticket does not exist, was deleted, or is not accessible."""


class OutOfTime(Exception):
    """The invocation's time budget is used up. Stop and let the next run continue."""


class ApiError(Exception):
    """A request kept failing after all retries."""


class RateLimiter:
    """Spaces requests evenly: at most one every 60/N seconds for N per minute.

    Why even spacing instead of "N requests, then wait for the window"?
    Invocations share no memory, so a new invocation cannot know how many
    requests the previous one sent in its last minute. With even spacing that
    does not matter: the gap between two invocations is always longer than
    the spacing, so no 60-second window ever holds more than N requests.

    Only one invocation calls the API at a time (the timer trigger runs as a
    singleton, see README), so no state needs to be shared between instances.
    """

    def __init__(self, requests_per_minute: int, clock: Clock):
        self.interval = 60.0 / requests_per_minute
        self.clock = clock
        self._last_request_at: float | None = None

    def seconds_until_allowed(self) -> float:
        if self._last_request_at is None:
            return 0.0
        return max(0.0, self._last_request_at + self.interval - self.clock.monotonic())

    def record_request(self) -> None:
        self._last_request_at = self.clock.monotonic()


class FacilityDeskClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        clock: Clock,
        rate_limit_per_minute: int,
        http: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.clock = clock
        self.rate_limiter = RateLimiter(rate_limit_per_minute, clock)
        self.http = http or httpx.Client(timeout=30)
        self.deadline: float | None = None  # monotonic time after which we stop
        self.requests_made = 0
        self._token: str | None = None
        self._token_expires_at = 0.0  # monotonic

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def list_buildings(self) -> list[dict]:
        return self._get_all_pages(f"{self.base_url}/buildings")

    def list_categories(self) -> list[dict]:
        # Not paginated.
        return self._get_json(f"{self.base_url}/tickets/categories").get("data") or []

    def tickets_first_page_url(self) -> str:
        return f"{self.base_url}/tickets?page=1"

    def get_tickets_page(self, url: str) -> tuple[list[dict], str | None]:
        """One page of GET /tickets (newest first). Returns (tickets, next_url).

        Exposed page by page (instead of an iterator) so the caller can stop
        mid-way and store `next_url` to continue in the next invocation."""
        body = self._get_json(url)
        return body.get("data") or [], (body.get("links") or {}).get("next")

    def get_ticket(self, ticket_id: int) -> dict:
        return self._get_json(f"{self.base_url}/tickets/{ticket_id}")["data"]

    def list_activities(self, ticket_id: int) -> list[dict]:
        return self._get_all_pages(
            f"{self.base_url}/tickets/{ticket_id}/activities", {"limit": PAGE_LIMIT}
        )

    def list_chat_messages(self, ticket_id: int, chat_type: str) -> list[dict]:
        return self._get_all_pages(
            f"{self.base_url}/tickets/{ticket_id}/chats/{chat_type}/messages",
            {"limit": PAGE_LIMIT},
        )

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _get_all_pages(self, url: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        seen_urls: set[str] = set()
        next_url: str | None = url
        while next_url:
            if next_url in seen_urls or len(seen_urls) >= MAX_PAGES:
                # The stateless mock can return a `next` link pointing at a
                # page we already fetched; never loop forever on it.
                log.warning("Stopping pagination, %s was already fetched", next_url)
                break
            seen_urls.add(next_url)
            # Our `params` are merged into every page's URL: `links.next` is
            # not guaranteed to carry our `limit` along.
            body = self._get_json(str(httpx.URL(next_url).copy_merge_params(params or {})))
            items.extend(body.get("data") or [])
            next_url = (body.get("links") or {}).get("next")
        return items

    # ------------------------------------------------------------------
    # Requests: auth, rate limit, retries
    # ------------------------------------------------------------------

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        return self._request("GET", url, params=params).json()

    def _request(
        self, method: str, url: str, *, params: dict | None = None,
        json: dict | None = None, authenticated: bool = True,
    ) -> httpx.Response:
        token_refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Accept": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {self._get_token()}"

            self._sleep(self.rate_limiter.seconds_until_allowed())
            self.rate_limiter.record_request()
            self.requests_made += 1
            try:
                response = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                log.warning("%s %s failed (%s), attempt %d", method, url, exc, attempt)
                self._sleep(2**attempt)
                continue

            if response.status_code == 401 and authenticated and not token_refreshed:
                # Token expired early or was revoked: get a new one, retry once.
                self._token = None
                token_refreshed = True
                continue
            if response.status_code == 429:
                # Someone else using our credentials ate the budget.
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                log.warning("429 on %s, waiting %ss", url, retry_after)
                self._sleep(retry_after)
                continue
            if response.status_code >= 500:
                log.warning("%s on %s, attempt %d", response.status_code, url, attempt)
                self._sleep(2**attempt)
                continue
            if response.status_code == 404:
                raise NotFound(url)
            if response.is_error:
                raise ApiError(f"{method} {url} -> {response.status_code}: {response.text[:200]}")
            return response
        raise ApiError(f"{method} {url} failed after {MAX_ATTEMPTS} attempts")

    def _get_token(self) -> str:
        if self._token and self.clock.monotonic() < self._token_expires_at:
            return self._token
        response = self._request(
            "POST",
            f"{self.base_url}/auth/token",
            json={"client_id": self.client_id, "client_secret": self.client_secret},
            authenticated=False,
        )
        self._token = response.json()["access_token"]
        # We ignore the returned `expires_at` and use the documented lifetime
        # on our own monotonic clock: it is immune to clock skew (the mock even
        # returns an `expires_at` in the past). A 401 still triggers a refresh.
        self._token_expires_at = (
            self.clock.monotonic() + TOKEN_LIFETIME_SECONDS - TOKEN_REFRESH_MARGIN_SECONDS
        )
        return self._token

    def _sleep(self, seconds: float) -> None:
        """Sleep, unless that would take us past the invocation's deadline."""
        if seconds <= 0:
            if self.deadline is not None and self.clock.monotonic() >= self.deadline:
                raise OutOfTime()
            return
        if self.deadline is not None and self.clock.monotonic() + seconds >= self.deadline:
            raise OutOfTime()
        self.clock.sleep(seconds)


def _parse_retry_after(value: str | None) -> float:
    try:
        return max(float(value), 1.0)
    except (TypeError, ValueError):
        return 30.0
