"""Shared OAuth-call + JSON-extract plumbing for the SP-7 aggressive beats.

Extracted from ``aggressive_edit`` / ``aggressive_quarantine``, which carried
byte-identical copies of the lazy subprocess runner and a balanced-brace JSON
parser, plus a call wrapper that differed ONLY by the system-prompt constant
(``_REFLECT_SYSTEM`` vs ``_ADJUDICATE_SYSTEM``) — now passed in as ``system=``.

This is plumbing, NOT a guard: the OAuth chokepoint (``claude_cli.run_claude``)
is unchanged — every call still routes through it (no metered SDK, OAuth-only),
and the provenance/bounds/eval wall lives in the caller modules, untouched.
"""
from __future__ import annotations

import json

from ultra_memory.claude_cli import run_claude


def default_runner():
    """The real-run runner — the OAuth claude CLI subprocess. Imported LAZILY so
    the module imports clean with no CLI present, and so a test that injects its own
    runner never touches this path. (subprocess is the only consumer; never the
    anthropic SDK — OAuth-only.)"""
    import subprocess
    return subprocess.run


def call_model(prompt: str, *, system: str, runner, model: str, env=None) -> str:
    """Issue the ONE batched OAuth call through the OAuth chokepoint `run_claude`
    (`ultra_memory.claude_cli.run_claude`) so the child `claude`'s env is sanitized
    in ONE place: it refuses if a stray metered-API key would outrank the OAuth token,
    requires the OAuth token, drops the key, and strips the in-session recursion
    markers. The `runner` seam is preserved (run_claude takes `runner=`); `env`
    (None → os.environ) lets a test inject a fake OAuth env. Returns stdout text.
    Raises on a non-zero exit / OAuth violation / runner error (the caller's fail-open
    turns a raise into an EMPTY plan)."""
    return run_claude(prompt, model=model, system=system,
                      runner=runner, timeout=120, env=env)


def extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a model reply (tolerant of a
    leading/trailing prose wrapper). Returns None if no object parses."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Tolerate a wrapped object — find the first '{' and its matching '}'.
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None
