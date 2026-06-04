"""Tests for ultra_memory.maintenance.consolidate — the conservative Tier-2
self-improvement consolidation drain (ported from the reference consumer's
test_consolidate_candidates.py, project-agnostic).

The drain reads un-resolved `session_events kind='skill_learning_candidate'` rows
(bounded N), dedups each against the store via `unified_recall` (NO LLM), builds
ONE batched prompt (candidates + dedup context + the anti-capture guardrails),
calls the LLM via an INJECTED runner (default `ultra_memory.claude_cli.run_claude`,
Sonnet-tier, OAuth) — exactly ONE call — parses a per-candidate plan
(graduate | merge | skip-transient), applies it deterministically, writes a
`record_link(predicate='validated_as')` per graduation, and marks the candidate
`resolved=1`.

HARD INVARIANTS under test:
  * exactly ONE OAuth call (the injected runner is the chokepoint);
  * a STUBBED plan applies deterministically: graduate → memories row, merge →
    consumer wiki append-validation-log, skip-transient → no write;
  * `validated_as` link written per graduation;
  * the candidate is marked `resolved=1`;
  * the PROVENANCE GATE refuses any action targeting a `created_by='human'` or
    `pinned` unit (the boundary fence, in CODE not just prompt);
  * the anti-capture guardrails are present in the built prompt;
  * bounded blast radius (per-run graduation cap + bounded candidate read N);
  * OAuth-only: the module imports no `anthropic` SDK / `ANTHROPIC_API_KEY`;
  * fail-open on a runner error (no wedge, candidate left un-resolved);
  * PROJECT-AGNOSTIC: with no wiki_gateway, a `merge` decision degrades to a logged
    skip (never re-drains forever).

These tests NEVER spawn `claude` (every LLM call goes through an injected stub
runner) and NEVER write outside their tmp dir.
"""
import json
import types
from pathlib import Path

import pytest

from ultra_memory import memory_lib
from ultra_memory.maintenance import consolidate as cc


# A valid fake OAuth env so run_claude's _child_env() does not raise OAuthViolation.
FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
# A dummy wiki gateway path — the runner is stubbed, so it is never executed; it
# only needs to be non-None to exercise the real merge write path.
DUMMY_GATEWAY = Path("/tmp/_does-not-run/wiki_lib.py")


@pytest.fixture(autouse=True)
def _isolate_audit_dir(tmp_path, monkeypatch):
    """Test-hygiene fence: redirect the module-default AUDIT_DIR to a per-test tmp
    dir so a `consolidate()` call that omits `audit_dir` can NEVER write into a real
    path. (The module default is already None — no file write — but this keeps the
    guarantee explicit and parity with the Trading original.)"""
    monkeypatch.setattr(cc, "AUDIT_DIR", tmp_path / "_audit-isolate", raising=True)


def _fake_embedder(dim=3):
    """A deterministic stub embedder — never loads fastembed / hits the network.
    Returns a fixed unit vector regardless of input so the dedup pre-filter runs
    without a real model."""
    def embed(texts):
        return [[1.0] + [0.0] * (dim - 1) for _ in texts]
    return embed


def _open_temp_db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _enqueue_candidate(conn, *, session_id, skill, reason="no update",
                       ts="2026-05-31T00:00:00Z", outcome_signal="recurring",
                       expected="/x/Learnings.md"):
    """Enqueue a candidate exactly as the SP-5 Stop hook does."""
    return memory_lib.record_session_event(
        conn,
        session_id=session_id,
        kind="skill_learning_candidate",
        title=f"{skill}: skill invoked, Learnings.md not updated ({reason})",
        ts=ts,
        detail=f"expected={expected}",
        outcome_signal=outcome_signal,
    )


def _candidate_rows(conn):
    return conn.execute(
        "SELECT id, title, detail, resolved, outcome_signal FROM session_events "
        "WHERE kind='skill_learning_candidate' ORDER BY id"
    ).fetchall()


