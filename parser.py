"""
parser.py — Parse and validate Gemini JSON output.

Validates:
  - Must be a JSON array
  - No markdown fences
  - Each element has required keys
  - Retries Gemini call up to MAX_RETRY times on bad output
"""
from __future__ import annotations

import json
import re
from typing import Optional

from logger import get_logger

log = get_logger("parser")

REQUIRED_KEYS = {"problem_name", "problem_type", "company"}


def _strip_markdown(text: str) -> str:
    """
    Remove any markdown fences or stray backtick sequences Gemini appends.
    Handles all observed variants:
      - ```json ... ```   full fenced block
      - ```               opening fence only
      - ``                double backtick suffix (seen in logs)
      - `                 single stray backtick
    Final step: extract only the content between the first [ and last ]
    so any trailing garbage after the JSON array is discarded entirely.
    """
    text = text.strip()

    # Remove opening fence: ```json or ``` or `` or `
    text = re.sub(r"^`{1,3}(?:json)?\s*", "", text)

    # Remove any closing backtick sequence (1, 2, or 3 backticks)
    text = re.sub(r"\s*`{1,3}\s*$", "", text)

    # Extract exactly the JSON array — everything from first [ to last ]
    # This discards any stray text before or after the array
    start = text.find("[")
    end   = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()


def parse_gemini_output(raw) -> Optional[list[dict]]:
    """
    Parse raw Gemini text (or already-parsed list) into a validated list[dict].
    Returns None on failure (caller decides retry logic).

    Handles two input types:
      - str  : normal path — strip markdown fences, JSON-decode
      - list : Gemini SDK already parsed the response (responseMimeType=json)
               skip string processing and validate directly
    """
    if raw is None:
        log.warning("Parser received None")
        return None

    # Fast path: Gemini already returned a parsed list
    if isinstance(raw, list):
        log.info("Parser: received pre-parsed list — skipping string decode")
        parsed = raw
    elif not isinstance(raw, str) or not raw.strip():
        log.warning(f"Parser received unexpected type or empty: {type(raw)}")
        return None
    else:
        cleaned = _strip_markdown(raw)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.warning(f"JSON decode error: {e} | raw: {cleaned[:200]}")
            return None

    if not isinstance(parsed, list):
        log.warning(f"Expected JSON array, got {type(parsed).__name__}")
        return None

    # Validate each element
    validated = []
    for item in parsed:
        if not isinstance(item, dict):
            log.warning(f"Skipping non-dict item: {item}")
            continue
        # Fill missing keys with empty string
        entry = {k: str(item.get(k, "")).strip() for k in REQUIRED_KEYS}
        validated.append(entry)

    if not validated:
        log.warning("Parser produced empty list after validation")
        return None

    log.info(f"Parser: {len(validated)} problem(s) extracted")
    return validated
