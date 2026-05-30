from ultra_memory import memory_lib
from ultra_memory.hooks import rehydrate


def _db(tmp_path):
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    return p, conn


def test_gist_includes_pinned_rules(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="r6", type="feedback", title="Year-End Tax Fence",
                           body="Close all US options Dec 30.", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    conn.commit()
    g = rehydrate.build_gist(conn)
    assert "Year-End Tax Fence" in g


def test_gist_includes_last_session_summary(tmp_path):
    p, conn = _db(tmp_path)
    conn.execute("INSERT INTO sessions (id, started_at, summary) VALUES (?,?,?)",
                 ("s-old", "2026-05-29T10:00:00Z", "Built the engine; 102 tests green."))
    conn.commit()
    g = rehydrate.build_gist(conn)
    assert "102 tests green" in g


def test_gist_lists_open_followups(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.record_session_event(conn, session_id="s1", kind="followup",
                                    title="Wire the MCP", ts="2026-05-29T10:00:00Z")
    g = rehydrate.build_gist(conn)
    assert "Wire the MCP" in g


def test_gist_respects_budget(tmp_path):
    p, conn = _db(tmp_path)
    for i in range(200):
        memory_lib.save_memory(conn, id=f"m{i}", type="project",
                               title=f"Memory title number {i} with padding text",
                               body="x" * 200, ts="2026-05-01T00:00:00Z")
    g = rehydrate.build_gist(conn, budget_chars=2000)
    assert len(g) <= 2200  # budget + small header slack


def test_gist_empty_db_is_safe(tmp_path):
    p, conn = _db(tmp_path)
    g = rehydrate.build_gist(conn)
    assert isinstance(g, str)


def _ready_db(tmp_path):
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    memory_lib.save_memory(conn, id="r6", type="feedback", title="Tax Fence",
                           body="Close Dec 30", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('import_complete','1')")
    conn.commit()
    conn.close()
    return p


def test_run_injects_when_live(tmp_path):
    p = _ready_db(tmp_path)
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                        ts="2026-05-30T16:00:00Z")
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Tax Fence" in out["hookSpecificOutput"]["additionalContext"]


def test_run_shadow_writes_file_and_injects_nothing(tmp_path):
    p = _ready_db(tmp_path)
    shadow_out = tmp_path / "shadow" / "rehydration.md"
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=True,
                        ts="2026-05-30T16:00:00Z", shadow_out=shadow_out)
    assert out == {}  # no injection in shadow
    assert "Tax Fence" in shadow_out.read_text()


def test_run_noops_for_cron(tmp_path, monkeypatch):
    p = _ready_db(tmp_path)
    monkeypatch.setenv("ULTRA_MEMORY_AGENT_ROLE", "cron")
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                        ts="2026-05-30T16:00:00Z")
    assert out == {}


def test_run_noops_when_db_not_ready(tmp_path):
    out = rehydrate.run({"source": "startup"}, db_path=tmp_path / "absent.db",
                        shadow=False, ts="2026-05-30T16:00:00Z")
    assert out == {}


def test_budget_from_env_default(monkeypatch):
    monkeypatch.delenv("ULTRA_MEMORY_REHYDRATE_BUDGET", raising=False)
    assert rehydrate._budget_from_env() == 2000


def test_budget_from_env_override(monkeypatch):
    monkeypatch.setenv("ULTRA_MEMORY_REHYDRATE_BUDGET", "4000")
    assert rehydrate._budget_from_env() == 4000


def test_budget_from_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ULTRA_MEMORY_REHYDRATE_BUDGET", "not-a-number")
    assert rehydrate._budget_from_env() == 2000
