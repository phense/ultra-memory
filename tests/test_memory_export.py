import sqlite3

from ultra_memory import db
from ultra_memory import memory_export as mx
from ultra_memory import memory_import as mi
from ultra_memory import memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _write(p, name, typ, desc, body, sid="s-1"):
    p.write_text(
        f"---\nname: {name}\ndescription: \"{desc}\"\nmetadata: \n"
        f"  node_type: memory\n  type: {typ}\n  originSessionId: {sid}\n---\n\n{body}\n")


def test_export_filenames_match_source_filenames(tmp_path):
    """C1: a roundtrip must NOT rename files. The export filename + MEMORY.md link
    must use the underscore filename slug, not the hyphenated name: (which is the
    DB id)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_email_routing.md", "email-routing", "feedback", "d", "B.")
    _write(mem / "user_language.md", "user-language", "user", "d", "B.")
    (mem / "MEMORY.md").write_text(
        "- [Email routing](feedback_email_routing.md) — hook\n"
        "- [Lang](user_language.md) — hook\n")
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    mi.import_memory_dir(conn, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    out = tmp_path / "exp"
    mx.export_memory(conn, out, ts="2026-05-30T12:00:00")
    exported = {p.name for p in (out / "views").glob("*.md") if p.name != "MEMORY.md"}
    assert exported == {"feedback_email_routing.md", "user_language.md"}
    # MEMORY.md links use the underscore filenames, not the hyphenated ids
    index = (out / "views" / "MEMORY.md").read_text()
    assert "(feedback_email_routing.md)" in index
    assert "(email-routing.md)" not in index
    conn.close()


def test_export_memory_index_preserves_source_order(tmp_path):
    """C1: MEMORY.md curated order must survive (sort_order), not be re-sorted
    alphabetically by id (which would move the pinned top line)."""
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "zzz_pinned.md", "zzz-pinned", "project", "d", "B.")
    _write(mem / "aaa_other.md", "aaa-other", "reference", "d", "B.")
    (mem / "MEMORY.md").write_text(
        "- [Pinned](zzz_pinned.md) — top\n"     # curated FIRST despite 'zzz'
        "- [Other](aaa_other.md) — below\n")
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    mi.import_memory_dir(conn, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    out = tmp_path / "exp"
    mx.export_memory(conn, out, ts="2026-05-30T12:00:00")
    lines = [l for l in (out / "views" / "MEMORY.md").read_text().splitlines() if l.strip()]
    assert lines[0].startswith("- [Pinned](zzz_pinned.md)")  # source order, not alphabetical
    assert lines[1].startswith("- [Other](aaa_other.md)")
    conn.close()


def test_dump_restore_reopen_roundtrips_without_crash(tmp_path):
    """C4: the .sql dump is the sole committed rollback artifact (the live .db is
    gitignored). It must carry user_version so a restore → reopen (which re-runs
    migrate) does not re-apply migrations and crash with 'duplicate column name'."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 2
    out = tmp_path / "memory_export"
    mx.export_memory(conn, out, ts="2026-05-30T12:00:00")
    conn.close()

    dump = (out / "memory.dump.sql").read_text()
    assert "PRAGMA user_version" in dump  # version preserved in the rollback artifact

    # Restore the dump into a fresh file, then reopen THROUGH open_memory_db (which
    # calls migrate). This must not raise and the data must survive.
    restored_path = tmp_path / "restored.db"
    raw = db.connect(restored_path)
    raw.executescript(dump)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == version
    raw.close()

    reopened = memory_lib.open_memory_db(restored_path)  # runs migrate() again
    assert reopened.execute("SELECT title FROM memories WHERE id='m1'").fetchone()[0] == "t"
    reopened.close()


def test_export_writes_dump_snapshot_and_views(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="feedback-x", type="feedback",
                           title="Feedback X", body="Body X.",
                           ts="2026-05-30T10:00:00", description="one liner",
                           index_hook="short hook X")
    out = tmp_path / "memory_export"
    changed = mx.export_memory(conn, out, ts="2026-05-30T12:00:00")
    assert changed is True
    assert (out / "memory.dump.sql").exists()
    assert (out / "memory.snapshot.db").exists()
    # snapshot is a valid sqlite db with the row
    snap = sqlite3.connect(out / "memory.snapshot.db")
    assert snap.execute("SELECT title FROM memories WHERE id='feedback-x'").fetchone()[0] == "Feedback X"
    snap.close()
    # views regenerated
    view = (out / "views" / "feedback-x.md").read_text()
    assert "name: feedback-x" in view and "type: feedback" in view
    assert "Body X." in view
    index = (out / "views" / "MEMORY.md").read_text()
    assert "- [Feedback X](feedback-x.md) — short hook X" in index
    conn.close()


def test_export_skips_when_unchanged(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    out = tmp_path / "memory_export"
    assert mx.export_memory(conn, out, ts="2026-05-30T12:00:00") is True
    assert mx.export_memory(conn, out, ts="2026-05-30T12:05:00") is False  # unchanged
    conn.close()


def test_export_ignores_access_telemetry_churn(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    out = tmp_path / "memory_export"
    assert mx.export_memory(conn, out, ts="2026-05-30T12:00:00") is True
    memory_lib.record_access(conn, target_kind="memory", target_id="m1",
                             ts="2026-05-30T12:01:00")  # telemetry only
    assert mx.export_memory(conn, out, ts="2026-05-30T12:02:00") is False  # still skipped
    conn.close()
