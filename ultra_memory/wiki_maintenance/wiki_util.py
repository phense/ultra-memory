"""Generic wiki utilities — the shared base every detector builds on.

Ported from a reference wiki-maintenance `_util.py` (move-generic), with the
REPO_ROOT/WIKI_ROOT module constants REMOVED: every function takes its root as a
parameter, so the engine names no consumer path. `split_frontmatter` is load-bearing
— its contract is preserved byte-for-byte (the detectors depend on it).
"""
from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import Any

import yaml


def today_iso() -> str:
    """Today's date as an ISO-8601 string, e.g. '2026-06-02'."""
    return datetime.date.today().isoformat()


def wiki_md_files(wiki_root) -> list[Path]:
    """Sorted list of all *.md files recursively under `wiki_root`."""
    return sorted(Path(wiki_root).rglob("*.md"))


def split_frontmatter(text: str) -> tuple[dict, str, str]:
    """Split a wiki page into (frontmatter_dict, raw_frontmatter_block, body).

    * No leading ``---`` delimiter → ``({}, "", text)``.
    * YAML block malformed or not a dict (a bare string/list) → ``({}, "", text)``.
    * Otherwise → ``(parsed_dict, raw_yaml_text, body_after_closing_delimiter)``.

    The closing fence must occupy its own line (``\\n---\\n`` inline, or a trailing
    ``\\n---`` for a frontmatter-only page) — a value like ``key: ---foo`` never
    matches. CRLF is normalized first.
    """
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return ({}, "", text)
    rest = text[4:]
    inline_close = rest.find("\n---\n")
    trailing_close = rest.endswith("\n---")
    if inline_close != -1:
        raw_fm = rest[:inline_close + 1]
        body = rest[inline_close + 5:]
    elif trailing_close:
        raw_fm = rest[: len(rest) - 4]
        body = ""
    else:
        return ({}, "", text)
    try:
        parsed: Any = yaml.safe_load(raw_fm)
    except yaml.YAMLError:
        return ({}, "", text)
    if not isinstance(parsed, dict):
        return ({}, "", text)
    return (parsed, raw_fm, body)


def git_lines(*args: str, repo_root) -> list[str]:
    """Run ``git <args>`` in `repo_root` → non-empty stdout lines. `repo_root` is a
    REQUIRED parameter (no consumer-path default). Raises CalledProcessError on a
    non-zero exit (the caller decides fail-open)."""
    result = subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]
