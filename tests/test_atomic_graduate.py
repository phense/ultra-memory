"""Atomic Graduation (Recall-Reflex 5.2) — the fenced atomic_graduate beat.

The drain is DETERMINISTIC (no LLM); gateway_run / signal_match / recall_fn are injected
(production binds the real ones; here, recorders/stubs). Tests cover the three-way dedup-gate
(merge / skip-grey / create), the blast-radius cap, the kill-switch, and per-candidate fail-open.
"""
from ultra_memory import memory_lib
from ultra_memory.maintenance import atomic_graduate as ag
from ultra_memory.maintenance import session_ingest as si

TS = "2026-06-05T00:00:00"


def _db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "m.db"))


def _seed(conn, **over):
    c = {"kind": "gotcha", "signal": "onnxruntime NoSuchFile model.onnx",
         "title": "fastembed cache purge", "body": "pin via persistent_cache_dir",
         "topic": "trading"}
    c.update(over)
    si._save_atomic_candidates(conn, [c], session_id="S1", ts=TS)


def _gw():
    calls = []

    def gw(verb, args, content):
        calls.append((verb, list(args), content))
    gw.calls = calls
    return gw


def test_create_on_novel_candidate(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", cap=3)
    assert res["created"] == 1
    verbs = [c[0] for c in gw.calls]
    assert "create-page" in verbs and "register-index" in verbs
    cp = next(c for c in gw.calls if c[0] == "create-page")
    assert "## Signal" in cp[2] and "onnxruntime NoSuchFile" in cp[2]
    assert si.pending_atomic_candidates(conn) == []   # resolved
    conn.close()


def test_dedup_gate_merges_on_high_cosine(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw,
        signal_match=lambda *a, **k: ("existing-slug", 0.95),
        wiki_root=tmp_path / "wiki", cap=3)
    assert res["merged"] == 1 and res["created"] == 0
    assert [c[0] for c in gw.calls] == ["append-validation-log"]
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


def test_dedup_gate_skips_grey_zone_and_leaves_unresolved(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw,
        signal_match=lambda *a, **k: ("x", 0.81),
        wiki_root=tmp_path / "wiki", cap=3)
    assert res["skipped"] == 1 and res["created"] == 0 and gw.calls == []
    assert len(si.pending_atomic_candidates(conn)) == 1
    conn.close()


def test_blast_radius_cap(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        _seed(conn, signal=f"distinct signal number {i}", title=f"candidate {i}")
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", cap=2)
    assert res["created"] == 2
    assert len(si.pending_atomic_candidates(conn)) == 3   # rest unresolved
    conn.close()


def test_disabled_by_killswitch(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={"ATOMIC_GRADUATE_DISABLE": "1"}, gateway_run=gw,
        signal_match=lambda *a, **k: None, wiki_root=tmp_path / "wiki", cap=3)
    assert res["mode"] == "disabled" and gw.calls == []
    assert len(si.pending_atomic_candidates(conn)) == 1
    conn.close()


def test_per_candidate_fail_open(tmp_path):
    conn = _db(tmp_path); _seed(conn)

    def gw_raises(verb, args, content):
        raise RuntimeError("gateway boom")
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw_raises, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", cap=3)
    assert res["created"] == 0
    assert len(si.pending_atomic_candidates(conn)) == 1   # unresolved → retry
    conn.close()
