"""Tests for replay_spool (A9): drain memory_spool/ — re-apply each spooled write
via its op, deleting the file on success; keep + record anything that fails."""
import hashlib
import json

from ultra_memory import memory_lib


def _spool(spool_dir, rec):
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (spool_dir / f"{key}.json").write_text(payload, encoding="utf-8")


def test_replay_empty_is_noop(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    assert memory_lib.replay_spool(conn) == {"replayed": 0, "failed": 0, "errors": []}
    conn.close()


def test_replay_drains_save_memory(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    _spool(sd, {"op": "save_memory", "id": "r1", "type": "project", "title": "t",
                "body": "b", "ts": "2026-05-01T00:00:00", "origin_session_id": None,
                "description": None, "index_hook": None, "node_type": "memory",
                "file_slug": None, "sort_order": None,
                "created_at": "2026-05-01T00:00:00", "updated_at": "2026-05-01T00:00:00"})
    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1 and s["failed"] == 0
    assert conn.execute("SELECT 1 FROM memories WHERE id='r1'").fetchone() is not None
    assert not list(sd.glob("*.json"))  # drained on success
    conn.close()


def test_replay_drops_non_param_keys(tmp_path):
    """record_session_event spools an `event_key` that is NOT a fn param — replay
    must filter it out (by signature), not crash."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    _spool(sd, {"op": "record_session_event", "session_id": "s1", "kind": "task_done",
                "title": "x", "ts": "2026-05-01T00:00:00", "detail": None,
                "files": None, "refs": None, "event_key": "deadbeef"})
    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1, s
    conn.close()


def test_replay_drains_knowledge_pin(tmp_path):
    """SP-3 Stage 4: a spooled knowledge pin (new source_kind/source_id arg shape)
    replays into knowledge_pins via the set_pinned dispatch entry."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    _spool(sd, {"op": "set_pinned", "source_kind": "knowledge",
                "source_id": "spooled-slug", "pinned": True,
                "ts": "2026-05-01T00:00:00", "reason": "manual pin"})
    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1 and s["failed"] == 0, s
    assert conn.execute(
        "SELECT pinned FROM knowledge_pins WHERE slug='spooled-slug'"
    ).fetchone()[0] == 1
    assert not list(sd.glob("*.json"))
    conn.close()


def test_replay_drains_legacy_id_pin(tmp_path):
    """Back-compat: a pre-SP-3 spooled set_pinned record (legacy id= arg) must still
    replay and flip the memory's pinned flag through the shim."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    memory_lib.save_memory(conn, id="legacy", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    _spool(sd, {"op": "set_pinned", "id": "legacy", "pinned": True,
                "ts": "2026-05-01T00:00:00", "reason": "manual pin"})
    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1 and s["failed"] == 0, s
    assert conn.execute("SELECT pinned FROM memories WHERE id='legacy'").fetchone()[0] == 1
    conn.close()


def test_replay_unknown_op_kept_and_recorded(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    (sd / "bad.json").write_text('{"op":"frobnicate","x":1}', encoding="utf-8")
    s = memory_lib.replay_spool(conn)
    assert s["failed"] == 1 and s["errors"]
    assert list(sd.glob("*.json"))  # kept for inspection, not silently dropped
    conn.close()
