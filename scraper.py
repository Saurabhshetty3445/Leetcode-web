
"""
scraper.py — LeetCode Interview Experience Scraper
Hosted on Railway | Self-scheduled every 4 hours via APScheduler
Endpoints: /list, /scrape-content (legacy), /run (manual trigger), /health

⚠️  Fetching engine: Scrapling's `StealthySession` (https://github.com/D4Vinci/Scrapling).
    Selenium + manual Cloudflare warm-up polling + hand-rolled stealth JS have been
    removed entirely — Scrapling's StealthyFetcher bypasses Cloudflare
    Turnstile/Interstitial, fingerprints, and headless-detection out of the box via
    `solve_cloudflare=True`. `ScraplingBrowser` below is a thin adapter that keeps the
    same `driver.get(url)` / `driver.page_source` / `driver.quit()` surface the rest of
    this codebase (workflow.py, links_workflow.py) already expects, so nothing outside
    this file needed to change.

    HTML parsing (BeautifulSoup selectors, keyword filters, date-extraction strategies,
    is_today_strict, timestamp_to_sort_key) is UNCHANGED from the original — only the
    fetch layer was swapped out.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional, Callable
import threading
import uuid
import multiprocessing as mp

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

from scrapling.fetchers import StealthySession

from config import (
    LEETCODE_URL_1, LEETCODE_URL_2,
    MAX_POSTS_URL1, MAX_POSTS_URL2, MAX_POSTS_COMBINED,
    SCRAPE_DELAY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Lock for pipeline runs — shared by BOTH the manual /run endpoint AND the
#    scheduler (see run_pipeline_isolated() + __main__ below). Previously
#    scraper.py and scheduler.py each had their OWN separate lock, so a
#    manual /run trigger and a scheduled cron run could execute concurrently.
#    Two Patchright/Playwright browser sessions racing inside the SAME shared
#    Node.js driver subprocess is exactly what produces CDP protocol
#    corruption ("Invalid InterceptionId") that hard-crashes the Node driver
#    — and once that driver process dies, EVERY future browser call in this
#    Python process fails forever (until the container restarts), which is
#    why the scraper appeared to "stop working and do nothing". One shared
#    lock makes that overlap impossible.
_run_lock = threading.Lock()

# Hard ceiling on how long a single pipeline run is allowed to take. If it
# hangs (e.g. a stuck browser wait) past this, the run is force-killed rather
# than holding the lock — and therefore blocking every future run — forever.
PIPELINE_TIMEOUT_SECONDS = int(os.environ.get("PIPELINE_TIMEOUT_SECONDS", "2700"))  # 45 min


# ── Scrapling-backed "driver" adapter ─────────────────────────────────────────
#
# The rest of this codebase (workflow.py, links_workflow.py) was written against
# a Selenium-style object: build_driver(cookies) -> driver, then
# driver.get(url) / driver.page_source / driver.title / driver.quit() repeatedly,
# reusing the same browser across many scrapes in one pipeline run.
#
# ScraplingBrowser gives them that exact surface while actually running on
# Scrapling's StealthySession under the hood, so workflow.py / links_workflow.py
# did not need to change at all.

class ScraplingBrowser:
    """
    Thin adapter around scrapling.fetchers.StealthySession that mimics just
    enough of the old Selenium WebDriver interface (.get, .page_source,
    .title, .quit) for the rest of the pipeline to keep working unmodified.
    """

    def __init__(self, cookies: Optional[list] = None):
        self._cookies = cookies or None
        self._session = StealthySession(
            headless=True,
            solve_cloudflare=True,     # bypasses Cloudflare Turnstile/Interstitial automatically
            real_chrome=False,         # bundled Chromium is fine; set True if a real Chrome is installed
            block_webrtc=True,
            hide_canvas=True,
            google_search=True,        # sets a Google referer — looks like organic traffic (cheap: header only)
            network_idle=False,        # see .get() — wait_selector already gates on real content;
                                        # network_idle waits for ALL network activity (ads/analytics
                                        # included) to go quiet, which was inflating every single
                                        # fetch's wall-clock time (→ billed CPU/memory-seconds on Railway)
            timeout=60000,             # generous timeout: CF challenge solving needs room to run
            cookies=self._cookies,
            # Cost-saving Chromium launch flags — the old Selenium build_driver()
            # shipped a long list of these (--single-process, --no-zygote,
            # --disable-gpu, etc.) to keep memory/CPU down on a small Railway
            # instance; Patchright's defaults don't apply any of that on their
            # own, so the browser was silently running "full fat" under Scrapling.
            extra_flags=[
                "--disable-gpu",
                "--disable-extensions",
                "--disable-software-rasterizer",
                "--disable-background-networking",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--memory-pressure-off",
                "--no-first-run",
                "--mute-audio",
            ],
        )
        self._session.__enter__()
        self.page = None   # last-fetched Scrapling Response, mirrors "current page"

    def get(
        self,
        url: str,
        wait_selector: Optional[str] = None,
        wait_selector_state: str = "attached",
        page_action: Optional[Callable] = None,
        **extra,
    ):
        """
        Navigate to `url`, wait for `wait_selector` (if given), and stash the
        response. Extra StealthFetchParams (network_idle, solve_cloudflare,
        timeout, wait, ...) can be passed per-call to override the session
        default — used by cheap/best-effort calls like
        find_leetcode_problem_url() that don't need the full CF-solving +
        network-idle treatment every single time.
        """
        self.page = self._session.fetch(
            url,
            wait_selector=wait_selector,
            wait_selector_state=wait_selector_state,
            page_action=page_action,
            **extra,
        )
        return self.page

    @property
    def page_source(self) -> bytes:
        """Raw HTML of the last-fetched page — BeautifulSoup accepts bytes directly."""
        if self.page is None:
            return b""
        return self.page.body

    @property
    def title(self) -> str:
        if self.page is None:
            return ""
        try:
            t = self.page.css("title::text").get()
            return t or ""
        except Exception:
            return ""

    def find_element_text(self) -> str:
        """Best-effort plain-text fallback, replacing Selenium's `body` text grab."""
        if self.page is None:
            return ""
        try:
            return self.page.get_all_text(strip=True)
        except Exception:
            try:
                return BeautifulSoup(self.page_source, "html.parser").get_text(" ", strip=True)
            except Exception:
                return ""

    def quit(self) -> None:
        try:
            self._session.__exit__(None, None, None)
        except Exception as e:
            log.warning(f"ScraplingBrowser.quit(): session close failed: {e}")


