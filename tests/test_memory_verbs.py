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


# ---------------------------------------------------------------------------
# SP-3 Stage 4 (D7): set_pinned generalizes to (source_kind, source_id, ...).
# Memory pins keep flipping memories.pinned; knowledge pins upsert knowledge_pins.
# The legacy id= signature MUST keep working (back-compat shim) so SP-1's
# /memory-pin + the spool records + the existing tests above stay green.
# ---------------------------------------------------------------------------

def test_set_pinned_knowledge_upserts_knowledge_pins(tmp_path):
    conn = _db(tmp_path)
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="some-slug",
                          pinned=True, ts="2026-05-02T00:00:00", reason="hard rule")
    row = conn.execute(
        "SELECT slug, pinned, reason FROM knowledge_pins WHERE slug='some-slug'"
    ).fetchone()
    assert row is not None
    assert row[0] == "some-slug" and row[1] == 1 and row[2] == "hard rule"
    # No memory row was created for a knowledge pin (a wiki page has no memories row).
    assert conn.execute("SELECT 1 FROM memories WHERE id='some-slug'").fetchone() is None
    conn.close()


def test_set_pinned_knowledge_clears_and_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="slug-x",
                          pinned=True, ts="2026-05-02T00:00:00")
    # Re-pin (upsert) — still exactly one row, refreshed.
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="slug-x",
                          pinned=True, ts="2026-05-03T00:00:00", reason="re-pin")
    assert conn.execute(
        "SELECT COUNT(*) FROM knowledge_pins WHERE slug='slug-x'").fetchone()[0] == 1
    # Unpin → flag flips to 0 (row kept, build_gist filters on pinned=1).
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="slug-x",
                          pinned=False, ts="2026-05-04T00:00:00")
    assert conn.execute(
        "SELECT pinned FROM knowledge_pins WHERE slug='slug-x'").fetchone()[0] == 0
    conn.close()


def test_set_pinned_knowledge_audited(tmp_path):
    conn = _db(tmp_path)
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="slug-a",
                          pinned=True, ts="2026-05-02T00:00:00", reason="hard rule")
    row = conn.execute(
        "SELECT op, target_kind, target_id FROM audit_log "
        "WHERE target_id='slug-a' AND op='pin'").fetchone()
    assert row is not None and row[1] == "knowledge"
    conn.close()


def test_set_pinned_legacy_id_shim_still_pins_a_memory(tmp_path):
    """The old set_pinned(id=...) signature (SP-1 /memory-pin + spool records) must
    keep flipping memories.pinned — treated as source_kind='memory', source_id=id."""
    conn = _db(tmp_path)
    _save(conn)
    memory_lib.set_pinned(conn, id="m", pinned=True, ts="2026-05-02T00:00:00")
    assert conn.execute("SELECT pinned FROM memories WHERE id='m'").fetchone()[0] == 1
    conn.close()


def test_set_pinned_explicit_memory_source_kind(tmp_path):
    conn = _db(tmp_path)
    _save(conn)
    memory_lib.set_pinned(conn, source_kind="memory", source_id="m",
                          pinned=True, ts="2026-05-02T00:00:00")
    assert conn.execute("SELECT pinned FROM memories WHERE id='m'").fetchone()[0] == 1
    conn.close()


def test_set_pinned_unknown_source_kind_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ValueError):
        memory_lib.set_pinned(conn, source_kind="bogus", source_id="x",
                              pinned=True, ts="2026-05-02T00:00:00")
    conn.close()


def test_set_pinned_requires_a_source(tmp_path):
    """Neither id= nor source_id= supplied → a clear error, not a silent no-op."""
    conn = _db(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        memory_lib.set_pinned(conn, pinned=True, ts="2026-05-02T00:00:00")
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
