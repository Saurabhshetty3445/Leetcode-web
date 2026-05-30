"""
links_workflow.py — Batch pipeline for manually-added LeetCode links.

Flow:
  1. Fetch up to BATCH_SIZE 'empty' links from leetcode_links table
  2. For each link: scrape → clean → Gemini extract → descriptions → save problems
  3. Mark link 'done' (or 'error' on repeated failure)
  4. Return summary identical in shape to run_pipeline() summary

This is entirely separate from the main scraping pipeline.
It reuses all existing modules (cleaner, gemini_client, gemini_description,
parser, supabase_client) without modifying them.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

from config import SUPABASE_URL, SUPABASE_KEY, MAX_RETRY, SCRAPE_DELAY
from logger import get_logger
from cleaner import clean_text
from gemini_client import extract_problems
from gemini_description import enrich_with_descriptions
from parser import parse_gemini_output
import supabase_client as db

log = get_logger("links_workflow")

BATCH_SIZE = 10

_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}


# ── Supabase helpers for leetcode_links ──────────────────────────────────────

def _sb_url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}"


def fetch_pending_links(limit: int = BATCH_SIZE) -> list[dict]:
    """Fetch up to `limit` rows with status='empty', oldest first."""
    resp = _requests.get(
        _sb_url("leetcode_links"),
        headers=_HEADERS,
        params={
            "status": "eq.empty",
            "order":  "created_at.asc",
            "limit":  str(limit),
            "select": "id,url,note",
        },
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"fetch_pending_links failed [{resp.status_code}]: {resp.text}")
    return resp.json()


def mark_link(link_id: str, status: str) -> None:
    """Set status = 'done' or 'error' on a link row."""
    resp = _requests.patch(
        _sb_url("leetcode_links"),
        headers={**_HEADERS, "Prefer": "return=minimal"},
        params={"id": f"eq.{link_id}"},
        json={"status": status},
        timeout=10,
    )
    if not resp.ok:
        log.warning(f"mark_link failed [{resp.status_code}]: {resp.text[:200]}")


# ── Gemini helpers (mirror of workflow.py) ────────────────────────────────────

def _is_no_problems(problems: list[dict]) -> bool:
    return (
        len(problems) == 1
        and problems[0].get("problem_name", "").strip().lower() == "no problems found"
    )


def _gemini_with_retry(title: str, content: str) -> Optional[list[dict]]:
    for attempt in range(1, MAX_RETRY + 2):
        raw = extract_problems(title, content)
        if raw is None:
            log.error("Gemini extraction returned None")
            return None
        parsed = parse_gemini_output(raw)
        if parsed is not None:
            return parsed
        log.warning(f"JSON parse failed (attempt {attempt}) — retrying")
        time.sleep(2)
    log.error("Gemini+parse failed after all retries")
    return None


# ── Store problems (same logic as workflow._store_results) ────────────────────

def _store_problems(post_url: str, timestamp: str, problems: list[dict]) -> int:
    """
    Save extracted problems to problems + company_problems tables.
    Returns count of problems successfully inserted.
    """
    if _is_no_problems(problems):
        log.info(f"No problems found for {post_url}")
        return 0

    inserted = 0
    for p in problems:
        try:
            problem_id = db.insert_problem_returning_id(
                company      = p.get("company", ""),
                problem_name = p.get("problem_name", ""),
                problem_type = p.get("problem_type", ""),
                description  = p.get("description", ""),
                posted_on    = timestamp,
                post_url     = post_url,
                problem_url  = None,
            )
            if not problem_id:
                continue

            company_name = p.get("company", "").strip()
            if company_name:
                company_id = db.upsert_company(company_name)
                if company_id:
                    db.insert_company_problem(
                        company_id   = company_id,
                        problem_id   = problem_id,
                        company_name = company_name,
                        problem_name = p.get("problem_name", ""),
                        problem_type = p.get("problem_type", ""),
                        description  = p.get("description", ""),
                        posted_on    = timestamp,
                        post_url     = post_url,
                        problem_url  = None,
                    )
            inserted += 1
        except Exception as e:
            log.error(f"Failed to store problem {p.get('problem_name')!r}: {e}")

    return inserted


# ── Main batch pipeline ───────────────────────────────────────────────────────

def run_links_pipeline(scrape_fn) -> dict:
    """
    Process up to BATCH_SIZE pending links from leetcode_links.

    Args:
        scrape_fn : callable(driver, url) → str  (scrape_post_detail from scraper.py)

    Returns summary dict.
    """
    from scraper import build_driver, load_cookies_from_env

    summary = {
        "status":          "ok",
        "batch_size":      BATCH_SIZE,
        "links_fetched":   0,
        "links_processed": 0,
        "links_error":     0,
        "problems_saved":  0,
        "errors":          [],
    }

    # ── Fetch pending links ───────────────────────────────────────────────────
    log.info("══ LINKS: Fetching pending links ══")
    try:
        links = fetch_pending_links(BATCH_SIZE)
    except Exception as e:
        log.exception(f"fetch_pending_links crashed: {e}")
        summary["status"] = "error"
        summary["errors"].append(str(e))
        return summary

    summary["links_fetched"] = len(links)
    log.info(f"Found {len(links)} pending link(s)")

    if not links:
        summary["status"] = "no_pending"
        log.info("No pending links — nothing to do")
        return summary

    # ── Build driver once for the whole batch ─────────────────────────────────
    cookies = load_cookies_from_env()
    driver  = None

    try:
        driver = build_driver(cookies)

        for i, link in enumerate(links, 1):
            link_id  = link["id"]
            post_url = link["url"].strip()
            note     = link.get("note", "") or ""
            timestamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

            log.info(f"══ Link [{i}/{len(links)}]: {post_url} ══")

            # ── Scrape ────────────────────────────────────────────────────────
            raw_text = None
            for attempt in range(1, MAX_RETRY + 2):
                try:
                    raw_text = scrape_fn(driver, post_url)
                    if raw_text:
                        break
                    log.warning(f"Scrape empty (attempt {attempt}): {post_url}")
                except Exception as e:
                    log.warning(f"Scrape error attempt {attempt}: {e}")
                if attempt <= MAX_RETRY:
                    time.sleep(2)

            if not raw_text:
                log.error(f"Scrape failed for: {post_url}")
                mark_link(link_id, "error")
                summary["links_error"] += 1
                summary["errors"].append(f"scrape_fail:{post_url}")
                continue

            log.info(f"Scraped {len(raw_text)} chars")

            # ── Clean ─────────────────────────────────────────────────────────
            cleaned = clean_text(raw_text)
            log.info(f"Cleaned: {len(cleaned)} chars")

            # Use note or URL as title hint for Gemini
            title = note if note else post_url.rstrip("/").split("/")[-1].replace("-", " ").title()

            # ── Gemini extract + parse ────────────────────────────────────────
            problems = _gemini_with_retry(title, cleaned)
            if problems is None:
                log.error(f"Gemini failed for: {post_url}")
                mark_link(link_id, "error")
                summary["links_error"] += 1
                summary["errors"].append(f"gemini_fail:{post_url}")
                continue

            log.info(f"Extracted {len(problems)} problem(s)")

            # ── Descriptions ──────────────────────────────────────────────────
            if not _is_no_problems(problems):
                try:
                    problems = enrich_with_descriptions(problems)
                    log.info(f"Descriptions generated for {len(problems)} problem(s)")
                except Exception as e:
                    log.error(f"Description enrichment failed (non-fatal): {e}")
                    for p in problems:
                        p.setdefault("description", "")

            # ── Store ─────────────────────────────────────────────────────────
            saved = _store_problems(post_url, timestamp, problems)
            summary["problems_saved"] += saved
            log.info(f"Saved {saved} problem(s) for {post_url}")

            # ── Mark done ─────────────────────────────────────────────────────
            mark_link(link_id, "done")
            summary["links_processed"] += 1
            log.info(f"Link marked done: {post_url}")

            time.sleep(SCRAPE_DELAY)

    finally:
        if driver:
            driver.quit()
            log.info("Driver closed")

    log.info(
        f"══ LINKS BATCH COMPLETE ══ "
        f"Fetched={summary['links_fetched']} | "
        f"Processed={summary['links_processed']} | "
        f"Errors={summary['links_error']} | "
        f"Problems saved={summary['problems_saved']}"
    )
    return summary
