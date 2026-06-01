"""Guard the Claude Code plugin manifest (.claude-plugin/plugin.json + marketplace.json).

Regression lock for the 2026-06-01 install failure: `/plugin install` rejected the
manifest with `userConfig.<field>.title: expected string, received undefined` — every
userConfig field MUST carry a string `title` (the label shown in the install prompt).
These checks are pure JSON-schema-shape assertions (no install, no network), so they
fail fast in CI if a userConfig field loses its title or the manifest drifts.
"""
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_JSON = _ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON = _ROOT / ".claude-plugin" / "marketplace.json"

_VALID_USERCONFIG_TYPES = {"string", "number", "boolean", "file", "directory"}


def _manifest():
    return json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))


def test_plugin_json_is_valid_json_with_required_top_level_keys():
    m = _manifest()
    for key in ("name", "version", "description"):
        assert isinstance(m.get(key), str) and m[key], f"plugin.json missing/blank '{key}'"
    assert m["name"] == "ultra-memory"


def test_every_userconfig_field_has_a_string_title():
    """The exact contract the installer enforces: each userConfig.<field>.title is a
    non-empty string. (Missing/undefined title => `/plugin install` validation error.)"""
    uc = _manifest().get("userConfig", {})
    assert uc, "plugin.json has no userConfig"
    for name, field in uc.items():
        assert isinstance(field.get("title"), str) and field["title"].strip(), (
            f"userConfig.{name}.title must be a non-empty string (the install-prompt label)"
        )


def test_userconfig_fields_have_valid_type_and_description():
    uc = _manifest()["userConfig"]
    for name, field in uc.items():
        assert field.get("type") in _VALID_USERCONFIG_TYPES, (
            f"userConfig.{name}.type={field.get('type')!r} not in {_VALID_USERCONFIG_TYPES}"
        )
        assert isinstance(field.get("description"), str) and field["description"].strip(), (
            f"userConfig.{name} needs a non-empty description"
        )


def test_data_db_path_field_is_optional_zero_config():
    """Zero-config install (2026-06-01): data_db_path is now OPTIONAL — leaving it
    empty auto-derives <project>/data/memory.db (or ~/.claude/memory.db at
    user scope), so `/plugin install` prompts nothing required. The field stays a
    declared userConfig key (with title + description), just not `required: True`."""
    uc = _manifest()["userConfig"]
    assert "data_db_path" in uc, "data_db_path must stay a declared userConfig key"
    assert uc["data_db_path"].get("required") is not True, (
        "data_db_path must NOT be required (zero-config install)"
    )


def test_no_userconfig_field_is_required_zero_config():
    """The whole point of zero-config: NO userConfig field is `required: True`, so
    the installer prompts for nothing mandatory."""
    uc = _manifest()["userConfig"]
    for name, field in uc.items():
        assert field.get("required") is not True, (
            f"userConfig.{name} must not be required (zero-config install)"
        )


def test_marketplace_json_is_valid_and_points_at_this_plugin():
    mk = json.loads(_MARKETPLACE_JSON.read_text(encoding="utf-8"))
    assert mk.get("name") == "ultra-memory"
    plugins = mk.get("plugins", [])
    assert any(p.get("name") == "ultra-memory" for p in plugins), (
        "marketplace.json must list the ultra-memory plugin"
    )
