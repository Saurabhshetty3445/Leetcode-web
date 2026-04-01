"""
gemini_client.py — Calls Gemini API to extract structured problems from post text.

Returns a list of dicts:
  [{ "problem_name": "", "problem_type": "", "company": "" }]

If no problems found:
  [{ "problem_name": "No Problems Found", "problem_type": "", "company": "" }]
"""
from __future__ import annotations

import json
import time
from typing import Optional

import requests

from config import GEMINI_API_KEY, GEMINI_MODEL, MAX_RETRY
from logger import get_logger

log = get_logger("gemini")

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# ── Gemini prompt (DO NOT MODIFY) ─────────────────────────────────────────────
GEMINI_SYSTEM_PROMPT = """You are a structured data extractor.
Given a LeetCode interview experience post title and content, extract all coding/technical problems mentioned.

Return ONLY a valid JSON array. No markdown. No explanation. No extra text.

Each element:
{
  "problem_name": "<exact problem name or short description>",
  "problem_type": "<e.g. Array, DP, Graph, String, Tree, etc.>",
  "company": "<company name if mentioned, else empty string>"
}

If no specific problems are mentioned, return exactly:
[{"problem_name": "No Problems Found", "problem_type": "", "company": ""}]
"""


def _build_payload(title: str, content: str) -> dict:
    user_text = f"Title: {title}\n\nContent:\n{content}"
    return {
        "contents": [
            {
                "parts": [
                    {"text": GEMINI_SYSTEM_PROMPT},
                    {"text": user_text},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
        },
    }


def extract_problems(title: str, content: str) -> Optional[list[dict]]:
    """
    Call Gemini to extract problems.
    Retries up to MAX_RETRY times on failure.
    Returns parsed list or None if all retries exhausted.
    """
    for attempt in range(1, MAX_RETRY + 2):   # +2 → initial + MAX_RETRY retries
        try:
            resp = requests.post(
                GEMINI_URL,
                json=_build_payload(title, content),
                timeout=30,
            )
            if not resp.ok:
                log.warning(f"Gemini HTTP {resp.status_code} (attempt {attempt}): {resp.text[:200]}")
                raise RuntimeError(f"Gemini HTTP {resp.status_code}")

            data = resp.json()
            raw_text = (
                data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
            )
            log.info(f"Gemini raw (attempt {attempt}): {raw_text[:120]}")
            return raw_text   # hand off to parser for JSON validation + retry

        except Exception as e:
            log.warning(f"Gemini attempt {attempt} failed: {e}")
            if attempt <= MAX_RETRY:
                time.sleep(2 ** attempt)

    log.error("Gemini: all retries exhausted")
    return None
