"""
scraper.py — LeetCode Interview Experience Scraper
Hosted on Railway | Self-scheduled every 4 hours via APScheduler
Endpoints: /list, /scrape-content (legacy), /run (manual trigger), /health

Page fetching is done through Cloudflare's managed Browser Rendering API
(see cloudflare_browser_client.py) instead of a local, fingerprint-spoofed
headless Chrome. That means:
  - no local browser process to crash, leak file descriptors, or OOM
  - no anti-detection/stealth JS — requests are honestly identifiable
  - if the target site's bot protection blocks a request, it just fails
    (BlockedError) and this module backs off — it does not retry through
    it or rotate infrastructure to dodge the block
All pipeline orchestration lives in workflow.py.
"""

import os
import re
import json
import time
import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

from cloudflare_browser_client import (
    fetch_rendered_html,
    backoff_delay,
    BlockedError,
    CloudflareConfigError,
)
from config import (
    LEETCODE_URL_1, LEETCODE_URL_2,
    MAX_POSTS_URL1, MAX_POSTS_URL2, MAX_POSTS_COMBINED,
    SCRAPE_DELAY, MAX_RETRY,
)
from logger import get_logger

log = get_logger("scraper")

app = Flask(__name__)

# ── Lock for /run endpoint ────────────────────────────────────────────────────
_run_lock = threading.Lock()


# ── Rendered-page adapter ─────────────────────────────────────────────────────
# Thin object exposing just the handful of things the rest of this codebase
# (and workflow.py / links_workflow.py) actually used from the old Selenium
# `webdriver.Chrome` object: get(), page_source, title, a body-text getter,
# and quit(). Backing it with Cloudflare's Browser Rendering API means every
# .get() call is a fresh, independent, fully-rendered fetch — there is no
# persistent browser session to keep alive or warm up between calls.

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class RenderedPage:
    def __init__(self, cookies: Optional[list] = None):
        self._cookies = cookies
        self.page_source = ""
        self.title = ""

    def get(self, url: str, wait_for_selector: Optional[str] = None) -> None:
        html = fetch_rendered_html(
            url,
            wait_for_selector=wait_for_selector,
            cookies=self._cookies,
        )
        self.page_source = html
        m = _TITLE_RE.search(html or "")
        self.title = m.group(1).strip() if m else ""

    def body_text(self, limit: int = 3000) -> str:
        soup = BeautifulSoup(self.page_source or "", "html.parser")
        body = soup.find("body")
        text = body.get_text(" ", strip=True) if body else ""
        return text[:limit]

    def quit(self) -> None:
        # No persistent process/session to tear down.
        return None


def build_driver(cookies: Optional[list] = None) -> RenderedPage:
    """
    Returns a RenderedPage backed by Cloudflare's Browser Rendering API.
    `cookies` (if provided via LEETCODE_COOKIES) is forwarded as-is to the
    API's own `cookies` parameter for requests that need an authenticated
    session — it is not used to fake or hide anything.
    """
    return RenderedPage(cookies=cookies)


