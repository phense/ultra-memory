"""Subsystem 4 (session-as-ingestion-stream) — slice 4a: the deterministic
substrate (NO LLM). The session-ingest queue + the transcript digest builder that
the throttled OAuth drain pass (4b) consumes. Ships-disabled by default
(SESSION_INGEST_ENABLE); the enqueue is a no-op until armed.
"""
import json

from ultra_memory import memory_lib
from ultra_memory.maintenance import session_ingest as si

TS = "2026-06-02T00:00:00Z"


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "m.db"))


def _transcript(tmp_path, events, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


def _msg(role, *blocks):
    return {"message": {"role": role, "content": list(blocks)}}


# --------------------------------------------------------------------------- #
# The queue.
# --------------------------------------------------------------------------- #

def test_enqueue_records_pending_event(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path="/x/t.jsonl", ts=TS)
    row = conn.execute(
        "SELECT kind, session_id, detail, resolved FROM session_events "
        "WHERE kind=?", (si.PENDING_KIND,)).fetchone()
    assert row["session_id"] == "s-1" and row["detail"] == "/x/t.jsonl"
    assert row["resolved"] == 0
    conn.close()


def test_enqueue_idempotent_same_session_ts(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path="/x/t.jsonl", ts=TS)
    si.enqueue(conn, session_id="s-1", transcript_path="/x/t.jsonl", ts=TS)
    n = conn.execute("SELECT COUNT(*) c FROM session_events WHERE kind=?",
                     (si.PENDING_KIND,)).fetchone()["c"]
    assert n == 1                                  # event_key dedup
    conn.close()


def test_enqueue_if_enabled_gated(tmp_path):
    conn = _conn(tmp_path)
    assert si.enqueue_if_enabled(conn, session_id="s", transcript_path="/x",
                                 ts=TS, env={}) is False
    assert si.pending_sessions(conn) == []
    assert si.enqueue_if_enabled(conn, session_id="s", transcript_path="/x",
                                 ts=TS, env={"SESSION_INGEST_ENABLE": "1"}) is True
    assert len(si.pending_sessions(conn)) == 1
    conn.close()


def test_pending_sessions_unresolved_only(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path="/a", ts=TS)
    si.enqueue(conn, session_id="s-2", transcript_path="/b", ts="2026-06-02T01:00:00Z")
    pend = si.pending_sessions(conn)
    assert {p["session_id"] for p in pend} == {"s-1", "s-2"}
    si.mark_resolved(conn, event_id=pend[0]["event_id"])
    pend2 = si.pending_sessions(conn)
    assert len(pend2) == 1 and pend2[0]["session_id"] == "s-2"
    conn.close()


def test_pending_sessions_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        si.enqueue(conn, session_id=f"s-{i}", transcript_path=f"/{i}",
                   ts=f"2026-06-02T0{i}:00:00Z")
    assert len(si.pending_sessions(conn, limit=3)) == 3
    conn.close()


def test_pending_exposes_transcript_path(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path="/x/t.jsonl", ts=TS)
    p = si.pending_sessions(conn)[0]
    assert p["transcript_path"] == "/x/t.jsonl" and p["ts"] == TS
    conn.close()


# --------------------------------------------------------------------------- #
# The digest builder.
# --------------------------------------------------------------------------- #

def test_build_digest_includes_user_and_assistant_text(tmp_path):
    t = _transcript(tmp_path, [
        _msg("user", {"type": "text", "text": "Always size with R-multiples."}),
        _msg("assistant", {"type": "text", "text": "Understood, sizing by R."}),
    ])
    digest = si.build_digest(t)
    assert "Always size with R-multiples." in digest
    assert "Understood, sizing by R." in digest


def test_build_digest_notes_tool_name_not_result_body(tmp_path):
    """Tool RESULT bodies are excluded (size + secret surface); only the tool NAME
    is noted, so the LLM sees that work happened without ingesting raw output."""
    t = _transcript(tmp_path, [
        _msg("assistant", {"type": "tool_use", "name": "Bash",
                           "input": {"command": "git commit"}}),
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "content": "SECRET_TOKEN=sk-leak-me-123456"}]}},
    ])
    digest = si.build_digest(t)
    assert "Bash" in digest
    assert "sk-leak-me-123456" not in digest


def test_build_digest_bounded(tmp_path):
    big = "x" * 50000
    t = _transcript(tmp_path, [_msg("user", {"type": "text", "text": big})])
    digest = si.build_digest(t, max_chars=2000)
    assert len(digest) <= 2000


def test_build_digest_missing_file_failopen(tmp_path):
    assert si.build_digest(tmp_path / "nope.jsonl") == ""


def test_build_digest_tolerates_bad_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('not json\n' + json.dumps(_msg("user", {"type": "text", "text": "ok"})) + '\n{bad')
    assert "ok" in si.build_digest(p)


# --------------------------------------------------------------------------- #
# Stop-hook wiring (checkpoint.run enqueues a pending ingest when armed).
# --------------------------------------------------------------------------- #

def _material_transcript(tmp_path):
    p = tmp_path / "sess.jsonl"
    p.write_text(json.dumps({"message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x"}}]}}))
    return p


def _ready_db(tmp_path):
    p = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(p))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('import_complete','1')")
    conn.commit()
    conn.close()
    return p


def test_checkpoint_enqueues_when_enabled(tmp_path, monkeypatch):
    from ultra_memory.hooks import checkpoint
    monkeypatch.setenv("SESSION_INGEST_ENABLE", "1")
    db = _ready_db(tmp_path)
    t = _material_transcript(tmp_path)
    checkpoint.run({"session_id": "s-hook", "transcript_path": str(t)},
                   db_path=db, ts=TS)
    conn = memory_lib.open_memory_db(str(db))
    assert any(p["session_id"] == "s-hook" for p in si.pending_sessions(conn))
    conn.close()


def test_checkpoint_no_enqueue_when_disabled(tmp_path, monkeypatch):
    from ultra_memory.hooks import checkpoint
    monkeypatch.delenv("SESSION_INGEST_ENABLE", raising=False)
    db = _ready_db(tmp_path)
    t = _material_transcript(tmp_path)
    checkpoint.run({"session_id": "s-hook", "transcript_path": str(t)},
                   db_path=db, ts=TS)
    conn = memory_lib.open_memory_db(str(db))
    assert si.pending_sessions(conn) == []
    conn.close()
