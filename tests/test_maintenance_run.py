"""Tests for ultra_memory.maintenance.run — the Tier-2 orchestrator skeleton."""
from ultra_memory import memory_lib
from ultra_memory.maintenance import run as mr
from ultra_memory.maintenance.config import MaintenanceConfig


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "m.db"))


def _cfg(tmp_path, **over):
    base = dict(project_dir=tmp_path, db_path=tmp_path / "m.db",
                export_dir=tmp_path / "exp")
    base.update(over)
    return MaintenanceConfig(**base)


TS = "2026-06-01T00:00:00Z"


def test_runs_due_enabled_registered_beats(tmp_path):
    conn = _conn(tmp_path)
    calls = []
    registry = {b: (lambda c, cfg, ts, env, _b=b: (calls.append(_b), _b)[1])
                for b in ("consolidate", "aggressive", "synthesize")}
    res = mr.run_pipeline(conn, _cfg(tmp_path), registry=registry, ts=TS)
    assert res.ran == ["consolidate", "aggressive", "synthesize"]
    assert calls == ["consolidate", "aggressive", "synthesize"]   # order preserved
    assert res.errors == {}


def test_unregistered_beat_skipped(tmp_path):
    conn = _conn(tmp_path)
    res = mr.run_pipeline(conn, _cfg(tmp_path),
                          registry={"consolidate": lambda c, cfg, ts, env: "ok"}, ts=TS)
    assert res.ran == ["consolidate"]
    assert res.skipped.get("aggressive") == "unregistered"
    assert res.skipped.get("synthesize") == "unregistered"


def test_disabled_beat_skipped(tmp_path):
    conn = _conn(tmp_path)
    cfg = _cfg(tmp_path, beats={"consolidate": False, "aggressive": True, "synthesize": True})
    registry = {b: lambda c, cfg, ts, env: "ok" for b in mr.BEAT_ORDER}
    res = mr.run_pipeline(conn, cfg, registry=registry, ts=TS)
    assert res.skipped.get("consolidate") == "disabled"
    assert "aggressive" in res.ran


def test_throttle_clock(tmp_path):
    conn = _conn(tmp_path)
    registry = {"consolidate": lambda c, cfg, ts, env: "ok"}
    cfg = _cfg(tmp_path, cadence_hours={"consolidate": 168})
    # first run: due (never ran) → runs + stamps the clock
    r1 = mr.run_pipeline(conn, cfg, registry=registry, ts="2026-06-01T00:00:00Z")
    assert r1.ran == ["consolidate"]
    # 1h later: NOT due (cadence 168h) → skipped
    r2 = mr.run_pipeline(conn, cfg, registry=registry, ts="2026-06-01T01:00:00Z")
    assert r2.skipped.get("consolidate") == "not-due"
    # 200h later: due again
    r3 = mr.run_pipeline(conn, cfg, registry=registry, ts="2026-06-09T08:00:00Z")
    assert r3.ran == ["consolidate"]
    # force ignores the clock
    r4 = mr.run_pipeline(conn, cfg, registry=registry, ts="2026-06-09T09:00:00Z", force=True)
    assert r4.ran == ["consolidate"]


def test_fail_open_per_beat(tmp_path):
    conn = _conn(tmp_path)

    def boom(c, cfg, ts, env):
        raise RuntimeError("beat exploded")

    registry = {"consolidate": boom,
                "aggressive": lambda c, cfg, ts, env: "ok",
                "synthesize": lambda c, cfg, ts, env: "ok"}
    res = mr.run_pipeline(conn, _cfg(tmp_path), registry=registry, ts=TS)
    assert "consolidate" in res.errors and "beat exploded" in res.errors["consolidate"]
    assert "aggressive" in res.ran and "synthesize" in res.ran   # others still run
    # a failed beat does NOT stamp its clock → still due next time
    assert mr.is_due(conn, "consolidate", 168, "2026-06-01T02:00:00Z") is True