def load_cookies_from_env() -> Optional[list]:
    raw = os.environ.get("LEETCODE_COOKIES", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.error(f"Failed to parse LEETCODE_COOKIES: {e}")
        return None


# ── Scraping logic ────────────────────────────────────────────────────────────

def scrape_post_detail(driver: RenderedPage, url: str) -> Optional[str]:
    """
    Scrape post content from a LeetCode discuss post.
    Collects text from: p, ul, li, b, h1, h2, h3, h4, i tags
    inside div.break-words — preserves full structure.
    Limit 6000 chars for AI safety.
    """
    try:
        driver.get(url, wait_for_selector="div.break-words")
    except BlockedError as e:
        log.warning(f"Detail scrape blocked for {url}: {e}")
        return None
    except Exception as e:
        log.error(f"Detail scrape failed for {url}: {e}")
        return None

    soup = BeautifulSoup(driver.page_source, "html.parser")
    for tag in soup.select("nav, footer, header, script, style, aside"):
        tag.decompose()

    CONTENT_TAGS = ["p", "ul", "li", "b", "h1", "h2", "h3", "h4", "i", "span"]
    lines = []

    def extract_from_container(container):
        for tag in container.find_all(CONTENT_TAGS):
            text = tag.get_text(separator=" ", strip=True)
            if text and len(text) > 1:
                if tag.name in ["h1", "h2", "h3", "h4"]:
                    lines.append(f"[{tag.name.upper()}] {text}")
                elif tag.name == "li":
                    lines.append(f"- {text}")
                else:
                    lines.append(text)

    container = soup.select_one("div.break-words")
    if container:
        log.info("Primary container div.break-words found")
        extract_from_container(container)

    if not lines:
        log.warning("Primary empty — trying break-words class fallback")
        container = soup.find("div", class_=lambda c: c and "break-words" in c)
        if container:
            extract_from_container(container)

    if not lines:
        log.warning("Trying full page content tags")
        extract_from_container(soup)

    if not lines:
        log.warning("Using body text fallback")
        lines = [driver.body_text()]

    full_text = "\n".join(lines)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

    if len(full_text) > 6000:
        full_text = full_text[:6000].strip() + "..."
        log.info("Truncated to 6000 chars")
    else:
        log.info(f"Full content: {len(full_text)} chars")

    return full_text if full_text else None


def scrape_post_detail_with_date(driver: RenderedPage, url: str) -> tuple:
    """
    Scrape post content AND the real posting date from a LeetCode discuss post.

    Waits for div.break-words to be present (via Cloudflare's
    waitForSelector + networkidle2) so the React app has finished
    hydrating before we parse — this covers both the content and the
    <time> element in a single fetch.

    Returns:
        (content: str | None, posted_on: str)
        posted_on is RFC 2822 format e.g. "Mon, 27 May 2026 08:30:00 GMT"
        Falls back to current UTC time only if no date found.
    """
    from email.utils import formatdate as _fmtdate
    from datetime import datetime as _dt, timezone as _tz

    content   = None
    posted_on = None

    try:
        driver.get(url, wait_for_selector="div.break-words")
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # ── Extract date ──────────────────────────────────────────────────────
        # Strategy 1: <time datetime="..."> — most reliable
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            dt_str = time_tag["datetime"].strip()
            log.info(f"scrape_post_detail_with_date: raw datetime attr = {dt_str!r}")
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d",
            ):
                try:
                    cleaned = dt_str.rstrip("Z").split("+")[0].split("-0")[0]
                    dt = _dt.strptime(cleaned, fmt.rstrip("Z"))
                    dt = dt.replace(tzinfo=_tz.utc)
                    posted_on = _fmtdate(dt.timestamp(), usegmt=True)
                    log.info(f"scrape_post_detail_with_date: date = {posted_on}")
                    break
                except ValueError:
                    continue

        # Strategy 2: find all <time> tags by JS-rendered text (e.g. "2 hours ago")
        if not posted_on:
            for t in soup.find_all("time"):
                tooltip = t.get("title", "") or t.get("data-tooltip", "")
                m = re.search(r"(\w{3,9}\s+\d{1,2},?\s+\d{4})", tooltip)
                if m:
                    try:
                        dt = _dt.strptime(m.group(1).replace(",", ""), "%B %d %Y")
                        dt = dt.replace(tzinfo=_tz.utc)
                        posted_on = _fmtdate(dt.timestamp(), usegmt=True)
                        log.info(f"scrape_post_detail_with_date: date from title attr = {posted_on}")
                        break
                    except ValueError:
                        pass

        # Strategy 3: data-tooltip on any element with a month name
        if not posted_on:
            for el in soup.find_all(attrs={"data-tooltip": True}):
                tooltip = el["data-tooltip"]
                m = re.search(r"(\w{3,9}\s+\d{1,2},?\s+\d{4})", tooltip)
                if m:
                    try:
                        dt = _dt.strptime(m.group(1).replace(",", ""), "%B %d %Y")
                        dt = dt.replace(tzinfo=_tz.utc)
                        posted_on = _fmtdate(dt.timestamp(), usegmt=True)
                        log.info(f"scrape_post_detail_with_date: date from data-tooltip = {posted_on}")
                        break
                    except ValueError:
                        pass

        # ── Extract content (same logic as scrape_post_detail) ────────────────
        for tag in soup.select("nav, footer, header, script, style, aside"):
            tag.decompose()

        CONTENT_TAGS = ["p", "ul", "li", "b", "h1", "h2", "h3", "h4", "i", "span"]
        lines = []

        def extract_from(container):
            for tag in container.find_all(CONTENT_TAGS):
                text = tag.get_text(separator=" ", strip=True)
                if text and len(text) > 1:
                    if tag.name in ["h1", "h2", "h3", "h4"]:
                        lines.append(f"[{tag.name.upper()}] {text}")
                    elif tag.name == "li":
                        lines.append(f"- {text}")
                    else:
                        lines.append(text)

        container = soup.select_one("div.break-words")
        if container:
            extract_from(container)
        if not lines:
            container = soup.find("div", class_=lambda c: c and "break-words" in c)
            if container:
                extract_from(container)
        if not lines:
            extract_from(soup)
        if not lines:
            lines = [driver.body_text()]

        full_text = "\n".join(lines)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()
        if len(full_text) > 6000:
            full_text = full_text[:6000].strip() + "..."
        content = full_text if full_text else None

    except BlockedError as e:
        log.warning(f"scrape_post_detail_with_date blocked for {url}: {e}")
    except Exception as e:
        log.error(f"scrape_post_detail_with_date failed for {url}: {e}")

    # Fallback date only if nothing worked
    if not posted_on:
        posted_on = _fmtdate(usegmt=True)
        log.warning(f"scrape_post_detail_with_date: no date found — fallback: {posted_on}")

    return content, posted_on


