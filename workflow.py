"""
workflow.py — Core pipeline: list → dedupe → scrape → clean → AI → store.

This module is the heart of the system. It is:
  - Idempotent  : safe to re-run at any time
  - Restart-safe: skips already-processed posts via Supabase check
  - Self-logging : detailed stage-by-stage output
"""
from __future__ import annotations

import time
from typing import Optional

from config import MAX_RETRY, SCRAPE_DELAY
from logger import get_logger
from cleaner import clean_text
from gemini_client import extract_problems
from parser import parse_gemini_output
import supabase_client as db

log = get_logger("workflow")


# ── Retry helpers ─────────────────────────────────────────────────────────────

def _scrape_with_retry(driver, url: str, scrape_fn) -> Optional[str]:
    """Call scrape_fn(driver, url) with up to MAX_RETRY retries."""
    for attempt in range(1, MAX_RETRY + 2):
        try:
            result = scrape_fn(driver, url)
            if result:
                return result
            log.warning(f"Scrape returned empty (attempt {attempt}): {url}")
        except Exception as e:
            log.warning(f"Scrape error attempt {attempt}: {e}")
        if attempt <= MAX_RETRY:
            time.sleep(2)
    log.error(f"Scrape failed after all retries: {url}")
    return None


def _gemini_with_retry(title: str, content: str) -> Optional[list[dict]]:
    """
    Call Gemini + parse with up to MAX_RETRY retries on bad JSON.
    Returns validated list[dict] or None.
    """
    for attempt in range(1, MAX_RETRY + 2):
        raw = extract_problems(title, content)
        if raw is None:
            log.error("Gemini extraction returned None; no more retries")
            return None

        parsed = parse_gemini_output(raw)
        if parsed is not None:
            return parsed

        log.warning(f"JSON parse failed (attempt {attempt}) — retrying Gemini")
        time.sleep(2)

    log.error("Gemini+parse failed after all retries")
    return None


# ── Router ────────────────────────────────────────────────────────────────────

def _is_no_problems(problems: list[dict]) -> bool:
    return (
        len(problems) == 1
        and problems[0].get("problem_name", "").strip().lower() == "no problems found"
    )