# A reusable stubbed LLM plan: one graduate, one merge, one skip-transient.
def _stub_plan(candidate_ids):
    return {
        "decisions": [
            {"candidate_id": candidate_ids[0], "action": "graduate",
             "skill": "backtest",
             "title": "Vol-normalized magnitude beats absolute 6% threshold",
             "body": "Backtests confirm Parkinson + rule-of-16 vol-normalized "
                     "magnitude filtering outperforms an absolute 6% move filter.",
             "reason": "recurs + positive outcome_signal"},
            {"candidate_id": candidate_ids[1], "action": "merge",
             "page": "wiki/trading/concepts/some-page.md",
             "entry": "Validated: the lesson reinforces the existing page.",
             "reason": "duplicate of an existing page"},
            {"candidate_id": candidate_ids[2], "action": "skip-transient",
             "reason": "environment-dependent flake, not durable"},
        ]
    }


# --------------------------------------------------------------------------- #
# OAuth-only static guard
# --------------------------------------------------------------------------- #

def test_module_is_oauth_only_no_sdk_import():
    """The module never imports the anthropic SDK or references ANTHROPIC_API_KEY /
    messages.create / api.anthropic.com — the OAuth-only hard rule."""
    src = Path(cc.__file__).read_text(encoding="utf-8")
    for forbidden in ("import anthropic", "from anthropic",
                      "ANTHROPIC_API_KEY", "messages.create",
                      "api.anthropic.com", "cache_control"):
        assert forbidden not in src, f"OAuth-only violation: found {forbidden!r}"


# --------------------------------------------------------------------------- #
# Read + bound
# --------------------------------------------------------------------------- #

def test_read_candidates_only_unresolved_and_bounded(tmp_path):
    conn = _open_temp_db(tmp_path)
    for i in range(5):
        _enqueue_candidate(conn, session_id=f"s{i}", skill="backtest",
                           ts=f"2026-05-31T00:0{i}:00Z")
    # mark one resolved → should be excluded
    rows = _candidate_rows(conn)
    conn.execute("UPDATE session_events SET resolved=1 WHERE id=?", (rows[0]["id"],))
    conn.commit()

    got = cc.read_candidates(conn, limit=2)
    assert len(got) == 2  # bounded to N
    assert all(c["resolved"] == 0 for c in got)


# --------------------------------------------------------------------------- #
# Prompt building — anti-capture guardrails present
# --------------------------------------------------------------------------- #

def test_prompt_contains_anti_capture_guardrails(tmp_path):
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cands = cc.read_candidates(conn, limit=10)
    enriched = cc.dedup_prefilter(conn, cands, embedder=_fake_embedder())
    prompt = cc.build_prompt(enriched)
    low = prompt.lower()
    assert "transient" in low
    assert "environment" in low
    assert "graduate" in low and "merge" in low and "skip-transient" in low


def test_system_prompt_names_conservative_boundary():
    sys_p = cc.build_sys().lower()
    assert "never" in sys_p
    assert ("rewrite" in sys_p or "revert" in sys_p or "delete" in sys_p)


# --------------------------------------------------------------------------- #
# Exactly ONE OAuth call
# --------------------------------------------------------------------------- #

def test_exactly_one_claude_call(tmp_path):
    conn = _open_temp_db(tmp_path)
    [_enqueue_candidate(conn, session_id=f"s{i}", skill="backtest",
                        ts=f"2026-05-31T00:0{i}:00Z") for i in range(3)]
    cand_ids = [r["id"] for r in _candidate_rows(conn)]

    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(_stub_plan(cand_ids)), stderr="")

    cc.consolidate(conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
                   project_dir=tmp_path, apply_merge=lambda *a, **k: None)
    # exactly ONE claude invocation (the bundled call). The merge apply is stubbed,
    # so the ONLY runner call is the claude call.
    assert len(calls) == 1
    assert calls[0][0] == "claude"


# --------------------------------------------------------------------------- #
# Apply: graduate → memories row + validated_as + resolved=1
# --------------------------------------------------------------------------- #