def is_today_strict(timestamp: str) -> bool:
    t = timestamp.strip().lower()

    if not t:
        return False
    if re.search(r"[a-z]{3}\s+\d{1,2},?\s+\d{4}", t):
        return False
    if "yesterday" in t:
        return False
    if "week" in t or "month" in t or "year" in t:
        return False
    day_m = re.search(r"(\d+)\s+day", t)
    if day_m:
        return False
    if "just now" in t:
        return True
    if "a few seconds" in t:
        return True
    if re.match(r"^a\s+minute", t):
        return True
    if re.match(r"^a\s+second", t):
        return True
    if re.match(r"^an?\s+hour", t):
        return True
    sec_m = re.search(r"(\d+)\s+second", t)
    if sec_m:
        return True
    min_m = re.search(r"(\d+)\s+minute", t)
    if min_m:
        n = int(min_m.group(1))
        return 1 <= n <= 59
    hr_m = re.search(r"(\d+)\s+hour", t)
    if hr_m:
        n = int(hr_m.group(1))
        return 1 <= n <= 23
    return False


def timestamp_to_sort_key(timestamp: str) -> int:
    from datetime import timedelta

    t   = timestamp.strip().lower()
    now = datetime.now(timezone.utc)

    if not t:
        return 0
    m = re.search(r"(\d+)\s+minute", t)
    if m:
        return int((now - timedelta(minutes=int(m.group(1)))).timestamp())
    m = re.search(r"(\d+)\s+hour", t)
    if m:
        return int((now - timedelta(hours=int(m.group(1)))).timestamp())
    m = re.search(r"(\d+)\s+day", t)
    if m:
        return int((now - timedelta(days=int(m.group(1)))).timestamp())
    if "just now" in t or "second" in t:
        return int(now.timestamp())
    if "yesterday" in t:
        return int((now - timedelta(days=1)).timestamp())
    m = re.search(r"([a-z]{3})\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return int(d.timestamp())
        except Exception:
            pass
    return 0


def post_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def scrape_listing(driver: RenderedPage, url: str, max_posts: int = 6) -> list:
    """
    Fetch and parse a LeetCode discuss listing page.

    Note: unlike the old live-browser version, this does not scroll to
    trigger additional lazy-loaded items — Cloudflare's renderer returns
    whatever is present after the page network-idles. If LeetCode's
    listing depends on scroll-triggered loading for more than the initial
    batch, max_posts effectively caps at what's rendered up front.
    """
    driver.get(url, wait_for_selector="a[href*='/discuss/']")
    soup = BeautifulSoup(driver.page_source, "html.parser")

    containers = soup.select("a[href*='/discuss/'][class*='no-underline']")

    if not containers:
        log.warning("Selector 1 empty, trying selector 2")
        containers = [
            a for a in soup.find_all("a", href=True)
            if re.search(r"/discuss/\d+/", a.get("href", ""))
        ]

    if not containers:
        log.warning("Selector 2 empty, trying selector 3")
        containers = [
            a for a in soup.find_all("a", href=True)
            if "/discuss/" in a.get("href", "") and len(a.get_text(strip=True)) > 10
        ]

    if not containers:
        log.warning(f"No post cards found for {url}")
        log.info("PAGE TITLE: " + driver.title)
        return []

    log.info(f"Raw containers found: {len(containers)}")

    posts      = []
    seen_urls  = set()

    for el in containers[: max_posts * 5]:
        if len(posts) >= max_posts:
            break

        href = el.get("href", "")
        post_url = f"https://leetcode.com{href}" if href.startswith("/") else href

        if not post_url or post_url in seen_urls:
            continue
        if "/discuss/topic/" in post_url or post_url in (LEETCODE_URL_1, LEETCODE_URL_2):
            continue
        seen_urls.add(post_url)

        title = ""
        for title_sel in [
            "div.text-sd-foreground.line-clamp-1",
            "div[class*='line-clamp-1']",
            "p[class*='line-clamp-1']",
            "span[class*='line-clamp-1']",
        ]:
            t = el.select_one(title_sel)
            if t:
                title = t.get_text(strip=True)
                break

        if not title:
            candidates = [
                tag.get_text(strip=True)
                for tag in el.find_all(["div", "p", "span", "h3"])
                if len(tag.get_text(strip=True)) > 10
            ]
            title = max(candidates, key=len) if candidates else el.get_text(strip=True)[:120]

        if not title:
            continue

        log.info(f"Post found: {title!r}")

        if not any(kw in title.lower() for kw in [
            "interview", "experience", "sde", "questions", "question",
            "swe", "rejected", "accepted", "reject", "accept", "l5", "selected", "select", "sse", "oa"
        ]):
            log.info(f"Skipping — no keyword match: {title!r}")
            continue

        description = ""
        for desc_sel in [
            "div.text-sd-muted-foreground.line-clamp-2",
            "div[class*='line-clamp-2']",
            "p[class*='line-clamp-2']",
        ]:
            d = el.select_one(desc_sel)
            if d:
                description = d.get_text(strip=True)
                break

        timestamp = ""
        for ts_sel in [
            "span[data-state='closed']",
            "span[class*='text-sd-muted']",
            "span[class*='time']",
            "time",
        ]:
            t = el.select_one(ts_sel)
            if t:
                timestamp = t.get("datetime", "") or t.get_text(strip=True)
                break

        if not timestamp:
            full_text = el.get_text(" ", strip=True)
            m = re.search(
                r"(\d+\s+(?:minute|hour|day|week|month)s?\s+ago|just now|yesterday)",
                full_text, re.I,
            )
            if m:
                timestamp = m.group(1)

        log.info(f"Timestamp: {timestamp!r}")

        if not is_today_strict(timestamp):
            log.info(f"Skipping — not today ({timestamp!r}): {title!r}")
            continue

        posts.append({
            "url":         post_url,
            "title":       title,
            "description": description,
            "timestamp":   timestamp,
            "sort_key":    timestamp_to_sort_key(timestamp),
        })

    posts.sort(key=lambda p: p["sort_key"], reverse=True)
    for p in posts:
        p.pop("sort_key", None)

    log.info(f"Returning {len(posts)} TODAY's interview posts (newest first)")
    for p in posts:
        log.info(f"  [{p['timestamp']}] {p['title']!r}")
    return posts


def _scrape_listing_with_retry(driver: RenderedPage, url: str, max_posts: int) -> list:
    """Bounded retry with exponential backoff — never retries through a
    confirmed block, just logs it and gives up for this cycle."""
    attempts = MAX_RETRY + 1
    for attempt in range(1, attempts + 1):
        try:
            return scrape_listing(driver, url, max_posts=max_posts)
        except BlockedError as e:
            log.warning(f"[attempt {attempt}/{attempts}] {url} blocked: {e}")
        except Exception as e:
            log.warning(f"[attempt {attempt}/{attempts}] {url} failed: {e}")
        if attempt < attempts:
            time.sleep(backoff_delay(attempt))
    log.error(f"Giving up on listing {url} after {attempts} attempts")
    return []


# ── List + content functions (used by workflow) ───────────────────────────────

def run_list_cycle() -> list:
    """
    Scrape listing pages and return post metadata list.
    Called by workflow.run_pipeline as list_fn.

    Every call to Cloudflare's Browser Rendering API is an independent,
    fully-rendered fetch — there's no persistent browser session to warm
    up beforehand, so each listing page is fetched (with its own retry +
    backoff) directly.
    """
    cookies = load_cookies_from_env()
    driver  = build_driver(cookies)
    posts   = []

    try:
        log.info(f"Scraping URL1: {LEETCODE_URL_1}")
        raw1 = _scrape_listing_with_retry(driver, LEETCODE_URL_1, MAX_POSTS_URL1)
        log.info(f"URL1 returned {len(raw1)} posts")

        log.info(f"Scraping URL2: {LEETCODE_URL_2}")
        raw2 = _scrape_listing_with_retry(driver, LEETCODE_URL_2, MAX_POSTS_URL2)
        log.info(f"URL2 returned {len(raw2)} posts")

        seen_urls = set()
        combined  = []
        for post in raw1 + raw2:
            if post["url"] not in seen_urls:
                seen_urls.add(post["url"])
                combined.append(post)

        combined.sort(
            key=lambda p: timestamp_to_sort_key(p.get("timestamp", "")),
            reverse=True,
        )
        combined = combined[:MAX_POSTS_COMBINED]

        from email.utils import formatdate as _rfc_fmt
        for post in combined:
            # Convert relative string (e.g. "2 hours ago") to absolute
            # RFC 2822 format (e.g. "Wed, 02 Apr 2026 07:30:00 GMT")
            epoch = timestamp_to_sort_key(post["timestamp"])
            abs_ts = _rfc_fmt(epoch, usegmt=True) if epoch else _rfc_fmt(usegmt=True)
            posts.append({
                "post_id":   post_hash(post["url"]),
                "title":     post["title"],
                "timestamp": abs_ts,
                "post_url":  post["url"],
            })

        log.info(f"List cycle done — {len(posts)} combined posts")

    except CloudflareConfigError:
        raise
    except Exception as e:
        log.exception(f"List cycle crashed: {e}")
        raise
    finally:
        driver.quit()

    return posts


# ── Flask auth ────────────────────────────────────────────────────────────────

def auth_check() -> bool:
    api_key  = request.headers.get("X-API-Key", "")
    expected = os.environ.get("SCRAPER_API_KEY", "")
    return not expected or api_key == expected


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/list", methods=["GET", "POST"])
def list_endpoint():
    """Legacy endpoint — returns post list (no pipeline execution)."""
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        posts  = run_list_cycle()
        result = {"status": "success", "count": len(posts), "posts": posts}
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "posts": []}), 500