def _store_results(
    post_id:   str,
    post_url:  str,
    timestamp: str,
    problems:  list[dict],
) -> None:
    """
    Router logic:
      CASE A — no problems → insert only into post_ids
      CASE B — problems found → insert each into problems, then post_ids
    """
    if _is_no_problems(problems):
        log.info(f"[CASE A] No problems — recording post_id only: {post_id}")
        db.insert_post_id(post_id, post_url, timestamp)
        return

    log.info(f"[CASE B] {len(problems)} problem(s) found — inserting to problems table")
    for p in problems:
        try:
            db.insert_problem(
                company      = p.get("company", ""),
                problem_name = p.get("problem_name", ""),
                problem_type = p.get("problem_type", ""),
                posted_on    = timestamp,
                post_url     = post_url,
                problem_url  = None,
            )
        except Exception as e:
            log.error(f"Failed to insert problem {p}: {e}")

    # Only record post_id AFTER all problems are safely stored
    db.insert_post_id(post_id, post_url, timestamp)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(list_fn, scrape_fn) -> dict:
    """
    Full autonomous pipeline.

    Args:
        list_fn   : callable() → list of {post_id, title, timestamp, post_url}
        scrape_fn : callable(driver, url) → str (raw post text)

    Returns summary dict with counts and status.
    """
    summary = {
        "status":       "ok",
        "fetched":      0,
        "new":          0,
        "skipped":      0,
        "scraped_ok":   0,
        "scraped_fail": 0,
        "gemini_ok":    0,
        "gemini_fail":  0,
        "problems_total": 0,
        "db_inserts":   0,
        "errors":       [],
    }

    # ── STEP 1: fetch listing ─────────────────────────────────────────────────
    log.info("══ STEP 1: Fetching post list ══")
    try:
        posts = list_fn()
    except Exception as e:
        log.exception(f"list_fn crashed: {e}")
        summary["status"]  = "error"
        summary["errors"].append(str(e))
        return summary

    summary["fetched"] = len(posts)
    log.info(f"Fetched {len(posts)} posts")

    if not posts:
        log.info("No posts returned — stopping")
        summary["status"] = "empty"
        return summary

    # ── STEP 2: deduplicate via Supabase ──────────────────────────────────────
    log.info("══ STEP 2: Deduplication check ══")
    new_posts = []
    for post in posts:
        try:
            if db.post_id_exists(post["post_id"]):
                log.info(f"SKIP (exists): {post['post_id']}")
                summary["skipped"] += 1
            else:
                new_posts.append(post)
        except Exception as e:
            log.error(f"Supabase check failed for {post['post_id']}: {e}")
            new_posts.append(post)   # fail-open: process uncertain posts

    summary["new"] = len(new_posts)
    log.info(f"New posts to process: {len(new_posts)} | Skipped: {summary['skipped']}")

    if not new_posts:
        log.info("All posts already processed — stopping until next run")
        summary["status"] = "all_duplicate"
        return summary

    # ── STEPS 3–7: per-post processing ────────────────────────────────────────
    from scraper import build_driver, load_cookies_from_env  # import here to avoid circular

    cookies = load_cookies_from_env()
    driver  = None

    try:
        driver = build_driver(cookies)

        for i, post in enumerate(new_posts, 1):
            post_id   = post["post_id"]
            post_url  = post["post_url"]
            title     = post["title"]
            timestamp = post["timestamp"]

            log.info(f"══ Processing [{i}/{len(new_posts)}]: {title!r} ══")

            # ── STEP 3: scrape ────────────────────────────────────────────────
            log.info(f"STEP 3 — Scraping: {post_url}")
            raw_text = _scrape_with_retry(driver, post_url, scrape_fn)
            if not raw_text:
                log.error(f"Scrape failed — skipping post: {post_id}")
                summary["scraped_fail"] += 1
                summary["errors"].append(f"scrape_fail:{post_id}")
                continue
            summary["scraped_ok"] += 1
            log.info(f"Scraped {len(raw_text)} chars")

            # ── STEP 4: clean ─────────────────────────────────────────────────
            log.info("STEP 4 — Cleaning text")
            cleaned = clean_text(raw_text)
            log.info(f"Cleaned: {len(cleaned)} chars")

            # ── STEP 5+6: Gemini extract + parse ─────────────────────────────
            log.info("STEP 5+6 — Gemini extraction + JSON parse")
            problems = _gemini_with_retry(title, cleaned)
            if problems is None:
                log.error(f"Gemini failed — skipping post: {post_id}")
                summary["gemini_fail"] += 1
                summary["errors"].append(f"gemini_fail:{post_id}")
                continue
            summary["gemini_ok"] += 1
            log.info(f"Extracted {len(problems)} problem(s)")
            summary["problems_total"] += 0 if _is_no_problems(problems) else len(problems)

            # ── STEP 7: route + store ─────────────────────────────────────────
            log.info("STEP 7 — Storing results")
            try:
                _store_results(post_id, post_url, timestamp, problems)
                summary["db_inserts"] += 1
            except Exception as e:
                log.error(f"DB store failed for {post_id}: {e}")
                summary["errors"].append(f"db_fail:{post_id}")

            time.sleep(SCRAPE_DELAY)

    finally:
        if driver:
            driver.quit()
            log.info("Driver closed")

    # ── STEP 8: summary ───────────────────────────────────────────────────────
    log.info("══ PIPELINE COMPLETE ══")
    log.info(
        f"Fetched={summary['fetched']} | New={summary['new']} | "
        f"Skipped={summary['skipped']} | Scraped OK={summary['scraped_ok']} | "
        f"Gemini OK={summary['gemini_ok']} | Problems={summary['problems_total']} | "
        f"DB inserts={summary['db_inserts']} | Errors={len(summary['errors'])}"
    )
    return summary
