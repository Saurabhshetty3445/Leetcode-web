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

def _build_payload(title: str, content: str) -> dict:
    """
    Build Gemini request payload using the master extraction prompt.
    POST_TITLE and POST_CONTENT are injected at the end of the prompt.
    problem_type is strictly "coding" or "design" (or "none" for no-problem case).
    """
    prompt_text = (
        "You are an expert system designed to aggressively extract and normalize ALL possible "
        "coding and system design problems from messy interview posts.\n\n"
        "GOAL:\n"
        "Maximize recall. NEVER miss a problem if any REAL problem exists.\n\n"
        "ADVANCED EXTRACTION RULES:\n"
        "- Extract problems even if poorly written or embedded in paragraphs\n"
        "- Extract from tables, inline text, or mixed UI content\n"
        "- Split multiple problems appearing in one line\n"
        "- ALWAYS treat each distinct requirement, task, or design ask as a separate problem\n"
        "- If a section contains multiple asks (e.g., scalability + rate limiting + caching), "
        "decide if they form ONE system design problem or MULTIPLE independent problems\n"
        "- Detect problems from keywords like array, string, tree, graph, DP, cache, system design, thread-safe\n"
        "- If a problem is implied, extract it as a valid problem\n"
        "- Treat EACH design scenario separately (e.g., \"Design API\" and "
        "\"Design OTT Scheduling Service\" are DIFFERENT problems)\n"
        "- Do NOT merge separate design questions into one\n"
        "- Ignore UI noise such as View Problem, View Post, Solve, links, buttons\n"
        "- Ignore numbering like 1., 2, etc.\n\n"
        "STRUCTURED EXTRACTION:\n"
        "- Treat sections like Round, Problem:, Task:, Topic:, Q1, Q2, Q3 as strong signals\n"
        "- ALWAYS split problems based on Q1, Q2, Q3 even if under same round\n"
        "- Always extract problems after Problem:, Task:, Design, Implement\n"
        "- Normalize variations into clean names\n\n"
        "QUESTION FILTER RULE:\n"
        "- If content is asking for advice, tips, or experience, DO NOT extract problems\n"
        "- If no actual problem statement or task exists, return no problems\n"
        "- Do NOT convert general topics like system design, HLD, LLD into problems\n\n"
        "STRICT RULES FOR problem_name:\n"
        "- MUST be clean, descriptive, and SEO-friendly\n"
        "- MAX 7 words\n"
        "- Use Title Case\n"
        "- Remove explanations, constraints, and complexity\n"
        "- Avoid generic names like Data Structures And Algorithms or Coding Problem\n"
        "- Use structure: Action + Object + Context when possible\n"
        "- Prefer meaningful names like Find Most Common Product Pair instead of vague names\n"
        "- If relevant, include technique or context like Sliding Window, Moving Average, Graph, DP\n"
        "- If the problem resembles a known LeetCode problem, align naming style closely\n"
        "- Never merge multiple problems into one title\n\n"
        "CLASSIFICATION RULES:\n"
        "- coding for DSA, algorithms, math, logic\n"
        "- design for system design, architecture, scalability\n\n"
        "COMPANY RULES:\n"
        "- Extract from POST TITLE first\n"
        "- If not found, use Unknown\n\n"
        "ANTI-MISS RULE:\n"
        "- If ANY section (especially Q1, Q2, Q3 or bullet blocks) contains a distinct task, "
        "it MUST be extracted as a separate problem\n"
        "- If multiple independent design scenarios exist, return ALL of them separately\n"
        "- If at least one real problem exists, NEVER return fewer problems than explicitly described in the content\n"
        "- Do not treat discussion or questions as problems\n\n"
        "CRITICAL OUTPUT ENFORCEMENT:\n"
        "- Output MUST be a raw JSON array\n"
        "- DO NOT wrap in markdown (no ```json or ```)\n"
        "- DO NOT add explanations, comments, or text before/after\n"
        "- DO NOT add trailing commas\n"
        "- Ensure valid JSON parsable by standard JSON.parse()\n"
        "- Response must start with [ and end with ]\n\n"
        "OUTPUT FORMAT:\n"
        "Each item must contain:\n"
        "problem_name (string)\n"
        "problem_type (coding or design)\n"
        "company (string)\n\n"
        "EDGE CASE:\n"
        "If no problems:\n"
        '[{"problem_name":"No Problems Found","problem_type":"none","company":"Unknown"}]\n\n'
        f"POST TITLE:{title}\n\n"
        f"POST CONTENT:\n{content}"
    )

    return {
        "contents": [
            {
                "parts": [{"text": prompt_text}]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
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