def build_driver(cookies: Optional[list] = None) -> ScraplingBrowser:
    """Build a Scrapling-backed browser session. Kept name/signature for compatibility."""
    return ScraplingBrowser(cookies)


def _normalize_cookies_for_playwright(cookies: list) -> list:
    """
    Playwright/Patchright's BrowserContext.add_cookies() is strict: every
    cookie dict MUST carry either a "url" or a "domain"+"path" pair, or the
    whole call raises. Selenium's driver.add_cookie() had no such requirement
    (it just used whatever domain the browser was currently on), so cookies
    exported from a browser extension or Selenium-era env var often only have
    {"name", "value"}. Backfill sane LeetCode defaults so those still work.
    """
    normalized = []
    for ck in cookies:
        if not isinstance(ck, dict) or "name" not in ck or "value" not in ck:
            log.warning(f"Skipping malformed cookie (needs name+value): {ck!r}")
            continue
        ck = dict(ck)  # don't mutate the caller's list
        if not ck.get("url") and not (ck.get("domain") and ck.get("path")):
            ck.setdefault("domain", ".leetcode.com")
            ck.setdefault("path", "/")
        normalized.append(ck)
    return normalized


def load_cookies_from_env() -> Optional[list]:
    """
    Reads LEETCODE_COOKIES from env — a JSON list of cookie dicts, e.g.:
    [{"name": "csrftoken", "value": "...", "domain": ".leetcode.com", "path": "/"}, ...]
    Bare {"name", "value"} pairs are also accepted — see
    _normalize_cookies_for_playwright() for the domain/path backfill.
    """
    raw = os.environ.get("LEETCODE_COOKIES", "")
    if not raw:
        return None
    try:
        cookies = json.loads(raw)
        if not isinstance(cookies, list):
            log.error("LEETCODE_COOKIES must be a JSON list of cookie dicts")
            return None
        return _normalize_cookies_for_playwright(cookies)
    except Exception as e:
        log.error(f"Failed to parse LEETCODE_COOKIES: {e}")
        return None


