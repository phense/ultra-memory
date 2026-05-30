"""One-time, idempotent import of the legacy harness memory into memory.db.

Parses the FIXED legacy formats with no YAML dependency, then writes through the
memory_lib single-writer path (spec §6). Real-data import runs at bootstrap
(§7.4) behind meta.import_complete; this module is unit-tested on fixtures.
"""
import re
from pathlib import Path

from . import memory_lib

_INDEX_LINE = re.compile(r"^- \[(?P<title>.+?)\]\((?P<slug>[^)]+?)\.md\)"
                         r"(?:\s+—\s+(?P<hook>.*\S))?\s*$")


def split_frontmatter(text):
    """Return (frontmatter_dict, body). frontmatter_dict has flat keys plus a
    nested 'metadata' dict. No YAML dep — parses the known memory-file shape."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm = {"metadata": {}}
    in_meta = False
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if raw.strip() == "metadata:":
            in_meta = True
            continue
        indented = raw[:1] in (" ", "\t")
        key, sep, val = raw.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        if in_meta and indented:
            fm["metadata"][key] = val
        else:
            in_meta = False
            fm[key] = val
    body = "\n".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


def parse_memory_index(text):
    """Parse MEMORY.md lines `- [Title](slug.md) — hook` → {slug: {title, hook}}."""
    out = {}
    for line in text.splitlines():
        m = _INDEX_LINE.match(line.rstrip())
        if m:
            out[m.group("slug")] = {"title": m.group("title"),
                                    "hook": m.group("hook")}
    return out
