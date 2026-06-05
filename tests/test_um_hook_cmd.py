import json, pathlib, re

CMD = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "um-hook.cmd"
PLUGIN_JSON = pathlib.Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"


def _userconfig_keys():
    return set(json.loads(PLUGIN_JSON.read_text()).get("userConfig", {}))


def _wrapper_option_reads():
    # every CLAUDE_PLUGIN_OPTION_<NAME> the wrapper reads → its lowercase userConfig key
    return {m.lower() for m in re.findall(r"CLAUDE_PLUGIN_OPTION_([A-Z0-9_]+)", CMD.read_text())}


def test_beats_verb_dispatches_maintenance_module():
    text = CMD.read_text()
    assert "beats)" in text, "no beats branch in um-hook.cmd"
    # the beats branch runs the heavy-beat entrypoint, fail-open (|| exit 0)
    assert "ultra_memory.maintenance" in text
    assert re.search(r"beats\).*\n.*ultra_memory\.maintenance", text, re.S)


def test_optout_userconfig_bridged_to_engine_env():
    text = CMD.read_text()
    # opt-OUT mapping: a CLAUDE_PLUGIN_OPTION_* value of 'off'/'0' must reach the engine var.
    assert "CLAUDE_PLUGIN_OPTION_SESSION_INGEST_ENABLE" in text
    assert "SESSION_INGEST_ENABLE=" in text
    assert "CLAUDE_PLUGIN_OPTION_ATTRIBUTION_ENABLE" in text
    assert "SP8_ATTRIBUTION_ENABLE=" in text
    assert "CLAUDE_PLUGIN_OPTION_AGGRESSIVE_ENABLE" in text     # mapped to SP7_AGGRESSIVE_DISABLE
    assert "CLAUDE_PLUGIN_OPTION_SYNTHESIZE_ENABLE" in text     # mapped to SP10_SYNTHESIS_DISABLE


def test_every_wrapper_option_has_a_userconfig_key():
    """Regression lock (D5-1/D5-3): a CLAUDE_PLUGIN_OPTION_* the wrapper reads but
    plugin.json never declares is a DEAD toggle — the harness only injects declared
    keys, so the /plugin UI can never drive it. This guards the whole bridge; it is
    exactly the reconciliation that would have caught the undeclared GRADUATE_ENABLE."""
    dead = _wrapper_option_reads() - _userconfig_keys()
    assert not dead, f"wrapper reads undeclared userConfig option(s): {sorted(dead)}"


def test_every_enable_toggle_is_bridged_to_the_wrapper():
    """The reverse guard: a declared `*_enable` userConfig toggle that um-hook.cmd never
    reads would silently never reach the SessionStart/Stop hooks."""
    enable_keys = {k for k in _userconfig_keys() if k.endswith("_enable")}
    unbridged = enable_keys - _wrapper_option_reads()
    assert not unbridged, f"userConfig enable toggle(s) not bridged in um-hook.cmd: {sorted(unbridged)}"