# ── Scraping logic — fetch layer now runs on Scrapling; extraction/filtering
#    logic (BeautifulSoup selectors, keyword rules, date parsing) UNCHANGED ──

def scrape_post_detail(driver: ScraplingBrowser, url: str) -> Optional[str]:
    """
    Scrape post content from LeetCode discuss post.
    Collects text from: p, ul, li, b, h1, h2, h3, h4, i tags
    inside div.break-words — preserves full structure.
    Limit 6000 chars for AI safety.
    """
    import re as _re
    try:
        # CSS supports comma-grouped selectors, so this waits for whichever of
        # these shows up first — same fallback chain Selenium polled one by one.
        driver.get(
            url,
            wait_selector="div.break-words, div[class*='break-words'], h1, body",
            wait_selector_state="attached",
            wait=500,   # small fixed settle instead of the costlier network_idle wait
        )
        log.info("Post page loaded")
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
            body = driver.find_element_text()
            lines = [body[:3000]]

        full_text = "\n".join(lines)
        full_text = _re.sub(r"\n{3,}", "\n\n", full_text).strip()

        if len(full_text) > 6000:
            full_text = full_text[:6000].strip() + "..."
            log.info("Truncated to 6000 chars")
        else:
            log.info(f"Full content: {len(full_text)} chars")

        return full_text if full_text else None

    except Exception as e:
        log.error(f"Detail scrape failed for {url}: {e}")
        try:
            body = driver.find_element_text()
            return body[:6000].strip() if body else None
        except Exception:
            pass
        return None


def scrape_post_detail_with_date(driver: ScraplingBrowser, url: str) -> tuple:
    """
    Scrape post content AND the real posting date from a LeetCode discuss post.

    Unlike scrape_post_date (which re-parses an already-loaded page and often
    misses the React-rendered <time> tag), this function:
      1. Navigates to the URL
      2. Waits for content (div.break-words) to load
      3. Explicitly waits up to 8s for a <time> element to appear
      4. Parses both content and timestamp from the same fully-rendered page

    Returns:
        (content: str | None, posted_on: str)
        posted_on is RFC 2822 format e.g. "Mon, 27 May 2026 08:30:00 GMT"
        Falls back to current UTC time only if no date found after full wait.
    """
    import re as _re
    from email.utils import formatdate as _fmtdate
    from datetime import datetime as _dt, timezone as _tz

    content   = None
    posted_on = None

    def _wait_for_time_tag(page) -> None:
        """
        Playwright page_action: explicitly wait for React to render the
        <time datetime="..."> element, since it often hydrates after the
        main container is already attached. Non-fatal if it never shows.
        """
        try:
            page.wait_for_selector("time[datetime]", timeout=8000, state="attached")
            log.info("scrape_post_detail_with_date: <time> element found")
        except Exception:
            log.warning("scrape_post_detail_with_date: <time> not found within 8s")

    try:
        # Single fetch: wait for the main container, running the <time>-tag
        # wait as a page_action along the way (see ScraplingBrowser.get()).
        driver.get(
            url,
            wait_selector="div.break-words, div[class*='break-words'], h1, body",
            wait_selector_state="attached",
            page_action=_wait_for_time_tag,
            wait=300,   # small settle on top of the explicit <time>-tag wait above
        )

        # ── Parse fully-rendered page ─────────────────────────────────────────
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
                m = _re.search(r"(\w{3,9}\s+\d{1,2},?\s+\d{4})", tooltip)
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
                m = _re.search(r"(\w{3,9}\s+\d{1,2},?\s+\d{4})", tooltip)
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
            try:
                body = driver.find_element_text()
                lines = [body[:3000]]
            except Exception:
                pass

        full_text = "\n".join(lines)
        full_text = _re.sub(r"\n{3,}", "\n\n", full_text).strip()
        if len(full_text) > 6000:
            full_text = full_text[:6000].strip() + "..."
        content = full_text if full_text else None

    except Exception as e:
        log.error(f"scrape_post_detail_with_date failed for {url}: {e}")

    # Fallback date only if nothing worked
    if not posted_on:
        posted_on = _fmtdate(usegmt=True)
        log.warning(f"scrape_post_detail_with_date: no date found — fallback: {posted_on}")

    return content, posted_on


