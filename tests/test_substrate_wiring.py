"""SP-3 Stage 7a — self-improvement SUBSTRATE wiring (columns/writers only).

This is the substrate the §7a self-improvement LOOP (SP-6/SP-7) will later read —
NOT the loop itself. Stage 7a ships ONLY:

  1. `outcome_signal=` on record_session_event (D13): the optional per-event
     deterministic outcome hint persists into session_events.outcome_signal AND
     replays from the durable spool. The idempotency event_key is UNCHANGED
     (outcome_signal is payload, not part of the key). A free-text
     `kind='skill_learning_candidate'` event round-trips.
  2. `created_by` per writer (D16): save_memory threads created_by (default
     'human') and it is stamped correctly at each engine call site — 'import'
     (memory_import), 'agent' (agent-initiated save), 'background_review'
     (Tier-2), 'human' (CLI / /memory-* verbs).
  3. A generic Learnings projection (D14/D15): memory_export regenerates a
     Learnings-style markdown projection filtered by a skill tag, the way
     memory_export regenerates views — AGNOSTIC (consumer-fed output path + skill
     tag; no Trading literal) and DETERMINISTIC (re-run = byte-identical).

The LOOP (GEPA-lite / scoring / self-reversion) is explicitly NOT built here.
"""
from pathlib import Path

from ultra_memory import memory_export, memory_import, memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


# ---------------------------------------------------------------------------
# 1. outcome_signal on record_session_event (D13).
# ---------------------------------------------------------------------------

