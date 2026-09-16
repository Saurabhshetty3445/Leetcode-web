"""
cloudflare_browser_client.py — Compliant HTTP client for fetching JS-rendered
LeetCode pages via Cloudflare's managed Browser Rendering API.

This replaces the previous local headless-Chrome + anti-detection stealth
layer (fingerprint spoofing, navigator.webdriver patching, etc.) with a
plain, honest HTTP client against Cloudflare's own rendering service.

Important: Cloudflare's Browser Rendering explicitly does NOT bypass
CAPTCHAs, Turnstile, or any other bot-protection mechanism — requests made
through it are always identifiable as automated. See:
https://developers.cloudflare.com/browser-run/quick-actions/crawl-endpoint/#bot-protection-may-block-crawling

That means this module's job is NOT to get past LeetCode's defenses. Its
job is to:
  - avoid hammering the target (rate limiting)
  - avoid re-fetching the same page repeatedly (TTL caching)
  - recover from transient failures (exponential backoff + jitter)
  - fail loudly and honestly when the target site declines to serve us
    (BlockedError), so callers can back off and move on instead of
    retrying forever or trying to work around it
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

import requests

from config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    HTTP_TIMEOUT_SECONDS,
    MAX_RETRY,
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    MIN_REQUEST_INTERVAL_SECONDS,
    PAGE_CACHE_TTL_SECONDS,
)
from logger import get_logger

log = get_logger("cf_browser_client")

_CONTENT_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering/content"
)

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


class CloudflareConfigError(RuntimeError):
    """CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing or invalid."""


class BlockedError(RuntimeError):
    """
    Raised when the target site's own bot protection served a challenge or
    access-denied page instead of real content. Callers should treat this
    as "temporarily unavailable" and back off — never as something to
    retry aggressively or route around.
    """


# ── Rate limiting: enforce a minimum gap between outbound calls ─────────────

_rate_lock = threading.Lock()
_last_call_ts = 0.0


def _throttle() -> None:
    global _last_call_ts
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


# ── Small in-memory TTL cache to avoid re-fetching the same URL ─────────────

_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()
_CACHE_MAX_ENTRIES = 500


def _cache_get(url: str) -> Optional[str]:
    with _cache_lock:
        entry = _cache.get(url)
    if not entry:
        return None
    html, fetched_at = entry
    if time.time() - fetched_at > PAGE_CACHE_TTL_SECONDS:
        return None
    return html


def _cache_set(url: str, html: str) -> None:
    with _cache_lock:
        _cache[url] = (html, time.time())
        if len(_cache) > _CACHE_MAX_ENTRIES:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][1])[:100]
            for k, _ in oldest:
                _cache.pop(k, None)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at BACKOFF_MAX_SECONDS."""
    base = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
    return base + random.uniform(0, base * 0.25)


_BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "cf-error-details",
    "cf-challenge",
    "access denied",
    "sorry, you have been blocked",
)


def _looks_blocked(html: str) -> bool:
    if not html:
        return False
    lowered = html[:4000].lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def fetch_rendered_html(
    url: str,
    wait_for_selector: Optional[str] = None,
    wait_until: str = "networkidle2",
    goto_timeout_ms: int = 30000,
    cookies: Optional[list] = None,
    use_cache: bool = True,
) -> str:
    """
    Fetch the fully-rendered HTML of `url` via Cloudflare's Browser
    Rendering /content endpoint.

    Raises:
        CloudflareConfigError — credentials not configured
        BlockedError          — target site served a bot-protection page
        RuntimeError          — other unrecoverable failure after retries
    """
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise CloudflareConfigError(
            "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN are not set. "
            "Create an API token with 'Browser Rendering - Edit' permission "
            "and set both environment variables."
        )

    if use_cache:
        cached = _cache_get(url)
        if cached is not None:
            log.info(f"[cache hit] {url}")
            return cached

    endpoint = _CONTENT_ENDPOINT.format(account_id=CLOUDFLARE_ACCOUNT_ID)
    payload: dict = {
        "url": url,
        "gotoOptions": {"waitUntil": wait_until, "timeout": goto_timeout_ms},
        # Skip assets we don't need — faster responses, less load on the
        # target and on our own Browser Rendering usage quota.
        "rejectResourceTypes": ["image", "media", "font"],
    }
    if wait_for_selector:
        payload["waitForSelector"] = {"selector": wait_for_selector, "timeout": 8000}
    if cookies:
        payload["cookies"] = cookies

    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}

    last_exc: Optional[Exception] = None
    attempts = MAX_RETRY + 1

    for attempt in range(1, attempts + 1):
        _throttle()
        try:
            resp = _session.post(
                endpoint, json=payload, headers=headers, timeout=HTTP_TIMEOUT_SECONDS
            )
        except requests.RequestException as e:
            last_exc = e
            log.warning(f"[attempt {attempt}/{attempts}] network error for {url}: {e}")
            if attempt < attempts:
                time.sleep(backoff_delay(attempt))
            continue

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 0) or 0)
            delay = max(retry_after, backoff_delay(attempt))
            log.warning(
                f"[attempt {attempt}/{attempts}] rate limited by Cloudflare API "
                f"for {url} — sleeping {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        if resp.status_code >= 500:
            last_exc = RuntimeError(f"Cloudflare API {resp.status_code}: {resp.text[:200]}")
            log.warning(f"[attempt {attempt}/{attempts}] server error for {url}: {last_exc}")
            if attempt < attempts:
                time.sleep(backoff_delay(attempt))
            continue

        if not resp.ok:
            # 4xx other than 429 (bad request, auth, disallowed) — retrying
            # won't help, fail fast instead of hammering the API.
            raise RuntimeError(f"Cloudflare API {resp.status_code}: {resp.text[:300]}")

        html = _extract_html(resp)
        if _looks_blocked(html):
            raise BlockedError(
                f"Target site served a bot-protection challenge page for {url}. "
                "Backing off rather than retrying through it."
            )

        if use_cache:
            _cache_set(url, html)
        return html

    raise RuntimeError(f"Failed to fetch {url} after {attempts} attempts: {last_exc}")


def _extract_html(resp: requests.Response) -> str:
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, dict):
            return result.get("html") or result.get("content") or ""
        if isinstance(result, str):
            return result
    return resp.text