def is_today_strict(timestamp: str) -> bool:
    import re
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
    import re
    from datetime import datetime as dt2, timedelta

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
            from datetime import datetime as dt2
            d = dt2.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return int(d.timestamp())
        except Exception:
            pass
    return 0


def post_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def _scroll_listing(page) -> None:
    """Playwright page_action: nudge the listing to trigger any lazy-rendered cards."""
    try:
        for _ in range(3):
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(500)
    except Exception as e:
        log.warning(f"scrape_listing: scroll action failed (non-fatal): {e}")


def scrape_listing(driver: ScraplingBrowser, url: str, max_posts: int = 6) -> list:
    import re

    waited = True
    fetch_err_msg = ""
    try:
        # CSS's comma-grouped-selector syntax replaces the old one-by-one
        # WebDriverWait polling loop — Scrapling waits for the first of these
        # to attach, and solve_cloudflare=True already handled any CF challenge.
        driver.get(
            url,
            wait_selector=(
                "div.flex.flex-col.gap-4, div[class*='topic-item'], "
                "a[href*='/discuss/'], div.overflow-hidden"
            ),
            wait_selector_state="attached",
            page_action=_scroll_listing,
        )
        log.info("Page loaded — listing selector matched")
    except Exception as e:
        # On a wait_selector timeout, driver.page never gets reassigned, so
        # don't trust driver.title/page_source here — they'd reflect a stale
        # previous fetch (or nothing at all). Just log the raw error.
        waited = False
        fetch_err_msg = str(e).lower()
        log.error(f"Timed out — no post cards found for {url}: {e}")

    if not waited:
        if "just a moment" in fetch_err_msg or "cloudflare" in fetch_err_msg or "timeout" in fetch_err_msg:
            # solve_cloudflare=True normally handles this on its own; reaching
            # here means the challenge genuinely won the round — bail and let
            # the next scheduled run (fresh browser/session) try again.
            log.error("Listing fetch failed/blocked — triggering redeploy")
            try:
                trigger_railway_redeploy()
            except Exception as _rd_err:
                log.error(f"Redeploy call failed: {_rd_err}")
        return []

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
            "swe", "rejected", "accepted", "reject", "accept", "l5","selected","select","sse","oa"
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
            import re
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
        time.sleep(SCRAPE_DELAY)

    posts.sort(key=lambda p: p["sort_key"], reverse=True)
    for p in posts:
        p.pop("sort_key", None)

    log.info(f"Returning {len(posts)} TODAY's interview posts (newest first)")
    for p in posts:
        log.info(f"  [{p['timestamp']}] {p['title']!r}")
    return posts


