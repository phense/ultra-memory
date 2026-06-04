import json, pathlib

HJ = pathlib.Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"


def test_beats_hook_is_wired_async_on_sessionstart():
    data = json.loads(HJ.read_text())
    starts = data["hooks"]["SessionStart"]
    beats = [e for e in starts
             if any("um-hook.cmd beats" in h["command"] for h in e["hooks"])]
    assert beats, "no SessionStart entry runs 'um-hook.cmd beats'"
    assert all(e.get("async") is True for e in beats), "beats hook must be async"
