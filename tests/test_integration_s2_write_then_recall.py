"""Integration tests for seam S2: write (memory_lib) -> recall (memory_query / knowledge_mcp).

These exercise the real write/read seam end-to-end through 2+ modules:
  - ultra_memory.memory_lib    -- the single audited SQLite WRITE path
                                  (save_memory / set_pinned / delete / consolidate /
                                  record_access), each wrapped in BEGIN IMMEDIATE +
                                  audit + redact.
  - ultra_memory.memory_query  -- the read-side ranker (cosine + title/access/strength/
                                  staleness signals), backed by
  - ultra_memory.retrieval_core -- the pack/store/unpack embedding cache.
  - ultra_memory.knowledge_mcp  -- the audited read-only MCP recall surface with
                                  per-caller-class type-scoping (the privilege boundary).

Hermetic & deterministic:
  - a migrated tmp-file SQLite via memory_lib.open_memory_db(tmp_path / "m.db")
    (NEVER the real data/memory.db); mirrors the _db() helper used across the suite,
  - no network, no real `claude` CLI / Anthropic SDK (this seam makes no LLM call),
  - embeddings come from an injected fake `embedder` (list[str] -> list[list[float]],
    matching the codebase contract) so cosine scores are fully determined.

The proposed S2 gate names are mapped onto this codebase's ACTUAL API (the originally
proposed names assumed a `query_memories(...)`/`set_pinned(id, bool)`/`knowledge_recall`
shape that does not exist here -- see each test's docstring for the mapping).
"""

from __future__ import annotations

import pytest

from ultra_memory import memory_lib, memory_query, retrieval_core, knowledge_mcp


# --- fixtures / helpers (consistent with tests/test_memory_query.py et al.) ---

DIM = 3
NOW = "2026-05-02T00:00:00"
TS = "2026-05-01T00:00:00"


@pytest.fixture
def conn(tmp_path):
    c = memory_lib.open_memory_db(tmp_path / "m.db")
    yield c
    c.close()


def save(conn, **kw):
    kw.setdefault("type", "project")
    kw.setdefault("ts", TS)
    return memory_lib.save_memory(conn, **kw)


def mapping_embedder(mapping, dim=DIM):
    """Deterministic fake embedder. Maps the FIRST `key in text` -> its vector, else a
    zero vector. Batched (list[str] -> list[list[float]]), per the real contract.

    Insertion order matters: put the full query text first so it resolves to its own
    vector rather than a substring of a stored doc (the pattern test_memory_query uses).
    """
    def _embed(texts):
        out = []
        for t in texts:
            vec = [0.0] * dim
            for key, v in mapping.items():
                if key in t:
                    vec = list(v)
                    break
            out.append(vec)
        return out
    return _embed


def flat_embedder(vec=(1.0, 0.0, 0.0)):
    """Every text -> the same vector, so cosine ties for all rows and the behaviour
    under test is type-scoping / exclusion / signal-application, not raw relevance."""
    v = list(vec)
    def _embed(texts):
        return [list(v) for _ in texts]
    return _embed


# ---------------------------------------------------------------------------
# G1 -- pinned does NOT participate in recall ranking.
# Proposed: "set_pinned one of two equal rows; assert query order unchanged and
# 'pinned' is not a result key." Mapped: query_memories takes no `pinned` signal;
# its result dicts intentionally don't even carry `pinned`. We pin the LESS-relevant
# row and assert ordering is byte-identical to the unpinned baseline.
# ---------------------------------------------------------------------------
def test_seam_pinned_does_not_boost_query_recall(conn):
    relevant = save(conn, id="apple", title="apple", body="apple fruit pie")
    other = save(conn, id="car", title="car", body="car engine oil")
    emb = mapping_embedder({
        "apple": [1.0, 0.0, 0.0],   # query "apple" -> this; "apple..." body -> this
        "car": [0.0, 1.0, 0.0],
    })

    baseline = memory_query.query_memories(conn, "apple", embedder=emb, dim=DIM, now_ts=NOW)
    baseline_order = [r["id"] for r in baseline]
    assert baseline_order[0] == relevant  # sanity: cosine puts the relevant row first

    # Pin the LESS-relevant row. If pinning leaked into ranking it would jump ahead.
    memory_lib.set_pinned(conn, id=other, pinned=True, ts=NOW)
    assert conn.execute("SELECT pinned FROM memories WHERE id=?", (other,)).fetchone()[0] == 1

    after = memory_query.query_memories(conn, "apple", embedder=emb, dim=DIM, now_ts=NOW)
    after_order = [r["id"] for r in after]

    assert after_order == baseline_order, "pinning must NOT change recall ordering"
    # The contract the proposal cared about: pin state is not a ranking result field.
    assert "pinned" not in after[0], "query result dicts must not expose a ranking 'pinned' key"


