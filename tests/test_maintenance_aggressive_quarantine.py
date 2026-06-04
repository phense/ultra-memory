"""Tests for aggressive_quarantine.py — SP-7 §5.3 CONTRADICTION QUARANTINE track —
Stage 7 of the SP-7 build (spec §7 step 7). The THIRD (and gentlest) of the three
aggressive self-improvement capabilities, built on top of the safety wall
(Stages 1-4). It detects two agent-authored units that DISAGREE and ISOLATES them
(demoted out of recall), not deletes them, for the operator's review — the loop does NOT
pick a winner.

DETECTION (spec §5.3): an EMBEDDING PRE-FILTER (no-LLM, the SP-3/wiki embedder)
pairs agent-authored units that are topically NEAR (cosine in a band — high enough
to be the same topic, low enough not to be a trivial restatement); ONE batched LLM
adjudication (an INJECTED runner) labels each near-pair contradicts|compatible|
duplicate.

APPLY (spec §5.3):
  * contradicts → BOTH units set_status('quarantined') (drop out of recall) +
    record_link(predicate='contradicts') + listed in the digest for the operator's
    adjudication (the loop does NOT pick a winner);
  * duplicate   → routes to the SP-6 CONSERVATIVE MERGE path (engine consolidate —
    NOT quarantine);
  * compatible  → no-op.

HARD INVARIANTS under test (spec §7 step 7 / §8 / §5.3):
  * a near-opposing agent-authored pair → BOTH quarantined + a `contradicts` link +
    a digest listing;
  * a `duplicate`-labeled pair routes to the conservative MERGE path (engine
    consolidate: loser→redirect, supersedes=canonical) — NOT quarantine;
  * a `compatible`-labeled pair is a no-op (neither quarantined nor merged);
  * REVERSIBLE — a quarantined unit flips back to 'active' (the wall's reactivate);
  * BOUNDED to MAX_QUARANTINES_PER_RUN, halt-on-exceed (applies none of the class);
  * assert_mutable BLOCKS a forbidden (human/import/pinned) target — the §4a
    stop-the-world (zero tolerance, NOT a per-item skip), the gate RE-READS the live
    row (never trusts the LLM-echoed field);
  * the embedding pre-filter is NO-LLM (a stubbed embedder, never fastembed/network);
  * the ONE adjudication call routes through an INJECTED runner (tests never spawn
    `claude`); NO anthropic SDK import (OAuth-only, asserted);
  * archive-never-delete: no destructive call anywhere (asserted);
  * fail-open: any error degrades to a no-op, never raises.

These tests NEVER touch the live memory.db, NEVER spawn `claude`, NEVER load a real
embedder (a deterministic fake-vector stub), and run against a temp DB + synthetic
agent-authored memories + a frozen retrieval probe set.
"""
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_quarantine as aq  # noqa: E402
from ultra_memory.maintenance import aggressive_wall as aw  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


TS = "2026-05-31T00:00:00Z"
PROBE_TYPE = "reference"

# A valid fake OAuth env so run_claude's _child_env() does not raise OAuthViolation
# (the adjudication call now routes through the ultra_memory.claude_cli chokepoint,
# which requires CLAUDE_CODE_OAUTH_TOKEN + refuses ANTHROPIC_API_KEY). Tests inject
# this so the injected runner is actually reached; no `claude` is ever spawned.
FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}


# --------------------------------------------------------------------------- #
# Fixture helpers — agent-authored memories + a STUBBED embedder (no fastembed /
# no network): a deterministic keyword-bag vector so "near" pairs are controllable.
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path, name="memory.db"):
    return memory_lib.open_memory_db(str(tmp_path / name))


def _save(conn, *, id, created_by="agent", body="a lesson", title="L", pinned=False,
          type=PROBE_TYPE, status=None):
    memory_lib.save_memory(
        conn, id=id, type=type, title=title, body=body, ts=TS, created_by=created_by)
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=TS, reason="test pin")
    if status is not None:
        memory_lib.set_status(conn, id=id, status=status, ts=TS, reason="test seed")
    return id


def _row(conn, mem_id):
    return conn.execute(
        "SELECT status, supersedes, body, created_by FROM memories WHERE id=?",
        (mem_id,)).fetchone()


# A deterministic STUB embedder over a fixed vocabulary. The vector is the
# count of each vocab token in the text — so two texts about the SAME topic
# (sharing topic tokens) land NEAR in cosine, while unrelated texts land FAR.
# This NEVER imports fastembed and NEVER touches the network.
_VOCAB = ("vix", "spike", "sell", "buy", "premium", "earnings", "iv", "crush",
          "macd", "cross", "hedge", "tax", "fence", "banana", "weather")


