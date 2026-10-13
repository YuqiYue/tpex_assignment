"""Turn raw API payloads into rows for our tables.

The API examples were captured from production and do not always match the
schemas (see README, "Surprises in the API"). The rule here is: be lenient
when reading, strict when writing. Unknown or malformed values never crash
the sync; they are normalised where the intent is obvious, otherwise stored
as-is, and the full original payload is always kept in `raw_json`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .constants import DEFAULT_TIMEZONE

log = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)


# ----------------------------------------------------------------------
# Field helpers
# ----------------------------------------------------------------------

def to_utc(value: object) -> str | None:
    """Parse an ISO 8601 timestamp and return it in UTC as 'YYYY-MM-DDTHH:MM:SSZ'.

    One fixed format means timestamps sort and compare correctly as text in
    any SQL dialect. Timestamps without an offset are assumed to be
    Europe/Amsterdam local time."""
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T", 1))
    except ValueError:
        log.warning("Unparseable timestamp %r, storing NULL", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_LOCAL_TZ)
    return format_utc(parsed)


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_int(value: object) -> int | None:
    """Accept 48213 and "48213" (both appear in the activity examples)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("Expected an integer, got %r", value)
        return None


def to_upper(value: object) -> str | None:
    """Enums are upper-case in the schema, but `priority: medium` occurs."""
    return str(value).upper() if value is not None else None


def to_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def flatten_address(value: object) -> str | None:
    """`address` is a string in the schema, but an object for the Berlin building."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        city_line = " ".join(p for p in (value.get("postal_code"), value.get("city")) if p)
        parts = (value.get("street"), city_line, value.get("country"))
        return ", ".join(p for p in parts if p)
    return to_json(value)


# ----------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------

def building(raw: dict) -> dict:
    return {
        "building_id": to_int(raw["building_id"]),
        "building_name": raw.get("building_name"),
        "building_code": raw.get("building_code"),
        "address": flatten_address(raw.get("address")),
        "currency": raw.get("currency"),
        "company_id": to_int(raw.get("company_id")),
        "raw_json": to_json(raw),
    }


def category(raw: dict) -> dict:
    return {
        "id": to_int(raw["id"]),
        "parent_id": to_int(raw.get("parent_id")),
        "company_id": to_int(raw.get("company_id")),
        "name": raw.get("name"),
    }


def ticket(raw: dict) -> tuple[dict, list[dict]]:
    """Returns the ticket row and its user rows. Every optional field is read
    with .get(): "nullable fields may be null or omitted"."""
    row = {
        "id": to_int(raw["id"]),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "category_id": to_int(raw.get("category_id")),
        "category_name": raw.get("category_name"),
        "priority": to_upper(raw.get("priority")),
        "current_step": to_upper(raw.get("current_step")),
        "building_id": to_int(raw.get("building_id")),
        "building_name": raw.get("building_name"),
        "exact_location": raw.get("exact_location"),
        "supplier_name": raw.get("supplier_name"),
        "tenant_company_name": raw.get("tenant_company_name"),
        "origin": raw.get("origin"),
        "deadline_at": to_utc(raw.get("deadline_at")),
        "site_visit_at": to_utc(raw.get("site_visit_at")),
        "created_at": to_utc(raw.get("created_at")),
        "updated_at": to_utc(raw.get("updated_at")),
        "raw_json": to_json(raw),
    }
    users = [
        {
            "ticket_id": row["id"],
            "user_id": to_int(user.get("id")),
            "name": user.get("name"),
            "email": user.get("email"),
            "role": to_upper(user.get("role")),
        }
        for user in raw.get("users") or []
    ]
    return row, users


def activity(raw: dict, ticket_id: int) -> tuple[dict, list[dict]]:
    """Returns the activity row and its email rows.

    `ticket_id` comes from the request path, which is authoritative; the
    payload's own ticket_id is sometimes a string."""
    payload_ticket_id = to_int(raw.get("ticket_id"))
    if payload_ticket_id is not None and payload_ticket_id != ticket_id:
        log.warning("Activity %s claims ticket %s but was listed under ticket %s",
                    raw.get("id"), payload_ticket_id, ticket_id)
    data = raw.get("data")
    creator = raw.get("creator") or {}
    row = {
        "id": to_int(raw["id"]),
        "ticket_id": ticket_id,
        "message": raw.get("message"),
        # `data` is documented as an object but comes back as [] when empty.
        "data_json": to_json(data if isinstance(data, dict) else {}),
        "creator_id": to_int(creator.get("id")),
        "creator_name": creator.get("full_name"),
        "creator_email": creator.get("email"),
        "creator_company_id": to_int(creator.get("company_id")),
        "creator_company_name": creator.get("company_name"),
        "created_at": to_utc(raw.get("created_at")),
        "raw_json": to_json(raw),
    }
    emails = [
        {
            "id": to_int(email["id"]),
            "activity_id": row["id"],
            "sender_email": email.get("sender_email"),
            "recipient_email": email.get("recipient_email"),
            "carbon_copies_json": to_json(email.get("carbon_copies") or []),
            "subject": email.get("subject"),
            "email_content": email.get("email_content"),
        }
        for email in raw.get("emails") or []
    ]
    return row, emails


def chat_message(raw: dict, ticket_id: int, chat_type: str) -> dict:
    """Chat messages carry neither ticket id nor chat type: both come from the path."""
    author = raw.get("author") or {}
    return {
        "id": to_int(raw["id"]),
        "ticket_id": ticket_id,
        "chat_type": chat_type,
        "ticket_chat_id": to_int(raw.get("ticket_chat_id")),
        "message": raw.get("message"),
        "author_name": author.get("name"),
        # Documented as `profile_photo`, but `thumbnail` occurs as well.
        "author_photo_url": author.get("profile_photo") or author.get("thumbnail"),
        "sent_at": to_utc(raw.get("sent_at")),
        "files_json": to_json(raw.get("files") or []),
        "mentions_json": to_json(raw.get("mentions") or []),
        "raw_json": to_json(raw),
    }
