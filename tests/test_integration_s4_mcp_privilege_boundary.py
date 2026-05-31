"""Integration tests for seam S4: the `knowledge` MCP privilege boundary.

Exercises the real seam knowledge_mcp.knowledge_recall -> memory_query.query_memories
-> memory_lib/db (access_log). The ONLY thing mocked is the embedder (a tiny fake
that returns deterministic vectors), exactly like the existing test_knowledge_mcp.py
suite — everything else is the real code path.

Two invariants that no current test asserts:

* G1 — over-fetch / post-filter starvation. ``knowledge_recall`` over-fetches
  ``top_k * 4`` candidates from ``query_memories`` and only THEN drops the
  disallowed (user/feedback) types, truncating to ``top_k``. If MORE than
  ``top_k * 4`` sensitive rows out-rank every allowed (project/reference) row,
  the entire over-fetch window is sensitive, all of it is filtered out, and the
  subagent is starved — it gets 0 allowed rows even though allowed rows exist.
  This doubles as a guard on the magic over-fetch factor 4.

* G2 — negative audit invariant. A DENIED (sensitive) row must never leave a
  trace in ``access_log``. ``knowledge_recall`` audits only ``out`` (the already
  type-filtered list), so this should hold; the test locks it as a regression
  guard against any future refactor that audits the raw recall.

Hermetic: tmp SQLite via memory_lib.open_memory_db(tmp_path); no network; the
embedder is a fake (the real fastembed model is never loaded); no claude CLI.
"""
import pytest

from ultra_memory import knowledge_mcp, memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _save(conn, **kw):
    kw.setdefault("ts", "2026-05-01T00:00:00")
    memory_lib.save_memory(conn, **kw)


# --- deterministic embedder -------------------------------------------------
# 3-D vectors. The query embeds to axis 0. Sensitive rows are seeded with text
# that embeds to axis 0 too (cosine 1.0 -> rank high); allowed rows embed to
# axis 1 (cosine 0.0 -> rank low). We drive this purely by the TEXT so that the
# same fake embedder serves both the query and the stored bodies.
_HIGH_TEXT = "HIGHRANK"   # -> [1,0,0]
_LOW_TEXT = "LOWRANK"     # -> [0,1,0]


def _ranking_embedder(dim=3):
    """Map text -> a deterministic 3-D vector so cosine ranking is fully
    controlled. HIGHRANK and the query rank together at the top; LOWRANK ranks
    last. Any other text -> a third axis (irrelevant)."""
    def _vec(t):
        s = t or ""
        if _HIGH_TEXT in s:
            return [1.0, 0.0, 0.0]
        if _LOW_TEXT in s:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def _embed(texts):
        return [_vec(t) for t in texts]

    return _embed


def _seed_scene(conn, *, n_sensitive, n_allowed):
    """Seed n_sensitive HIGH-ranking sensitive (user/feedback) rows + n_allowed
    LOW-ranking allowed (project/reference) rows. Returns (sensitive_ids, allowed_ids)."""
    sensitive_ids, allowed_ids = [], []
    for i in range(n_sensitive):
        mid = f"s{i}"
        mtype = "user" if i % 2 == 0 else "feedback"
        _save(conn, id=mid, type=mtype, title=f"sensitive {i}",
              body=f"{_HIGH_TEXT} secret body {i}")
        sensitive_ids.append(mid)
    for i in range(n_allowed):
        mid = f"a{i}"
        mtype = "project" if i % 2 == 0 else "reference"
        _save(conn, id=mid, type=mtype, title=f"allowed {i}",
              body=f"{_LOW_TEXT} public body {i}")
        allowed_ids.append(mid)
    return sensitive_ids, allowed_ids


def test_fixture_sanity_sensitive_rows_outrank_allowed(tmp_path):
    """Guard on the test's premise: with no privilege scope (orchestrator sees
    all types), the HIGH-ranking sensitive rows must come back ahead of the
    LOW-ranking allowed rows. If this fails, G1's setup is invalid."""
    conn = _db(tmp_path)
    sens, allowed = _seed_scene(conn, n_sensitive=4, n_allowed=4)
    out = knowledge_mcp.knowledge_recall(
        conn, _HIGH_TEXT, caller_class="orchestrator",
        embedder=_ranking_embedder(), dim=3, top_k=4,
        now_ts="2026-05-02T00:00:00", audit=False)
    # The top-4 by cosine must be exactly the sensitive (HIGH) rows.
    assert [r["id"] for r in out] and all(r["id"] in sens for r in out)
    assert {r["type"] for r in out} <= {"user", "feedback"}
    conn.close()


@pytest.mark.xfail(
    reason=(
        "BUG (high): knowledge_recall over-fetches top_k*4 candidates from "
        "query_memories and type-filters AFTER that bound. When > top_k*4 "
        "sensitive rows out-rank the allowed rows, the whole over-fetch window "
        "is sensitive and gets filtered to nothing -> the subagent is starved "
        "(0 rows) even though allowed rows exist. The type scope must be pushed "
        "into the fetch, not applied after a fixed over-fetch."
    ),
    strict=True,
)
def test_subagent_not_starved_when_sensitive_rows_rank_high(tmp_path):
    conn = _db(tmp_path)
    top_k = 2
    # Must exceed top_k*4 (=8) so the over-fetch window is ALL sensitive.
    n_sensitive = top_k * 4 + 1  # 9
    n_allowed = 4
    sens, allowed = _seed_scene(conn, n_sensitive=n_sensitive, n_allowed=n_allowed)

    out = knowledge_mcp.knowledge_recall(
        conn, _HIGH_TEXT, caller_class="subagent",
        embedder=_ranking_embedder(), dim=3, top_k=top_k,
        now_ts="2026-05-02T00:00:00", audit=False)

    ids = {r["id"] for r in out}
    # The privilege boundary itself must always hold.
    assert {r["type"] for r in out} <= {"project", "reference"}
    assert ids.isdisjoint(set(sens))
    # The actual claim: subagent is NOT starved — it still gets top_k allowed rows.
    assert len(out) == top_k, (
        f"subagent starved: got {len(out)} rows, expected {top_k}; "
        f"allowed rows exist: {allowed}")
    assert ids <= set(allowed)
    conn.close()


def test_denied_rows_leave_no_access_log_trace(tmp_path):
    """G2: a denied (sensitive) row must NOT appear in access_log. Seed allowed +
    sensitive rows, recall as subagent with audit on, then assert the audited
    target_ids are a subset of the allowed ids and disjoint from the sensitive
    ids. Locks the negative audit invariant as a regression guard."""
    conn = _db(tmp_path)
    sens, allowed = _seed_scene(conn, n_sensitive=4, n_allowed=4)

    knowledge_mcp.knowledge_recall(
        conn, _HIGH_TEXT, caller_class="subagent",
        embedder=_ranking_embedder(), dim=3, top_k=4,
        now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00", audit=True)

    rows = conn.execute(
        "SELECT target_id FROM access_log WHERE target_kind='memory'").fetchall()
    logged = {r["target_id"] for r in rows}

    assert logged.isdisjoint(set(sens)), (
        f"denied rows leaked into access_log: {logged & set(sens)}")
    assert logged <= set(allowed)
    conn.close()
