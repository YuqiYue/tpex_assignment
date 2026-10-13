"""Each test documents one way the real payloads deviate from the schema."""

from __future__ import annotations

from fd_sync import normalize


def test_timestamps_are_converted_to_utc():
    assert normalize.to_utc("2026-10-01T08:12:44+02:00") == "2026-10-01T06:12:44Z"
    assert normalize.to_utc("2026-10-01T07:02:51+00:00") == "2026-10-01T07:02:51Z"
    assert normalize.to_utc("2026-10-01T07:02:51Z") == "2026-10-01T07:02:51Z"


def test_timestamp_without_offset_is_assumed_amsterdam_time():
    # closed_ticket.site_visit_at in the examples; CEST is UTC+2 in September
    assert normalize.to_utc("2026-09-28 13:30:00") == "2026-09-28T11:30:00Z"
    # and UTC+1 in winter
    assert normalize.to_utc("2026-12-01 13:30:00") == "2026-12-01T12:30:00Z"


def test_bad_or_missing_timestamps_become_null():
    assert normalize.to_utc(None) is None
    assert normalize.to_utc("") is None
    assert normalize.to_utc("next tuesday") is None


def test_lowercase_priority_is_normalised():
    row, _ = normalize.ticket({"id": 1, "priority": "medium", "current_step": "ISSUE_CLOSED"})
    assert row["priority"] == "MEDIUM"


def test_omitted_optional_fields_do_not_crash():
    # sparse_ticket in the examples has no tenant_company_name / deadline_at at all
    row, users = normalize.ticket({"id": 48177, "title": "Badge reader", "current_step": "ISSUE_REPORTED"})
    assert row["tenant_company_name"] is None and row["deadline_at"] is None
    assert users == []


def test_unknown_step_is_kept_as_is():
    row, _ = normalize.ticket({"id": 1, "current_step": "ON_HOLD"})
    assert row["current_step"] == "ON_HOLD"


def test_activity_ticket_id_as_string_and_data_as_empty_list():
    row, emails = normalize.activity(
        {"id": 880020, "ticket_id": "48213", "message": "x", "data": [], "creator": None,
         "created_at": "2026-10-01T06:40:05+00:00",
         "emails": [{"id": 5501, "sender_email": "a@x", "recipient_email": "b@x",
                     "carbon_copies": ["c@x"]}]},
        ticket_id=48213,
    )
    assert row["ticket_id"] == 48213
    assert row["data_json"] == "{}"
    assert row["creator_id"] is None
    assert emails[0]["activity_id"] == 880020 and emails[0]["carbon_copies_json"] == '["c@x"]'


def test_address_object_is_flattened():
    row = normalize.building({
        "building_id": 215, "building_name": "Spree Campus", "company_id": 7,
        "address": {"street": "Stralauer Allee 2", "postal_code": "10245", "city": "Berlin", "country": "DE"},
    })
    assert row["address"] == "Stralauer Allee 2, 10245 Berlin, DE"


def test_chat_author_photo_from_thumbnail():
    row = normalize.chat_message(
        {"id": 330188, "message": "hi", "sent_at": "2026-10-01T07:02:51+00:00",
         "author": {"name": "Mark Jansen", "thumbnail": "https://example/u/17.png"}},
        ticket_id=48213, chat_type="TECHNICAL_MANAGER_AND_TENANT",
    )
    assert row["author_photo_url"] == "https://example/u/17.png"
    assert (row["ticket_id"], row["chat_type"]) == (48213, "TECHNICAL_MANAGER_AND_TENANT")