@app.route("/scrape-content", methods=["POST"])
def content_endpoint():
    """Legacy endpoint — scrape a single post URL and return raw text."""
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    body     = request.get_json(force=True, silent=True) or {}
    post_url = body.get("post_url", "").strip()

    if not post_url:
        return jsonify({"error": "Missing post_url in request body"}), 400

    cookies = load_cookies_from_env()
    driver  = None
    try:
        driver    = build_driver(cookies)
        post_text = scrape_post_detail(driver, post_url)
        if post_text is None:
            return jsonify({"status": "error", "message": "Could not scrape", "content": ""}), 500
        return jsonify({"status": "success", "post_url": post_url, "content": post_text}), 200
    except Exception as e:
        log.exception(f"Content scrape crashed: {e}")
        return jsonify({"status": "error", "message": str(e), "content": ""}), 500
    finally:
        if driver:
            driver.quit()


# ── Pipeline run state (in-memory, sufficient for single-process Railway) ─────
_pipeline_state: dict = {
    "status":     "idle",   # idle | running | done | error
    "run_id":     None,
    "started_at": None,
    "finished_at": None,
    "summary":    None,
}


def _execute_pipeline_bg(run_id: str) -> None:
    """Background thread target — runs the full pipeline and updates state."""
    global _pipeline_state
    try:
        from workflow import run_pipeline
        summary = run_pipeline(
            list_fn   = run_list_cycle,
            scrape_fn = scrape_post_detail,
        )
        _pipeline_state.update({
            "status":      "done",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     summary,
        })
        log.info(f"[run_id={run_id}] Pipeline finished: {summary}")
    except Exception as e:
        log.exception(f"[run_id={run_id}] Pipeline crashed: {e}")
        _pipeline_state.update({
            "status":      "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     {"error": str(e)},
        })
    finally:
        _run_lock.release()


