"""Shared LLM-reply parsing for the consolidate / session-ingest / synthesize beats.

Distinct from ``aggressive_utils.extract_json`` (a different tolerance strategy): these
beats fail CLOSED on malformed JSON, so they only strip a markdown code fence and then
let ``json.loads`` raise — the caller turns that into a no-op / un-resolved candidates.
"""
from __future__ import annotations


def strip_json_fence(stdout: str) -> str:
    """Strip a leading ```json (and trailing ```) markdown code fence from an LLM reply,
    returning the inner text. A reply with no fence is returned stripped. Does NOT parse
    — the caller runs ``json.loads`` so a JSONDecodeError (⊂ ValueError) propagates and
    the beat fails closed."""
    text = (stdout or "").strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    return text