def test_graduate_writes_memory_link_and_resolves(tmp_path):
    conn = _open_temp_db(tmp_path)
    for i in range(3):
        _enqueue_candidate(conn, session_id=f"s{i}", skill="backtest",
                           ts=f"2026-05-31T00:0{i}:00Z")
    cand_ids = [r["id"] for r in _candidate_rows(conn)]

    merge_calls = []

    def runner(cmd, **kw):
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(_stub_plan(cand_ids)), stderr="")

    summary = cc.consolidate(
        conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
        project_dir=tmp_path, apply_merge=lambda **k: merge_calls.append(k))

    # graduate → a new memories row created_by='background_review'
    grad = conn.execute(
        "SELECT id, created_by, index_hook FROM memories "
        "WHERE created_by='background_review'").fetchall()
    assert len(grad) == 1
    assert grad[0]["index_hook"] == "backtest"

    # a validated_as link from the source event to the graduated memory
    links = conn.execute(
        "SELECT src_kind, predicate, dst_kind, dst_id FROM links "
        "WHERE predicate='validated_as'").fetchall()
    assert len(links) == 1
    assert links[0]["dst_kind"] == "memory"
    assert links[0]["dst_id"] == grad[0]["id"]

    # merge → exactly one append-validation-log apply call (custom writer injected,
    # so the no-wiki degrade is bypassed)
    assert len(merge_calls) == 1

    # ALL three candidates marked resolved=1 (graduate, merge, skip-transient)
    rows = _candidate_rows(conn)
    assert all(r["resolved"] == 1 for r in rows)

    assert summary["graduated"] == 1
    assert summary["merged"] == 1
    assert summary["skipped"] == 1


# --------------------------------------------------------------------------- #
# _mark_resolved routes through a bounded busy-retry txn
# --------------------------------------------------------------------------- #

def test_mark_resolved_succeeds_normally(tmp_path):
    """The common path: _mark_resolved flips resolved=1 in a committed txn."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_id = _candidate_rows(conn)[0]["id"]
    cc._mark_resolved(conn, cand_id)
    row = conn.execute(
        "SELECT resolved FROM session_events WHERE id=?", (cand_id,)).fetchone()
    assert row["resolved"] == 1


def test_mark_resolved_retries_on_transient_busy(tmp_path):
    """A transient SQLITE_BUSY on the txn must be retried (bounded backoff), not
    raise out and leave the candidate un-resolved after the wiki write already
    committed. We wrap the connection so the first BEGIN raises one 'database is
    locked' OperationalError, then succeeds."""
    import sqlite3
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_id = _candidate_rows(conn)[0]["id"]

    state = {"raised": False}

    class _FlakyConn:
        """A thin proxy over the real connection that injects ONE busy error on the
        first BEGIN (sqlite3.Connection.execute is read-only → can't patch directly)."""
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if (not state["raised"] and isinstance(sql, str)
                    and sql.strip().upper().startswith("BEGIN")):
                state["raised"] = True
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *args, **kwargs)

        @property
        def in_transaction(self):
            return self._real.in_transaction

        def __getattr__(self, name):
            return getattr(self._real, name)

    cc._mark_resolved(_FlakyConn(conn), cand_id)

    assert state["raised"], "the test must have injected one busy error"
    row = conn.execute(
        "SELECT resolved FROM session_events WHERE id=?", (cand_id,)).fetchone()
    assert row["resolved"] == 1, "the candidate must be resolved after a retried busy"


# --------------------------------------------------------------------------- #
# A FAILED merge wiki-write must NOT mark the candidate resolved
# --------------------------------------------------------------------------- #