# ---------------------------------------------------------------------------
# G2 -- raw query_memories does NO type-scoping (scoping is the MCP's job).
# ---------------------------------------------------------------------------
def test_seam_query_memories_returns_all_types_no_scoping(conn):
    u = save(conn, id="usr", type="user", title="u", body="user secret password")
    p = save(conn, id="proj", type="project", title="p", body="project status note")
    f = save(conn, id="fb", type="feedback", title="f", body="feedback preference")

    out = memory_query.query_memories(conn, "anything", embedder=flat_embedder(),
                                      dim=DIM, now_ts=NOW)
    ids = {r["id"] for r in out}
    types = {r["type"] for r in out}

    assert {u, p, f} <= ids, "query_memories must return ALL types -- it does no scoping"
    assert {"user", "project", "feedback"} <= types


# ---------------------------------------------------------------------------
# G3 -- MCP subagent recall excludes deleted + redirect rows.
# (mirrors test_memory_query::test_query_excludes_deleted_and_redirect on the
#  higher-stakes audited MCP surface.)
# ---------------------------------------------------------------------------
def test_seam_mcp_recall_excludes_deleted_and_redirect(conn):
    keep = save(conn, id="keep", type="project", title="keep", body="keep me")
    gone = save(conn, id="gone", type="project", title="gone", body="delete me")
    moved = save(conn, id="moved", type="project", title="moved", body="redirect me")
    canon = save(conn, id="canon", type="project", title="canon", body="canonical")

    memory_lib.delete(conn, id=gone, reason="garbage", tier="volatile", ts=NOW)
    memory_lib.consolidate(conn, loser_id=moved, canonical_id=canon, reason="dup", ts=NOW)

    out = knowledge_mcp.knowledge_recall(conn, "q", caller_class="subagent",
                                         embedder=flat_embedder(), dim=DIM,
                                         now_ts=NOW, audit=False)
    ids = {r["id"] for r in out}

    assert gone not in ids, "soft-deleted memory leaked into MCP recall"
    assert moved not in ids, "redirect/consolidated memory leaked into MCP recall"
    assert keep in ids
    assert canon in ids


def test_seam_mcp_subagent_scoping_blocks_user_and_feedback(conn):
    """The privilege boundary, end-to-end from the write path: a subagent caller gets
    project/reference only; user/feedback are fail-closed. Concrete enforcement of
    feedback_subagents_can_leak_secrets as a TOOL constraint."""
    secret = save(conn, id="secret", type="user", title="peter pref", body="user secret token")
    fb = save(conn, id="fb", type="feedback", title="how to work", body="feedback note")
    proj = save(conn, id="proj", type="project", title="p", body="project fact")
    ref = save(conn, id="ref", type="reference", title="r", body="reference datum")

    out = knowledge_mcp.knowledge_recall(conn, "q", caller_class="subagent",
                                         embedder=flat_embedder(), dim=DIM,
                                         now_ts=NOW, audit=False)
    ids = {r["id"] for r in out}
    types = {r["type"] for r in out}

    assert secret not in ids, "user-type memory leaked to a subagent caller"
    assert fb not in ids, "feedback-type memory leaked to a subagent caller"
    assert proj in ids
    assert ref in ids
    assert types <= {"project", "reference"}


