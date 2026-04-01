"""
scraper.py — LeetCode Interview Experience Scraper
Hosted on Railway | Self-scheduled every 4 hours via APScheduler
Endpoints: /list, /scrape-content (legacy), /run (manual trigger), /health

⚠️  Scraping logic (build_driver, scrape_post_detail, scrape_listing,
    is_today_strict, timestamp_to_sort_key) is UNCHANGED from the original.
    All pipeline orchestration lives in workflow.py.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
import threading
import uuid

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from flask import Flask, jsonify, request

from config import (
    LEETCODE_URL_1, LEETCODE_URL_2,
    MAX_POSTS_URL1, MAX_POSTS_URL2, MAX_POSTS_COMBINED,
    SCRAPE_DELAY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Lock for /run endpoint ────────────────────────────────────────────────────
_run_lock = threading.Lock()


# ── Selenium Driver (UNCHANGED) ───────────────────────────────────────────────

def build_driver(cookies: Optional[list] = None) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-background-networking")
    opts.page_load_strategy = "eager"
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.set_capability("pageLoadStrategy", "eager")
    opts.binary_location = "/usr/bin/google-chrome-stable"

    from selenium.webdriver.chrome.service import Service as ChromeService
    service = ChromeService(executable_path="/usr/bin/chromedriver")
    try:
        driver = webdriver.Chrome(service=service, options=opts)
    except Exception:
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(25)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )

    if cookies:
        driver.get("https://leetcode.com")
        time.sleep(0.5)
        for ck in cookies:
            try:
                driver.add_cookie(ck)
            except Exception as e:
                log.warning(f"Cookie inject failed: {e}")
        log.info(f"Injected {len(cookies)} cookies")

    return driver


def load_cookies_from_env() -> Optional[list]:
    raw = os.environ.get("LEETCODE_COOKIES", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.error(f"Failed to parse LEETCODE_COOKIES: {e}")
        return None


# ── Scraping Logic (UNCHANGED) ────────────────────────────────────────────────

def scrape_post_detail(driver: webdriver.Chrome, url: str) -> Optional[str]:
    """
    Scrape post content from LeetCode discuss post.
    Collects text from: p, ul, li, b, h1, h2, h3, h4, i tags
    inside div.break-words — preserves full structure.
    Limit 6000 chars for AI safety.
    """
    import re as _re
    try:
        driver.get(url)

        for sel in ["div.break-words", "div[class*='break-words']", "h1", "body"]:
            try:
                WebDriverWait(driver, 12).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                log.info(f"Post page loaded: {sel}")
                break
            except TimeoutException:
                continue

        time.sleep(1.5)
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
            body = driver.find_element(By.TAG_NAME, "body").text
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
            body = driver.find_element(By.TAG_NAME, "body").text
            return body[:6000].strip() if body else None
        except Exception:
            pass
        return None


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


def scrape_listing(driver: webdriver.Chrome, url: str, max_posts: int = 6) -> list:
    import re
    driver.get(url)

    waited = False
    for wait_sel in [
        "div.flex.flex-col.gap-4",
        "div[class*='topic-item']",
        "a[href*='/discuss/']",
        "div.overflow-hidden",
    ]:
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_sel))
            )
            log.info(f"Page loaded — wait selector matched: {wait_sel}")
            waited = True
            break
        except TimeoutException:
            continue

    if not waited:
        log.error("Timed out — no post cards found after all wait selectors")
        log.info("PAGE TITLE: " + driver.title)
        log.info("PAGE SNIPPET: " + driver.page_source[:2000])
        return []

    for _ in range(3):
        driver.execute_script("window.scrollBy(0, 400);")
        time.sleep(0.5)

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
            "swe", "rejected", "accepted", "reject", "accept", "oa"
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

        log.info(f"Scraping URL1: {LEETCODE_URL_1}")
        raw1 = scrape_listing(driver, LEETCODE_URL_1, max_posts=MAX_POSTS_URL1)
        log.info(f"URL1 returned {len(raw1)} posts")

        log.info(f"Scraping URL2: {LEETCODE_URL_2}")
        raw2 = scrape_listing(driver, LEETCODE_URL_2, max_posts=MAX_POSTS_URL2)
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

        for post in combined:
            posts.append({
                "post_id":   post_hash(post["url"]),
                "title":     post["title"],
                "timestamp": post["timestamp"],
                "post_url":  post["url"],
            })

        log.info(f"List cycle done — {len(posts)} combined posts")

    except Exception as e:
        log.exception(f"List cycle crashed: {e}")
        raise
    finally:
        if driver:
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
