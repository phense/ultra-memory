import os
from ultra_memory.hooks import common


def test_optout_when_role_env_set(monkeypatch):
    monkeypatch.setenv("ULTRA_MEMORY_AGENT_ROLE", "cron")
    assert common.agent_role_optout({"source": "startup"}) is True


def test_optout_when_role_env_subagent(monkeypatch):
    monkeypatch.setenv("ULTRA_MEMORY_AGENT_ROLE", "subagent")
    assert common.agent_role_optout(None) is True


def test_in_role_for_interactive_startup(monkeypatch):
    monkeypatch.delenv("ULTRA_MEMORY_AGENT_ROLE", raising=False)
    assert common.agent_role_optout({"source": "startup"}) is False
    assert common.agent_role_optout({"source": "resume"}) is False
    assert common.agent_role_optout({"source": "clear"}) is False


def test_optout_for_noninteractive_source(monkeypatch):
    monkeypatch.delenv("ULTRA_MEMORY_AGENT_ROLE", raising=False)
    # an --agent / -p run reports a non-interactive source
    assert common.agent_role_optout({"source": "agent"}) is True


def test_db_ready_false_when_missing(tmp_path):
    assert common.db_ready(tmp_path / "nope.db") is False


def test_db_ready_false_when_import_incomplete(tmp_path):
    from ultra_memory import memory_lib
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    conn.close()
    # schema present but no import_complete row → not ready
    assert common.db_ready(p) is False


def test_db_ready_true_when_import_complete(tmp_path):
    from ultra_memory import memory_lib
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('import_complete', '1')")
    conn.commit()
    conn.close()
    assert common.db_ready(p) is True


def test_session_id_prefers_payload():
    sid = common.session_id_of({"session_id": "uuid-abc"}, "/x/uuid-zzz.jsonl")
    assert sid == "uuid-abc"


def test_session_id_falls_back_to_transcript_stem():
    sid = common.session_id_of({}, "/x/uuid-zzz.jsonl")
    assert sid == "uuid-zzz"