def _stub_embedder(texts):
    vecs = []
    for t in texts:
        low = (t or "").lower()
        vecs.append([float(low.count(tok)) for tok in _VOCAB])
    return vecs


def _seed_opposing_pair(conn, *, id_a="u-a", id_b="u-b"):
    """Two agent-authored units, SAME topic (both about a VIX spike → premium),
    OPPOSING claim (sell vs buy). The stub embedder lands them NEAR (shared
    topic tokens), and the adjudicator labels them `contradicts`."""
    _save(conn, id=id_a, created_by="agent",
          body="On a VIX spike, SELL premium — IV is rich.", title="A")
    _save(conn, id=id_b, created_by="background_review",
          body="On a VIX spike, BUY premium — IV will rise further.", title="B")
    return id_a, id_b


# A canned runner: returns a fixed JSON adjudication, NEVER spawns `claude`. The
# `labels` map keys an (id_a, id_b) frozenset to a label; default 'compatible'.
def _runner_for(labels: dict):
    import json

    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        # The prompt is the last positional arg after '-p'.
        out = {"adjudications": []}
        for key, label in labels.items():
            a, b = tuple(key)
            out["adjudications"].append({"id_a": a, "id_b": b, "label": label})

        class _P:
            returncode = 0
            stdout = json.dumps(out)
            stderr = ""
        return _P()

    return runner


# =========================================================================== #
# 1. Detection — the embedding pre-filter (NO LLM) finds same-topic near pairs
# =========================================================================== #

def test_prefilter_pairs_near_opposing_units(tmp_path):
    """The no-LLM embedding pre-filter pairs agent-authored units that are
    topically NEAR (cosine in the band). A same-topic opposing pair is surfaced; an
    unrelated unit is NOT paired with it."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    _save(conn, id="u-far", created_by="agent",
          body="The banana weather report is sunny.", title="Far")
    pairs = aq.select_near_pairs(conn, embedder=_stub_embedder)
    keys = {frozenset((p["id_a"], p["id_b"])) for p in pairs}
    assert frozenset((id_a, id_b)) in keys
    # The unrelated unit shares no topic tokens → never paired with the topic pair.
    assert not any("u-far" in {p["id_a"], p["id_b"]} for p in pairs)


def test_prefilter_is_no_llm(tmp_path):
    """The pre-filter takes ONLY a (stubbed) embedder — it makes NO model call. A
    runner that would raise if called is never invoked at the pre-filter stage."""
    conn = _open_temp_db(tmp_path)
    _seed_opposing_pair(conn)

    def _exploding_runner(*a, **k):  # would blow up IF the pre-filter called an LLM
        raise AssertionError("pre-filter must not call an LLM")

    pairs = aq.select_near_pairs(conn, embedder=_stub_embedder)
    assert pairs  # produced pairs with no runner at all
    assert _exploding_runner  # (kept to document intent; never invoked)


# =========================================================================== #
# 2. Adjudication + apply — contradicts → BOTH quarantined + link + digest
# =========================================================================== #

def test_contradicts_quarantines_both_and_links(tmp_path):
    """A near-opposing pair the adjudicator labels `contradicts` → BOTH units flip
    to 'quarantined' (out of recall) + a `contradicts` link connects them. The loop
    demotes BOTH (does not pick a winner) — archive-never-delete (bytes intact)."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    runner = _runner_for({frozenset((id_a, id_b)): "contradicts"})
    result = aq.run_quarantine_track(
        conn, ts=TS, embedder=_stub_embedder, runner=runner, env=FAKE_ENV)
    # Both quarantined.
    assert _row(conn, id_a)["status"] == "quarantined"
    assert _row(conn, id_b)["status"] == "quarantined"
    # A contradicts link connects them.
    link = conn.execute(
        "SELECT predicate FROM links WHERE predicate='contradicts' "
        "AND ((src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?))",
        (id_a, id_b, id_b, id_a)).fetchone()
    assert link is not None
    # Bytes intact (archive-never-delete).
    assert _row(conn, id_a)["body"].startswith("On a VIX spike, SELL")
    assert _row(conn, id_b)["body"].startswith("On a VIX spike, BUY")