def find_leetcode_problem_url(driver: ScraplingBrowser, search_keyword: str) -> Optional[str]:
    """
    Search LeetCode's problem set for `search_keyword` and return the canonical
    /problems/<slug>/ URL of the first match, or None if nothing was found.

    Referenced by workflow.py STEP 8 (attaching a real LeetCode problem URL to
    each extracted problem) — added here since it previously had no
    implementation anywhere in the codebase.
    """
    import urllib.parse

    query = urllib.parse.quote(search_keyword.strip())
    search_url = f"https://leetcode.com/problemset/?search={query}"

    try:
        driver.get(
            search_url,
            wait_selector="a[href*='/problems/'], div[role='row']",
            wait_selector_state="attached",
            # Cheap/best-effort call: the session already solved Cloudflare on
            # an earlier fetch this run, so don't pay for full re-detection —
            # and cap wait time tighter since a miss here just means "no URL
            # found", not a broken pipeline.
            solve_cloudflare=False,
            timeout=20000,
        )
        soup = BeautifulSoup(driver.page_source, "html.parser")
        link = soup.select_one("a[href*='/problems/']")
        if link and link.get("href"):
            href = link["href"].split("/description")[0].rstrip("/") + "/"
            url = f"https://leetcode.com{href}" if href.startswith("/") else href
            log.info(f"find_leetcode_problem_url: {search_keyword!r} -> {url}")
            return url
        log.info(f"find_leetcode_problem_url: no match for {search_keyword!r}")
        return None
    except Exception as e:
        log.warning(f"find_leetcode_problem_url failed for {search_keyword!r}: {e}")
        return None


# ── List + content functions (used by workflow) ───────────────────────────────

def run_list_cycle() -> list:
    """
    Scrape listing pages and return post metadata list.
    Called by workflow.run_pipeline as list_fn.
    """
    cookies = load_cookies_from_env()
    driver  = None
    posts   = []

    try:
        driver = build_driver(cookies)

        # No manual Cloudflare warm-up/polling needed anymore — StealthySession's
        # solve_cloudflare=True handles the Turnstile/Interstitial challenge
        # internally on every fetch, including the very first navigation.
        log.info(f"Scraping URL1: {LEETCODE_URL_1}")
        raw1 = scrape_listing(driver, LEETCODE_URL_1, max_posts=MAX_POSTS_URL1)
        log.info(f"URL1 returned {len(raw1)} posts")

        log.info(f"Scraping URL2: {LEETCODE_URL_2}")
        raw2 = scrape_listing(driver, LEETCODE_URL_2, max_posts=MAX_POSTS_URL2)
        log.info(f"URL2 returned {len(raw2)} posts")

        # ── URL2 retry ────────────────────────────────────────────────────────
        # Even with solve_cloudflare=True, a listing fetch can occasionally come
        # back empty (transient block, slow hydration). One retry after a short
        # pause is cheap insurance before giving up on this cycle.
        if not raw2:
            log.warning("URL2 returned 0 posts — retrying once")
            time.sleep(2)
            log.info(f"Retrying URL2: {LEETCODE_URL_2}")
            raw2 = scrape_listing(driver, LEETCODE_URL_2, max_posts=MAX_POSTS_URL2)
            log.info(f"URL2 retry returned {len(raw2)} posts")

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

    except Exception as e:
        err_msg = str(e).lower()
        # Browser/session crash or OS resource exhaustion mid-run. Trigger one
        # redeploy to get a fresh container, then return [] so the pipeline
        # exits cleanly instead of crashing. Signals updated for Scrapling's
        # Playwright-based engine instead of Selenium/Chrome's error strings.
        _RENDERER_SIGNALS = [
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser closed",
            "connection closed",
            "session not created",
            "executable doesn't exist",
            "errno 11",
            "resource temporarily unavailable",
            "blockingioerror",
            "storage full",
            "device or resource busy",
        ]
        if any(sig in err_msg for sig in _RENDERER_SIGNALS):
            log.error(f"Renderer/storage crash in list cycle: {e} — triggering redeploy")
            try:
                trigger_railway_redeploy()
            except Exception as _rd:
                log.error(f"Redeploy call failed: {_rd}")
            return []
        log.exception(f"List cycle crashed: {e}")
        raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return posts


# ── Flask auth ────────────────────────────────────────────────────────────────