# ---------------------------------------------------------------------------
# G4 -- the STORED embedding blob is what gets scored on later recalls.
# Guards the pack -> store(embeddings.vector) -> unpack -> cosine cache seam.
# After the first recall persists the row's embedding, a second recall whose
# embedder RAISES if asked to re-embed the stored doc text must still score it.
# ---------------------------------------------------------------------------
def test_seam_stored_embedding_blob_is_what_is_scored(conn):
    save(conn, id="target", type="project", title="cache", body="cache target unique content")
    doc_text = "cache\ncache target unique content"  # memory_query._doc_text = title\nbody

    # First recall: persists embeddings.vector for the target.
    populate = mapping_embedder({"populate-query": [0.0, 1.0, 0.0]}, dim=DIM)
    # nudge the populate embedder to give the stored doc a known vector:
    populate = mapping_embedder({
        "first-query": [0.5, 0.5, 0.5],
        "cache target unique content": [0.0, 1.0, 0.0],
    }, dim=DIM)
    memory_query.query_memories(conn, "first-query", embedder=populate, dim=DIM, now_ts=NOW)

    # The blob now exists and unpacks to the stored vector.
    row = conn.execute(
        "SELECT vector, dim FROM embeddings WHERE target_kind='memory' AND target_id=?",
        ("target",)).fetchone()
    assert row is not None, "first recall did not persist an embedding blob"
    stored_vec = retrieval_core.unpack_vector(row["vector"], dim=row["dim"])
    assert stored_vec == pytest.approx([0.0, 1.0, 0.0])

    # Second recall: embedder explodes if it is ever asked to re-embed the stored doc.
    def strict_embedder(texts):
        out = []
        for t in texts:
            if "cache target unique content" in t:
                raise AssertionError(
                    "stored doc was re-embedded -- cached blob was not used for scoring")
            out.append([0.0, 1.0, 0.0])  # the query "second-query" -> [0,1,0]
        return out

    out = memory_query.query_memories(conn, "second-query", embedder=strict_embedder,
                                      dim=DIM, now_ts=NOW)
    scored = {r["id"]: r["score"] for r in out}
    # query [0,1,0] vs stored [0,1,0] -> cosine 1.0, proving the stored blob drove it.
    assert scored["target"] == pytest.approx(1.0)


def test_seam_recall_recomputes_when_body_changes(conn):
    """Complement to G4: the cache is content-addressed, so editing the body (a new
    save_memory) MUST invalidate the stored vector and re-embed on the next recall.
    Proves the pack/cache/unpack seam keys on content_sha256, not just on id."""
    save(conn, id="m", type="project", title="t", body="original body")
    emb1 = mapping_embedder({"q": [1.0, 0.0, 0.0], "original body": [1.0, 0.0, 0.0]})
    out1 = memory_query.query_memories(conn, "q", embedder=emb1, dim=DIM, now_ts=NOW)
    assert next(r for r in out1 if r["id"] == "m")["score"] == pytest.approx(1.0)

    # Rewrite the body -> content hash changes.
    memory_lib.save_memory(conn, id="m", type="project", title="t",
                           body="completely different text", ts="2026-05-01T01:00:00")
    calls = []
    def emb2(texts):
        calls.append(list(texts))
        out = []
        for t in texts:
            out.append([0.0, 1.0, 0.0] if "completely different text" in t else [1.0, 0.0, 0.0])
        return out
    memory_query.query_memories(conn, "q", embedder=emb2, dim=DIM, now_ts=NOW)
    # the changed doc must have been re-embedded (its new text appears in a batch call)
    assert any("completely different text" in t for batch in calls for t in batch), \
        "changed body was not re-embedded -- stale cached vector would have been scored"


# ---------------------------------------------------------------------------
# G5 -- strength multiplies the score on recall.
# Proposed: set strength via raw UPDATE, assert two equal-cosine rows order by
# strength. In THIS codebase strength is a recall signal (memory_query line ~87:
# `score *= strength`), so this is a real passing contract, not an xfail.
# ---------------------------------------------------------------------------
def test_seam_strength_multiplier_applies_on_recall(conn):
    save(conn, id="low", type="project", title="low", body="identical relevance text")
    save(conn, id="high", type="project", title="high", body="identical relevance text")

    # Equal cosine for both rows against the query. (Title tokens 'low'/'high' are not
    # in the query, so no title boost skews the comparison.)
    emb = mapping_embedder({
        "q": [1.0, 0.0, 0.0],
        "identical relevance text": [1.0, 0.0, 0.0],
    })

    # No write-strength verb exists in memory_lib -> set via raw UPDATE (the proposal's
    # explicit instruction). The READ side is what we are pinning.
    conn.execute("UPDATE memories SET strength=? WHERE id=?", (0.1, "low"))
    conn.execute("UPDATE memories SET strength=? WHERE id=?", (0.9, "high"))

    out = memory_query.query_memories(conn, "q", embedder=emb, dim=DIM, now_ts=NOW)
    order = [r["id"] for r in out]
    assert order[0] == "high", "higher-strength row must rank first on equal cosine"
    hi = next(r for r in out if r["id"] == "high")
    lo = next(r for r in out if r["id"] == "low")
    assert hi["score"] > lo["score"]
    # And the multiplier is the actual mechanism: high≈0.9*1.0, low≈0.1*1.0.
    assert hi["score"] == pytest.approx(0.9, abs=1e-6)
    assert lo["score"] == pytest.approx(0.1, abs=1e-6)
