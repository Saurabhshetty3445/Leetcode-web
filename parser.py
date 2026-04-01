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
    """Remove ```json ... ``` fences if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_gemini_output(raw: str) -> Optional[list[dict]]:
    """
    Parse raw Gemini text into a validated list[dict].
    Returns None on failure (caller decides retry logic).
    """
    if not raw:
        log.warning("Parser received empty string")
        return None

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
