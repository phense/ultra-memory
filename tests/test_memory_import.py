from ultra_memory import memory_import as mi

_SAMPLE = '''---
name: feedback-x
description: "HARD RULE — never use the API; keep at 08:30 sharp"
metadata:
  node_type: memory
  type: feedback
  originSessionId: abc-123
---

Body line one.

Body --- with a dashes line.
'''


def test_split_frontmatter_extracts_fields_and_body():
    fm, body = mi.split_frontmatter(_SAMPLE)
    assert fm["name"] == "feedback-x"
    assert fm["description"] == "HARD RULE — never use the API; keep at 08:30 sharp"
    assert fm["metadata"]["type"] == "feedback"
    assert fm["metadata"]["node_type"] == "memory"
    assert fm["metadata"]["originSessionId"] == "abc-123"
    assert body.startswith("Body line one.")
    assert "--- with a dashes line." in body  # body delimiters not mis-parsed


def test_split_frontmatter_no_frontmatter_returns_text():
    fm, body = mi.split_frontmatter("just text\nno fm")
    assert fm == {} and body == "just text\nno fm"


def test_parse_memory_index_reads_title_and_hook():
    text = (
        "- [Claude OAuth-only](feedback_claude_oauth_only.md) — every LLM call uses OAuth\n"
        "- [No hook here](bare.md)\n"
    )
    idx = mi.parse_memory_index(text)
    assert idx["feedback_claude_oauth_only"]["title"] == "Claude OAuth-only"
    assert idx["feedback_claude_oauth_only"]["hook"] == "every LLM call uses OAuth"
    assert idx["bare"]["title"] == "No hook here"
    assert idx["bare"]["hook"] is None


from ultra_memory import memory_lib


def _write(p, name, typ, desc, body, sid="s-1"):
    p.write_text(
        f"---\nname: {name}\ndescription: \"{desc}\"\nmetadata: \n"
        f"  node_type: memory\n  type: {typ}\n  originSessionId: {sid}\n---\n\n{body}\n")


