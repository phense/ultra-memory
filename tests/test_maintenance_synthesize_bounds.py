"""Tests for synthesize_bounds.py — SP-10 Stage 5a gate + 'skills' cap."""
import sys
from pathlib import Path


from ultra_memory.maintenance import synthesize_bounds as sb  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


def _clear(monkeypatch):
    monkeypatch.delenv("SP10_SYNTHESIS_DISABLE", raising=False)
    monkeypatch.delenv("SP10_SYNTHESIS_DRYRUN", raising=False)


def test_gate_live(monkeypatch):
    _clear(monkeypatch)
    g = sb.run_gate()
    assert g.mode == "live" and g.may_apply is True


def test_gate_dryrun(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SP10_SYNTHESIS_DRYRUN", "1")
    g = sb.run_gate()
    assert g.mode == "dryrun" and g.may_apply is False


def test_gate_disabled_outranks_dryrun(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SP10_SYNTHESIS_DRYRUN", "1")
    monkeypatch.setenv("SP10_SYNTHESIS_DISABLE", "1")
    g = sb.run_gate()
    assert g.mode == "noop" and g.may_apply is False


def test_cap_admits_one():
    out = sb.enforce_skill_cap({"skills": ["s1"]})
    assert out["admitted"] == ["s1"] and out["bound"] is None


def test_cap_halts_on_exceed():
    out = sb.enforce_skill_cap({"skills": ["s1", "s2"]})
    assert out["admitted"] == [] and out["bound"]["scope"] == "run"


def test_period_cap(tmp_path):
    conn = memory_lib.open_memory_db(str(tmp_path / "m.db"))
    sb.commit_period_usage(conn, period="2026-06", applied_count=1)
    out = sb.enforce_skill_cap({"skills": ["s1"]}, conn=conn, period="2026-06",
                               period_cap=1)
    assert out["admitted"] == [] and out["bound"]["scope"] == "period"
