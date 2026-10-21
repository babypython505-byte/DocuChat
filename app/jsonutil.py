"""Robust JSON extraction from LLM output (code fences, chatter around the object, trailing commas)."""
from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _candidates(text: str):
    text = (text or "").strip()
    yield text
    for m in _FENCE.finditer(text):
        yield m.group(1).strip()
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = text.find(open_), text.rfind(close)
        if start != -1 and end > start:
            yield text[start : end + 1]


def parse_json(text: str):
    """Return the first JSON value found in text, or raise ValueError."""
    for cand in _candidates(text):
        if not cand:
            continue
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                return json.loads(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
    raise ValueError("model did not return valid JSON")
