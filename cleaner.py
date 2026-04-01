"""
cleaner.py — Mandatory regex cleaning pipeline for scraped text.

Applied in strict sequence before sending to Gemini.
"""
import re


def clean_text(raw: str) -> str:
    """
    Sequential cleaning pipeline:
      1. Strip noise punctuation clusters
      2. Remove non-ASCII characters
      3. Collapse newlines
      4. Remove literal 'null' tokens
      5. Collapse extra whitespace
    """
    if not raw:
        return ""

    text = raw

    # Step 1: noise punctuation clusters
    text = re.sub(r"[\:./~*\",']+", " ", text)

    # Step 2: non-ASCII
    text = re.sub(r"[^\x00-\x7F]+", " ", text)

    # Step 3: carriage-return / newline normalization
    text = re.sub(r"[\r\n]+", "\n", text)

    # Step 4: bare 'null' tokens (case-insensitive, word-boundary)
    text = re.sub(r"\bnull\b", "", text, flags=re.IGNORECASE)

    # Step 5: collapse horizontal whitespace
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Step 6: strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.splitlines()]
    text  = "\n".join(line for line in lines if line)

    return text.strip()
