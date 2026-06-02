# tests/test_wiki_gateway_base.py
import pytest
from pathlib import Path
from ultra_memory.wiki_gateway import WikiGateway

def test_default_route_is_topic_concepts_slug(tmp_path):
    gw = WikiGateway(wiki_root=tmp_path, topic="research")
    p = gw.route({"title": "Liquidity Spirals & Reflexivity"})
    assert p == Path("research/concepts/liquidity-spirals-reflexivity.md")

def test_default_confidence_is_standard():
    assert WikiGateway(topic="research").confidence_label({"speaker": "x"}) == "Standard"

def test_default_dedup_is_off():
    assert WikiGateway(topic="research").dedup_check("any text", topic="research") is None

def test_default_anchor_is_none():
    assert WikiGateway(topic="research").derive_anchor({"title": "t"}, existing=None) is None

def test_cosine_identity():
    gw = WikiGateway(topic="t")
    assert gw.cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert gw.cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert gw.cosine_sim([1.0], [1.0, 2.0]) == 0.0  # length guard


# ── Task 4: page-loading + section parsing ──────────────────────────────────

CONCEPT_PAGE = """\
---
type: concept
title: Test Concept
---

### Alpha Section {#alpha-slug}

**Mechanism**: Markets rise when liquidity is ample.

More text here.

### Beta Section {#beta-slug}

**Mechanism**: Volatility spikes when uncertainty grows.
"""

MECHANISM_PAGE = """\
---
type: mechanism
title: A Simple Mechanism
---

**Mechanism**: Interest rates fall when inflation drops.
"""

THEME_INDEX_PAGE = """\
---
type: theme-index
title: Some Index
---

- [[some-page]] — summary line.
"""


def test_extract_concept_sections_returns_anchor_text_pairs():
    gw = WikiGateway(topic="t")
    sections = gw.extract_concept_sections(CONCEPT_PAGE)
    assert len(sections) == 2
    anchors = [a for a, _ in sections]
    assert "alpha-slug" in anchors
    assert "beta-slug" in anchors
    # Each text is the mechanism block content
    by_anchor = dict(sections)
    assert "liquidity" in by_anchor["alpha-slug"]
    assert "Volatility" in by_anchor["beta-slug"]


def test_file_is_atomic_mechanism_true_for_mechanism():
    gw = WikiGateway(topic="t")
    assert gw._file_is_atomic_mechanism(MECHANISM_PAGE) is True


def test_file_is_atomic_mechanism_false_for_theme_index():
    gw = WikiGateway(topic="t")
    assert gw._file_is_atomic_mechanism(THEME_INDEX_PAGE) is False


def test_file_is_concept_with_sections_true():
    gw = WikiGateway(topic="t")
    assert gw._file_is_concept_with_sections(CONCEPT_PAGE) is True


def test_file_is_concept_with_sections_false_for_mechanism():
    gw = WikiGateway(topic="t")
    assert gw._file_is_concept_with_sections(MECHANISM_PAGE) is False


def test_extract_mechanism_text_from_mechanism_page():
    gw = WikiGateway(topic="t")
    text = gw.extract_mechanism_text(MECHANISM_PAGE)
    assert "Interest rates fall" in text


def test_locate_section_slice_finds_anchor():
    gw = WikiGateway(topic="t")
    result = gw._locate_section_slice(CONCEPT_PAGE, "alpha-slug")
    assert result is not None
    start, end = result
    assert "Markets rise" in CONCEPT_PAGE[start:end]


def test_locate_section_slice_returns_none_for_missing_anchor():
    gw = WikiGateway(topic="t")
    assert gw._locate_section_slice(CONCEPT_PAGE, "nonexistent") is None


def test_candidate_text_for_standalone(tmp_path):
    gw = WikiGateway(topic="t")
    text = gw._candidate_text_for(MECHANISM_PAGE, anchor=None)
    assert "Interest rates fall" in text


def test_candidate_text_for_section(tmp_path):
    gw = WikiGateway(topic="t")
    text = gw._candidate_text_for(CONCEPT_PAGE, anchor="beta-slug")
    assert "Volatility" in text


def test_load_all_atomic_mechanisms_from_wiki_root(tmp_path):
    """load_all_atomic_mechanisms walks concepts/ and returns (path, anchor, vec) triples."""
    # Set up a minimal wiki tree
    concepts = tmp_path / "t" / "concepts"
    concepts.mkdir(parents=True)
    mech_file = concepts / "simple-mechanism.md"
    mech_file.write_text(MECHANISM_PAGE)

    gw = WikiGateway(wiki_root=tmp_path, topic="t")
    # Should not raise even if fastembed is not installed (it's in plugin deps, so likely IS installed)
    # But we test the structural walk: file is found, result is a list
    try:
        units = gw.load_all_atomic_mechanisms()
        assert isinstance(units, list)
        assert len(units) == 1
        path, anchor, vec = units[0]
        assert path == mech_file
        assert anchor is None
        assert isinstance(vec, list)
    except ImportError:
        pytest.skip("fastembed not available")


