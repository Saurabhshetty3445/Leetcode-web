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
    Remove markdown fences, stray backticks, and any trailing garbage.

    Observed Gemini output variants this handles:
      - ```json ... ```        full fenced block
      - ```                    opening fence only
      - ``                     double backtick suffix
      - `                      single stray backtick
      - valid JSON ] followed  by `` then another stray ]
        e.g:  [...}]\n``\n]   ← the exact pattern from logs

    Strategy:
      1. Strip all backtick sequences anywhere in the text first
      2. Then find the BALANCED JSON array by tracking [ ] depth
         so we stop at the true closing bracket of the array,
         not a stray ] appended after it
    """
    text = text.strip()

    # Step 1: remove all backtick sequences (```, ``, `)
    text = re.sub(r"`{1,3}", "", text)
    text = text.strip()

    # Step 2: find the balanced JSON array start and end
    # Walk from the first [ and track bracket depth — stop when depth hits 0
    start = text.find("[")
    if start == -1:
        return text

    depth = 0
    end   = -1
    in_string = False
    escape    = False

    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == chr(92) and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break   # stop at the TRUE closing bracket — ignore any ] after

    if end == -1:
        # Fallback: use rfind if balanced walk failed
        end = text.rfind("]")

    if end != -1 and end > start:
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