# ── Railway auto-redeploy (one-shot) ─────────────────────────────────────────
# Called when [Errno 11] / Chrome cannot start after all retries.
# A flag file ensures it fires ONLY ONCE per container lifetime — never loops.
# After redeploy, the new container has a clean FD/process table.
#
# Required Railway env vars:
#   RAILWAY_API_TOKEN  — from Railway dashboard → Account → Tokens
#   RAILWAY_SERVICE_ID — from Railway dashboard → Service → Settings

_REDEPLOY_FLAG = "/tmp/.railway_redeploy_triggered"


def trigger_railway_redeploy() -> bool:
    """
    Trigger one Railway redeploy via the Railway GraphQL API.
    Returns True if the call succeeded, False otherwise.
    Will never fire more than once per container lifetime.
    """
    if os.path.exists(_REDEPLOY_FLAG):
        log.info("Auto-redeploy already triggered this session — skipping")
        return False

    api_token  = os.environ.get("RAILWAY_API_TOKEN", "")
    service_id = os.environ.get("RAILWAY_SERVICE_ID", "")

    if not api_token or not service_id:
        log.warning(
            "Auto-redeploy skipped: RAILWAY_API_TOKEN or RAILWAY_SERVICE_ID not set. "
            "Add these to Railway env vars to enable auto-recovery."
        )
        return False

    query = """
    mutation serviceInstanceRedeploy($serviceId: String!) {
      serviceInstanceRedeploy(serviceId: $serviceId)
    }
    """
    try:
        resp = requests.post(
            "https://backboard.railway.com/graphql/v2",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type":  "application/json",
            },
            json={"query": query, "variables": {"serviceId": service_id}},
            timeout=15,
        )
        if resp.ok:
            open(_REDEPLOY_FLAG, "w").write("1")   # set one-shot flag
            log.info(f"🔄 Railway auto-redeploy triggered (service={service_id})")
            return True
        else:
            log.error(f"Railway redeploy API [{resp.status_code}]: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Railway redeploy request failed: {e}")
        return False


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
    if not _run_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "Main pipeline is using the browser — try again shortly", "posts": []}), 409
    try:
        posts  = run_list_cycle()
        result = {"status": "success", "count": len(posts), "posts": posts}
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "posts": []}), 500
    finally:
        _run_lock.release()


@app.route("/scrape-content", methods=["POST"])
def content_endpoint():
    """Legacy endpoint — scrape a single post URL and return raw text."""
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    body     = request.get_json(force=True, silent=True) or {}
    post_url = body.get("post_url", "").strip()

    if not post_url:
        return jsonify({"error": "Missing post_url in request body"}), 400

    if not _run_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "Main pipeline is using the browser — try again shortly", "content": ""}), 409

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
        _run_lock.release()


# ── Pipeline run state (in-memory, sufficient for single-process Railway) ─────
_pipeline_state: dict = {
    "status":     "idle",   # idle | running | done | error
    "run_id":     None,
    "started_at": None,
    "finished_at": None,
    "summary":    None,
}


def _pipeline_worker(result_queue: "mp.Queue", run_kind: str) -> None:
    """
    Runs the ENTIRE pipeline in its own OS process (see run_pipeline_isolated
    below). Patchright's Node.js driver process can hard-crash on protocol
    errors (e.g. "Invalid InterceptionId" from two browser sessions racing
    each other) — an uncaught crash there is unrecoverable for the rest of
    that Python process's lifetime. Running each pipeline execution in a
    fresh child process means such a crash only takes down that one child;
    the long-lived Flask/scheduler process is untouched, and the very next
    run gets a completely clean Node driver.
    """
    try:
        if run_kind == "scheduled":
            import supabase_client as db
            db.cleanup_old_post_ids()
        from workflow import run_pipeline
        summary = run_pipeline(list_fn=run_list_cycle, scrape_fn=scrape_post_detail)
        result_queue.put(("done", summary))
    except BaseException as e:
        # BaseException on purpose — a dying Node driver can surface as
        # unusual exceptions bubbling up through patchright's sync wrapper;
        # we still want to report it rather than let the child vanish silently.
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


