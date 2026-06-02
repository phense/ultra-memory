"""Model B (projection-coupled skill evolution) — the union-blend managed-block
renderer in memory_export. Locked params (spec 2026-06-02): feed = UNION of
(source_domain, gen-<slug>) hooks de-duped; cap = 20; ranking = blend
score = outcome_weight * 0.5 ** (age_days / HALFLIFE_DAYS), HALFLIFE_DAYS=45.
"""
from ultra_memory import memory_export as mx
from ultra_memory import memory_lib

NOW = "2026-06-02T00:00:00Z"


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _learning(conn, *, id, hook, title, body="b.", weight=1.0,
              created_at="2026-06-02T00:00:00Z", node_type="learning",
              status="active", created_by="background_review"):
    memory_lib.save_memory(conn, id=id, type="memory", title=title, body=body,
                           ts=created_at, index_hook=hook, node_type=node_type,
                           created_by=created_by, created_at=created_at)
    conn.execute("UPDATE memories SET outcome_weight=?, status=? WHERE id=?",
                 (weight, status, id))
    conn.commit()


def _titles(block):
    return [l[4:].strip() for l in block.splitlines() if l.startswith("### ")]


def test_block_ranks_by_blend_score(tmp_path):
    """Highest outcome_weight*recency_decay first. A heavily-proven recent lesson
    beats a fresh weak one beats a stale one."""
    conn = _db(tmp_path)
    _learning(conn, id="L1", hook="backtest", title="L1", weight=5.0,
              created_at="2026-05-01T00:00:00Z")   # ~32d → ~3.0
    _learning(conn, id="L2", hook="backtest", title="L2", weight=1.0,
              created_at="2026-06-02T00:00:00Z")   # 0d  → 1.0
    _learning(conn, id="L3", hook="backtest", title="L3", weight=2.0,
              created_at="2025-01-01T00:00:00Z")   # ~517d → ~0.0
    block = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    assert _titles(block) == ["L1", "L2", "L3"]
    conn.close()


def test_block_caps_at_20(tmp_path):
    conn = _db(tmp_path)
    for i in range(25):
        _learning(conn, id=f"L{i:02d}", hook="backtest", title=f"L{i:02d}",
                  weight=float(i + 1), created_at="2026-06-01T00:00:00Z")
    block = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW, cap=20)
    assert len(_titles(block)) == 20
    # the 20 kept are the highest-weight ones (L24..L05), L00..L04 dropped.
    assert "L24" in _titles(block) and "L00" not in _titles(block)
    conn.close()


def test_block_unions_two_hooks(tmp_path):
    """source_domain + gen-<slug> own-usage feed both flow in."""
    conn = _db(tmp_path)
    _learning(conn, id="D1", hook="backtest", title="DOMAIN-ONE", weight=2.0)
    _learning(conn, id="G1", hook="gen-backtest", title="OWN-USAGE", weight=3.0)
    block = mx.render_union_blend_block(
        conn, hooks=["backtest", "gen-backtest"], now=NOW)
    assert _titles(block) == ["OWN-USAGE", "DOMAIN-ONE"]
    conn.close()


def test_block_dedups_duplicate_hook(tmp_path):
    """hooks=[x, x] (source_domain == gen-slug edge) must not double-count a row."""
    conn = _db(tmp_path)
    _learning(conn, id="D1", hook="gen-x", title="ONLY", weight=1.0)
    block = mx.render_union_blend_block(conn, hooks=["gen-x", "gen-x"], now=NOW)
    assert _titles(block) == ["ONLY"]
    conn.close()


def test_block_excludes_non_learning_and_inactive(tmp_path):
    conn = _db(tmp_path)
    _learning(conn, id="OK", hook="backtest", title="KEEP")
    _learning(conn, id="M1", hook="backtest", title="NOTLEARNING",
              node_type="memory")
    _learning(conn, id="R1", hook="backtest", title="REDIRECTED",
              status="redirect")
    block = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    assert _titles(block) == ["KEEP"]
    conn.close()


def test_block_empty_feed_sentinel(tmp_path):
    conn = _db(tmp_path)
    block = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    assert "### " not in block
    assert "No learnings recorded yet" in block
    conn.close()


def test_block_renders_body_verbatim(tmp_path):
    conn = _db(tmp_path)
    _learning(conn, id="L1", hook="backtest", title="T1",
              body="Line one.\nLine two.")
    block = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    assert "### T1" in block
    assert "Line one.\nLine two." in block
    conn.close()


def test_block_deterministic_given_now(tmp_path):
    conn = _db(tmp_path)
    _learning(conn, id="L1", hook="backtest", title="A", weight=2.0)
    _learning(conn, id="L2", hook="backtest", title="B", weight=1.0)
    a = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    b = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    assert a == b
    conn.close()


def test_block_tolerates_unparseable_created_at(tmp_path):
    """A garbage created_at must not crash — fail-open to age 0 (decay 1.0)."""
    conn = _db(tmp_path)
    _learning(conn, id="L1", hook="backtest", title="GARBAGE",
              created_at="not-a-date")
    block = mx.render_union_blend_block(conn, hooks=["backtest"], now=NOW)
    assert _titles(block) == ["GARBAGE"]
    conn.close()


def test_block_empty_hooks_is_sentinel(tmp_path):
    conn = _db(tmp_path)
    _learning(conn, id="L1", hook="backtest", title="A")
    block = mx.render_union_blend_block(conn, hooks=[], now=NOW)
    assert "### " not in block
    conn.close()
