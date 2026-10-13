"""Azure Functions entry point (Python v2 programming model).

One timer-triggered function runs a sync cycle every 5 minutes. Timer
triggers run as a singleton across instances and a tick is skipped while the
previous run is still busy, so there is only ever one caller of the API:
that is what lets us keep the rate limiter in memory (see README).

Settings come from application settings (environment variables), e.g.
FD_BASE_URL, FD_CLIENT_ID, FD_CLIENT_SECRET (in production from Key Vault).
"""

import logging

import azure.functions as func

from fd_sync.config import Settings
from fd_sync.sync import run_cycle

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def facilitydesk_sync(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("Timer is past due; running the cycle now")
    run_cycle(Settings.from_env())