def run_pipeline_isolated(run_kind: str, run_id: str) -> None:
    """
    Acquire _run_lock (caller must have already grabbed it — see below),
    execute the pipeline in a fresh child process with a hard timeout, update
    _pipeline_state, and ALWAYS release the lock + reap the child, whether
    the run succeeds, raises, times out, or the child process dies outright.
    """
    global _pipeline_state
    ctx = mp.get_context("spawn")   # spawn, not fork — never inherit a live Node driver
    result_queue: "mp.Queue" = ctx.Queue()
    proc = ctx.Process(target=_pipeline_worker, args=(result_queue, run_kind), daemon=True)
    proc.start()

    status, payload = None, None
    deadline = time.time() + PIPELINE_TIMEOUT_SECONDS
    try:
        while time.time() < deadline:
            if not result_queue.empty():
                status, payload = result_queue.get()
                break
            if not proc.is_alive():
                time.sleep(0.2)   # tiny grace period in case put() raced with exit
                if not result_queue.empty():
                    status, payload = result_queue.get()
                else:
                    status  = "crashed"
                    payload = f"Pipeline process exited unexpectedly (exitcode={proc.exitcode})"
                break
            time.sleep(1)
        else:
            status  = "timeout"
            payload = f"Pipeline exceeded {PIPELINE_TIMEOUT_SECONDS}s — force-killed"
            log.error(payload)
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
        result_queue.close()

        _pipeline_state.update({
            "status":      "done" if status == "done" else "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     payload,
        })
        log.info(f"[run_id={run_id}] Pipeline {status}: {payload}")
        _run_lock.release()


@app.route("/run", methods=["POST"])
def run_endpoint():
    """
    Manual pipeline trigger — fires async, returns immediately with run_id.
    Poll /run/status to check progress.
    Shares _run_lock with the scheduler (see __main__) so a manual trigger
    and a scheduled cron run can never execute concurrently.
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

    t = threading.Thread(target=run_pipeline_isolated, args=("manual", run_id), daemon=True)
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

    # Guard against overlapping with the main /run pipeline or the scheduler —
    # they'd otherwise launch a second, colliding browser session (see the
    # big comment on _run_lock near the top of this file).
    got_browser_slot = _run_lock.acquire(blocking=False)
    if not got_browser_slot:
        log.warning(f"[date-filter run_id={run_id}] Skipped — main pipeline is using the browser")
        _date_filter_state.update({
            "status":      "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     {"error": "Main pipeline was running — try again shortly"},
        })
        _date_filter_lock.release()
        return

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
            # No warm-up navigation needed — StealthySession solves Cloudflare
            # per-request via solve_cloudflare=True.

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
        _run_lock.release()


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

    # Same cross-pipeline browser guard as date-filter — see _run_lock comment.
    got_browser_slot = _run_lock.acquire(blocking=False)
    if not got_browser_slot:
        log.warning(f"[links run_id={run_id}] Skipped — main pipeline is using the browser")
        _links_state.update({
            "status":      "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary":     {"error": "Main pipeline was running — try again shortly"},
        })
        _links_lock.release()
        return

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
        _run_lock.release()


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
    from scheduler import start_scheduler

    def scheduled_pipeline():
        """
        Zero-arg wrapper used by the scheduler. Shares _run_lock with the
        manual /run endpoint (see comment on _run_lock above) and runs
        through the exact same isolated-subprocess path, so a scheduled run
        and a manual run can never execute concurrently and a crashed
        Node/browser driver can never take down the main process.
        """
        acquired = _run_lock.acquire(blocking=False)
        if not acquired:
            log.warning("Scheduled run skipped — a pipeline run is already in progress")
            return

        run_id = str(uuid.uuid4())[:8]
        _pipeline_state.update({
            "status":      "running",
            "run_id":      run_id,
            "started_at":  datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "summary":     None,
        })
        run_pipeline_isolated("scheduled", run_id)   # releases _run_lock itself when done

    scheduler = start_scheduler(scheduled_pipeline)

    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting Flask on port {port}")
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        scheduler.shutdown()