# ── Task 5: semantic dedup + overlap + anchor-disambiguation ─────────────────

def test_find_overlap_match_returns_best_page(tmp_path):
    """find_overlap_match returns the best page when cosine >= threshold."""
    try:
        from fastembed import TextEmbedding  # noqa: F401
    except ImportError:
        pytest.skip("fastembed not available")

    concepts = tmp_path / "t" / "concepts"
    concepts.mkdir(parents=True)

    mech1 = concepts / "mech-alpha.md"
    mech1.write_text(
        "---\ntype: mechanism\ntitle: Alpha\n---\n\n**Mechanism**: Interest rates rise when inflation is high.\n"
    )
    mech2 = concepts / "mech-beta.md"
    mech2.write_text(
        "---\ntype: mechanism\ntitle: Beta\n---\n\n**Mechanism**: Bears hibernate in winter months.\n"
    )

    gw = WikiGateway(wiki_root=tmp_path, topic="t")
    # Querying with text very similar to mech1 should find it
    result = gw.find_overlap_match(
        "When inflation runs high, central banks raise interest rates.",
        theme_dir=concepts,
        threshold=0.70,
    )
    assert result is not None
    path, anchor, sim = result
    assert path == mech1
    assert sim >= 0.70


def test_find_overlap_match_returns_none_below_threshold(tmp_path):
    """find_overlap_match returns None when no page meets the threshold."""
    try:
        from fastembed import TextEmbedding  # noqa: F401
    except ImportError:
        pytest.skip("fastembed not available")

    concepts = tmp_path / "t" / "concepts"
    concepts.mkdir(parents=True)
    mech = concepts / "mech.md"
    mech.write_text(
        "---\ntype: mechanism\ntitle: M\n---\n\n**Mechanism**: The moon is made of cheese.\n"
    )

    gw = WikiGateway(wiki_root=tmp_path, topic="t")
    result = gw.find_overlap_match(
        "Quantum entanglement enables faster-than-light communication.",
        theme_dir=concepts,
        threshold=0.999,  # impossibly high threshold
    )
    assert result is None


def test_disambiguate_anchor_idempotent(tmp_path):
    """_disambiguate_anchor: same claim_text → same anchor (deterministic)."""
    gw = WikiGateway(wiki_root=tmp_path, topic="t")
    # colliding_path that doesn't exist on disk → fresh anchor
    col_path = tmp_path / "base-anchor-1234.md"
    anchor1, is_idem1 = gw._disambiguate_anchor("base-anchor-1234", "some claim text", col_path)
    anchor2, is_idem2 = gw._disambiguate_anchor("base-anchor-1234", "some claim text", col_path)
    assert anchor1 == anchor2  # deterministic
    assert is_idem1 is False   # nothing on disk → fresh


# ── Task 6: reentrant fcntl write-lock ─────────────────────────────────────

def test_lock_reentrant_no_deadlock(tmp_path):
    """Re-entering the lock on the same thread is a no-op (depth counter);
    no self-deadlock on nested calls."""
    gw = WikiGateway(wiki_root=tmp_path, topic="t")
    # Outer acquire.
    with gw._wiki_write_lock():
        # Inner reentrant acquire (same thread) must not deadlock.
        with gw._wiki_write_lock():
            pass  # depth = 2, OS lock held once
        # Still inside outer — should be fine.
    # After release, re-acquire must succeed immediately.
    with gw._wiki_write_lock():
        pass


def test_lock_file_created_under_wiki_root(tmp_path):
    """The lock file is created under wiki_root."""
    gw = WikiGateway(wiki_root=tmp_path, topic="t")
    with gw._wiki_write_lock():
        assert (tmp_path / ".wiki-write.lock").exists()


def test_lock_missing_wiki_root_fails_open(tmp_path):
    """A missing lock dir fails open — no raise, proceeds unlocked."""
    missing = tmp_path / "nonexistent_dir"
    # missing_dir does not exist — the lock should create it or fail open, never raise.
    gw = WikiGateway(wiki_root=missing, topic="t")
    # Should not raise:
    with gw._wiki_write_lock():
        pass


def test_lock_no_wiki_root_fails_open():
    """wiki_root=None: lock context manager should proceed without error (fail-open)."""
    gw = WikiGateway(wiki_root=None, topic="t")
    with gw._wiki_write_lock():
        pass
