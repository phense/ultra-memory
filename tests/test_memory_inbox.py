"""Tests for the human-correction inbox importer (spec §14): the operator types directive
lines into a watched inbox file; the importer applies them via memory_lib and clears
the file, preserving any unrecognized free-text under an Unprocessed section."""
from ultra_memory import memory_inbox, memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def test_parse_inbox_directives_and_notes():
    text = "# how to use\n\npin foo-123\nunpin bar-9\nverify baz-7\nplease double-check the tax rule\n"
    d = memory_inbox.parse_inbox(text)
    ops = {(x["op"], x.get("id")) for x in d}
    assert ("pin", "foo-123") in ops
    assert ("unpin", "bar-9") in ops
    assert ("verify", "baz-7") in ops
    assert any(x["op"] == "note" for x in d)  # free prose captured, not a directive
    # comment/blank lines are not directives
    assert not any(x["op"] != "note" and x.get("id", "").startswith("#") for x in d)


def test_import_inbox_applies_directives_and_clears(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="foo-123", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    inbox = tmp_path / "inbox.md"
    inbox.write_text("pin foo-123\nverify foo-123\n", encoding="utf-8")
    summary = memory_inbox.import_inbox(conn, inbox, ts="2026-05-02T00:00:00")
    row = conn.execute(
        "SELECT pinned, last_verified FROM memories WHERE id='foo-123'").fetchone()
    assert row[0] == 1 and row[1] == "2026-05-02T00:00:00"
    assert summary["applied"] == 2
    assert "pin foo-123" not in inbox.read_text(encoding="utf-8")  # consumed
    conn.close()


def test_import_inbox_preserves_unprocessed_notes(tmp_path):
    conn = _db(tmp_path)
    inbox = tmp_path / "inbox.md"
    inbox.write_text("double-check the german tax fence\n", encoding="utf-8")
    summary = memory_inbox.import_inbox(conn, inbox, ts="2026-05-02T00:00:00")
    after = inbox.read_text(encoding="utf-8")
    assert "double-check the german tax fence" in after  # not silently lost
    assert summary["notes"] >= 1
    conn.close()


def test_import_inbox_unknown_id_recorded_not_crash(tmp_path):
    conn = _db(tmp_path)
    inbox = tmp_path / "inbox.md"
    inbox.write_text("pin ghost-1\n", encoding="utf-8")
    summary = memory_inbox.import_inbox(conn, inbox, ts="2026-05-02T00:00:00")
    assert summary["applied"] == 0
    assert summary["errors"] and any("ghost-1" in e for e in summary["errors"])
    conn.close()


def test_import_inbox_missing_file_is_noop(tmp_path):
    conn = _db(tmp_path)
    summary = memory_inbox.import_inbox(conn, tmp_path / "nope.md", ts="2026-05-02T00:00:00")
    assert summary["applied"] == 0 and summary["notes"] == 0
    conn.close()