def test_merge_write_failure_leaves_candidate_unresolved(tmp_path):
    """If `append-validation-log` exits non-zero (e.g. the target page doesn't
    exist → wiki_lib raises → rc=1), the source candidate must stay resolved=0 so
    `read_candidates` re-drains it next run."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_id = _candidate_rows(conn)[0]["id"]

    plan = {"decisions": [
        {"candidate_id": cand_id, "action": "merge",
         "page": "wiki/trading/concepts/missing-page.md",
         "entry": "Validated: reinforces the page.",
         "reason": "duplicate of an existing page"},
    ]}

    def runner(cmd, **kw):
        # The claude plan call → rc=0; the wiki_lib append-validation-log → rc=1.
        if cmd[:2] == ["uv", "run"] and "append-validation-log" in cmd:
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="ValueError: page does not exist")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(plan), stderr="")

    # Use the REAL _default_apply_merge (apply_merge=None) WITH a wiki_gateway so the
    # rc=1 path runs (a configured wiki that fails the write, not a missing wiki).
    summary = cc.consolidate(
        conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
        project_dir=tmp_path, apply_merge=None, wiki_gateway=DUMMY_GATEWAY)

    rows = _candidate_rows(conn)
    assert len(rows) == 1
    assert rows[0]["resolved"] == 0, "failed merge must leave the candidate un-resolved"

    assert summary["merged"] == 0
    assert summary.get("merge_failed", 0) == 1

    again = cc.read_candidates(conn, limit=10)
    assert any(c["id"] == cand_id for c in again), "the un-resolved candidate must be re-drainable"


# --------------------------------------------------------------------------- #
# M1: gateway_prefix — the resolved argv prefix replaces the hardcoded ["uv","run"]
# --------------------------------------------------------------------------- #

def test_merge_uses_resolved_gateway_prefix(tmp_path):
    """When consolidate is given a gateway_prefix, the merge write shells
    prefix + [append-validation-log, …, --from-file, tmp] — NOT ['uv','run',<gw>,…]."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_id = _candidate_rows(conn)[0]["id"]

    plan = {"decisions": [
        {"candidate_id": cand_id, "action": "merge",
         "page": "wiki/trading/concepts/p.md", "entry": "Validated.", "reason": "dup"},
    ]}
    wiki_calls = []

    def runner(cmd, **kw):
        if "append-validation-log" in cmd:
            wiki_calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(plan), stderr="")

    prefix = ["python", "-m", "ultra_memory.wiki_gateway",
              "--gateway-class", "wiki_lib:TradingWikiGateway"]
    # apply_merge=None → the real _default_apply_merge; a gateway_prefix is supplied.
    summary = cc.consolidate(
        conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
        project_dir=tmp_path, apply_merge=None, wiki_gateway=DUMMY_GATEWAY,
        gateway_prefix=prefix)

    assert summary["merged"] == 1
    assert wiki_calls, "append-validation-log was not shelled"
    cmd = wiki_calls[0]
    assert cmd[:5] == prefix
    assert cmd[5] == "append-validation-log"
    assert "uv" not in cmd and "run" not in cmd


def test_merge_prefix_none_falls_back_to_uv_run(tmp_path):
    """Back-compat: no gateway_prefix → the legacy ['uv','run',<wiki_gateway>] argv."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_id = _candidate_rows(conn)[0]["id"]

    plan = {"decisions": [
        {"candidate_id": cand_id, "action": "merge",
         "page": "wiki/trading/concepts/p.md", "entry": "Validated.", "reason": "dup"},
    ]}
    wiki_calls = []

    def runner(cmd, **kw):
        if "append-validation-log" in cmd:
            wiki_calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(plan), stderr="")

    summary = cc.consolidate(
        conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
        project_dir=tmp_path, apply_merge=None, wiki_gateway=DUMMY_GATEWAY)

    assert summary["merged"] == 1
    assert wiki_calls and wiki_calls[0][:2] == ["uv", "run"]
    assert str(DUMMY_GATEWAY) in wiki_calls[0]


# --------------------------------------------------------------------------- #
# PROJECT-AGNOSTIC: no wiki_gateway → merge degrades to a logged skip
# --------------------------------------------------------------------------- #

def test_no_wiki_gateway_degrades_merge_to_skip(tmp_path):
    """A pure-memory install (no wiki) has no page to merge into. A `merge` decision
    must degrade to a resolved skip (never re-drain forever), and the default writer
    must NOT be shelled."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_id = _candidate_rows(conn)[0]["id"]

    plan = {"decisions": [
        {"candidate_id": cand_id, "action": "merge",
         "page": "wiki/trading/concepts/some-page.md",
         "entry": "Validated.", "reason": "dup"},
    ]}
    wiki_calls = []

    def runner(cmd, **kw):
        if "append-validation-log" in cmd:
            wiki_calls.append(cmd)  # must never happen
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(plan), stderr="")

    # apply_merge=None → the default writer; wiki_gateway=None → no wiki.
    summary = cc.consolidate(
        conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
        project_dir=tmp_path, apply_merge=None, wiki_gateway=None)

    assert wiki_calls == [], "the default wiki writer must not run without a gateway"
    assert summary["merged"] == 0
    assert summary["skipped"] == 1
    rows = _candidate_rows(conn)
    assert rows[0]["resolved"] == 1, "the degraded merge must resolve the candidate (no forever re-drain)"


# --------------------------------------------------------------------------- #
# THE PROVENANCE GATE (the boundary fence, in CODE)
# --------------------------------------------------------------------------- #

