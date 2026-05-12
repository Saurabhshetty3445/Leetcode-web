"""
leetcode_url_finder.py — Search LeetCode /problemset/ and return the first matching problem URL.

Flow:
  1. Navigate to https://leetcode.com/problemset/
  2. Type search_keyword into the search input
  3. Wait for results to appear
  4. Grab the first result's href
  5. Return full URL or None if not found

Used by workflow.py after Gemini extraction to enrich each problem with
its LeetCode problem URL before saving to the database.
"""
from __future__ import annotations

import time
from typing import Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup

from logger import get_logger

log = get_logger("url_finder")

PROBLEMSET_URL = "https://leetcode.com/problemset/"

# CSS selector for the search input — matches the exact input shown in the UI
_SEARCH_SELECTORS = [
    "input[placeholder='Search questions']",
    "input.rounded-full[placeholder*='Search']",
    "input[class*='rounded-full']",
    "input[placeholder*='search' i]",
]

# Result link selectors — problem links in the problemset table
_RESULT_SELECTORS = [
    "a[href*='/problems/']",
    "div[role='rowgroup'] a[href*='/problems/']",
    "table a[href*='/problems/']",
]


def find_leetcode_problem_url(
    driver: webdriver.Chrome,
    search_keyword: str,
    timeout: int = 10,
) -> Optional[str]:
    """
    Navigate to LeetCode problemset, search for keyword, return first result URL.

    Args:
        driver         : active Selenium Chrome driver (with cookies injected)
        search_keyword : keyword from Gemini (e.g. 'LRU Cache', 'Two Sum')
        timeout        : seconds to wait for search results

    Returns:
        Full LeetCode problem URL (e.g. 'https://leetcode.com/problems/lru-cache/')
        or None if not found / keyword is empty.
    """
    keyword = search_keyword.strip()
    if not keyword:
        log.info("URL finder: empty keyword — skipping")
        return None

    log.info(f"URL finder: searching for '{keyword}'")

    try:
        # ── Navigate to problemset ────────────────────────────────────────────
        driver.get(PROBLEMSET_URL)

        # Wait for page to load — search input must be present
        search_input = None
        for sel in _SEARCH_SELECTORS:
            try:
                search_input = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                log.info(f"URL finder: search input found via '{sel}'")
                break
            except TimeoutException:
                continue

        if not search_input:
            log.warning("URL finder: search input not found — skipping")
            return None

        # ── Clear any existing value and type keyword ─────────────────────────
        search_input.clear()
        time.sleep(0.3)
        search_input.send_keys(keyword)
        log.info(f"URL finder: typed '{keyword}'")

        # ── Wait for results to update ────────────────────────────────────────
        # LeetCode filters the table as you type — wait for at least one result
        time.sleep(2.5)   # debounce wait for React re-render

        # ── Parse the results page ────────────────────────────────────────────
        soup = BeautifulSoup(driver.page_source, "html.parser")

        problem_url = None
        for sel in _RESULT_SELECTORS:
            links = soup.select(sel)
            if links:
                href = links[0].get("href", "")
                if href.startswith("/problems/"):
                    problem_url = f"https://leetcode.com{href}"
                elif href.startswith("https://leetcode.com/problems/"):
                    problem_url = href
                if problem_url:
                    # Strip query params / fragments — keep clean URL
                    problem_url = problem_url.split("?")[0].split("#")[0]
                    if not problem_url.endswith("/"):
                        problem_url += "/"
                    log.info(f"URL finder: found → {problem_url}")
                    break

        if not problem_url:
            log.warning(f"URL finder: no results for '{keyword}'")

        return problem_url

    except Exception as e:
        log.error(f"URL finder error for '{keyword}': {e}")
        return None
