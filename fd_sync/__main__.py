"""Command line entry point.

    uv run python -m fd_sync cycle            # one sync cycle (what the Azure timer runs)
    uv run python -m fd_sync loop             # a cycle every 5 minutes, like the timer
    uv run python -m fd_sync status           # row counts in the local database
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import time

from .config import Settings
from .db import Database
from .sync import run_cycle


def main() -> None:
    parser = argparse.ArgumentParser(prog="fd_sync", description="FacilityDesk -> SQL sync")
    sub = parser.add_subparsers(dest="command", required=True)
    cycle = sub.add_parser("cycle", help="run one sync cycle")
    cycle.add_argument("--seconds", type=int, help="override the cycle time budget")
    loop = sub.add_parser("loop", help="run a cycle every --interval seconds")
    loop.add_argument("--interval", type=int, default=300)
    sub.add_parser("status", help="print row counts")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too noisy
    settings = Settings.from_env()

    if args.command == "cycle":
        if args.seconds:
            settings = dataclasses.replace(settings, cycle_seconds=args.seconds)
        run_cycle(settings)
        print_status(settings)
    elif args.command == "loop":
        while True:
            started = time.monotonic()
            run_cycle(settings)
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    elif args.command == "status":
        print_status(settings)


def print_status(settings: Settings) -> None:
    db = Database(settings.db_path)
    try:
        print(json.dumps(db.summary(), indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
