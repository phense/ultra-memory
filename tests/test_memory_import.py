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


def test_import_today_file_never_crashes_on_junk(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    n, warnings = mi.import_today_file(conn, "garbage\n## not a time | x\nmore", day="2026-05-27")
    assert n == 0  # '## not a time' is not a valid HH:MM header
    conn.close()
