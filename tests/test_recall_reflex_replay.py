"""Recall-Reflex §8a — the originating-incident replay, against the LIVE store.

Success criterion §8a: after the fastembed atomic is captured, a recall() with
incident-#2's error text surfaces incident-#1's lesson in top-N — the second fix
becomes a ~2-second search hit, not a re-derivation.

This is an INTEGRATION eval, not a unit test: it runs against the real global
store (~/.ultra-memory/memory.db) populated by wiki_sync. It SKIPS cleanly when
that environment is absent (CI / a fresh machine), so it never fails spuriously —
it only asserts the criterion where the store actually exists.
"""
import os
from pathlib import Path

import pytest

from ultra_memory import recall

_DB = Path(os.environ.get("ULTRA_MEMORY_DB") or (Path.home() / ".ultra-memory" / "memory.db"))
_FASTEMBED_SLUG = "fastembed-model-cache-tmpdir-purge"


def _mirror_has(slug):
    if not _DB.exists():
        return False
    import sqlite3
    try:
        conn = sqlite3.connect(str(_DB))
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM unified_index WHERE slug=?", (slug,)).fetchone()[0]
        finally:
            conn.close()
        return n > 0
    except sqlite3.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _mirror_has(_FASTEMBED_SLUG),
    reason="live ~/.ultra-memory mirror not populated with the fastembed atomic "
           "(run `python -m ultra_memory.wiki_sync`); §8a replay is env-gated")


def test_fastembed_incident_replay_surfaces_the_atomic():
    """Incident-#2's error text recalls incident-#1's atomic in top-N (§8a)."""
    hits = recall.recall(
        "onnxruntime NoSuchFile model_optimized.onnx fastembed cache temp dir purge",
        top_k=5, caller_class="subagent", agent_topics=None)
    slugs = [h.get("slug") for h in hits]
    assert _FASTEMBED_SLUG in slugs, f"expected {_FASTEMBED_SLUG} in top-5, got {slugs}"


def test_mirror_is_populated_for_trading_topic():
    """Phase-0 gate: the plugin retrieval mirror is non-empty for topic=trading."""
    import sqlite3
    conn = sqlite3.connect(str(_DB))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM unified_index WHERE topic='trading'").fetchone()[0]
    finally:
        conn.close()
    assert n > 0