def test_record_session_event_persists_outcome_signal(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_session_event(
        conn, session_id="s1", kind="skill_learning_candidate",
        title="risk-manager invoked", ts="2026-05-31T10:00:00",
        detail="ran the pre-trade gate", outcome_signal="tests_passed")
    row = conn.execute(
        "SELECT kind, outcome_signal FROM session_events WHERE session_id='s1'"
    ).fetchone()
    assert row["kind"] == "skill_learning_candidate"
    assert row["outcome_signal"] == "tests_passed"
    conn.close()


def test_outcome_signal_defaults_null_when_omitted(tmp_path):
    """Inert by default: an event recorded without the new kwarg stores NULL —
    byte-identical to pre-Stage-7a behavior."""
    conn = _db(tmp_path)
    memory_lib.record_session_event(
        conn, session_id="s1", kind="task_done", title="t",
        ts="2026-05-31T10:00:00")
    row = conn.execute(
        "SELECT outcome_signal FROM session_events WHERE session_id='s1'"
    ).fetchone()
    assert row["outcome_signal"] is None
    conn.close()


def test_outcome_signal_not_part_of_event_key_idempotency(tmp_path):
    """D13 invariant: outcome_signal is PAYLOAD, not part of the idempotency key.
    Two events identical on (session, ts, kind, title, detail) but differing only
    in outcome_signal still collide to ONE row (the key is unchanged) — the first
    write wins via INSERT OR IGNORE."""
    conn = _db(tmp_path)
    k1 = memory_lib.record_session_event(
        conn, session_id="s1", kind="note", title="same", ts="2026-05-31T10:00:00",
        detail="d", outcome_signal="tests_passed")
    k2 = memory_lib.record_session_event(
        conn, session_id="s1", kind="note", title="same", ts="2026-05-31T10:00:00",
        detail="d", outcome_signal="trade_win")
    assert k1 == k2  # same content-addressed key
    rows = conn.execute(
        "SELECT outcome_signal FROM session_events WHERE event_key=?", (k1,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome_signal"] == "tests_passed"  # first write won
    conn.close()


def _spool(spool_dir, rec):
    """Write a spool record exactly as _spool() in memory_lib does (content-hash
    filename), mirroring the existing test_memory_spool_replay convention."""
    import hashlib
    import json
    spool_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (spool_dir / f"{key}.json").write_text(payload, encoding="utf-8")


def test_record_session_event_spool_dict_carries_outcome_signal(tmp_path):
    """The spool dict (the replay contract) must include outcome_signal so a
    busy-casualty event is replayed with its outcome hint, not dropped."""
    conn = _db(tmp_path)
    real = memory_lib._write_txn
    captured = {}

    def capture(c, work, **kw):
        captured["spool"] = kw.get("spool")
        return real(c, work, **kw)

    memory_lib._write_txn = capture
    try:
        memory_lib.record_session_event(
            conn, session_id="s1", kind="skill_learning_candidate",
            title="t", ts="2026-05-31T10:00:00", detail="d",
            outcome_signal="commit_landed")
    finally:
        memory_lib._write_txn = real
    assert captured["spool"]["op"] == "record_session_event"
    assert captured["spool"]["outcome_signal"] == "commit_landed"
    conn.close()


def test_spooled_outcome_signal_replays_into_column(tmp_path):
    """A spooled record_session_event carrying outcome_signal replays the hint into
    the session_events.outcome_signal column (the event_key is a non-param key that
    replay_spool filters out by signature)."""
    conn = _db(tmp_path)
    sd = tmp_path / "memory_spool"
    _spool(sd, {"op": "record_session_event", "session_id": "s2",
                "kind": "skill_learning_candidate", "title": "t",
                "ts": "2026-05-31T10:00:00", "detail": "d", "files": None,
                "refs": None, "event_key": "deadbeef",
                "outcome_signal": "commit_landed"})
    summary = memory_lib.replay_spool(conn)
    assert summary["replayed"] == 1 and summary["failed"] == 0, summary
    row = conn.execute(
        "SELECT outcome_signal FROM session_events WHERE session_id='s2'"
    ).fetchone()
    assert row["outcome_signal"] == "commit_landed"
    conn.close()


# ---------------------------------------------------------------------------
# 2. created_by per writer (D16).
# ---------------------------------------------------------------------------

def test_created_by_defaults_human(tmp_path):
    """Default 'human' = the safe-immutable default the §7a provenance gate reads."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00")
    row = conn.execute("SELECT created_by FROM memories WHERE id='m1'").fetchone()
    assert row["created_by"] == "human"
    conn.close()


def test_created_by_agent_path(tmp_path):
    """The agent-initiated save path stamps 'agent' (distinct from the human CLI)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", created_by="agent")
    row = conn.execute("SELECT created_by FROM memories WHERE id='m1'").fetchone()
    assert row["created_by"] == "agent"
    conn.close()


def test_created_by_background_review_path(tmp_path):
    """A Tier-2 maintenance-origin write stamps 'background_review' (auto-editable
    by the SP-7 loop, unlike 'human')."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", created_by="background_review")
    row = conn.execute("SELECT created_by FROM memories WHERE id='m1'").fetchone()
    assert row["created_by"] == "background_review"
    conn.close()


def test_created_by_import_path(tmp_path):
    """memory_import stamps 'import' on every imported row."""
    conn = _db(tmp_path)
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "feedback_x.md").write_text(
        "---\nname: feedback_x\nmetadata:\n  type: feedback\n---\n\nbody text\n")
    memory_import.import_memory_dir(conn, mem_dir, ts="2026-05-31T10:00:00")
    row = conn.execute(
        "SELECT created_by FROM memories WHERE id='feedback_x'").fetchone()
    assert row["created_by"] == "import"
    conn.close()


def test_created_by_cli_human_path(tmp_path):
    """The /memory-* CLI save verb stamps 'human' (the human-authored origin)."""
    from ultra_memory import memory_cli
    body = tmp_path / "body.txt"
    body.write_text("a human-written durable memory")
    rc = memory_cli.main(
        ["save", "--id", "m1", "--type", "reference", "--title", "T",
         "--from-file", str(body)],
        db_path=str(tmp_path / "m.db"), ts="2026-05-31T10:00:00")
    assert rc == 0
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    row = conn.execute("SELECT created_by FROM memories WHERE id='m1'").fetchone()
    assert row["created_by"] == "human"
    conn.close()


def test_created_by_preserved_on_update(tmp_path):
    """An update via save_memory must keep stamping created_by (an 'agent' re-save
    stays 'agent'); the column never silently reverts to the default on UPDATE."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", created_by="agent")
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b2",
                           ts="2026-05-31T11:00:00", created_by="agent")
    row = conn.execute(
        "SELECT body, created_by FROM memories WHERE id='m1'").fetchone()
    assert row["body"] == "b2" and row["created_by"] == "agent"
    conn.close()


def test_save_memory_spool_dict_carries_created_by(tmp_path):
    """The save_memory spool dict carries created_by so a busy-casualty save keeps
    its provenance on replay."""
    conn = _db(tmp_path)
    real = memory_lib._write_txn
    captured = {}

    def capture(c, work, **kw):
        captured["spool"] = kw.get("spool")
        return real(c, work, **kw)

    memory_lib._write_txn = capture
    try:
        memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                               ts="2026-05-31T10:00:00", created_by="agent")
    finally:
        memory_lib._write_txn = real
    assert captured["spool"]["op"] == "save_memory"
    assert captured["spool"]["created_by"] == "agent"
    conn.close()


