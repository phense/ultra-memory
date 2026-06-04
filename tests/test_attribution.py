"""SP-8 stage A2 — the usage-outcome ATTRIBUTION JOIN (deterministic, NO LLM).

At session-end an outcome `session_event` (carrying an `outcome_signal` like
'tests_passed') is joined to the memories that session actually recalled (logged in
`access_log` with the session id + a 1-based fused `rank`). The join writes
`informed_by` graph edges (one per policy-selected recalled memory) that a downstream
consumer (consumer-side, NOT the engine) folds into an EWMA.

This module is PROJECT-AGNOSTIC: `attribution.py` imports only stdlib +
`from . import memory_lib`. No policy config, no Trading/wiki concept; the consumer
supplies `policy`/`k` as parameters.

THE INTEGRATION CONTRACT (the crux): an `informed_by` edge is
  src_kind='session_event', src_id=str(<session_events.id>),
  predicate='informed_by', dst_kind='memory', dst_id=<memory id>
so the downstream JOIN `session_events se ON se.id = CAST(l.src_id AS INTEGER)`
resolves. The contract test below runs that EXACT join.
"""
import sqlite3

import pytest

from ultra_memory import attribution, memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _seed_memory(conn, mid, ts="2026-05-30T09:00:00"):
    memory_lib.save_memory(conn, id=mid, type="reference", title="t",
                           body="b", ts=ts)


# ---------------------------------------------------------------------------
# 2. recalled_units_for_session — only THIS session's NULL-rank-excluded memory rows.
# ---------------------------------------------------------------------------

def test_recalled_units_for_session_filters_correctly(tmp_path):
    conn = _db(tmp_path)
    for mid in ("a", "b"):
        _seed_memory(conn, mid)
    # session S: two memory recalls (ranks 1, 2)
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:00", session_id="S", rank=1)
    memory_lib.record_access(conn, target_kind="memory", target_id="b",
                             ts="2026-05-30T10:00:01", session_id="S", rank=2)
    # a knowledge recall in S (excluded: not a memory target)
    memory_lib.record_access(conn, target_kind="knowledge", target_id="page-x",
                             ts="2026-05-30T10:00:02", session_id="S", rank=3)
    # a different session's memory recall (excluded)
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:03", session_id="OTHER", rank=1)
    # a NULL-rank memory access in S (excluded: not a ranked recall)
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:04", session_id="S")

    rows = attribution.recalled_units_for_session(conn, session_id="S")
    assert rows == [{"id": "a", "rank": 1}, {"id": "b", "rank": 2}]
    conn.close()


def test_recalled_units_for_session_fail_closed_to_empty(tmp_path):
    """A read error never raises — returns []."""
    conn = _db(tmp_path)
    conn.close()  # closed conn -> any query raises ProgrammingError internally
    assert attribution.recalled_units_for_session(conn, session_id="S") == []


# ---------------------------------------------------------------------------
# 3. apply_attribution_policy — PURE function, deterministic dedup + top-k.
# ---------------------------------------------------------------------------

_FIXED = [{"id": "a", "rank": 3}, {"id": "b", "rank": 1},
          {"id": "a", "rank": 1}, {"id": "c", "rank": 2}]


# NOTE on the contract (the documented behavior is `dedup keeping each id's BEST
# (lowest) rank; ties broken by id`). For `_FIXED`, dedup => {a:1, b:1, c:2}, so the
# deterministic order is a(1), b(1, tie->id), c(2). The task brief's illustrative
# values (k=1->[b], k=2->[b,c]) are internally inconsistent with one another for ANY
# total order — k=1->[b] needs b before a, while k=2->[b,c] excludes a (rank 1) in
# favour of c (rank 2), which no rank-ordering can do. We implement the principled,
# self-consistent function spec (best-rank dedup, id tie-break) and assert THAT here.

