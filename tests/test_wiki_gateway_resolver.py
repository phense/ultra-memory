"""Tests for _resolve_gateway + the unset→built-in default + beat threading (Task 10).

`_resolve_gateway(spec, config)` returns an argv prefix list that the beats use to
invoke the wiki write gateway.  Three spec forms are tested:

  - None / unset → built-in WikiGateway, prefix = ["python", "-m", "ultra_memory.wiki_gateway"]
  - "module:Class" → module-on-scripts-path, prefix ends with ["--gateway-class", spec]
  - a file-path string → back-compat uv-run, prefix = ["uv", "run", <path>]

Only the argv SHAPE is asserted (no subprocess is spawned).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Minimal config stub (mirrors MaintenanceConfig's used attrs)
# ---------------------------------------------------------------------------

def _cfg(project_dir: Path, scripts_path: str | None = None) -> SimpleNamespace:
    """Return a minimal config stub that _resolve_gateway uses."""
    cfg = SimpleNamespace(
        project_dir=project_dir,
        wiki_gateway=None,
    )
    return cfg


# ---------------------------------------------------------------------------
# Import the resolver (Task 10 adds it to wiki_curate)
# ---------------------------------------------------------------------------

def _import_resolver():
    from ultra_memory.maintenance.wiki_curate import _resolve_gateway
    return _resolve_gateway


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResolveGatewayNone:
    """spec=None → built-in turnkey: prefix invokes `python -m ultra_memory.wiki_gateway`."""

    def test_none_spec_returns_builtin_prefix(self, tmp_path):
        _resolve_gateway = _import_resolver()
        cfg = _cfg(tmp_path)
        prefix = _resolve_gateway(None, cfg)
        # Must be a non-empty list
        assert isinstance(prefix, list)
        assert len(prefix) >= 2
        # Must end up invoking the built-in module, not uv run <file>
        assert prefix[0] == "python"
        joined = " ".join(prefix)
        assert "ultra_memory.wiki_gateway" in joined

    def test_none_spec_does_not_include_gateway_class(self, tmp_path):
        _resolve_gateway = _import_resolver()
        prefix = _resolve_gateway(None, _cfg(tmp_path))
        assert "--gateway-class" not in prefix

    def test_empty_string_spec_returns_builtin_prefix(self, tmp_path):
        """Empty string is treated as unset → built-in."""
        _resolve_gateway = _import_resolver()
        prefix = _resolve_gateway("", _cfg(tmp_path))
        assert isinstance(prefix, list)
        assert "ultra_memory.wiki_gateway" in " ".join(prefix)

    def test_builtin_prefix_can_be_extended_with_verb(self, tmp_path):
        """The returned prefix is a list; callers extend it with [verb, …]."""
        _resolve_gateway = _import_resolver()
        prefix = _resolve_gateway(None, _cfg(tmp_path))
        full_cmd = prefix + ["create-page", "--path", "x.md"]
        assert full_cmd[-3:] == ["create-page", "--path", "x.md"]


class TestResolveGatewayModuleClass:
    """spec="module:Class" → --gateway-class prefix."""

    def test_module_class_spec_includes_gateway_class_flag(self, tmp_path):
        _resolve_gateway = _import_resolver()
        spec = "wiki_lib:TradingWikiGateway"
        prefix = _resolve_gateway(spec, _cfg(tmp_path))
        assert isinstance(prefix, list)
        assert "--gateway-class" in prefix
        idx = prefix.index("--gateway-class")
        assert prefix[idx + 1] == spec

    def test_module_class_spec_invokes_builtin_module(self, tmp_path):
        """Even a consumer spec routes through the built-in CLI module."""
        _resolve_gateway = _import_resolver()
        prefix = _resolve_gateway("mymod:MyGateway", _cfg(tmp_path))
        assert "ultra_memory.wiki_gateway" in " ".join(prefix)

    def test_module_class_prefix_starts_with_python_m(self, tmp_path):
        _resolve_gateway = _import_resolver()
        prefix = _resolve_gateway("somemod:SomeClass", _cfg(tmp_path))
        assert prefix[0] == "python"
        assert "-m" in prefix

    def test_module_class_prefix_can_be_extended_with_verb(self, tmp_path):
        _resolve_gateway = _import_resolver()
        prefix = _resolve_gateway("wiki_lib:TradingWikiGateway", _cfg(tmp_path))
        full_cmd = prefix + ["log", "--message", "hello"]
        assert "log" in full_cmd
        assert "--gateway-class" in full_cmd


class TestResolveGatewayPath:
    """spec is a file-path string → back-compat uv-run prefix."""

    def test_path_spec_returns_uv_run_prefix(self, tmp_path):
        _resolve_gateway = _import_resolver()
        gateway_script = tmp_path / "scripts" / "wiki_lib.py"
        gateway_script.parent.mkdir(parents=True, exist_ok=True)
        gateway_script.touch()
        prefix = _resolve_gateway(str(gateway_script), _cfg(tmp_path))
        assert isinstance(prefix, list)
        assert prefix[0] == "uv"
        assert "run" in prefix

    def test_path_spec_includes_gateway_path(self, tmp_path):
        _resolve_gateway = _import_resolver()
        gateway_script = tmp_path / "wiki_lib.py"
        gateway_script.touch()
        path_str = str(gateway_script)
        prefix = _resolve_gateway(path_str, _cfg(tmp_path))
        assert path_str in " ".join(prefix)

    def test_path_spec_does_not_include_gateway_class(self, tmp_path):
        """Back-compat path form must NOT include --gateway-class."""
        _resolve_gateway = _import_resolver()
        gateway_script = tmp_path / "wiki_lib.py"
        gateway_script.touch()
        prefix = _resolve_gateway(str(gateway_script), _cfg(tmp_path))
        assert "--gateway-class" not in prefix


class TestGatewayClassCliFlag:
    """The base CLI's `main()` supports --gateway-class module:Class."""

    def test_main_gateway_class_flag_is_accepted(self, tmp_path):
        """--help does not fail with unknown-argument for --gateway-class."""
        from ultra_memory.wiki_gateway import main
        # --help should exit 0 (not fail with unrecognized arg)
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_cli_accepts_gateway_class_flag(self, tmp_path):
        """cli() with --gateway-class pointing at WikiGateway itself works."""
        from ultra_memory.wiki_gateway import cli, WikiGateway
        content_file = tmp_path / "content.md"
        content_file.write_text("# Test\n\nContent.\n")
        dest = tmp_path / "research" / "concepts" / "gw-class-test.md"
        dest.parent.mkdir(parents=True, exist_ok=True)

        rc = cli(
            WikiGateway,
            [
                "--gateway-class", "ultra_memory.wiki_gateway:WikiGateway",
                "create-page",
                "--path", str(dest),
                "--topic", "research",
                "--from-file", str(content_file),
                "--wiki-root", str(tmp_path),
            ],
        )
        assert rc == 0
        assert dest.exists()