def test_spooled_created_by_replays_into_column(tmp_path):
    """A spooled save_memory carrying created_by replays the provenance correctly."""
    conn = _db(tmp_path)
    sd = tmp_path / "memory_spool"
    _spool(sd, {"op": "save_memory", "id": "m1", "type": "project", "title": "t",
                "body": "b", "ts": "2026-05-31T10:00:00", "origin_session_id": None,
                "description": None, "index_hook": None, "node_type": "memory",
                "file_slug": None, "sort_order": None,
                "created_at": "2026-05-31T10:00:00",
                "updated_at": "2026-05-31T10:00:00", "topic": None,
                "created_by": "agent"})
    summary = memory_lib.replay_spool(conn)
    assert summary["replayed"] == 1 and summary["failed"] == 0, summary
    row = conn.execute("SELECT created_by FROM memories WHERE id='m1'").fetchone()
    assert row["created_by"] == "agent"
    conn.close()


# ---------------------------------------------------------------------------
# 3. Generic Learnings projection (D14/D15) — agnostic + deterministic.
# ---------------------------------------------------------------------------

def _seed_learnings(conn):
    """Seed a tagged store: two memories tagged for skill 'risk-manager' (one via
    index_hook, one via refs) and one for a different skill — to prove the filter
    is by tag, not by everything."""
    memory_lib.save_memory(
        conn, id="L-risk-1", type="project", title="Block-by-default sizing",
        body="Always size from R-multiple, never gut feel.",
        ts="2026-05-31T10:00:00", index_hook="skill:risk-manager",
        created_by="agent")
    memory_lib.save_memory(
        conn, id="L-risk-2", type="project", title="Circuit breaker on 3 losses",
        body="Halt the day after three consecutive stop-outs.",
        ts="2026-05-31T10:01:00", index_hook="skill:risk-manager",
        created_by="background_review")
    memory_lib.save_memory(
        conn, id="L-pine-1", type="project", title="Mandatory stop in v5",
        body="Every Pine strategy must declare a stop.",
        ts="2026-05-31T10:02:00", index_hook="skill:pine-script",
        created_by="agent")


def test_learnings_projection_filters_by_skill_tag(tmp_path):
    conn = _db(tmp_path)
    _seed_learnings(conn)
    out = tmp_path / "risk-Learnings.md"
    memory_export.export_learnings_projection(
        conn, out, skill_tag="skill:risk-manager")
    text = out.read_text()
    assert "Block-by-default sizing" in text
    assert "Circuit breaker on 3 losses" in text
    assert "Mandatory stop in v5" not in text  # other skill excluded
    conn.close()


