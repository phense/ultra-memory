"""SP-3 Stage 2 — topic on the memory write path + the generic fallback router.

D2/D3/D11 + the PROJECT-AGNOSTIC NFR. This stage adds, in the ultra-memory engine:
  - `save_memory(topic=...)` persists the topic string (just stored TEXT).
  - a deterministic, GENERIC `route_topic(...)` fallback (keyword / caller_class /
    origin_session heuristic, NO LLM, NO wiki dependency) used only when `topic` is
    omitted AND a router is enabled; default None on abstain.
  - an OPTIONAL injectable genesis hook (default no-op) the CONSUMER (Trading) wires
    `wiki_topics.ensure_topic` into; the engine never imports wiki_topics.
  - `query_memories(topic=...)` filter, with NO regression for topic IS NULL rows.

Every test runs on a tmp DB; no live store is touched; no `claude` CLI / LLM is
invoked (the router is pure-Python keyword matching).
"""
import pytest

from ultra_memory import memory_lib
from ultra_memory import memory_query


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _embedder(texts):
    # Deterministic 3-dim stub (no model download); proportional to text length.
    return [[float(len(t)), 1.0, 0.0] for t in texts]


def _topic_of(conn, mid):
    return conn.execute("SELECT topic FROM memories WHERE id=?", (mid,)).fetchone()[0]


# ---------------------------------------------------------------------------
# 1. Explicit topic= persists (just stored TEXT — D1).
# ---------------------------------------------------------------------------

def test_save_memory_persists_explicit_topic(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t",
                           body="body about python", ts="2026-05-31T10:00:00",
                           topic="programming")
    assert _topic_of(conn, "m1") == "programming"
    conn.close()


def test_save_memory_default_topic_is_null(tmp_path):
    """No topic, no router → NULL (today's behavior preserved, D1)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00")
    assert _topic_of(conn, "m1") is None
    conn.close()


def test_explicit_topic_survives_update(tmp_path):
    """An upsert (second save of the same id) keeps/replaces the topic explicitly."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", topic="trading")
    memory_lib.save_memory(conn, id="m1", type="project", title="t2", body="b2",
                           ts="2026-05-31T11:00:00", topic="programming")
    assert _topic_of(conn, "m1") == "programming"
    conn.close()


# ---------------------------------------------------------------------------
# 2. The deterministic, generic fallback router (D3) — NO LLM, NO wiki dep.
# ---------------------------------------------------------------------------

def test_route_topic_keyword_assignment_is_deterministic(tmp_path):
    """A generic keyword router maps a body/title to a topic deterministically."""
    router = memory_lib.make_keyword_router({
        "programming": ("python", "refactor", "pytest"),
        "trading": ("spread", "options", "ibkr"),
    })
    assert router(type="project", title="t", body="a python refactor",
                  origin_session_id=None, caller_class=None) == "programming"
    assert router(type="project", title="t", body="bull put spread on SPY",
                  origin_session_id=None, caller_class=None) == "trading"


def test_route_topic_abstains_to_none(tmp_path):
    """No keyword hit → abstain → None (default NULL, D3)."""
    router = memory_lib.make_keyword_router({"trading": ("spread",)})
    assert router(type="project", title="t", body="nothing relevant here",
                  origin_session_id=None, caller_class=None) is None


def test_save_memory_uses_router_when_topic_omitted(tmp_path):
    conn = _db(tmp_path)
    router = memory_lib.make_keyword_router({"programming": ("pytest",)})
    memory_lib.save_memory(conn, id="m1", type="project", title="t",
                           body="ran pytest green", ts="2026-05-31T10:00:00",
                           topic_router=router)
    assert _topic_of(conn, "m1") == "programming"
    conn.close()


def test_explicit_topic_wins_over_router(tmp_path):
    """An explicit topic= is authoritative; the router is fallback only."""
    conn = _db(tmp_path)
    router = memory_lib.make_keyword_router({"programming": ("pytest",)})
    memory_lib.save_memory(conn, id="m1", type="project", title="t",
                           body="ran pytest green", ts="2026-05-31T10:00:00",
                           topic="trading", topic_router=router)
    assert _topic_of(conn, "m1") == "trading"
    conn.close()


def test_router_abstain_leaves_topic_null(tmp_path):
    conn = _db(tmp_path)
    router = memory_lib.make_keyword_router({"trading": ("spread",)})
    memory_lib.save_memory(conn, id="m1", type="project", title="t",
                           body="unrelated note", ts="2026-05-31T10:00:00",
                           topic_router=router)
    assert _topic_of(conn, "m1") is None
    conn.close()


