"""Tests for the human-correction write verbs (spec §14): pin/unpin + verify.
These back the /memory-pin and /memory-verify slash commands + the inbox importer."""
import pytest

from ultra_memory import memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _save(conn, id="m"):
    memory_lib.save_memory(conn, id=id, type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")


def test_set_pinned_sets_and_clears(tmp_path):
    conn = _db(tmp_path)
    _save(conn)
    memory_lib.set_pinned(conn, id="m", pinned=True, ts="2026-05-02T00:00:00", reason="hard rule")
    assert conn.execute("SELECT pinned FROM memories WHERE id='m'").fetchone()[0] == 1
    memory_lib.set_pinned(conn, id="m", pinned=False, ts="2026-05-03T00:00:00", reason="unpin")
    assert conn.execute("SELECT pinned FROM memories WHERE id='m'").fetchone()[0] == 0
    conn.close()


def test_set_pinned_missing_id_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(KeyError):
        memory_lib.set_pinned(conn, id="nope", pinned=True, ts="2026-05-02T00:00:00", reason="x")
    conn.close()


def test_set_pinned_audited(tmp_path):
    conn = _db(tmp_path)
    _save(conn)
    memory_lib.set_pinned(conn, id="m", pinned=True, ts="2026-05-02T00:00:00", reason="hard rule")
    row = conn.execute(
        "SELECT op, target_id FROM audit_log WHERE target_id='m' AND op='pin'").fetchone()
    assert row is not None
    conn.close()


def test_set_verified_stamps_last_verified(tmp_path):
    conn = _db(tmp_path)
    _save(conn)
    memory_lib.set_verified(conn, id="m", ts="2026-05-05T00:00:00")
    assert conn.execute(
        "SELECT last_verified FROM memories WHERE id='m'").fetchone()[0] == "2026-05-05T00:00:00"
    conn.close()


def test_set_verified_missing_id_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(KeyError):
        memory_lib.set_verified(conn, id="nope", ts="2026-05-05T00:00:00")
    conn.close()