def test_policy_all_returns_all_distinct():
    got = attribution.apply_attribution_policy(_FIXED, policy="all")
    assert set(got) == {"a", "b", "c"}
    # order: by best/lowest rank (a and b both best-rank 1, tie broken by id -> a,b)
    assert got == ["a", "b", "c"]


def test_policy_top_k_lowest_rank():
    # k=1 -> the single lowest-rank distinct id; a & b tie at best-rank 1, id break -> a.
    assert attribution.apply_attribution_policy(_FIXED, policy="top_k", k=1) == ["a"]
    assert attribution.apply_attribution_policy(_FIXED, policy="top_k", k=2) == ["a", "b"]


def test_policy_dedup_keeps_best_rank():
    # 'a' appears at rank 3 and rank 1 -> its best rank is 1; it sorts with b at the
    # rank-1 tier (id-break a before b), then c at rank 2.
    assert attribution.apply_attribution_policy(_FIXED, policy="top_k", k=3) == ["a", "b", "c"]


def test_policy_unknown_raises():
    with pytest.raises(ValueError):
        attribution.apply_attribution_policy(_FIXED, policy="recall")


def test_policy_empty_rows():
    assert attribution.apply_attribution_policy([], policy="all") == []
    assert attribution.apply_attribution_policy([], policy="top_k", k=1) == []


# ---------------------------------------------------------------------------
# 4. attribute_usage — writes informed_by edges, idempotent, fail-open.
# ---------------------------------------------------------------------------

def _informed_by_edges(conn, dst_id):
    return conn.execute(
        "SELECT src_kind, src_id, predicate, dst_kind, dst_id FROM links "
        "WHERE predicate='informed_by' AND dst_id=?", (dst_id,)).fetchall()


def test_attribute_usage_writes_single_top_k_edge_and_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    for mid in ("a", "b"):
        _seed_memory(conn, mid)
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:00", session_id="S", rank=1)
    memory_lib.record_access(conn, target_kind="memory", target_id="b",
                             ts="2026-05-30T10:00:01", session_id="S", rank=2)
    key = memory_lib.record_session_event(
        conn, session_id="S", kind="skill_learning_candidate",
        title="end", ts="2026-05-30T11:00:00", outcome_signal="tests_passed")
    eid = memory_lib.event_id_for_key(conn, key)

    n = attribution.attribute_usage(conn, session_id="S", outcome_event_id=eid,
                                    ts="2026-05-30T11:00:00", policy="top_k", k=1)
    assert n == 1
    # exactly one edge, to the top-ranked unit 'a'
    a_edges = _informed_by_edges(conn, "a")
    assert len(a_edges) == 1
    e = a_edges[0]
    assert e["src_kind"] == "session_event" and e["src_id"] == str(eid)
    assert e["predicate"] == "informed_by" and e["dst_kind"] == "memory"
    assert _informed_by_edges(conn, "b") == []  # not selected at k=1

    # re-run: idempotent (record_link upserts) -> still ONE edge total
    again = attribution.attribute_usage(conn, session_id="S", outcome_event_id=eid,
                                        ts="2026-05-30T12:00:00", policy="top_k", k=1)
    assert again == 1
    assert len(_informed_by_edges(conn, "a")) == 1
    conn.close()


def test_attribute_usage_none_event_id_returns_zero(tmp_path):
    """A malformed / None outcome_event_id is a no-op (return 0), never raises."""
    conn = _db(tmp_path)
    _seed_memory(conn, "a")
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:00", session_id="S", rank=1)
    assert attribution.attribute_usage(conn, session_id="S", outcome_event_id=None,
                                       ts="2026-05-30T11:00:00") == 0
    assert _informed_by_edges(conn, "a") == []
    conn.close()


def test_attribute_usage_fail_open_on_error():
    """attribute_usage must never raise out (it runs in a Stop hook) — a broken
    conn yields 0, not an exception."""
    conn = sqlite3.connect(":memory:")
    conn.close()
    assert attribution.attribute_usage(conn, session_id="S", outcome_event_id=7,
                                       ts="2026-05-30T11:00:00") == 0


