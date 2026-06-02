"""Tests for ultra_memory/wiki_embed_cache.py — SQLite-backed embedding cache.

Ported from Trading/tests/test_wiki_embed_cache.py (which tests the Trading
scripts/wiki_embed_cache.py source); this tests the plugin-local copy.

Two tables in one DB file:
- wiki_atomic_embeddings: path-keyed, sha256-validated
- text_embeddings: text-keyed (text IS the key — change text = new entry)

Both tables carry a model_name column for auto-invalidation on model swap.
"""
from ultra_memory import wiki_embed_cache as ec


MODEL = "BAAI/bge-small-en-v1.5"


def test_put_get_roundtrip(tmp_path):
    """Round-trip: put then get returns (sha256, vec); sha mismatch returns None."""
    db = tmp_path / "e.db"
    ec.init_db(db_path=db)
    ec.put_atomic("/wiki/concepts/a.md", "deadbeef", [0.1] * 384, MODEL, db_path=db)
    got = ec.get_atomic("/wiki/concepts/a.md", model_name=MODEL, db_path=db)
    assert got is not None
    sha, vec = got
    assert sha == "deadbeef"
    assert len(vec) == 384
    assert abs(vec[0] - 0.1) < 1e-6
    # model mismatch → None (the sha mismatch the plan described maps to model_name mismatch)
    assert ec.get_atomic("/wiki/concepts/a.md", model_name="other-model", db_path=db) is None
