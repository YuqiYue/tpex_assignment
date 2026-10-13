"""End-to-end behaviour of the pipeline against the stateful fake API.

These are the three things the assignment asks for (new, changed, deleted)
plus the edge cases that make them tricky."""

from __future__ import annotations


def ticket_row(db, ticket_id):
    return db.conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()


def chat_ids(db, ticket_id):
    rows = db.conn.execute("SELECT id FROM chat_messages WHERE ticket_id = ?", (ticket_id,))
    return {r["id"] for r in rows}


def run_cycles(job, clock, n, minutes_between=5):
    for _ in range(n):
        job.run_cycle()
        clock.advance(minutes_between)


def test_first_cycle_loads_everything(api, db, make_job):
    for ticket_id in (1, 2, 3, 4, 5):
        api.add_ticket(ticket_id)
    api.add_ticket(6, current_step="ISSUE_CLOSED")
    activity = api.add_activity(2)
    message = api.add_chat_message(2, "INTERNAL_TEAM")

    make_job().run_cycle()

    assert db.summary()["tickets"] == 6  # 3 pages of 2: needs the full scan
    assert db.summary()["buildings"] == 1 and db.summary()["categories"] == 1
    stored = db.conn.execute("SELECT ticket_id FROM activities WHERE id = ?", (activity["id"],)).fetchone()
    assert stored["ticket_id"] == 2
    chat = db.conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message["id"],)).fetchone()
    assert (chat["ticket_id"], chat["chat_type"]) == (2, "INTERNAL_TEAM")  # from the path
    # The closed ticket's history was backfilled with leftover budget.
    assert ticket_row(db, 6)["children_synced_at"] is not None


def test_new_ticket_is_picked_up_in_the_next_cycle(api, db, clock, make_job):
    api.add_ticket(1)
    job = make_job()
    run_cycles(job, clock, 1)

    api.add_ticket(2, title="Brand new")
    job.run_cycle()

    assert ticket_row(db, 2)["title"] == "Brand new"
    assert ticket_row(db, 2)["children_synced_at"] is not None  # new open tickets go first


def test_field_change_on_an_old_ticket_is_picked_up_by_the_full_scan(api, db, clock, make_job):
    for ticket_id in range(1, 11):
        api.add_ticket(ticket_id)
    job = make_job(scan_pages_per_cycle=2)  # 5 pages => a pass takes 3 cycles
    run_cycles(job, clock, 3)

    # Ticket 1 is on the last page: the "new tickets" step never sees it.
    api.tickets[1].update(priority="CRITICAL", updated_at="2026-10-12T10:00:00+00:00")
    run_cycles(job, clock, 4)

    assert ticket_row(db, 1)["priority"] == "CRITICAL"
    assert ticket_row(db, 1)["updated_at"] == "2026-10-12T10:00:00Z"


def test_new_chat_message_on_an_open_ticket_arrives_within_an_hour(api, db, clock, make_job):
    """updated_at does NOT change when a message is added; we still find it."""
    api.add_ticket(1)
    job = make_job()
    job.run_cycle()

    new_message = api.add_chat_message(1, "SUPPLIER_AND_TENANT")
    chats_polled = api.count_requests("/tickets/1/chats/")
    clock.advance(10)
    job.run_cycle()
    assert api.count_requests("/tickets/1/chats/") == chats_polled  # not due yet: saves budget

    clock.advance(45)  # 55 minutes after the first sync
    job.run_cycle()
    assert new_message["id"] in chat_ids(db, 1)


def test_deleted_ticket_is_soft_deleted(api, db, clock, make_job):
    for ticket_id in (1, 2, 3):
        api.add_ticket(ticket_id, current_step="ISSUE_CLOSED")
    job = make_job()
    run_cycles(job, clock, 1)

    api.delete_ticket(2)
    run_cycles(job, clock, 2)

    assert ticket_row(db, 2)["deleted_at"] is not None  # kept, but flagged
    assert ticket_row(db, 1)["deleted_at"] is None
    assert db.summary()["tickets"] == 2


def test_ticket_missing_from_the_list_but_still_existing_is_not_deleted(api, db, clock, make_job):
    """Pages shift while we scan, so a ticket can be skipped. Only a 404 on
    GET /tickets/{id} may mark it deleted."""
    for ticket_id in (1, 2, 3):
        api.add_ticket(ticket_id, current_step="ISSUE_CLOSED")
    job = make_job()
    run_cycles(job, clock, 1)

    api.hidden_from_list.add(2)
    run_cycles(job, clock, 2)

    assert ticket_row(db, 2)["deleted_at"] is None
    assert api.count_requests("GET /tickets/2?") >= 1  # it was verified individually


def test_open_ticket_deleted_is_detected_through_its_sub_resources(api, db, clock, make_job):
    api.add_ticket(1)
    job = make_job()
    run_cycles(job, clock, 1)

    api.delete_ticket(1)
    clock.advance(50)
    job.run_cycle()

    assert ticket_row(db, 1)["deleted_at"] is not None


def test_reopened_ticket_is_polled_again(api, db, clock, make_job):
    api.add_ticket(1, current_step="ISSUE_CLOSED")
    job = make_job()
    run_cycles(job, clock, 1)

    api.tickets[1]["current_step"] = "REVIEWING_FIX"
    message = api.add_chat_message(1, "TECHNICAL_MANAGER_AND_TENANT", "It is broken again")
    run_cycles(job, clock, 2)

    assert ticket_row(db, 1)["current_step"] == "REVIEWING_FIX"
    assert message["id"] in chat_ids(db, 1)


def test_unknown_step_is_treated_as_open(api, db, clock, make_job):
    api.add_ticket(1, current_step="ON_HOLD")
    job = make_job()
    job.run_cycle()
    message = api.add_chat_message(1, "INTERNAL_TEAM")
    clock.advance(50)
    job.run_cycle()

    assert message["id"] in chat_ids(db, 1)


def test_cycle_stops_at_its_deadline_and_the_next_one_continues(api, db, clock, make_job):
    api.ticket_page_size = 50
    for ticket_id in range(1, 41):
        api.add_ticket(ticket_id)
    # 40 open tickets x 6 requests = 240 requests: more than a 60s cycle allows.
    job = make_job(cycle_seconds=60, rate_limit_per_minute=55, scan_pages_per_cycle=50)

    stats = job.run_cycle()
    assert stats["stopped_at_deadline"] == 1
    assert stats["requests"] <= 55  # one every 60/55 s for 60 s
    assert db.summary()["tickets_without_children_synced"] > 0

    run_cycles(job, clock, 8)
    assert db.summary()["tickets_without_children_synced"] == 0