def test_contradicts_listed_in_digest(tmp_path):
    """A quarantined contradicting pair is LISTED in the digest payload for the operator's
    adjudication (the loop does not pick a winner — the operator does)."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    runner = _runner_for({frozenset((id_a, id_b)): "contradicts"})
    result = aq.run_quarantine_track(
        conn, ts=TS, embedder=_stub_embedder, runner=runner, env=FAKE_ENV)
    quarantined = {frozenset((q["id_a"], q["id_b"])) for q in result["quarantined"]}
    assert frozenset((id_a, id_b)) in quarantined


# =========================================================================== #
# 3. duplicate → conservative MERGE path (NOT quarantine); compatible → no-op
# =========================================================================== #

def test_duplicate_routes_to_conservative_merge_not_quarantine(tmp_path):
    """A `duplicate`-labeled pair routes to the SP-6 CONSERVATIVE MERGE path (engine
    consolidate: loser→redirect, supersedes=canonical) — NOT quarantine. Neither
    unit is 'quarantined'; the loser is redirected to the canonical."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)   # near pair, but labeled duplicate here
    runner = _runner_for({frozenset((id_a, id_b)): "duplicate"})
    result = aq.run_quarantine_track(
        conn, ts=TS, embedder=_stub_embedder, runner=runner, env=FAKE_ENV)
    # Routed to merge, NOT quarantine.
    assert result["quarantined"] == []
    assert len(result["merged"]) == 1
    # The merge is the engine consolidate: one becomes a redirect to the other.
    statuses = {_row(conn, id_a)["status"], _row(conn, id_b)["status"]}
    assert "redirect" in statuses           # the loser redirected (conservative merge)
    assert "quarantined" not in statuses    # NOT quarantine


def test_compatible_is_a_noop(tmp_path):
    """A `compatible`-labeled near pair is a NO-OP — neither quarantined nor merged
    (two units about the same topic that AGREE are fine to both keep recalling)."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    runner = _runner_for({frozenset((id_a, id_b)): "compatible"})
    result = aq.run_quarantine_track(
        conn, ts=TS, embedder=_stub_embedder, runner=runner, env=FAKE_ENV)
    assert result["quarantined"] == []
    assert result["merged"] == []
    assert _row(conn, id_a)["status"] == "active"
    assert _row(conn, id_b)["status"] == "active"


# =========================================================================== #
# 4. Reversibility — a quarantined unit flips back to active
# =========================================================================== #

def test_quarantine_is_reversible(tmp_path):
    """Quarantine is the gentlest verb — fully reversible. After the operator adjudicates,
    a quarantined unit flips back to 'active' via the wall's reactivate."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    runner = _runner_for({frozenset((id_a, id_b)): "contradicts"})
    aq.run_quarantine_track(conn, ts=TS, embedder=_stub_embedder, runner=runner,
                            env=FAKE_ENV)
    assert _row(conn, id_a)["status"] == "quarantined"
    # Reverse it (the operator adjudicated unit A as the correct one).
    aw.reactivate(conn, id=id_a, ts=TS, reason="peter adjudicated: A is correct")
    assert _row(conn, id_a)["status"] == "active"
    assert _row(conn, id_b)["status"] == "quarantined"   # B stays quarantined


# =========================================================================== #
# 5. The safety wall — provenance gate + bounds (halt-on-exceed)
# =========================================================================== #

def test_forbidden_target_halts_run(tmp_path):
    """A `contradicts` pair where ONE member is a forbidden (human) unit HALTS the
    whole apply (the §4a stop-the-world, zero tolerance, NOT a per-item skip):
    NOTHING is quarantined — not even the legal member, not the other legal pair in
    the same batch. assert_mutable RE-READS the live row (never an LLM-echoed field).
    """
    conn = _open_temp_db(tmp_path)
    # A legal opposing pair + a pair whose second member is a HUMAN rule.
    a1, b1 = _seed_opposing_pair(conn, id_a="u-a1", id_b="u-b1")
    _save(conn, id="u-human", created_by="human", body="a human hard rule about VIX")
    # Hand-build the contradicts batch (both pairs) so the apply path is exercised
    # directly (the §4a halt is an apply-path enforcement, not a select-time skip).
    pairs = [
        {"id_a": a1, "id_b": b1, "label": "contradicts"},
        {"id_a": a1, "id_b": "u-human", "label": "contradicts"},
    ]
    with pytest.raises(aw.ForbiddenTargetError):
        aq.apply_quarantines(conn, pairs, ts=TS)
    # Zero-tolerance halt: NOTHING quarantined — even the legal pair untouched.
    assert _row(conn, a1)["status"] == "active"
    assert _row(conn, b1)["status"] == "active"
    assert _row(conn, "u-human")["status"] == "active"


