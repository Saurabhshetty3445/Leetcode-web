"""
scheduler.py — Background cron scheduler (runs every SCHEDULER_INTERVAL hours).

Concurrency note: locking against overlapping runs now lives centrally in
scraper.py's _run_lock, shared by both the manual /run endpoint and the
scheduled job below (see scraper.py's scheduled_pipeline() and
run_pipeline_isolated()). This module intentionally has NO lock of its own
anymore — a previous version kept a separate lock here, which meant a manual
/run trigger and a scheduled run could execute concurrently, each spinning up
its own browser session. Two such sessions racing inside the same shared
Node.js driver process is what caused hard, unrecoverable driver crashes.
`max_instances=1` below still stops APScheduler from double-firing this same
job internally, but pipeline_fn itself is responsible for the real
manual-vs-scheduled mutual exclusion.
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from config import SCHEDULER_INTERVAL
from logger import get_logger

log = get_logger("scheduler")


def start_scheduler(pipeline_fn) -> BackgroundScheduler:
    """
    Start background cron job.
    pipeline_fn: zero-arg callable that runs the full pipeline. It is
    responsible for its own locking (see scraper.py's scheduled_pipeline()).
    Returns the scheduler (caller can call .shutdown() on it).
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        func=pipeline_fn,
        trigger="interval",
        hours=SCHEDULER_INTERVAL,
        id="pipeline_cron",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    log.info(f"Scheduler started — runs every {SCHEDULER_INTERVAL} hours")
    return scheduler
