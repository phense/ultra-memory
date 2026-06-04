import pathlib, re

CMD = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "um-hook.cmd"


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