def test_provenance_gate_refuses_human_target(tmp_path):
    """A graduate/merge decision that would re-target an existing created_by='human'
    memory is REFUSED in the apply path — never a rewrite of a human-authored unit."""
    conn = _open_temp_db(tmp_path)
    memory_lib.save_memory(
        conn, id="human-mem-1", type="memory", title="Human rule",
        body="A human-authored rule that must never be auto-edited.",
        ts="2026-05-30T00:00:00Z", index_hook="backtest", created_by="human")
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_ids = [r["id"] for r in _candidate_rows(conn)]

    bad_plan = {"decisions": [
        {"candidate_id": cand_ids[0], "action": "graduate",
         "target_id": "human-mem-1", "skill": "backtest",
         "title": "Hijack", "body": "Overwrite the human rule.",
         "reason": "illegal"}]}

    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(bad_plan), stderr="")

    summary = cc.consolidate(conn, runner=runner, embedder=_fake_embedder(),
                             env=FAKE_ENV, project_dir=tmp_path,
                             apply_merge=lambda **k: None)

    row = conn.execute(
        "SELECT body, created_by FROM memories WHERE id='human-mem-1'").fetchone()
    assert row["created_by"] == "human"
    assert row["body"] == "A human-authored rule that must never be auto-edited."
    assert summary["graduated"] == 0
    assert summary["refused"] >= 1


def test_provenance_gate_refuses_pinned_target(tmp_path):
    """A decision re-targeting a pinned memory is REFUSED."""
    conn = _open_temp_db(tmp_path)
    memory_lib.save_memory(
        conn, id="pinned-mem-1", type="memory", title="Pinned rule",
        body="A pinned rule.", ts="2026-05-30T00:00:00Z",
        index_hook="backtest", created_by="background_review")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='pinned-mem-1'")
    conn.commit()
    _enqueue_candidate(conn, session_id="s0", skill="backtest")
    cand_ids = [r["id"] for r in _candidate_rows(conn)]

    bad_plan = {"decisions": [
        {"candidate_id": cand_ids[0], "action": "graduate",
         "target_id": "pinned-mem-1", "skill": "backtest",
         "title": "Hijack pinned", "body": "Overwrite the pinned rule.",
         "reason": "illegal"}]}

    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(bad_plan), stderr="")

    summary = cc.consolidate(conn, runner=runner, embedder=_fake_embedder(),
                             env=FAKE_ENV, project_dir=tmp_path,
                             apply_merge=lambda **k: None)
    row = conn.execute(
        "SELECT body FROM memories WHERE id='pinned-mem-1'").fetchone()
    assert row["body"] == "A pinned rule."
    assert summary["refused"] >= 1


# --------------------------------------------------------------------------- #
# Bounded blast radius — graduation cap
# --------------------------------------------------------------------------- #

def test_graduation_cap_bounds_blast_radius(tmp_path):
    """At most `max_graduations` graduations are applied per run; the rest are
    left un-resolved for the next run and the cap is surfaced in the summary."""
    conn = _open_temp_db(tmp_path)
    for i in range(5):
        _enqueue_candidate(conn, session_id=f"s{i}", skill="backtest",
                           ts=f"2026-05-31T00:0{i}:00Z")
    cand_ids = [r["id"] for r in _candidate_rows(conn)]
    plan = {"decisions": [
        {"candidate_id": cid, "action": "graduate", "skill": "backtest",
         "title": f"Lesson {i}", "body": f"A durable lesson number {i}.",
         "reason": "recurs"}
        for i, cid in enumerate(cand_ids)]}

    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(plan), stderr="")

    summary = cc.consolidate(conn, runner=runner, embedder=_fake_embedder(),
                             env=FAKE_ENV, project_dir=tmp_path,
                             apply_merge=lambda **k: None, max_graduations=2)
    grad = conn.execute(
        "SELECT id FROM memories WHERE created_by='background_review'").fetchall()
    assert len(grad) == 2
    assert summary["graduated"] == 2
    assert summary["cap_hit"] is True
    resolved = conn.execute(
        "SELECT COUNT(*) c FROM session_events "
        "WHERE kind='skill_learning_candidate' AND resolved=1").fetchone()["c"]
    assert resolved == 2


# --------------------------------------------------------------------------- #
# Skip-if-empty — ZERO LLM calls
# --------------------------------------------------------------------------- #