# ---------------------------------------------------------------------------
# 3. user/feedback rows stay NULL regardless of router (D11).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op_type", ["user", "feedback"])
def test_operational_rows_stay_null_even_with_router(tmp_path, op_type):
    conn = _db(tmp_path)
    # A router that WOULD match — but operational types are cross-topic → NULL.
    router = memory_lib.make_keyword_router({"programming": ("pytest",)})
    memory_lib.save_memory(conn, id="m1", type=op_type, title="t",
                           body="ran pytest green", ts="2026-05-31T10:00:00",
                           topic_router=router)
    assert _topic_of(conn, "m1") is None
    conn.close()


@pytest.mark.parametrize("op_type", ["user", "feedback"])
def test_route_topic_returns_none_for_operational_types(tmp_path, op_type):
    """The router itself abstains for operational types (defense in depth, D11)."""
    router = memory_lib.make_keyword_router({"programming": ("pytest",)})
    assert router(type=op_type, title="t", body="ran pytest",
                  origin_session_id=None, caller_class=None) is None


def test_explicit_topic_on_user_row_is_ignored(tmp_path):
    """Even an explicit topic= on a user/feedback row stays NULL (D11 is a hard
    invariant: operational rows are cross-topic, visible to all)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="feedback", title="t", body="b",
                           ts="2026-05-31T10:00:00", topic="programming")
    assert _topic_of(conn, "m1") is None
    conn.close()


# ---------------------------------------------------------------------------
# 4. Optional injectable genesis hook (default no-op; consumer wires the wiki).
# ---------------------------------------------------------------------------

def test_genesis_hook_fires_on_assigned_topic(tmp_path):
    conn = _db(tmp_path)
    seen = []
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", topic="programming",
                           genesis_hook=lambda topic: seen.append(topic))
    assert seen == ["programming"]
    conn.close()


def test_genesis_hook_default_is_noop(tmp_path):
    """No registered hook → nothing fires, write succeeds (engine has no wiki dep)."""
    conn = _db(tmp_path)
    # Just must not raise; topic still persists.
    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", topic="programming")
    assert _topic_of(conn, "m1") == "programming"
    conn.close()


def test_genesis_hook_not_fired_when_topic_is_null(tmp_path):
    """No topic assigned (abstain / operational) → no genesis call."""
    conn = _db(tmp_path)
    seen = []
    # operational type → NULL → no genesis
    memory_lib.save_memory(conn, id="op", type="user", title="t", body="b",
                           ts="2026-05-31T10:00:00", topic="programming",
                           genesis_hook=lambda topic: seen.append(topic))
    # router abstains → NULL → no genesis
    router = memory_lib.make_keyword_router({"trading": ("spread",)})
    memory_lib.save_memory(conn, id="ab", type="project", title="t", body="nope",
                           ts="2026-05-31T10:00:00", topic_router=router,
                           genesis_hook=lambda topic: seen.append(topic))
    assert seen == []
    conn.close()


def test_genesis_hook_fires_for_router_assigned_topic(tmp_path):
    conn = _db(tmp_path)
    seen = []
    router = memory_lib.make_keyword_router({"programming": ("pytest",)})
    memory_lib.save_memory(conn, id="m1", type="project", title="t",
                           body="ran pytest green", ts="2026-05-31T10:00:00",
                           topic_router=router,
                           genesis_hook=lambda topic: seen.append(topic))
    assert seen == ["programming"]
    conn.close()


def test_genesis_hook_failure_does_not_break_write(tmp_path):
    """The hook is consumer-side and best-effort: a raising hook must not abort the
    write (fail-open — the topic still persists; the engine stays agnostic)."""
    conn = _db(tmp_path)

    def boom(topic):
        raise RuntimeError("consumer genesis blew up")

    memory_lib.save_memory(conn, id="m1", type="project", title="t", body="b",
                           ts="2026-05-31T10:00:00", topic="programming",
                           genesis_hook=boom)
    assert _topic_of(conn, "m1") == "programming"
    conn.close()


# ---------------------------------------------------------------------------
# 5. query_memories(topic=...) filter + NO regression for topic IS NULL rows.
# ---------------------------------------------------------------------------

def _seed_topiced(conn):
    memory_lib.save_memory(conn, id="prog", type="project", title="prog",
                           body="shared body", ts="2026-05-30T10:00:00",
                           topic="programming")
    memory_lib.save_memory(conn, id="trade", type="project", title="trade",
                           body="shared body", ts="2026-05-30T10:00:00",
                           topic="trading")
    # operational, stays NULL (cross-topic)
    memory_lib.save_memory(conn, id="op", type="user", title="op",
                           body="shared body", ts="2026-05-30T10:00:00")


def test_query_topic_filter_scopes_to_topic_plus_null(tmp_path):
    """query_memories(topic='programming') returns the programming row AND the
    NULL-topic (cross-topic) row, but NOT the trading row."""
    conn = _db(tmp_path)
    _seed_topiced(conn)
    out = memory_query.query_memories(conn, "shared body", embedder=_embedder,
                                      dim=3, now_ts="2026-05-30T12:00:00",
                                      topic="programming")
    ids = {r["id"] for r in out}
    assert "prog" in ids
    assert "op" in ids        # NULL-topic always visible (D11)
    assert "trade" not in ids
    conn.close()


def test_query_without_topic_returns_all_no_regression(tmp_path):
    """No topic filter → all rows returned exactly as before (Stage 1 behavior).
    This is the NO-REGRESSION fence: topic IS NULL rows stay visible."""
    conn = _db(tmp_path)
    _seed_topiced(conn)
    out = memory_query.query_memories(conn, "shared body", embedder=_embedder,
                                      dim=3, now_ts="2026-05-30T12:00:00")
    ids = {r["id"] for r in out}
    assert ids == {"prog", "trade", "op"}
    conn.close()


def test_query_topic_filter_still_returns_null_topic_rows(tmp_path):
    """Even a topic that matches NOTHING topiced still returns the NULL-topic rows
    (no retrieval regression: an untopic'd corpus stays fully visible)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="op", type="user", title="op", body="shared body",
                           ts="2026-05-30T10:00:00")
    memory_lib.save_memory(conn, id="plain", type="project", title="plain",
                           body="shared body", ts="2026-05-30T10:00:00")  # NULL topic
    out = memory_query.query_memories(conn, "shared body", embedder=_embedder,
                                      dim=3, now_ts="2026-05-30T12:00:00",
                                      topic="nonexistent-topic")
    ids = {r["id"] for r in out}
    assert ids == {"op", "plain"}
    conn.close()