@app.route("/run", methods=["POST"])
def run_endpoint():
    """
    Manual pipeline trigger — fires async, returns immediately with run_id.
    Poll /run/status to check progress.
    Prevents overlapping runs via lock.
    """
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    acquired = _run_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({
            "status":  "busy",
            "message": "Pipeline already running",
            "run_id":  _pipeline_state.get("run_id"),
        }), 409

    run_id = str(uuid.uuid4())[:8]
    _pipeline_state.update({
        "status":      "running",
        "run_id":      run_id,
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "summary":     None,
    })

    t = threading.Thread(target=_execute_pipeline_bg, args=(run_id,), daemon=True)
    t.start()

    log.info(f"[run_id={run_id}] Pipeline started in background")
    return jsonify({
        "status":     "started",
        "run_id":     run_id,
        "message":    "Pipeline running in background. Poll /run/status for result.",
        "status_url": "/run/status",
    }), 202


@app.route("/run/status", methods=["GET"])
def run_status_endpoint():
    """Poll this after calling /run to get pipeline progress and final summary."""
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(_pipeline_state), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


# ── Date filter pipeline ──────────────────────────────────────────────────────
# POST /date-filter/<date>   → find all problems with that posted_on date,
#                              scrape each unique post_url to get the real date,
#                              update posted_on on all matching problems
# GET  /date-filter/status   → poll for result

