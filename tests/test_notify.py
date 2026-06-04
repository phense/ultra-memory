# tests/test_notify.py
from pathlib import Path
from types import SimpleNamespace

from ultra_memory.maintenance import notify
from ultra_memory.maintenance.run import PipelineResult


def _cfg(tmp_path, spec=""):
    return SimpleNamespace(project_dir=tmp_path, notifier=spec)


def test_build_event_summarizes_errors(tmp_path):
    res = PipelineResult(ran=["consolidate"], skipped={"aggressive": "not-due"},
                         errors={"consolidate": "RuntimeError('boom')"})
    ev = notify.build_event(_cfg(tmp_path), res, "2026-06-04T00:00:00Z")
    assert ev.kind == "maintenance_failure"
    assert "consolidate" in ev.subject
    assert "boom" in ev.body
    assert ev.errors == {"consolidate": "RuntimeError('boom')"}
    assert ev.run_ts == "2026-06-04T00:00:00Z"


def test_noop_notifier_logs_and_never_raises(tmp_path, capsys):
    res = PipelineResult(errors={"x": "y"})
    ev = notify.build_event(_cfg(tmp_path), res, "T")
    notify._noop_notifier(ev)  # must not raise
    assert "no notifier configured" in capsys.readouterr().err


def test_resolve_notifier_defaults_to_noop(tmp_path):
    assert notify.resolve_notifier(_cfg(tmp_path)) is notify._noop_notifier


def test_notify_failure_fires_configured_hook(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sentinel = tmp_path / "fired.txt"
    (scripts / "notify_myhook.py").write_text(
        "def go(ev):\n"
        f"    open(r'{sentinel}', 'w').write(ev.subject)\n")
    res = PipelineResult(errors={"consolidate": "E"})
    notify.notify_failure(_cfg(tmp_path, "notify_myhook:go"), result=res, ts="T",
                          log=lambda m: None)
    assert sentinel.exists() and "consolidate" in sentinel.read_text()


def test_notify_failure_noop_when_no_errors(tmp_path):
    calls = []
    cfg = _cfg(tmp_path, "x:y")
    # monkeypatch-free: empty errors must short-circuit before resolving
    notify.notify_failure(cfg, result=PipelineResult(errors={}), ts="T",
                          log=lambda m: calls.append(m))
    assert calls == []


def test_notify_failure_swallows_hook_exception(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "notify_boomhook.py").write_text("def go(ev):\n    raise ValueError('nope')\n")
    logged = []
    res = PipelineResult(errors={"b": "E"})
    # must NOT raise
    notify.notify_failure(_cfg(tmp_path, "notify_boomhook:go"), result=res, ts="T",
                          log=lambda m: logged.append(m))
    assert any("failed" in m for m in logged)
