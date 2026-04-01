"""
supabase_client.py — All Supabase read/write operations.

Tables expected:
  post_ids  : post_id (PK), post_url, timestamp, created_at
  problems  : id, company, problem_name, problem_type,
              posted_on, post_url, problem_url
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from config import SUPABASE_URL, SUPABASE_KEY, POST_IDS_TTL_HOURS
from logger import get_logger

log = get_logger("supabase")

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=minimal",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}"


def _raise(resp: requests.Response, ctx: str) -> None:
    if not resp.ok:
        raise RuntimeError(f"Supabase {ctx} failed [{resp.status_code}]: {resp.text}")


# ── post_ids ──────────────────────────────────────────────────────────────────

def post_id_exists(post_id: str) -> bool:
    """Return True if post_id already stored (i.e. already processed)."""
    resp = requests.get(
        _url("post_ids"),
        headers=HEADERS,
        params={"post_id": f"eq.{post_id}", "select": "post_id"},
        timeout=10,
    )
    _raise(resp, "post_id_exists")
    return len(resp.json()) > 0


def insert_post_id(post_id: str, post_url: str, timestamp: str) -> None:
    """Record a processed post in post_ids."""
    payload = {
        "post_id":    post_id,
        "post_url":   post_url,
        "timestamp":  timestamp,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.post(_url("post_ids"), headers=HEADERS, json=payload, timeout=10)
    _raise(resp, "insert_post_id")
    log.info(f"post_ids ← {post_id}")


# ── problems ──────────────────────────────────────────────────────────────────

def insert_problem(
    company:      str,
    problem_name: str,
    problem_type: str,
    posted_on:    str,
    post_url:     str,
    problem_url:  Optional[str] = None,
) -> None:
    """Insert one extracted problem row."""
    payload = {
        "company":      company,
        "problem_name": problem_name,
        "problem_type": problem_type,
        "posted_on":    posted_on,
        "post_url":     post_url,
        "problem_url":  problem_url,
    }
    resp = requests.post(_url("problems"), headers=HEADERS, json=payload, timeout=10)
    _raise(resp, "insert_problem")
    log.info(f"problems ← {problem_name!r} ({company})")


# ── TTL cleanup ───────────────────────────────────────────────────────────────

def cleanup_old_post_ids() -> int:
    """Delete post_ids older than POST_IDS_TTL_HOURS. Returns deleted count."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=POST_IDS_TTL_HOURS)).isoformat()
    # Supabase REST delete with filter
    resp = requests.delete(
        _url("post_ids"),
        headers={**HEADERS, "Prefer": "return=representation"},
        params={"created_at": f"lt.{cutoff}"},
        timeout=10,
    )
    _raise(resp, "cleanup_old_post_ids")
    deleted = resp.json() if resp.text else []
    count = len(deleted) if isinstance(deleted, list) else 0
    log.info(f"TTL cleanup: deleted {count} old post_ids (cutoff={cutoff})")
    return count
