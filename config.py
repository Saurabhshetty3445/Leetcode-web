"""
config.py — Centralized configuration from environment variables.
"""
import os

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")

# ── Gemini ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-3.5-flash-lite"

# ── Scraper auth ──────────────────────────────────────────────────────────────
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

# ── LeetCode targets ──────────────────────────────────────────────────────────
LEETCODE_URL_1    = "https://leetcode.com/discuss/topic/interview-experience/"
LEETCODE_URL_2    = "https://leetcode.com/discuss/topic/interview/"
MAX_POSTS_URL1    = 6
MAX_POSTS_URL2    = 8
MAX_POSTS_COMBINED = 12

# ── Pipeline tuning ───────────────────────────────────────────────────────────
SCRAPE_DELAY        = 1      # polite delay (seconds) between individual scrapes
MAX_RETRY           = int(os.environ.get("MAX_RETRY", "3"))   # retry attempts for scrape / gemini / json parse
POST_IDS_TTL_HOURS  = 24     # delete post_ids older than this
SCHEDULER_INTERVAL  = 4      # cron hours between runs
SCHEDULER_JITTER_SECONDS = int(os.environ.get("SCHEDULER_JITTER_SECONDS", "300"))
                              # randomize the scheduled run time by up to this many
                              # seconds so requests aren't perfectly periodic

# ── STEP 8: LeetCode problem-URL lookup ───────────────────────────────────────
# Each lookup is a full extra browser page-load (search LeetCode's problem set).
# Uncapped, this scales linearly with however many NEW problems a run finds —
# cap it so one unusually large run can't blow the day's compute budget.
ENABLE_PROBLEM_URL_LOOKUP      = os.environ.get("ENABLE_PROBLEM_URL_LOOKUP", "true").lower() == "true"
MAX_PROBLEM_URL_LOOKUPS_PER_RUN = int(os.environ.get("MAX_PROBLEM_URL_LOOKUPS_PER_RUN", "5"))
