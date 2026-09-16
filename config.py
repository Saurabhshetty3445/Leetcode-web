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

# ── Cloudflare Browser Rendering ────────────────────────────────────────────────
# Used instead of a local headless Chrome to fetch JS-rendered LeetCode pages.
# Create a Cloudflare API token with "Browser Rendering - Edit" permission.
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN  = os.environ.get("CLOUDFLARE_API_TOKEN", "")

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

# ── Reliability / request-handling tuning (Cloudflare Browser Rendering) ──────
HTTP_TIMEOUT_SECONDS         = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "45"))
BACKOFF_BASE_SECONDS         = float(os.environ.get("BACKOFF_BASE_SECONDS", "2"))
BACKOFF_MAX_SECONDS          = float(os.environ.get("BACKOFF_MAX_SECONDS", "60"))
MIN_REQUEST_INTERVAL_SECONDS = float(os.environ.get("MIN_REQUEST_INTERVAL_SECONDS", "1.5"))
                              # minimum gap enforced between outbound requests
PAGE_CACHE_TTL_SECONDS       = int(os.environ.get("PAGE_CACHE_TTL_SECONDS", "300"))
                              # how long a fetched page is reused instead of re-fetched
