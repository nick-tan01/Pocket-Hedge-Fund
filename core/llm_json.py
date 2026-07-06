"""
core/llm_json.py
Tolerant JSON extraction + one-retry completion for LLM agents.

Audit recommendation #1 (reliability): every agent did `json.loads(strip_fences(text))`,
which throws on three common, recoverable conditions:
  - the model wraps JSON in prose ("Here is the analysis: { ... }")
  - a trailing comma before } or ]
  - the response is TRUNCATED at the token cap mid-object (the 2026-06-17 bear failure
    at char 4061 — a ~1000-token cut-off)
A thrown parse becomes a conviction-1 fallback, which silently drops a real candidate
(and, in one case, caused the AMAT buy/skip flip-flop that orphaned a position).

extract_json() recovers the first two. complete_json() adds a single retry so a
truncated/garbled response gets one more attempt before falling back — and callers
should pair it with a generous max_tokens so truncation is rare in the first place.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """
    Parse a JSON object out of an LLM response, tolerating markdown fences,
    surrounding prose, and trailing commas. Raises ValueError if nothing parses.
    """
    if not text or not text.strip():
        raise ValueError("empty LLM response")
    t = text.strip()

    # Strip a leading ```json / ``` fence if present.
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            if t.lstrip().lower().startswith("json"):
                t = t.lstrip()[4:]
            t = t.strip()

    # Fast path.
    try:
        return json.loads(t)
    except Exception:
        pass

    # Outermost {...} span, with trailing commas removed.
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        candidate = re.sub(r",(\s*[}\]])", r"\1", t[start:end + 1])
        return json.loads(candidate)  # may raise — caller handles

    raise ValueError("no JSON object found in response")


_client = None


def get_client():
    """Shared, lazily-constructed Anthropic client.

    The agent modules used to each build their own client at IMPORT time, so a
    missing/empty ANTHROPIC_API_KEY killed the whole process at `import agents.*`
    — before even the mechanical stop checks that need no LLM could run. Lazy
    construction defers that failure to the first actual LLM call, which every
    agent already guards with a neutral fallback.
    """
    global _client
    if _client is None:
        import anthropic
        import config
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def complete_json(
    client,
    *,
    model: str,
    max_tokens: int,
    prompt: str,
    label: str = "",
    retries: int = 1,
) -> dict:
    """
    Call the Anthropic API and return a parsed JSON dict, retrying once on a parse
    failure OR an API error. The API call sits inside the try — previously a
    transient 529/timeout on attempt 1 escaped the retry loop entirely while a
    garbled response got retried. Raises the last error if every attempt fails —
    callers keep their existing fallback for that case.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text if resp.content else ""
            return extract_json(raw)
        except Exception as exc:
            last_exc = exc
            logger.warning("LLM call/parse failed for %s (attempt %d/%d): %s",
                           label or "agent", attempt + 1, retries + 1, exc)
    raise last_exc
