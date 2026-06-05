"""Generic wiki-maintenance — slice 3: detect_dedup (move-with-config). For each NEW
atomic, find its single best candidate by cosine over precomputed vectors; classify
auto-merge / grey-zone / drop by the schema's dedup thresholds. Pure: vectors, the
cosine function, and `text_of` are all injected — no fastembed on the test path.
"""
from pathlib import Path

from ultra_memory.wiki_maintenance import detect_dedup as dd
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def _w():
    return wl.new_worklist("wiki", generated_at="2026-06-02")


def _cosine_table(table):
    """A symmetric cosine over single-element vectors whose value is an id key."""
    def _cos(a, b):
        return table.get((a[0], b[0]), table.get((b[0], a[0]), 0.0))
    return _cos


def _text_of(p):
    return f"body of {p}"


def test_auto_merge_above_upper_threshold():
    w = _w()
    vecs = {"new.md": (None, ["n"]), "old.md": (None, ["o"])}
    cos = _cosine_table({("n", "o"): 0.90})
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos)
    assert len(w["items"]) == 1
    it = w["items"][0]
    assert it["kind"] == "greyzone-dedup" and it["priority"] == 1
    assert it["candidate_path"] == "old.md" and it["cosine"] == 0.9
    assert "auto-merge" in it["evidence"]


def test_grey_zone_between_thresholds():
    w = _w()
    vecs = {"new.md": (None, ["n"]), "old.md": (None, ["o"])}
    cos = _cosine_table({("n", "o"): 0.80})
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos)
    assert w["items"][0]["priority"] == 3
    assert "grey-zone" in w["items"][0]["evidence"]


def test_drop_below_lower_threshold():
    w = _w()
    vecs = {"new.md": (None, ["n"]), "old.md": (None, ["o"])}
    cos = _cosine_table({("n", "o"): 0.5})
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos)
    assert w["items"] == []


def test_tie_break_prefers_lexicographically_smaller_path():
    w = _w()
    vecs = {"new.md": (None, ["n"]), "b.md": (None, ["b"]), "a.md": (None, ["a"])}
    cos = _cosine_table({("n", "a"): 0.9, ("n", "b"): 0.9})   # exact tie
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos)
    assert w["items"][0]["candidate_path"] == "a.md"


def test_new_atomic_without_vector_is_skipped():
    w = _w()
    vecs = {"old.md": (None, ["o"])}                          # new.md has no vec
    cos = _cosine_table({})
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos)
    assert w["items"] == []


def test_custom_thresholds_via_schema():
    w = _w()
    schema = WikiSchemaConfig(dedup_lower=0.5, dedup_upper=0.95)
    vecs = {"new.md": (None, ["n"]), "old.md": (None, ["o"])}
    cos = _cosine_table({("n", "o"): 0.6})                    # grey under the wider band
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos, schema=schema)
    assert w["items"][0]["priority"] == 3


def test_no_trading_or_path_literal():
    src = Path(dd.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src


def test_signal_axis_flags_pair_when_mechanism_is_below_band():
    """Recall-Reflex: a pair with LOW mechanism cosine but HIGH ## Signal cosine is
    flagged via the signal axis (the literal-observable dedup the mechanism misses)."""
    w = _w()
    vecs = {"new.md": (None, ["nm"]), "old.md": (None, ["om"])}
    signal_vecs = {"new.md": (None, ["ns"]), "old.md": (None, ["os"])}
    cos = _cosine_table({("nm", "om"): 0.50, ("ns", "os"): 0.90})
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos,
           signal_vecs=signal_vecs)
    assert len(w["items"]) == 1
    it = w["items"][0]
    assert it["candidate_path"] == "old.md"
    assert it["cosine"] == 0.9 and it["priority"] == 1


def test_signal_axis_absent_preserves_mechanism_only_behavior():
    """No signal_vecs → identical to before: a mechanism cosine below the band
    yields no finding (backward-compatible)."""
    w = _w()
    vecs = {"new.md": (None, ["nm"]), "old.md": (None, ["om"])}
    cos = _cosine_table({("nm", "om"): 0.50})
    dd.run(w, new_atomics=["new.md"], vecs=vecs, text_of=_text_of, cosine=cos)
    assert w["items"] == []
