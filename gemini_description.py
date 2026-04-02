"""
gemini_description.py — Batch-generates a clean description for every extracted problem.

Input  : list of { problem_name, problem_type, company }
Output : same list with "description" added to each item

Uses the description prompt verbatim, sending ALL problems from one post
in a single Gemini call (batch) to minimise API usage.

Retries up to MAX_RETRY times on failure.
Falls back to empty string per problem if Gemini/parse fails.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

import requests

from config import GEMINI_API_KEY, GEMINI_MODEL, MAX_RETRY
from logger import get_logger

log = get_logger("gemini_desc")

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_payload(problems: list[dict]) -> dict:
    """
    Build the description-generation payload.
    Sends all problems for one post in a single call.
    """
    problems_json = json.dumps(
        [{"problem_name": p["problem_name"], "problem_type": p["problem_type"]}
         for p in problems],
        indent=2,
    )

    prompt_text = (
        "You are an expert technical writer specializing in transforming coding and "
        "system design problems into clear, structured, and concise descriptions.\n\n"
        "GOAL:\n"
        "Generate a clean, professional problem description for EACH input problem.\n\n"
        "STRICT OUTPUT RULES:\n"
        "- Output MUST be a JSON array\n"
        "- Each item must contain:\n"
        "  - problem_name\n"
        "  - description\n"
        "- DO NOT include markdown, explanations, or extra text\n"
        "- DO NOT include code fences\n"
        "- Response must start with [ and end with ]\n\n"
        "DESCRIPTION RULES:\n"
        "- Description MUST be between 30 to 40 words\n"
        "- Maintain consistent tone and structure across all problems\n"
        "- Start with: \"Write a function\" OR \"Design a system\" depending on problem type\n"
        "- Clearly explain:\n"
        "  1. What needs to be built\n"
        "  2. What constraints or logic are involved\n"
        "  3. What the goal is\n"
        "- Use simple, professional English\n"
        "- Avoid unnecessary details, constraints, or examples\n"
        "- Avoid vague phrases\n"
        "- Keep sentence flow natural and readable\n\n"
        "STYLE TEMPLATE (STRICTLY FOLLOW):\n"
        "\"Write a function to [main task]. The problem involves [core logic or constraint]. "
        "This requires understanding [key concept] and ensuring [final objective or correctness condition].\"\n\n"
        "IMPORTANT:\n"
        "- Keep ALL descriptions roughly SAME LENGTH\n"
        "- Do NOT exceed 40 words\n"
        "- Do NOT go below 30 words\n"
        "- Maintain consistency across all outputs\n\n"
        f"INPUT:\n{problems_json}"
    )

    return {
        "contents": [
            {"parts": [{"text": prompt_text}]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }


# ── Parse helpers ─────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_descriptions(raw: str) -> Optional[list[dict]]:
    """Parse Gemini response into list[{problem_name, description}]."""
    if not raw:
        return None
    try:
        data = json.loads(_strip_fences(raw))
        if not isinstance(data, list):
            return None
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            result.append({
                "problem_name": str(item.get("problem_name", "")).strip(),
                "description":  str(item.get("description", "")).strip(),
            })
        return result if result else None
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Description parse error: {e} | raw: {raw[:200]}")
        return None


def _build_lookup(desc_list: list[dict]) -> dict[str, str]:
    """Build { problem_name → description } lookup (case-insensitive key)."""
    return {item["problem_name"].lower(): item["description"] for item in desc_list}


# ── Public API ────────────────────────────────────────────────────────────────

def enrich_with_descriptions(problems: list[dict]) -> list[dict]:
    """
    Take a list of extracted problems and add a 'description' field to each.

    - Skips the "No Problems Found" sentinel silently.
    - Sends all problems in ONE Gemini call (batch).
    - Retries up to MAX_RETRY times on failure.
    - Falls back to empty string if Gemini/parse fails after all retries.
    - Returns the same list with 'description' added in-place.
    """
    # Nothing to describe
    if not problems:
        return problems

    # Skip batch call for the no-problem sentinel
    if (
        len(problems) == 1
        and problems[0].get("problem_name", "").strip().lower() == "no problems found"
    ):
        problems[0]["description"] = ""
        return problems

    desc_lookup: dict[str, str] = {}

    for attempt in range(1, MAX_RETRY + 2):
        try:
            resp = requests.post(
                GEMINI_URL,
                json=_build_payload(problems),
                timeout=30,
            )
            if not resp.ok:
                log.warning(
                    f"Description Gemini HTTP {resp.status_code} "
                    f"(attempt {attempt}): {resp.text[:200]}"
                )
                raise RuntimeError(f"HTTP {resp.status_code}")

            data     = resp.json()
            raw_text = (
                data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
            )
            log.info(f"Description Gemini raw (attempt {attempt}): {raw_text[:120]}")

            parsed = _parse_descriptions(raw_text)
            if parsed:
                desc_lookup = _build_lookup(parsed)
                log.info(f"Descriptions generated for {len(parsed)} problem(s)")
                break
            else:
                log.warning(f"Description parse failed (attempt {attempt})")

        except Exception as e:
            log.warning(f"Description attempt {attempt} failed: {e}")

        if attempt <= MAX_RETRY:
            time.sleep(2 ** attempt)
    else:
        log.error("Description generation failed after all retries — using empty strings")

    # Attach descriptions (fallback to "" if not found in lookup)
    for p in problems:
        key = p.get("problem_name", "").lower()
        p["description"] = desc_lookup.get(key, "")
        if not p["description"]:
            log.warning(f"No description found for: {p['problem_name']!r}")

    return problems