def test_skip_if_empty_makes_no_claude_call(tmp_path):
    conn = _open_temp_db(tmp_path)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

    summary = cc.consolidate(conn, runner=runner, embedder=_fake_embedder(),
                             env=FAKE_ENV, project_dir=tmp_path,
                             apply_merge=lambda **k: None)
    assert calls == []  # ZERO LLM calls on an empty queue
    assert summary["graduated"] == 0
    assert summary["candidates"] == 0


# --------------------------------------------------------------------------- #
# Fail-open on a runner error / unparseable plan
# --------------------------------------------------------------------------- #

def test_failopen_on_runner_error(tmp_path):
    """A runner/LLM error degrades to a no-op + one diagnostic line — never raises,
    never wedges, and leaves the candidates UN-resolved for the next run."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")

    def runner(cmd, **kw):
        raise RuntimeError("simulated claude failure")

    summary = cc.consolidate(conn, runner=runner, embedder=_fake_embedder(),
                             env=FAKE_ENV, project_dir=tmp_path,
                             apply_merge=lambda **k: None)
    assert summary["error"] is True
    rows = _candidate_rows(conn)
    assert all(r["resolved"] == 0 for r in rows)


def test_failopen_on_unparseable_plan(tmp_path):
    """A malformed LLM response degrades to a no-op + leaves candidates un-resolved."""
    conn = _open_temp_db(tmp_path)
    _enqueue_candidate(conn, session_id="s0", skill="backtest")

    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout="not json at all", stderr="")

    summary = cc.consolidate(conn, runner=runner, embedder=_fake_embedder(),
                             env=FAKE_ENV, project_dir=tmp_path,
                             apply_merge=lambda **k: None)
    assert summary["error"] is True
    rows = _candidate_rows(conn)
    assert all(r["resolved"] == 0 for r in rows)


# --------------------------------------------------------------------------- #
# Audit row
# --------------------------------------------------------------------------- #

def test_consolidation_writes_audit_row(tmp_path):
    conn = _open_temp_db(tmp_path)
    for i in range(2):
        _enqueue_candidate(conn, session_id=f"s{i}", skill="backtest",
                           ts=f"2026-05-31T00:0{i}:00Z")
    cand_ids = [r["id"] for r in _candidate_rows(conn)]
    plan = {"decisions": [
        {"candidate_id": cand_ids[0], "action": "graduate", "skill": "backtest",
         "title": "L", "body": "A durable lesson.", "reason": "x"},
        {"candidate_id": cand_ids[1], "action": "skip-transient", "reason": "noise"},
    ]}

    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(plan), stderr="")

    audit_dir = tmp_path / "maintenance-logs"
    cc.consolidate(conn, runner=runner, embedder=_fake_embedder(), env=FAKE_ENV,
                   project_dir=tmp_path, apply_merge=lambda **k: None,
                   audit_dir=audit_dir)
    rows = list(audit_dir.glob("consolidation-*.jsonl"))
    assert len(rows) == 1
    payload = json.loads(rows[0].read_text().strip().splitlines()[-1])
    assert payload["graduated"] == 1
    assert payload["skipped"] == 1
    assert payload["op"] == "consolidate"


# --------------------------------------------------------------------------- #
# Registry adapter wires the config seam into consolidate()
# --------------------------------------------------------------------------- #

def test_registry_beat_threads_config_seam(tmp_path):
    """The `beat(conn, config, ts, env)` registry entry reads model / briefings_dir
    / wiki_gateway / topics off the MaintenanceConfig and runs a drain (here on an
    empty queue → ZERO LLM calls, just proving the wiring + audit path)."""
    from ultra_memory.maintenance.config import MaintenanceConfig

    conn = _open_temp_db(tmp_path)
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "memory.db",
        export_dir=tmp_path / "exp", briefings_dir=tmp_path / "briefings",
        topics=["trading"], model="claude-sonnet-4-6")
    res = cc.beat(conn, cfg, "2026-06-01T00:00:00Z", FAKE_ENV)
    assert res["op"] == "consolidate"
    assert res["candidates"] == 0
    # audit lands under <briefings>/maintenance-logs (the adapter's derived dir)
    assert (tmp_path / "briefings" / "maintenance-logs").exists()
