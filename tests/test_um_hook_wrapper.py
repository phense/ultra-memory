import os
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "hooks" / "um-hook.cmd"


def _run(args, env, stdin_text="{}"):
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        input=stdin_text, capture_output=True, text=True, env=env,
    )


def test_fail_open_when_venv_absent(tmp_path):
    """No venv under CLAUDE_PLUGIN_DATA → exit 0, no stdout (never blocks a session)."""
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "no-such-data")
    env["CLAUDE_PLUGIN_OPTION_DATA_DB_PATH"] = str(tmp_path / "m.db")
    r = _run(["rehydrate"], env)
    assert r.returncode == 0
    assert r.stdout == ""


def test_unknown_verb_exits_zero(tmp_path):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    r = _run(["bogus"], env)
    assert r.returncode == 0


def test_resolves_db_path_into_env(tmp_path):
    """The wrapper must export ULTRA_MEMORY_DB from CLAUDE_PLUGIN_OPTION_DATA_DB_PATH.
    We assert via a stand-in 'python' on PATH that echoes the env it received."""
    data = tmp_path / "data"
    venvbin = data / "venv" / "bin"
    venvbin.mkdir(parents=True)
    fake_py = venvbin / "python"
    fake_py.write_text(
        "#!/usr/bin/env bash\n"
        'echo "DB=$ULTRA_MEMORY_DB SHADOW=$ULTRA_MEMORY_SHADOW '
        'CALLER=$ULTRA_MEMORY_CALLER_CLASS BUDGET=$ULTRA_MEMORY_REHYDRATE_BUDGET"\n'
    )
    fake_py.chmod(0o755)
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data)
    env["CLAUDE_PLUGIN_OPTION_DATA_DB_PATH"] = "/tmp/the-consumer.db"
    env["CLAUDE_PLUGIN_OPTION_CALLER_CLASS"] = "subagent"
    r = _run(["rehydrate"], env)
    assert r.returncode == 0
    assert "DB=/tmp/the-consumer.db" in r.stdout
    assert "SHADOW=0" in r.stdout           # live injection, not the engine's shadow default
    assert "CALLER=subagent" in r.stdout
    assert "BUDGET=2000" in r.stdout        # default when OPTION unset
