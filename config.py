"""
config.py — Centralized configuration from environment variables.
"""
import os

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")

# ── Gemini ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-3.1-flash-lite-preview"

# ── Scraper auth ──────────────────────────────────────────────────────────────
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

# ── Bot-check email alert ─────────────────────────────────────────────────────
ALERT_EMAIL_FROM     = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO       = os.environ.get("ALERT_EMAIL_TO", "")
ALERT_EMAIL_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")
RAILWAY_URL          = os.environ.get("RAILWAY_URL", "")

# ── LeetCode targets ──────────────────────────────────────────────────────────
LEETCODE_URL_1    = "https://leetcode.com/discuss/topic/interview-experience/"
LEETCODE_URL_2    = "https://leetcode.com/discuss/topic/interview/"
MAX_POSTS_URL1    = 6
MAX_POSTS_URL2    = 8
MAX_POSTS_COMBINED = 12

# ── Pipeline tuning ───────────────────────────────────────────────────────────
SCRAPE_DELAY        = 1      # seconds between scrapes
MAX_RETRY           = 2      # retry attempts for scrape / gemini / json parse
POST_IDS_TTL_HOURS  = 24     # delete post_ids older than this
SCHEDULER_INTERVAL  = 4      # cron hours between runs
