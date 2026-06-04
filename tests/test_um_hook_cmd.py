import pathlib, re

CMD = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "um-hook.cmd"


def test_beats_verb_dispatches_maintenance_module():
    text = CMD.read_text()
    assert "beats)" in text, "no beats branch in um-hook.cmd"
    # the beats branch runs the heavy-beat entrypoint, fail-open (|| exit 0)
    assert "ultra_memory.maintenance" in text
    assert re.search(r"beats\).*\n.*ultra_memory\.maintenance", text, re.S)
