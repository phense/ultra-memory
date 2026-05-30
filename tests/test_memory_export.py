import sqlite3

from ultra_memory import memory_export as mx
from ultra_memory import memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


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