_date_filter_lock = threading.Lock()
_date_filter_state: dict = {
    "status":     "idle",
    "run_id":     None,
    "date":       None,
    "started_at": None,
    "finished_at": None,
    "summary":    None,
}


def _execute_date_filter_bg(run_id: str, date_str: str) -> None:
    """Background thread — runs the date filter correction pipeline."""
    global _date_filter_state
    try:
        import supabase_client as _db
        from links_workflow import scrape_post_date

        log.info(f"[date-filter run_id={run_id}] Starting for date: {date_str!r}")

        # ── 1. Fetch all problems with this posted_on date ────────────────────
        problems = _db.get_problems_by_posted_on(date_str)
        log.info(f"[date-filter] Found {len(problems)} problem(s) matching {date_str!r}")

        if not problems:
            _date_filter_state.update({
                "status":      "done",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "date":             date_str,
                    "problems_found":   0,
                    "links_found":      0,
                    "links_scraped":    0,
                    "problems_updated": 0,
                    "errors":           [],
                },
            })
            return

        # ── 2. Collect unique post_urls ───────────────────────────────────────
        unique_urls = list({p["post_url"] for p in problems if p.get("post_url")})
        log.info(f"[date-filter] Unique post URLs: {len(unique_urls)}")

        summary = {
            "date":             date_str,
            "problems_found":   len(problems),
            "links_found":      len(unique_urls),
            "links_scraped":    0,
            "problems_updated": 0,
            "errors":           [],
        }

        cookies = load_cookies_from_env()
        driver  = None

        try:
            driver = build_driver(cookies)

            # Warm-up
            driver.get("https://leetcode.com")
            time.sleep(3)

            for i, post_url in enumerate(unique_urls, 1):
                log.info(f"[date-filter] Scraping [{i}/{len(unique_urls)}]: {post_url}")
                try:
                    # Use combined scrape — waits for React <time> before parsing
                    _, real_date = scrape_post_detail_with_date(driver, post_url)
                    log.info(f"[date-filter] Real date: {real_date}")
                    summary["links_scraped"] += 1

                    # Update all problems with this post_url
                    updated = _db.update_posted_on_by_post_url(post_url, real_date)
                    summary["problems_updated"] += updated
                    log.info(f"[date-filter] Updated {updated} problem(s) for {post_url}")

                except Exception as e:
                    log.error(f"[date-filter] Failed for {post_url}: {e}")
                    summary["errors"].append(f"fail:{post_url}:{str(e)[:80]}")

                time.sleep(1)

        finally:
            if driver:
                driver.quit()
                log.info("[date-filter] Driver closed")

        _date_filter_state.update({
            "status":      "done",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     summary,
        })
        log.info(f"[date-filter run_id={run_id}] Complete: {summary}")

    except Exception as e:
        log.exception(f"[date-filter run_id={run_id}] Crashed: {e}")
        _date_filter_state.update({
            "status":      "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     {"error": str(e)},
        })
    finally:
        _date_filter_lock.release()


