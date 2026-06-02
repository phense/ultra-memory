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
