"""End-to-end: atomic_graduate → the REAL gateway CLI (`wiki_gateway.cli`, base
WikiGateway) → a brand-new theme's theme-index is created AND wired into the index
hierarchy. The unit tests stub `gateway_run`; this drives the actual register-index
verb so the create+wire path is exercised for real.
"""
from pathlib import Path

from ultra_memory import memory_lib, wiki_gateway
from ultra_memory.maintenance import atomic_graduate as ag
from ultra_memory.maintenance import session_ingest as si

TS = "2026-06-05T00:00:00"


def _db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "m.db"))


def test_e2e_new_theme_created_and_wired_via_real_gateway(tmp_path):
    root = tmp_path / "wiki"
    (root / "trading" / "concepts").mkdir(parents=True)
    conn = _db(tmp_path)
    # A candidate with a kind ("gotcha") that — with no theme_map — becomes a NEW
    # theme, so register-index must auto-create + wire its theme-index.
    si._save_atomic_candidates(conn, [{
        "kind": "gotcha", "signal": "onnxruntime NoSuchFile model_optimized.onnx",
        "title": "fastembed cache purged by the OS reaper",
        "body": "pin a persistent cache dir", "topic": "trading"}],
        session_id="S1", ts=TS)

    def gw(verb, args, content):
        a = list(args)
        if content is not None:
            f = tmp_path / "page.md"
            f.write_text(content)
            a += ["--from-file", str(f)]
        rc = wiki_gateway.cli(argv=[verb, *a], wiki_root=root)
        assert rc == 0, f"real gateway verb {verb!r} failed (rc={rc})"

    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=root, cap=3, valid_topics={"trading"}, default_topic="trading")

    assert res["created"] == 1, res

    # The new theme defaults to the candidate kind "gotcha".
    theme_indexes = list(root.rglob("gotcha-index.md"))
    assert theme_indexes, "register-index must auto-create the new theme-index"
    ti = theme_indexes[0].read_text()
    assert "type: theme-index" in ti

    # The atomic page itself was created under the topic concepts tree.
    atomics = [p for p in (root / "trading" / "concepts").glob("*.md")
               if not p.name.endswith("-index.md")]
    assert len(atomics) == 1 and "## Signal" in atomics[0].read_text()

    # Wired: the new theme-index is linked from some index.md (topic master /
    # master-over-masters), i.e. reachable from the browse hierarchy.
    index_texts = [p.read_text() for p in root.rglob("index.md")]
    assert any("[[gotcha-index]]" in t for t in index_texts), \
        "the new theme-index must be wired into the index hierarchy"
    conn.close()
