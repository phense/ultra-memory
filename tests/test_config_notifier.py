# tests/test_config_notifier.py
from ultra_memory.maintenance.config import load_config


def test_notifier_defaults_empty(tmp_path):
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.notifier == ""


def test_notifier_from_toml(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nnotifier = "mymod:notify"\n')
    cfg = load_config(project_dir=tmp_path, env={})
    assert cfg.notifier == "mymod:notify"


def test_notifier_env_overrides_toml(tmp_path):
    (tmp_path / ".ultra-memory").mkdir()
    (tmp_path / ".ultra-memory" / "config.toml").write_text(
        '[maintenance]\nnotifier = "file:fn"\n')
    cfg = load_config(project_dir=tmp_path, env={"ULTRA_MEMORY_NOTIFIER": "env:fn"})
    assert cfg.notifier == "env:fn"