def test_learnings_projection_is_deterministic(tmp_path):
    """Re-run = byte-identical (the way memory_export regenerates views)."""
    conn = _db(tmp_path)
    _seed_learnings(conn)
    out = tmp_path / "risk-Learnings.md"
    memory_export.export_learnings_projection(
        conn, out, skill_tag="skill:risk-manager")
    first = out.read_bytes()
    memory_export.export_learnings_projection(
        conn, out, skill_tag="skill:risk-manager")
    second = out.read_bytes()
    assert first == second
    conn.close()


def test_learnings_projection_is_agnostic_consumer_fed(tmp_path):
    """The projection takes the output PATH and the skill TAG as parameters — no
    Trading literal, no hardcoded skill name in the engine signature. An arbitrary
    consumer tag projects to an arbitrary consumer path."""
    conn = _db(tmp_path)
    memory_lib.save_memory(
        conn, id="L-x", type="project", title="Some lesson",
        body="A lesson body.", ts="2026-05-31T10:00:00",
        index_hook="skill:any-consumer-skill", created_by="agent")
    out = tmp_path / "sub" / "any.md"
    memory_export.export_learnings_projection(
        conn, out, skill_tag="skill:any-consumer-skill")
    assert out.is_file()
    assert "Some lesson" in out.read_text()
    conn.close()


def test_learnings_projection_writes_atomically_via_tmp_replace(tmp_path):
    """R3 FIX 3: the git-tracked Learnings.md projection must be written atomically —
    to a `<path>.tmp` then os.replace into place — so a SIGKILL/crash mid-write never
    truncates the projection (Stage 3 then commits a torn file). We observe os.replace
    is invoked with a `.tmp` source pointing at the final path, and that the final
    content is whole with no leftover `.tmp`."""
    import os as _os

    conn = _db(tmp_path)
    _seed_learnings(conn)
    out = tmp_path / "risk-Learnings.md"

    seen = {}
    real_replace = _os.replace

    def _spy_replace(src, dst):
        seen["src"] = str(src)
        seen["dst"] = str(dst)
        return real_replace(src, dst)

    orig = memory_export.os.replace
    memory_export.os.replace = _spy_replace
    try:
        memory_export.export_learnings_projection(
            conn, out, skill_tag="skill:risk-manager")
    finally:
        memory_export.os.replace = orig

    # The atomic swap ran: tmp source -> final dst.
    assert seen.get("src", "").endswith(".tmp")
    assert seen.get("dst") == str(out)
    # The final file is whole; no torn temp left behind.
    text = out.read_text()
    assert "Block-by-default sizing" in text
    assert "Circuit breaker on 3 losses" in text
    assert list(tmp_path.glob("*.tmp")) == []
    conn.close()


def test_learnings_projection_excludes_inactive(tmp_path):
    """A tombstoned / redirected learning is not projected (mirrors views' active
    filter) — a deleted lesson must not reappear in the per-skill surface."""
    conn = _db(tmp_path)
    memory_lib.save_memory(
        conn, id="L-1", type="project", title="Kept lesson", body="kept",
        ts="2026-05-31T10:00:00", index_hook="skill:risk-manager",
        created_by="agent")
    memory_lib.save_memory(
        conn, id="L-2", type="project", title="Gone lesson", body="gone",
        ts="2026-05-31T10:01:00", index_hook="skill:risk-manager",
        created_by="agent")
    memory_lib.delete(conn, id="L-2", reason="superseded", tier="durable",
                      ts="2026-05-31T11:00:00")
    out = tmp_path / "risk-Learnings.md"
    memory_export.export_learnings_projection(
        conn, out, skill_tag="skill:risk-manager")
    text = out.read_text()
    assert "Kept lesson" in text
    assert "Gone lesson" not in text
    conn.close()
