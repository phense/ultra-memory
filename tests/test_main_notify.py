# tests/test_main_notify.py
from ultra_memory.maintenance import __main__ as m
from ultra_memory.maintenance import notify
from ultra_memory.maintenance.run import PipelineResult


def test_main_fires_notify_on_errors(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(m.memory_lib, "open_memory_db", lambda p: _Dummy())
    monkeypatch.setattr(m, "run_pipeline",
                        lambda *a, **k: PipelineResult(ran=[], errors={"consolidate": "E"}))
    monkeypatch.setattr(notify, "notify_failure",
                        lambda config, *, result, ts, log: calls.setdefault("r", result))
    rc = m.main(["--project-dir", str(tmp_path)])
    assert rc == 1
    assert calls["r"].errors == {"consolidate": "E"}


def test_main_does_not_fire_when_clean(tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr(m.memory_lib, "open_memory_db", lambda p: _Dummy())
    monkeypatch.setattr(m, "run_pipeline", lambda *a, **k: PipelineResult(ran=["x"]))
    monkeypatch.setattr(notify, "notify_failure",
                        lambda *a, **k: fired.append(1))
    rc = m.main(["--project-dir", str(tmp_path)])
    assert rc == 0 and fired == []


class _Dummy:
    def close(self):
        pass