@app.route("/date-filter/<path:date_str>", methods=["POST"])
def date_filter_endpoint(date_str: str):
    """
    Trigger posted_on correction for all problems matching a date string.
    date_str: any partial date e.g. '2026-05-27', 'May 27', 'Mon, 27 May 2026'
    Returns 202 immediately — poll /date-filter/status for result.
    Returns 409 if already running.
    """
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    date_str = date_str.strip()
    if not date_str:
        return jsonify({"error": "Date string required in URL"}), 400

    acquired = _date_filter_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({
            "status":  "busy",
            "message": "Date filter already running",
            "run_id":  _date_filter_state.get("run_id"),
        }), 409

    run_id = str(uuid.uuid4())[:8]
    _date_filter_state.update({
        "status":      "running",
        "run_id":      run_id,
        "date":        date_str,
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "summary":     None,
    })

    t = threading.Thread(
        target=_execute_date_filter_bg,
        args=(run_id, date_str),
        daemon=True,
    )
    t.start()

    log.info(f"[date-filter run_id={run_id}] Started for date: {date_str!r}")
    return jsonify({
        "status":     "started",
        "run_id":     run_id,
        "date":       date_str,
        "message":    "Date filter running. Poll /date-filter/status for result.",
        "status_url": "/date-filter/status",
    }), 202


@app.route("/date-filter/status", methods=["GET"])
def date_filter_status_endpoint():
    """Poll this after POST /date-filter/<date> to get progress and summary."""
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(_date_filter_state), 200


# ── Manual links batch pipeline ───────────────────────────────────────────────
# Separate from /run — processes URLs stored in leetcode_links Supabase table.
# POST /process-links       → starts batch, returns 202 with run_id
# GET  /process-links/status → poll for result

_links_lock = threading.Lock()
_links_state: dict = {
    "status":     "idle",
    "run_id":     None,
    "started_at": None,
    "finished_at": None,
    "summary":    None,
}


def _execute_links_bg(run_id: str) -> None:
    """Background thread — runs the links batch pipeline."""
    global _links_state
    try:
        from links_workflow import run_links_pipeline
        summary = run_links_pipeline(scrape_fn=scrape_post_detail)
        _links_state.update({
            "status":      "done",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     summary,
        })
        log.info(f"[links run_id={run_id}] Batch finished: {summary}")
    except Exception as e:
        log.exception(f"[links run_id={run_id}] Batch crashed: {e}")
        _links_state.update({
            "status":      "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     {"error": str(e)},
        })
    finally:
        _links_lock.release()


@app.route("/process-links", methods=["POST"])
def process_links_endpoint():
    """
    Trigger batch processing of pending links in leetcode_links table.
    Returns 202 immediately — poll /process-links/status for result.
    Returns 409 if a batch is already running.
    """
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    acquired = _links_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({
            "status":  "busy",
            "message": "Links batch already running",
            "run_id":  _links_state.get("run_id"),
        }), 409

    run_id = str(uuid.uuid4())[:8]
    _links_state.update({
        "status":      "running",
        "run_id":      run_id,
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "summary":     None,
    })

    t = threading.Thread(target=_execute_links_bg, args=(run_id,), daemon=True)
    t.start()

    log.info(f"[links run_id={run_id}] Links batch started in background")
    return jsonify({
        "status":     "started",
        "run_id":     run_id,
        "message":    "Links batch running. Poll /process-links/status for result.",
        "status_url": "/process-links/status",
    }), 202


@app.route("/process-links/status", methods=["GET"])
def process_links_status_endpoint():
    """Poll this after POST /process-links to get batch progress and summary."""
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(_links_state), 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from workflow import run_pipeline
    from scheduler import start_scheduler
    import supabase_client as db

    def scheduled_pipeline():
        """Zero-arg wrapper used by the scheduler."""
        db.cleanup_old_post_ids()
        return run_pipeline(
            list_fn   = run_list_cycle,
            scrape_fn = scrape_post_detail,
        )

    scheduler = start_scheduler(scheduled_pipeline)

    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting Flask on port {port}")
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        scheduler.shutdown()
