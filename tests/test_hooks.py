# tests/test_hooks.py
from pathlib import Path
from types import SimpleNamespace

from ultra_memory.maintenance._hooks import resolve_hook


def _cfg(tmp_path):
    return SimpleNamespace(project_dir=tmp_path)


def test_resolve_hook_imports_module_function(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "myhook.py").write_text("def go(x):\n    return x + 1\n")
    fn = resolve_hook(_cfg(tmp_path), "myhook:go", "notifier")
    assert fn is not None and fn(1) == 2


def test_resolve_hook_empty_or_malformed_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert resolve_hook(cfg, "", "x") is None
    assert resolve_hook(cfg, "nocolon", "x") is None
    assert resolve_hook(cfg, "mod:", "x") is None
    assert resolve_hook(cfg, ":fn", "x") is None


def test_resolve_hook_unimportable_returns_none(tmp_path):
    assert resolve_hook(_cfg(tmp_path), "does_not_exist_mod:go", "x") is None


def test_wiki_curate_reexport_is_the_same_callable():
    from ultra_memory.maintenance import wiki_curate, _hooks
    assert wiki_curate._resolve_hook is _hooks.resolve_hook
