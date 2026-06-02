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
