"""
supabase_client.py — All Supabase read/write operations.

Tables expected:
  post_ids         : post_id (PK), post_url, timestamp, created_at
  problems         : id, company, problem_name, problem_type, description,
                     posted_on, post_url, problem_url, created_at
  companies        : id, name, slug, problem_count, last_seen_at, created_at
  company_problems : id, company_id, problem_id, company_name, problem_name,
                     problem_type, description, posted_on, post_url,
                     problem_url, created_at

Note: company_problems is also auto-populated by a DB trigger on every
INSERT into problems. The Python calls below are belt-and-suspenders to
guarantee consistency even if the trigger is ever disabled.
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

_DESIGN_KEYWORDS = {
    "design", "system", "architecture", "scalability", "hld", "lld",
    "low level", "high level", "distributed", "microservice", "api",
    "service", "infra", "infrastructure", "database", "db schema",
    "rate limit", "cache", "caching", "messaging", "queue", "kafka",
    "load balancer", "cdn", "storage", "ott", "booking",
}

_CODING_KEYWORDS = {
    "array", "string", "tree", "graph", "dp", "dynamic programming",
    "greedy", "backtracking", "recursion", "bit", "math", "sort",
    "search", "hash", "heap", "stack", "queue", "linked list",
    "sliding window", "two pointer", "binary search", "trie",
    "segment tree", "union find", "bfs", "dfs", "matrix",
}


def _normalize_problem_type(raw: str) -> str:
    """
    Map any Gemini-produced problem_type to the DB-allowed values:
      "coding"  | "design"  | "none"
    Falls back to "coding" for unrecognised values.
    """
    val = raw.strip().lower()

    # Already correct values
    if val in ("coding", "design", "none"):
        return val

    # Explicit no-problem sentinel
    if val in ("", "unknown", "n/a", "na"):
        return "none"

    # Check against design keyword set
    for kw in _DESIGN_KEYWORDS:
        if kw in val:
            return "design"

    # Check against coding keyword set
    for kw in _CODING_KEYWORDS:
        if kw in val:
            return "coding"

    # Default: treat as coding (DSA catch-all)
    log.warning(f"Unknown problem_type {raw!r} — defaulting to 'coding'")
    return "coding"


def _build_problem_payload(
    company: str, problem_name: str, problem_type: str,
    description: str, posted_on: str, post_url: str,
    problem_url: Optional[str],
) -> dict:
    """Shared payload builder for problem insert functions."""
    return {
        "company":      company,
        "problem_name": problem_name,
        "problem_type": _normalize_problem_type(problem_type),
        "description":  description,
        "posted_on":    posted_on,
        "post_url":     post_url,
        "problem_url":  problem_url,
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }


def insert_problem(
    company:      str,
    problem_name: str,
    problem_type: str,
    posted_on:    str,
    post_url:     str,
    description:  str = "",
    problem_url:  Optional[str] = None,
) -> None:
    """Insert one problem row (fire-and-forget, does not return id)."""
    payload = _build_problem_payload(
        company, problem_name, problem_type,
        description, posted_on, post_url, problem_url,
    )
    resp = requests.post(_url("problems"), headers=HEADERS, json=payload, timeout=10)
    _raise(resp, "insert_problem")
    log.info(f"problems ← {problem_name!r} ({company}) [type={payload['problem_type']}]")


def insert_problem_returning_id(
    company:      str,
    problem_name: str,
    problem_type: str,
    posted_on:    str,
    post_url:     str,
    description:  str = "",
    problem_url:  Optional[str] = None,
) -> Optional[str]:
    """
    Insert one problem row and return its UUID.
    Used by workflow to link the problem into company_problems.
    Returns None on failure (caller should handle gracefully).
    """
    payload = _build_problem_payload(
        company, problem_name, problem_type,
        description, posted_on, post_url, problem_url,
    )
    resp = requests.post(
        _url("problems"),
        headers={**HEADERS, "Prefer": "return=representation"},
        json=payload,
        timeout=10,
    )
    _raise(resp, "insert_problem_returning_id")
    data = resp.json()
    problem_id = data[0].get("id") if isinstance(data, list) and data else None
    log.info(f"problems ← {problem_name!r} ({company}) [id={problem_id}]")
    return problem_id


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


# ── companies ─────────────────────────────────────────────────────────────────

import re as _re


def _make_slug(name: str) -> str:
    """Build a URL-safe slug from a company name. e.g. 'Goldman Sachs' → 'goldman-sachs'."""
    s = name.strip().lower()
    s = _re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def upsert_company(company_name: str) -> Optional[str]:
    """
    Insert company if not present, increment problem_count and update
    last_seen_at if it already exists.
    Returns the company's UUID id, or None on failure.
    """
    name = company_name.strip()
    if not name:
        return None

    slug = _make_slug(name)

    # Try insert first
    payload = {
        "name":         name,
        "slug":         slug,
        "problem_count": 1,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    # upsert: on name conflict, increment count + refresh last_seen_at
    resp = requests.post(
        _url("companies"),
        headers={**HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"},
        json=payload,
        params={"on_conflict": "slug"},
        timeout=10,
    )

    if resp.ok:
        data = resp.json()
        if isinstance(data, list) and data:
            company_id = data[0].get("id")
            log.info(f"companies ← upsert '{name}' (id={company_id})")
            return company_id

    # Fallback: fetch existing row by slug
    log.warning(f"Company upsert POST failed [{resp.status_code}] — fetching by slug")
    get_resp = requests.get(
        _url("companies"),
        headers=HEADERS,
        params={"slug": f"eq.{slug}", "select": "id"},
        timeout=10,
    )
    if get_resp.ok:
        rows = get_resp.json()
        if rows:
            return rows[0].get("id")

    log.error(f"upsert_company: could not find or create '{name}'")
    return None


def insert_company_problem(
    company_id:   str,
    problem_id:   str,
    company_name: str,
    problem_name: str,
    problem_type: str,
    description:  str,
    posted_on:    str,
    post_url:     str,
    problem_url:  Optional[str] = None,
    created_at:   Optional[str] = None,
) -> None:
    """
    Insert one row into company_problems.
    ON CONFLICT (company_id, problem_id) → silently skipped (idempotent).
    """
    payload = {
        "company_id":   company_id,
        "problem_id":   problem_id,
        "company_name": company_name,
        "problem_name": problem_name,
        "problem_type": _normalize_problem_type(problem_type),
        "description":  description,
        "posted_on":    posted_on,
        "post_url":     post_url,
        "problem_url":  problem_url,
        "created_at":   created_at or datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.post(
        _url("company_problems"),
        headers={**HEADERS, "Prefer": "return=minimal,resolution=ignore-duplicates"},
        json=payload,
        params={"on_conflict": "company_id,problem_id"},
        timeout=10,
    )
    if not resp.ok:
        # Non-fatal: the DB trigger already handles this; log but don't raise
        log.warning(
            f"company_problems insert warning [{resp.status_code}]: {resp.text[:200]}"
        )
    else:
        log.info(f"company_problems ← '{problem_name}' → '{company_name}'")