def test_import_memory_dir_excludes_index_and_upserts(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_x.md", "feedback-x", "feedback", "one liner", "Body X.")
    _write(mem / "project_y.md", "project-y", "project", "two liner", "Body Y.")
    (mem / "MEMORY.md").write_text(
        "- [Feedback X](feedback_x.md) — short hook X\n"
        "- [Project Y](project_y.md) — short hook Y\n")
    conn = memory_lib.open_memory_db(tmp_path / "m.db")

    n = mi.import_memory_dir(conn, mem, index_path=mem / "MEMORY.md",
                             ts="2026-05-30T10:00:00")
    assert n == 2  # MEMORY.md not imported as a memory

    row = conn.execute("SELECT id, type, title, description, index_hook, node_type, "
                       "origin_session_id, body FROM memories WHERE id='feedback-x'").fetchone()
    assert row["type"] == "feedback"
    assert row["title"] == "Feedback X"          # from the index
    assert row["description"] == "one liner"     # from the frontmatter
    assert row["index_hook"] == "short hook X"   # from the index
    assert row["node_type"] == "memory"
    assert row["origin_session_id"] == "s-1"
    assert row["body"].strip() == "Body X."

    # idempotent: second run does not duplicate
    mi.import_memory_dir(conn, mem, index_path=mem / "MEMORY.md",
                         ts="2026-05-30T11:00:00")
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert total == 2
    conn.close()


def test_import_memory_dir_title_falls_back_to_slug(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "orphan.md", "orphan", "reference", "d", "B.")
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    mi.import_memory_dir(conn, mem, index_path=None, ts="2026-05-30T10:00:00")
    title = conn.execute("SELECT title FROM memories WHERE id='orphan'").fetchone()[0]
    assert title == "orphan"  # no index → slug fallback
    conn.close()


_TODAY = """preamble prose before any header should be skipped
## 23:11 | main
Extracted 8 claims from a video; rejected 127 segments.

## 19:36-20:21 | main
Designed the trade DB schema and the weekly review.
"""


def test_import_today_file_parses_headers_and_ranges(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    n, warnings = mi.import_today_file(conn, _TODAY, day="2026-05-27")
    assert n == 2
    rows = conn.execute(
        "SELECT ts, title, detail FROM session_events "
        "WHERE session_id='legacy-2026-05-27' ORDER BY ts").fetchall()
    assert rows[0]["ts"] == "2026-05-27T19:36:00"   # range → start time
    assert rows[1]["ts"] == "2026-05-27T23:11:00"
    assert "Extracted 8 claims" in rows[1]["detail"]
    assert any("prose" in w or "skip" in w.lower() for w in warnings)
    conn.close()


def test_import_today_file_is_idempotent(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    for _ in range(2):
        mi.import_today_file(conn, _TODAY, day="2026-05-27")
    n = conn.execute("SELECT COUNT(*) FROM session_events "
                     "WHERE session_id='legacy-2026-05-27'").fetchone()[0]
    assert n == 2
    conn.close()


def test_import_today_file_captures_malformed_header_with_warning(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    n, warnings = mi.import_today_file(conn, "garbage\n## not a time | x\nmore", day="2026-05-27")
    # A '## ' line that is not HH:MM is captured as its own (midnight) block with a
    # warning — NOT silently dropped or folded. The leading 'garbage' prose warns too.
    assert n == 1
    assert any("non-time" in w.lower() for w in warnings)
    conn.close()


# Mirrors the real .remember anomalies the audit reproduced: EN-DASH range, a date
# header, and a timeless '## Active:' header — all previously folded silently.
_TODAY_REAL = """## 21:24-22:15 | main
ascii range block.

## 22:32–23:03 | main
en-dash range block, a distinct work session.

## 2026-05-24 evening | main — pivot done
date-header block.

## Active: IBKR integration setup
timeless header block.
"""


def test_import_today_count_reflects_recorded_not_blocks(tmp_path):
    """M1: two byte-identical blocks at the same minute dedupe to one row — the
    returned count must reflect what was recorded (1), not the block count (2),
    and the dropped duplicate must be warned, not silent."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    text = "## 10:00 | main\ndup line\n\n## 10:00 | main\ndup line\n"
    n, warnings = mi.import_today_file(conn, text, day="2026-05-30")
    rows = conn.execute(
        "SELECT COUNT(*) FROM session_events WHERE session_id='legacy-2026-05-30'").fetchone()[0]
    assert rows == 1
    assert n == 1  # count does not lie
    assert any("duplicate" in w.lower() for w in warnings)
    conn.close()


def test_import_today_endash_range_is_its_own_block(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    n, warnings = mi.import_today_file(conn, _TODAY_REAL, day="2026-05-29")
    assert n == 4  # all four headers are distinct blocks, none folded
    rows = {r["ts"]: r["detail"] for r in conn.execute(
        "SELECT ts, detail FROM session_events WHERE session_id='legacy-2026-05-29'")}
    assert "2026-05-29T22:32:00" in rows  # EN-DASH range parsed → own block at start time
    assert "en-dash range block" in rows["2026-05-29T22:32:00"]
    # the en-dash block is NOT swallowed into the 21:24 block
    assert "en-dash range block" not in rows["2026-05-29T21:24:00"]
    conn.close()


def test_import_today_non_time_headers_warn_and_capture(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    n, warnings = mi.import_today_file(conn, _TODAY_REAL, day="2026-05-29")
    # date-header + timeless header → 2 distinct midnight blocks, both warned.
    midnight = conn.execute(
        "SELECT detail FROM session_events "
        "WHERE session_id='legacy-2026-05-29' AND ts='2026-05-29T00:00:00'").fetchall()
    details = " ".join(r["detail"] or "" for r in midnight)
    assert "date-header block" in details and "timeless header block" in details
    assert sum("non-time" in w.lower() for w in warnings) == 2
    conn.close()


def test_import_uses_file_mtime_for_timestamps(tmp_path):
    """M2: an imported memory's created_at/updated_at must reflect the FILE's age
    (mtime), not the import moment — otherwise the §8 staleness signal treats every
    memory as freshly written for 90 days after a bootstrap import."""
    import datetime as _dt
    import os

    mem = tmp_path / "memory"
    mem.mkdir()
    f = mem / "old_note.md"
    _write(f, "old-note", "reference", "d", "Body.")
    old = _dt.datetime(2026, 1, 1, 12, 0, 0).timestamp()
    os.utime(f, (old, old))
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    mi.import_memory_dir(conn, mem, index_path=None, ts="2026-05-30T10:00:00")
    row = conn.execute(
        "SELECT created_at, updated_at FROM memories WHERE id='old-note'").fetchone()
    assert row["updated_at"].startswith("2026-01-01")  # file age drives staleness
    assert row["created_at"].startswith("2026-01-01")
    # the audit row still records the import action time, not the file mtime
    audit_ts = conn.execute(
        "SELECT ts FROM audit_log WHERE target_id='old-note'").fetchone()[0]
    assert audit_ts == "2026-05-30T10:00:00"
    conn.close()


def test_import_stores_updated_at_in_canonical_utc(tmp_path):
    """Round-4 FIX 5: the import path must store updated_at/created_at in the
    canonical tz-aware UTC `%Y-%m-%dT%H:%M:%SZ` format (the engine's
    maintain/retention convention), NOT a naive-local isoformat (19 chars, no
    offset). A naive-local ts vs an aware-UTC ts compare off by the local offset
    in the raw-string ORDER BYs the rehydrate gist uses."""
    import datetime as _dt
    import os
    import re

    mem = tmp_path / "memory"
    mem.mkdir()
    f = mem / "old_note.md"
    _write(f, "old-note", "reference", "d", "Body.")
    old = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    os.utime(f, (old, old))
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    mi.import_memory_dir(conn, mem, index_path=None, ts="2026-05-30T10:00:00")
    row = conn.execute(
        "SELECT created_at, updated_at FROM memories WHERE id='old-note'").fetchone()
    # Canonical: ends with 'Z', no '+00:00' offset, no microseconds.
    canonical = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    assert canonical.match(row["updated_at"]), row["updated_at"]
    assert canonical.match(row["created_at"]), row["created_at"]
    # Still reflects the FILE's age (mtime), 2026-01-01 UTC.
    assert row["updated_at"].startswith("2026-01-01")
    conn.close()


def test_import_persists_file_slug_and_sort_order(tmp_path):
    """C1: the underscore filename slug is NOT derivable from name: (which drops
    prefixes, e.g. feedback_email_routing.md → name: email-routing). It must be
    persisted as its own column, plus the MEMORY.md line order."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_email_routing.md", "email-routing", "feedback", "d", "B.")
    _write(mem / "user_language.md", "user-language", "user", "d", "B.")
    (mem / "MEMORY.md").write_text(
        "- [Lang](user_language.md) — hook L\n"             # FIRST in the index
        "- [Email](feedback_email_routing.md) — hook E\n")  # SECOND in the index
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    mi.import_memory_dir(conn, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    row = conn.execute(
        "SELECT file_slug, sort_order FROM memories WHERE id='email-routing'").fetchone()
    assert row["file_slug"] == "feedback_email_routing"  # underscore filename, not id
    assert row["sort_order"] == 1                          # second line in MEMORY.md
    first = conn.execute(
        "SELECT sort_order FROM memories WHERE id='user-language'").fetchone()
    assert first["sort_order"] == 0                        # first line
    conn.close()
