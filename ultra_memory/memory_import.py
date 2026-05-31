"""One-time, idempotent import of the legacy harness memory into memory.db.

Parses the FIXED legacy formats with no YAML dependency, then writes through the
memory_lib single-writer path (spec §6). Real-data import runs at bootstrap
(§7.4) behind meta.import_complete; this module is unit-tested on fixtures.
"""
import re
from datetime import datetime
from pathlib import Path

from . import memory_lib
from .redact_secrets import strip_secrets

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


def import_memory_dir(conn, memory_dir, *, index_path=None, ts):
    """Import every memory/*.md (excluding MEMORY.md) → save_memory upserts.
    Returns the count imported. Idempotent (per-id upsert)."""
    memory_dir = Path(memory_dir)
    index = {}
    if index_path is not None and Path(index_path).exists():
        index = parse_memory_index(Path(index_path).read_text())
    # MEMORY.md line order → sort_order (keyed by filename slug, the index's link target).
    order_map = {slug: i for i, slug in enumerate(index)}
    count = 0
    seen = {}
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        fm, body = split_frontmatter(path.read_text())
        name = fm.get("name") or path.stem
        # Two files resolving to the same id (frontmatter `name:` is NOT unique
        # across files — the harness strips type prefixes) would silently upsert
        # onto one row, destroying the first and over-reporting the count. Fail
        # loud, naming both offenders, instead of losing a memory.
        if name in seen:
            raise ValueError(
                f"duplicate memory id {name!r}: {path.name} collides with "
                f"{seen[name]} — frontmatter 'name:' must be unique across files")
        seen[name] = path.name
        meta = fm.get("metadata", {})
        slug = path.stem  # underscore filename stem = how the harness addresses the file
        idx = index.get(slug, {})
        # The file's real age (mtime) drives the §8 staleness signal; without it a
        # bootstrap import stamps every memory with the import moment.
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        memory_lib.save_memory(
            conn, id=name, type=meta.get("type", "reference"),
            title=idx.get("title") or name, body=body, ts=ts,
            origin_session_id=meta.get("originSessionId"),
            description=fm.get("description"),
            index_hook=idx.get("hook"),
            node_type=meta.get("node_type", "memory"),
            file_slug=slug,
            sort_order=order_map.get(slug),
            created_at=mtime, updated_at=mtime,
        )
        count += 1
    return count


# Range separator accepts ASCII hyphen AND en-dash (U+2013) / em-dash (U+2014),
# all of which appear in the real .remember files.
_TODAY_HEADER = re.compile(r"^##\s+(?P<start>\d{2}:\d{2})(?:[-–—](?P<end>\d{2}:\d{2}))?"
                           r"\s*\|\s*(?P<ctx>.*)$")
_ANY_HEADER = re.compile(r"^##\s+(?P<text>.*\S)\s*$")


def import_today_file(conn, text, *, day):
    """Import a .remember/today-<day>.md into session_events under a synthetic
    'legacy-<day>' session. Returns (count, warnings). Idempotent; never crashes.

    Every '## ' line starts a new block. HH:MM[-HH:MM] headers get the start time;
    any other '## ' header (date header, '## Active: …') is captured as its own
    block at day-midnight WITH a warning — never silently folded into the prior
    block (which would collapse distinct work sessions and lose timestamps)."""
    session_id = f"legacy-{day}"
    lines = text.splitlines()
    blocks = []          # (ts, ctx, [body lines])
    warnings = []
    current = None
    for line in lines:
        m = _TODAY_HEADER.match(line)
        if m:
            ts = f"{day}T{m.group('start')}:00"
            current = (ts, m.group("ctx").strip(), [])
            blocks.append(current)
            continue
        h = _ANY_HEADER.match(line)
        if h:
            ts = f"{day}T00:00:00"
            header_text = h.group("text").strip()
            current = (ts, header_text, [])
            blocks.append(current)
            warnings.append(f"non-time header captured at {ts}: {header_text!r}")
            continue
        if current is not None:
            current[2].append(line)
        elif line.strip():
            warnings.append(f"skip non-conforming prose: {line.strip()[:40]!r}")
    count = 0
    seen = set()
    for ts, ctx, body_lines in blocks:
        detail = "\n".join(body_lines).strip()
        title = (detail.splitlines()[0] if detail else ctx)[:120]
        # Dedupe within the run on the same content-addressed key record_session_event
        # uses (computed on redacted text), so the returned count reflects rows
        # actually recorded — not the block count — and true dupes are warned, not
        # silently swallowed by INSERT OR IGNORE.
        key = memory_lib._event_key(
            session_id, ts, "legacy_note", strip_secrets(title), strip_secrets(detail))
        if key in seen:
            warnings.append(f"duplicate block skipped (identical content at {ts}): {title!r}")
            continue
        seen.add(key)
        memory_lib.record_session_event(
            conn, session_id=session_id, kind="legacy_note", title=title, ts=ts,
            detail=detail, session_fields={"started_at": f"{day}T00:00:00"})
        count += 1
    return count, warnings