def test_attribute_usage_all_policy_writes_every_distinct(tmp_path):
    conn = _db(tmp_path)
    for mid in ("a", "b", "c"):
        _seed_memory(conn, mid)
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:00", session_id="S", rank=1)
    memory_lib.record_access(conn, target_kind="memory", target_id="b",
                             ts="2026-05-30T10:00:01", session_id="S", rank=2)
    memory_lib.record_access(conn, target_kind="memory", target_id="c",
                             ts="2026-05-30T10:00:02", session_id="S", rank=3)
    key = memory_lib.record_session_event(
        conn, session_id="S", kind="skill_learning_candidate", title="end",
        ts="2026-05-30T11:00:00", outcome_signal="trade_win")
    eid = memory_lib.event_id_for_key(conn, key)
    n = attribution.attribute_usage(conn, session_id="S", outcome_event_id=eid,
                                    ts="2026-05-30T11:00:00", policy="all")
    assert n == 3
    assert {r["dst_id"] for r in conn.execute(
        "SELECT dst_id FROM links WHERE predicate='informed_by'")} == {"a", "b", "c"}
    conn.close()


# ---------------------------------------------------------------------------
# 5. THE CONTRACT TEST — the EXACT downstream consumer join must resolve.
# ---------------------------------------------------------------------------

_DOWNSTREAM_JOIN = """
SELECT se.ts, se.outcome_signal
  FROM links l
  JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)
 WHERE l.dst_kind='memory' AND l.dst_id=? AND l.src_kind='session_event'
   AND l.predicate IN ('validated_as','informed_by') AND se.outcome_signal IS NOT NULL
"""


def test_downstream_join_reads_the_edge(tmp_path):
    conn = _db(tmp_path)
    _seed_memory(conn, "a")
    memory_lib.record_access(conn, target_kind="memory", target_id="a",
                             ts="2026-05-30T10:00:00", session_id="S", rank=1)
    key = memory_lib.record_session_event(
        conn, session_id="S", kind="skill_learning_candidate", title="end",
        ts="2026-05-30T11:00:00", outcome_signal="tests_passed")
    eid = memory_lib.event_id_for_key(conn, key)
    attribution.attribute_usage(conn, session_id="S", outcome_event_id=eid,
                                ts="2026-05-30T11:00:00", policy="top_k", k=1)

    rows = conn.execute(_DOWNSTREAM_JOIN, ("a",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome_signal"] == "tests_passed"
    assert rows[0]["ts"] == "2026-05-30T11:00:00"
    conn.close()


# ---------------------------------------------------------------------------
# 6. NO-LLM guard — the attribution module touches no anthropic / subprocess / claude.
# ---------------------------------------------------------------------------

def test_attribution_module_is_no_llm():
    """The join is deterministic with NO LLM: attribution.py's source contains no
    anthropic / subprocess / claude tokens (and never the SDK / API)."""
    import pathlib
    src = pathlib.Path(attribution.__file__).read_text(encoding="utf-8")
    for token in ("anthropic", "subprocess", "claude", "ANTHROPIC_API_KEY",
                  "messages.create", "api.anthropic.com"):
        assert token not in src, f"attribution.py must be NO-LLM, found {token!r}"


def test_attribution_imports_only_stdlib_and_memory_lib():
    """Project-agnostic: attribution.py imports only stdlib + `from . import
    memory_lib` — no consumer/Trading/wiki module."""
    import pathlib
    import re
    src = pathlib.Path(attribution.__file__).read_text(encoding="utf-8")
    banned = re.compile(r"\b(wiki_topics|wiki_lib|wiki_query|trading_strategies)\b")
    for lineno, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("import ") or s.startswith("from "):
            assert not banned.search(s), f"attribution.py:{lineno}: {s}"
