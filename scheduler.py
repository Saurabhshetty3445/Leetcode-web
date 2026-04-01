"""
scheduler.py — Background cron scheduler (runs every SCHEDULER_INTERVAL hours).

Uses a threading lock to prevent overlapping runs.
"""
from __future__ import annotations

import threading
from apscheduler.schedulers.background import BackgroundScheduler

from config import SCHEDULER_INTERVAL
from logger import get_logger

log = get_logger("scheduler")

_run_lock = threading.Lock()


def _locked_run(pipeline_fn):
    """Execute pipeline_fn only if no other run is in progress."""
    acquired = _run_lock.acquire(blocking=False)
    if not acquired:
        log.warning("Scheduler: previous run still active — skipping this cycle")
        return
    try:
        log.info("Scheduler: starting scheduled pipeline run")
        summary = pipeline_fn()
        log.info(f"Scheduler: run complete → {summary}")
    except Exception as e:
        log.exception(f"Scheduler: pipeline crashed: {e}")
    finally:
        _run_lock.release()


def start_scheduler(pipeline_fn) -> BackgroundScheduler:
    """
    Start background cron job.
    pipeline_fn: zero-arg callable that runs the full pipeline.
    Returns the scheduler (caller can call .shutdown() on it).
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        func=lambda: _locked_run(pipeline_fn),
        trigger="interval",
        hours=SCHEDULER_INTERVAL,
        id="pipeline_cron",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    log.info(f"Scheduler started — runs every {SCHEDULER_INTERVAL} hours")
    return scheduler
