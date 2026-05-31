"""Tests for the read-only knowledge MCP core (spec §13): per-caller-class type
allowlist (the privilege boundary), read-path redaction, access-log audit."""
import pytest

from ultra_memory import memory_lib, knowledge_mcp


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _flat_embedder(dim=3):
    """Every text → the same unit vector, so cosine ties and type-filtering (not
    ranking) is what's under test."""
    def _embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]
    return _embed


def _save(conn, **kw):
    kw.setdefault("ts", "2026-05-01T00:00:00")
    memory_lib.save_memory(conn, **kw)


def test_subagent_cannot_recall_user_or_feedback(tmp_path):
    """The MCP is a privilege boundary: an untrusted caller (subagent) must get
    project/reference facts only — NEVER user/feedback memories. Directly the
    feedback_subagents_can_leak_secrets defense, as a TOOL constraint."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="project x", body="public project fact")
    _save(conn, id="ref", type="reference", title="ref x", body="a reference pointer")
    _save(conn, id="usr", type="user", title="peter pref", body="personal preference")
    _save(conn, id="fb", type="feedback", title="how to work", body="a feedback note")
    out = knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    ids = {r["id"] for r in out}
    assert ids == {"proj", "ref"}
    assert all(r["type"] in ("project", "reference") for r in out)
    conn.close()


def test_orchestrator_recalls_all_types(tmp_path):
    """A trusted caller (orchestrator) sees everything, incl. user/feedback."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="p", body="proj")
    _save(conn, id="usr", type="user", title="u", body="user pref")
    _save(conn, id="fb", type="feedback", title="f", body="fb note")
    out = knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="orchestrator", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    assert {r["type"] for r in out} >= {"project", "user", "feedback"}
    conn.close()


def test_unknown_caller_class_fails_closed(tmp_path):
    """An unrecognised/None caller_class is treated as untrusted (SAFE_TYPES)."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="p", body="proj")
    _save(conn, id="usr", type="user", title="u", body="user pref")
    for cc in (None, "", "weird", "agent"):
        out = knowledge_mcp.knowledge_recall(
            conn, "x", caller_class=cc, embedder=_flat_embedder(), dim=3,
            now_ts="2026-05-02T00:00:00", audit=False)
        assert {r["type"] for r in out} <= {"project", "reference"}
    conn.close()


def test_snippet_present_from_body(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="m", type="project", title="t", body="the body text here")
    out = knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="orchestrator", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    assert out[0]["snippet"] and "body text" in out[0]["snippet"]
    conn.close()


def test_read_path_redacts_secret_that_bypassed_write(tmp_path):
    """Defense-in-depth (§13): a secret that entered the DB by a path other than
    save_memory (e.g. a migration/import) is still redacted on the READ path."""
    conn = _db(tmp_path)
    _save(conn, id="m", type="project", title="t", body="placeholder")
    conn.execute("UPDATE memories SET body=? WHERE id=?",
                 ("x <private>hunter2-supersecret</private> y", "m"))
    out = knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="orchestrator", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    assert "hunter2-supersecret" not in out[0]["snippet"]
    assert "[REDACTED]" in out[0]["snippet"]
    conn.close()


def test_recall_writes_access_log_with_caller_identity(tmp_path):
    """§13: every recall logs to access_log with caller identity so exfiltration
    is auditable."""
    conn = _db(tmp_path)
    _save(conn, id="m", type="project", title="t", body="b")
    knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00", audit=True)
    rows = conn.execute(
        "SELECT target_id, context FROM access_log WHERE target_kind='memory'").fetchall()
    assert any(r["target_id"] == "m" and "subagent" in (r["context"] or "") for r in rows)
    conn.close()


def test_no_audit_when_disabled(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="m", type="project", title="t", body="b")
    knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    assert conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
    conn.close()


def test_top_k_respected_after_filtering(tmp_path):
    conn = _db(tmp_path)
    for i in range(6):
        _save(conn, id=f"p{i}", type="project", title=f"t{i}", body=f"b{i}")
    out = knowledge_mcp.knowledge_recall(
        conn, "x", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        top_k=3, now_ts="2026-05-02T00:00:00", audit=False)
    assert len(out) == 3
    conn.close()


def test_query_tool_returns_json_textcontent(tmp_path):
    """The MCP tool handler maps {query,top_k} → knowledge_recall → a single
    JSON TextContent the caller can parse."""
    import json
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="alpha", body="alpha fact")
    _save(conn, id="usr", type="user", title="secret", body="user thing")
    res = knowledge_mcp.run_query_tool(
        {"query": "alpha", "top_k": 5}, conn=conn, embedder=_flat_embedder(),
        caller_class="subagent", dim=3, now_ts="2026-05-02T00:00:00", ts=None)
    assert len(res) == 1
    assert res[0].type == "text"
    payload = json.loads(res[0].text)
    ids = {item["id"] for item in payload["results"]}
    assert "proj" in ids
    assert "usr" not in ids  # subagent type-allowlist still enforced through the tool
    conn.close()


def test_query_tool_missing_query_is_error(tmp_path):
    """A tool call without a 'query' arg returns a structured error, not a crash."""
    import json
    conn = _db(tmp_path)
    res = knowledge_mcp.run_query_tool(
        {}, conn=conn, embedder=_flat_embedder(), caller_class="subagent", dim=3)
    assert len(res) == 1
    payload = json.loads(res[0].text)
    assert "error" in payload
    conn.close()


def test_caller_class_from_env_fails_closed():
    """No explicit class → untrusted 'subagent'; the cron/subagent role marker is
    also untrusted; only an explicit ULTRA_MEMORY_CALLER_CLASS unlocks a class."""
    assert knowledge_mcp.caller_class_from_env({}) == "subagent"
    assert knowledge_mcp.caller_class_from_env(
        {"ULTRA_MEMORY_AGENT_ROLE": "cron"}) == "subagent"
    assert knowledge_mcp.caller_class_from_env(
        {"ULTRA_MEMORY_CALLER_CLASS": "orchestrator"}) == "orchestrator"
    assert knowledge_mcp.caller_class_from_env(
        {"ULTRA_MEMORY_CALLER_CLASS": "  owner "}) == "owner"


def test_db_path_from_env_required(tmp_path):
    """The DB path comes from config (ULTRA_MEMORY_DB), never cwd. Missing → raises."""
    import pytest
    p = tmp_path / "memory.db"
    assert knowledge_mcp.db_path_from_env({"ULTRA_MEMORY_DB": str(p)}) == p
    with pytest.raises(knowledge_mcp.ConfigError):
        knowledge_mcp.db_path_from_env({})
    with pytest.raises(knowledge_mcp.ConfigError):
        knowledge_mcp.db_path_from_env({"ULTRA_MEMORY_DB": "   "})


def test_knowledge_tools_declares_query_tool():
    tools = knowledge_mcp.knowledge_tools()
    assert any(t.name == "knowledge_query" for t in tools)
    qt = next(t for t in tools if t.name == "knowledge_query")
    assert "query" in qt.inputSchema.get("required", [])