def test_query_topic_filter_composes_with_include_types(tmp_path):
    """The topic axis composes with the existing type-scope (orthogonal AND)."""
    conn = _db(tmp_path)
    _seed_topiced(conn)
    memory_lib.save_memory(conn, id="prog_ref", type="reference", title="ref",
                           body="shared body", ts="2026-05-30T10:00:00",
                           topic="programming")
    out = memory_query.query_memories(
        conn, "shared body", embedder=_embedder, dim=3,
        now_ts="2026-05-30T12:00:00", topic="programming",
        include_types=("reference",))
    ids = {r["id"] for r in out}
    # programming + reference type only; 'op' is type=user (excluded by type),
    # 'prog' is type=project (excluded by type), 'trade' is wrong topic.
    assert ids == {"prog_ref"}
    conn.close()


# ---------------------------------------------------------------------------
# 6. Spool round-trip — a spooled save with a topic replays the topic.
#    (The router/hook are NOT spooled — they are in-process callables; the spool
#    carries the RESOLVED topic string, keeping replay deterministic.)
# ---------------------------------------------------------------------------

def test_spooled_save_replays_resolved_topic(tmp_path):
    import json
    conn = _db(tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "s.json").write_text(json.dumps({
        "op": "save_memory", "id": "m1", "type": "project", "title": "t",
        "body": "b", "ts": "2026-05-31T10:00:00", "topic": "programming"}))
    summary = memory_lib.replay_spool(conn, spool_dir=spool)
    assert summary["replayed"] == 1 and summary["failed"] == 0
    assert _topic_of(conn, "m1") == "programming"
    conn.close()


# ---------------------------------------------------------------------------
# 7. PROJECT-AGNOSTIC NFR — the engine has NO import of wiki_topics / Trading.
# ---------------------------------------------------------------------------

def test_engine_has_no_wiki_or_trading_import():
    """Hard NFR: the dependency is one-way (Trading imports ultra-memory, never the
    reverse). No engine source may import wiki_topics / wiki_lib / any Trading
    consumer module. The genesis hook is injectable precisely to keep this true."""
    import pathlib
    import re
    pkg = pathlib.Path(memory_lib.__file__).resolve().parent
    banned = re.compile(r"\b(wiki_topics|wiki_lib|wiki_query|trading_strategies)\b")
    offenders = []
    for p in sorted(pkg.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if (stripped.startswith("import ") or stripped.startswith("from ")) \
                    and banned.search(stripped):
                offenders.append(f"{p.name}:{lineno}: {stripped}")
    assert not offenders, (
        "Engine imports a consumer/wiki module — violates the one-way "
        "project-agnostic boundary:\n" + "\n".join(offenders))
