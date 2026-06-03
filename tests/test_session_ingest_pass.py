"""Subsystem 4 slice 4b — the OAuth drain pass: one `claude` call per pending
session → {extracted_knowledge, correction} → the memory store. OAuth-only
(injected runner), fail-open, idempotent, gated SESSION_INGEST_ENABLE (default OFF).
"""
import json
import types

from ultra_memory import memory_lib
from ultra_memory.maintenance import session_ingest as si

TS = "2026-06-02T00:00:00Z"
ON = {"SESSION_INGEST_ENABLE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "m.db"))


def _transcript(tmp_path, name="t.jsonl"):
    p = tmp_path / name
    p.write_text(json.dumps({"message": {"role": "user", "content": [
        {"type": "text", "text": "Always close US options before the German tax fence."}]}}))
    return p


def _payload(facts=None, correction=None, skill_learnings=None):
    return {"extracted_knowledge": facts or [],
            "skill_learnings": skill_learnings or [],
            "correction_detected": correction is not None,
            "correction": correction}


def _runner(payload):
    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return runner


def _runner_raises():
    def runner(cmd, **kw):
        raise RuntimeError("claude CLI exploded")
    return runner


# --------------------------------------------------------------------------- #
# parse_ingest — grounded-or-dropped.
# --------------------------------------------------------------------------- #

def test_parse_ingest_extracts_facts_and_correction():
    out = json.dumps(_payload(
        facts=[{"title": "Tax fence", "body": "Close US options before year-end."}],
        correction={"behavior": "committed to main", "do_instead": "branch first"}))
    r = si.parse_ingest(out)
    assert len(r["facts"]) == 1 and r["facts"][0]["title"] == "Tax fence"
    assert r["correction"]["do_instead"] == "branch first"


def test_parse_ingest_handles_code_fence():
    out = "```json\n" + json.dumps(_payload(facts=[{"title": "t", "body": "b"}])) + "\n```"
    assert len(si.parse_ingest(out)["facts"]) == 1


def test_parse_ingest_drops_factless_entries():
    out = json.dumps({"extracted_knowledge": [{"title": "", "body": ""},
                                              {"title": "ok", "body": "real."}],
                      "correction_detected": False, "correction": None})
    r = si.parse_ingest(out)
    assert [f["title"] for f in r["facts"]] == ["ok"]


def test_parse_ingest_caps_facts():
    facts = [{"title": f"t{i}", "body": "b."} for i in range(50)]
    r = si.parse_ingest(json.dumps(_payload(facts=facts)), max_facts=8)
    assert len(r["facts"]) == 8


def test_parse_ingest_malformed_raises():
    import pytest
    with pytest.raises(ValueError):
        si.parse_ingest("not json at all")


# --------------------------------------------------------------------------- #
# parse_ingest — skill_learnings (grounded to skills_used).
# --------------------------------------------------------------------------- #

def test_parse_ingest_extracts_skill_learnings_grounded():
    out = json.dumps(_payload(skill_learnings=[
        {"skill": "backtest", "title": "Fill at bid/ask", "body": "Never the mid."},
        {"skill": "not-used", "title": "x", "body": "y"}]))   # not in skills_used → dropped
    r = si.parse_ingest(out, skills_used={"backtest", "risk-manager"})
    assert [s["skill"] for s in r["skill_learnings"]] == ["backtest"]
    assert r["skill_learnings"][0]["title"] == "Fill at bid/ask"


def test_parse_ingest_drops_incomplete_skill_learnings():
    out = json.dumps(_payload(skill_learnings=[
        {"skill": "backtest", "title": "", "body": "b"},      # no title
        {"skill": "", "title": "t", "body": "b"},             # no skill
        {"skill": "backtest", "title": "ok", "body": "real"}]))
    r = si.parse_ingest(out, skills_used={"backtest"})
    assert [s["title"] for s in r["skill_learnings"]] == ["ok"]


def test_parse_ingest_skill_learnings_default_empty_without_skills_used():
    out = json.dumps(_payload(skill_learnings=[{"skill": "backtest", "title": "t", "body": "b"}]))
    assert si.parse_ingest(out)["skill_learnings"] == []   # skills_used=None → ground to empty


def test_save_skill_learnings_writes_learning_rows(tmp_path):
    conn = _conn(tmp_path)
    n = si._save_skill_learnings(
        conn, [{"skill": "backtest", "title": "Fill at bid/ask", "body": "Never the mid."}],
        ts=TS)
    assert n == 1
    row = conn.execute(
        "SELECT node_type, index_hook, created_by FROM memories "
        "WHERE index_hook='backtest'").fetchone()
    assert row["node_type"] == "learning" and row["index_hook"] == "backtest"
    assert row["created_by"] == "background_review"
    # idempotent: re-save same content → no second row
    si._save_skill_learnings(conn, [{"skill": "backtest", "title": "Fill at bid/ask",
                                     "body": "Never the mid."}], ts=TS)
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE index_hook='backtest'").fetchone()[0] == 1
    conn.close()


def test_skills_used_and_resolve_candidates(tmp_path):
    conn = _conn(tmp_path)
    for skill in ("backtest", "risk-manager", "backtest"):   # dup backtest → de-duped
        memory_lib.record_session_event(
            conn, session_id="S1", kind="skill_learning_candidate",
            title=f"{skill}: skill invoked, Learnings.md not updated (x)", ts=TS,
            detail="expected=p")
    memory_lib.record_session_event(   # other session — untouched
        conn, session_id="S2", kind="skill_learning_candidate",
        title="pine-script: skill invoked, Learnings.md not updated (x)", ts=TS, detail="e")
    assert si.skills_used_for(conn, "S1") == {"backtest", "risk-manager"}
    si.resolve_skill_candidates(conn, "S1")
    open_s1 = conn.execute("SELECT COUNT(*) FROM session_events WHERE session_id='S1' "
                           "AND kind='skill_learning_candidate' AND resolved=0").fetchone()[0]
    open_s2 = conn.execute("SELECT COUNT(*) FROM session_events WHERE session_id='S2' "
                           "AND kind='skill_learning_candidate' AND resolved=0").fetchone()[0]
    assert open_s1 == 0 and open_s2 == 1     # S1 resolved, S2 untouched
    conn.close()


def test_build_prompt_includes_skills_used_and_schema():
    p = si.build_prompt("DIGEST TEXT", skills_used={"backtest"})
    assert "backtest" in p and "skill_learnings" in p
    assert "skill_learnings" in si.build_sys()


# --------------------------------------------------------------------------- #
# run_session_ingest_pass.
# --------------------------------------------------------------------------- #

def test_pass_disabled_is_noop(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path=str(_transcript(tmp_path)), ts=TS)
    calls = []
    res = si.run_session_ingest_pass(
        conn, ts=TS, env={}, runner=lambda *a, **k: calls.append(1))
    assert res["mode"] == "disabled" and not calls
    assert len(si.pending_sessions(conn)) == 1          # untouched
    conn.close()


def test_pass_saves_facts_and_resolves(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path=str(_transcript(tmp_path)), ts=TS)
    res = si.run_session_ingest_pass(
        conn, ts=TS, env=ON,
        runner=_runner(_payload(facts=[{"title": "Tax fence",
                                        "body": "Close US options before year-end."}])))
    assert res["ingested"] == 1 and res["sessions"] == 1
    row = conn.execute("SELECT body, created_by FROM memories WHERE title='Tax fence'").fetchone()
    assert "year-end" in row["body"] and row["created_by"] == "background_review"
    assert si.pending_sessions(conn) == []              # marked resolved
    conn.close()


def test_pass_saves_correction_as_feedback(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path=str(_transcript(tmp_path)), ts=TS)
    res = si.run_session_ingest_pass(
        conn, ts=TS, env=ON,
        runner=_runner(_payload(correction={"behavior": "committed to main",
                                            "do_instead": "branch first"})))
    assert res["corrections"] == 1
    row = conn.execute("SELECT type, body FROM memories WHERE type='feedback'").fetchone()
    assert row is not None and "branch first" in row["body"]
    conn.close()


def test_pass_idempotent_no_duplicate_facts(tmp_path):
    conn = _conn(tmp_path)
    t = str(_transcript(tmp_path))
    si.enqueue(conn, session_id="s-1", transcript_path=t, ts=TS)
    fact = _payload(facts=[{"title": "F", "body": "the same fact."}])
    si.run_session_ingest_pass(conn, ts=TS, env=ON, runner=_runner(fact))
    si.enqueue(conn, session_id="s-2", transcript_path=t, ts="2026-06-02T02:00:00Z")
    si.run_session_ingest_pass(conn, ts=TS, env=ON, runner=_runner(fact))
    n = conn.execute("SELECT COUNT(*) c FROM memories WHERE title='F'").fetchone()["c"]
    assert n == 1                                       # content-hash id upsert
    conn.close()


def test_pass_failopen_leaves_session_unresolved(tmp_path):
    conn = _conn(tmp_path)
    si.enqueue(conn, session_id="s-1", transcript_path=str(_transcript(tmp_path)), ts=TS)
    res = si.run_session_ingest_pass(conn, ts=TS, env=ON, runner=_runner_raises())
    assert res["sessions"] == 0
    assert len(si.pending_sessions(conn)) == 1          # left for retry
    conn.close()


def test_pass_no_call_when_no_pending(tmp_path):
    conn = _conn(tmp_path)
    calls = []
    si.run_session_ingest_pass(conn, ts=TS, env=ON, runner=lambda *a, **k: calls.append(1))
    assert not calls
    conn.close()


def test_pass_empty_digest_resolves_without_call(tmp_path):
    conn = _conn(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    si.enqueue(conn, session_id="s-1", transcript_path=str(empty), ts=TS)
    calls = []
    si.run_session_ingest_pass(conn, ts=TS, env=ON, runner=lambda *a, **k: calls.append(1))
    assert not calls and si.pending_sessions(conn) == []   # nothing to mine → resolved
    conn.close()
