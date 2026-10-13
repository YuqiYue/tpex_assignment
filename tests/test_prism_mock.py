"""Smoke test against the Prism mock, i.e. against the real spec and its
production-captured examples. Skipped unless the mock is running:

    npx @stoplight/prism-cli@5 mock openapi.yaml
"""

from __future__ import annotations

import socket

import pytest

from fd_sync.config import Settings
from fd_sync.db import Database
from fd_sync.sync import run_cycle


def prism_is_running() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 4010), timeout=0.5).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not prism_is_running(), reason="Prism mock not running on :4010")


def test_one_cycle_against_the_mock(tmp_path):
    settings = Settings(db_path=str(tmp_path / "mock.db"), rate_limit_per_minute=6000)  # the mock has no limit
    stats = run_cycle(settings)
    assert "errors" not in stats

    db = Database(settings.db_path)
    summary = db.summary()
    assert summary["tickets"] == 4
    assert summary["open_tickets"] == 3  # 48213, 48177 and the ON_HOLD one

    closed = db.conn.execute("SELECT * FROM tickets WHERE id = 48190").fetchone()
    assert closed["priority"] == "MEDIUM"
    assert closed["site_visit_at"] == "2026-09-28T11:30:00Z"

    berlin = db.conn.execute("SELECT address FROM buildings WHERE building_id = 215").fetchone()
    assert berlin["address"] == "Stralauer Allee 2, 10245 Berlin, DE"

    assert summary["activities"] == 3
    assert summary["chat_messages"] == 2
    db.close()
