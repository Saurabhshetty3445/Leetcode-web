"""
botcheck.py — Cloudflare / bot-protection detection and email alert.

After each scrape, call check_for_bot(content) to inspect the raw text.
If Cloudflare is detected:
  1. Sends an URGENT email alert via Gmail SMTP (App Password).
  2. Raises BotDetectedError — the pipeline catches this, stops cleanly,
     and marks the run as "bot_blocked".

Required environment variables:
  ALERT_EMAIL_FROM      — Gmail address to send FROM  (e.g. bot@gmail.com)
  ALERT_EMAIL_TO        — Address to send TO           (e.g. you@gmail.com)
  ALERT_EMAIL_PASSWORD  — Gmail App Password (16-char, no spaces)
  RAILWAY_URL           — Your Railway deployment URL  (for the alert message)
"""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from logger import get_logger

log = get_logger("botcheck")

# ── Cloudflare fingerprints ───────────────────────────────────────────────────
_BOT_PHRASES = [
    "security service to protect",
    "checking your browser",
    "enable javascript and cookies",
    "cloudflare",
    "ray id",
    "please wait while we check your browser",
    "access denied",
    "cf-mitigated",
]

RAILWAY_URL = os.environ.get("RAILWAY_URL", "")
ALERT_FROM     = os.environ.get("ALERT_EMAIL_FROM", "")
ALERT_TO       = os.environ.get("ALERT_EMAIL_TO", "")
ALERT_PASSWORD = os.environ.get("ALERT_EMAIL_PASSWORD", "")


class BotDetectedError(RuntimeError):
    """Raised when Cloudflare / bot-check page is detected."""


# ── Detection ─────────────────────────────────────────────────────────────────

def is_bot_page(content: str) -> bool:
    """Return True if the scraped content looks like a Cloudflare challenge page."""
    if not content:
        return False
    lower = content.lower()
    return any(phrase in lower for phrase in _BOT_PHRASES)


# ── Alert ─────────────────────────────────────────────────────────────────────

def _send_email_alert(blocked_url: str) -> None:
    """Send URGENT email via Gmail SMTP. Logs warning if credentials missing."""
    if not all([ALERT_FROM, ALERT_TO, ALERT_PASSWORD]):
        log.warning(
            "Bot detected but email credentials not set "
            "(ALERT_EMAIL_FROM / ALERT_EMAIL_TO / ALERT_EMAIL_PASSWORD). "
            "Skipping email alert."
        )
        return

    subject = "URGENT 🚨 — LeetCode cookies expired, update now"
    body = (
        "URGENT 🚨 — LeetCode cookies expired, update now\n\n"
        "The scraper hit a Cloudflare bot check.\n\n"
        f"Blocked URL : {blocked_url}\n"
        f"Railway URL : {RAILWAY_URL}\n\n"
        "ACTION REQUIRED:\n"
        "1. Open LeetCode in your browser and log in.\n"
        "2. Copy your fresh cookies (use EditThisCookie or DevTools → Application → Cookies).\n"
        "3. Go to Railway → your service → Variables.\n"
        "4. Update LEETCODE_COOKIES with the new value.\n"
        "5. Redeploy the service.\n\n"
        "The pipeline has been stopped automatically.\n"
        "It will resume normally on the next scheduled run after cookies are updated."
    )

    msg = MIMEMultipart()
    msg["From"]    = ALERT_FROM
    msg["To"]      = ALERT_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(ALERT_FROM, ALERT_PASSWORD)
            server.sendmail(ALERT_FROM, ALERT_TO, msg.as_string())
        log.info(f"Bot alert email sent to {ALERT_TO}")
    except Exception as e:
        log.error(f"Failed to send bot alert email: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def check_for_bot(content: str, url: str = "") -> None:
    """
    Call after every scrape.
    If bot page detected → send email + raise BotDetectedError.
    BotDetectedError must be caught by the pipeline to stop cleanly.
    """
    if not is_bot_page(content):
        return

    log.error(f"🚨 BOT CHECK DETECTED at: {url}")
    _send_email_alert(url)
    raise BotDetectedError(
        f"Cloudflare bot-check page detected at {url}. "
        "Pipeline stopped. Update LEETCODE_COOKIES and redeploy."
    )