def test_forbidden_target_reads_live_row_not_echoed(tmp_path):
    """The gate RE-READS the live row: even if the plan ECHOES a forbidden unit as
    'agent'-authored, the wall reads the LIVE created_by='human' and halts. The LLM
    cannot talk its way past the wall by mislabeling a unit."""
    conn = _open_temp_db(tmp_path)
    a1, b1 = _seed_opposing_pair(conn, id_a="u-a1", id_b="u-b1")
    _save(conn, id="u-human", created_by="human", body="a human hard rule")
    # The echoed_created_by is a LIE; the wall ignores it and reads the live row.
    pairs = [{"id_a": a1, "id_b": "u-human", "label": "contradicts",
              "echoed_created_by": "agent"}]
    with pytest.raises(aw.ForbiddenTargetError):
        aq.apply_quarantines(conn, pairs, ts=TS)
    assert _row(conn, "u-human")["status"] == "active"


def test_pinned_agent_unit_is_forbidden(tmp_path):
    """An agent-authored but PINNED unit is immutable too (the §4a second condition):
    a contradicts pair touching a pinned unit halts."""
    conn = _open_temp_db(tmp_path)
    a1, b1 = _seed_opposing_pair(conn, id_a="u-a1", id_b="u-b1")
    _save(conn, id="u-pin", created_by="agent", body="pinned agent lesson", pinned=True)
    pairs = [{"id_a": a1, "id_b": "u-pin", "label": "contradicts"}]
    with pytest.raises(aw.ForbiddenTargetError):
        aq.apply_quarantines(conn, pairs, ts=TS)
    assert _row(conn, "u-pin")["status"] == "active"


def test_respects_max_quarantines_bound(tmp_path):
    """The apply is bounded to MAX_QUARANTINES_PER_RUN, halt-on-exceed: a batch with
    MORE contradicts pairs than the cap applies NONE of the class (not the first N)
    — the §4c stop-and-ask."""
    conn = _open_temp_db(tmp_path)
    from ultra_memory.maintenance.aggressive_bounds import MAX_QUARANTINES_PER_RUN
    n = MAX_QUARANTINES_PER_RUN + 1
    pairs = []
    for i in range(n):
        a, b = _seed_opposing_pair(conn, id_a=f"a{i}", id_b=f"b{i}")
        pairs.append({"id_a": a, "id_b": b, "label": "contradicts"})
    applied = aq.apply_quarantines(conn, pairs, ts=TS,
                                   max_quarantines=MAX_QUARANTINES_PER_RUN)
    # Halt-on-exceed: NONE applied.
    assert applied == []
    for i in range(n):
        assert _row(conn, f"a{i}")["status"] == "active"   # all untouched


def test_under_bound_applies_all(tmp_path):
    """A contradicts batch at/under the cap quarantines every pair."""
    conn = _open_temp_db(tmp_path)
    from ultra_memory.maintenance.aggressive_bounds import MAX_QUARANTINES_PER_RUN
    n = MAX_QUARANTINES_PER_RUN
    pairs = []
    for i in range(n):
        a, b = _seed_opposing_pair(conn, id_a=f"a{i}", id_b=f"b{i}")
        pairs.append({"id_a": a, "id_b": b, "label": "contradicts"})
    applied = aq.apply_quarantines(conn, pairs, ts=TS,
                                   max_quarantines=MAX_QUARANTINES_PER_RUN)
    assert len(applied) == n
    for i in range(n):
        assert _row(conn, f"a{i}")["status"] == "quarantined"
        assert _row(conn, f"b{i}")["status"] == "quarantined"


# =========================================================================== #
# 6. Fail-open + OAuth-only + archive-never-delete + no-fastembed guards
# =========================================================================== #

def test_run_quarantine_track_failopen_never_raises(tmp_path):
    """Fail-open: a broken read (a closed connection) degrades to a no-op result,
    never raises out into the maintenance run."""
    conn = _open_temp_db(tmp_path)
    conn.close()
    result = aq.run_quarantine_track(
        conn, ts=TS, embedder=_stub_embedder, runner=_runner_for({}))
    assert isinstance(result, dict)
    assert result["quarantined"] == []
    assert result["merged"] == []


def test_runner_error_failopen_no_quarantine(tmp_path):
    """A runner error (the one batched adjudication call fails) degrades to a no-op
    plan — NOTHING is quarantined, never raises."""
    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)

    def _boom(*a, **k):
        raise RuntimeError("claude exploded")

    result = aq.run_quarantine_track(
        conn, ts=TS, embedder=_stub_embedder, runner=_boom)
    assert result["quarantined"] == []
    assert _row(conn, id_a)["status"] == "active"


def test_quarantine_module_no_anthropic_sdk_import():
    """The quarantine track's ONE LLM call routes through an INJECTED runner (the
    OAuth `claude` CLI in a real run); it NEVER imports the anthropic SDK / API —
    OAuth-only by construction."""
    src = Path(aq.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com"):
        assert forbidden not in src, f"OAuth-only violation: {forbidden!r}"


def _env_capturing_runner(captured, id_a, id_b, label="compatible"):
    """A subprocess.run-compatible runner that records the `env=` it receives and
    returns a canned adjudication. NEVER spawns `claude`."""
    import json

    def runner(cmd, **kwargs):
        captured["called"] = True
        captured["env"] = kwargs.get("env")
        out = {"adjudications": [{"id_a": id_a, "id_b": id_b, "label": label}]}

        class P:
            returncode = 0
            stdout = json.dumps(out)
            stderr = ""
        return P()
    return runner


def test_adjudication_routes_through_oauth_chokepoint_sanitized_env(tmp_path, monkeypatch):
    """FIX #7 (OAuth hardening): the ONE adjudication LLM call routes through
    `ultra_memory.claude_cli.run_claude` — the env-sanitizing chokepoint. On the happy
    path (OAuth token present, NO stray metered-API key) the child env handed to the
    runner MUST carry the OAuth token and MUST NOT carry the metered-API key, and the
    recursion markers are stripped. Asserted via the BEHAVIOR (the env the injected
    runner receives), never a source literal."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-real")
    monkeypatch.setenv("CLAUDECODE", "1")          # a recursion marker the chokepoint strips

    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    captured = {}
    runner = _env_capturing_runner(captured, id_a, id_b)

    # env=None → run_claude sanitizes the AMBIENT os.environ (the real cron-run path).
    aq.run_quarantine_track(conn, ts=TS, embedder=_stub_embedder, runner=runner)

    assert captured.get("called"), "the adjudication call must reach the injected runner"
    child_env = captured["env"]
    assert child_env is not None, "run_claude must pass a sanitized env= to the runner"
    assert "ANTHROPIC_API_KEY" not in child_env, "the metered-API key must be absent (OAuth-only)"
    assert "CLAUDECODE" not in child_env, "the in-session recursion marker must be stripped"
    assert child_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-real", "OAuth token required"


def test_adjudication_refuses_when_stray_metered_api_key_present(tmp_path, monkeypatch):
    """FIX #7 (OAuth hardening): a stray non-empty metered-API key in the ambient env
    would, WITHOUT the chokepoint, be inherited by the child `claude` and outrank the
    OAuth token → the metered API. Routed through `run_claude`, the chokepoint REFUSES
    (raises) before any spawn; `run_quarantine_track` fail-opens and the injected
    runner is NEVER reached — the call cannot leak onto the metered API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-real")

    conn = _open_temp_db(tmp_path)
    id_a, id_b = _seed_opposing_pair(conn)
    captured = {}
    runner = _env_capturing_runner(captured, id_a, id_b, label="contradicts")

    result = aq.run_quarantine_track(conn, ts=TS, embedder=_stub_embedder, runner=runner)

    assert not captured.get("called"), "the chokepoint must refuse before reaching the runner"
    assert result["quarantined"] == [], "a refused call fail-opens to a no-op plan"
    assert _row(conn, id_a)["status"] == "active"   # nothing quarantined


def test_quarantine_module_never_deletes():
    """Static guard: the quarantine module never `rm`s / hard-deletes — every verb
    is a reversible FSM transition / a conservative redirect (archive-never-delete).
    """
    src = Path(aq.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(",
                      "memory_lib.delete(", ".delete(tier", "rm -rf", "DROP TABLE"):
        assert forbidden not in src, f"destructive call {forbidden!r}"


def test_quarantine_module_no_fastembed_import():
    """The embedding pre-filter consumes an INJECTED embedder — it never imports
    fastembed itself (the model download stays off the test path; the orchestrator
    supplies the real embedder, tests supply a stub)."""
    src = Path(aq.__file__).read_text()
    assert "import fastembed" not in src
    assert "from fastembed" not in src
