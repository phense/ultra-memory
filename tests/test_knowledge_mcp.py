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


def test_knowledge_recall_threads_session_id_from_env(tmp_path, monkeypatch):
    """SP-8 substrate: the knowledge MCP recall site stamps the env session id onto
    each audited access_log row; unset env -> NULL session_id (graceful, no error)."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="p", body="proj fact")
    monkeypatch.setenv("ULTRA_MEMORY_SESSION_ID", "K-SESS")
    out = knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00")
    assert out
    rows = conn.execute("SELECT session_id FROM access_log").fetchall()
    assert rows and all(r["session_id"] == "K-SESS" for r in rows)
    # unset -> NULL, still no error. Clear BOTH the explicit override and the ambient
    # CLAUDE_CODE_SESSION_ID (the SP-8 A3 fallback; present because the suite runs under
    # Claude Code) so this exercises the truly-unset NULL path.
    monkeypatch.delenv("ULTRA_MEMORY_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    conn.execute("DELETE FROM access_log")
    knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:01:00", ts="2026-05-02T00:01:00")
    rows2 = conn.execute("SELECT session_id FROM access_log").fetchall()
    assert rows2 and all(r["session_id"] is None for r in rows2)
    conn.close()


def test_session_id_from_env_mirrors_caller_class_pattern():
    """SP-8 substrate: session_id_from_env is the generic env-read mirror of
    caller_class_from_env — stripped ULTRA_MEMORY_SESSION_ID or None. Exposed from
    knowledge_mcp next to caller_class_from_env (re-export of memory_lib's canonical)."""
    assert knowledge_mcp.session_id_from_env({}) is None
    assert knowledge_mcp.session_id_from_env(
        {"ULTRA_MEMORY_SESSION_ID": ""}) is None
    assert knowledge_mcp.session_id_from_env(
        {"ULTRA_MEMORY_SESSION_ID": " S-1 "}) == "S-1"


# ---------------------------------------------------------------------------
# SP-8 bughunt FIX 3 — the type wall must extend to the `links` of a returned row.
# A subagent recalling an ALLOWED project/reference memory that carries an edge to
# a FORBIDDEN user/feedback memory must NOT receive that forbidden memory's id/type
# via the `links` field (a sideband leak past the primary-row type wall).
# ---------------------------------------------------------------------------

def _link(conn, *, src_id, predicate, dst_id, dst_type, ts="2026-05-01T00:00:00"):
    memory_lib.record_link(
        conn, src_kind="memory", src_id=src_id, predicate=predicate,
        dst_kind="memory", dst_id=dst_id, dst_type=dst_type, ts=ts)


def test_fix3_subagent_recall_drops_links_to_forbidden_type(tmp_path):
    """A subagent recalling the project memory must NOT see, via `links`, the id/type
    of a feedback memory the project memory links to."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="proj x", body="public project fact")
    _save(conn, id="fb", type="feedback", title="how to work", body="a feedback note")
    _link(conn, src_id="proj", predicate="references", dst_id="fb", dst_type="feedback")
    out = knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    proj = [r for r in out if r["id"] == "proj"]
    assert proj, "subagent must still recall the allowed project memory"
    dst_ids = {l["dst_id"] for l in proj[0]["links"]}
    dst_types = {l["dst_type"] for l in proj[0]["links"]}
    assert "fb" not in dst_ids, "forbidden feedback id leaked via links"
    assert "feedback" not in dst_types, "forbidden feedback type leaked via links"
    conn.close()


def test_fix3_subagent_recall_keeps_links_to_allowed_type(tmp_path):
    """An edge to an ALLOWED type (project→reference) is retained for the subagent —
    no over-filtering."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="proj x", body="public project fact")
    _save(conn, id="ref", type="reference", title="ref x", body="a reference pointer")
    _link(conn, src_id="proj", predicate="references", dst_id="ref", dst_type="reference")
    out = knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    proj = [r for r in out if r["id"] == "proj"][0]
    assert "ref" in {l["dst_id"] for l in proj["links"]}
    conn.close()


def test_fix3_orchestrator_recall_keeps_links_to_all_types(tmp_path):
    """The full-recall orchestrator caller is NOT subject to the links filter — it
    still sees the edge to the feedback memory (no over-filtering of trusted)."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="proj x", body="public project fact")
    _save(conn, id="fb", type="feedback", title="how to work", body="a feedback note")
    _link(conn, src_id="proj", predicate="references", dst_id="fb", dst_type="feedback")
    out = knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="orchestrator", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    proj = [r for r in out if r["id"] == "proj"][0]
    assert "fb" in {l["dst_id"] for l in proj["links"]}
    conn.close()


def test_fix3_subagent_drops_link_when_endpoint_type_unresolvable(tmp_path):
    """Fail-closed: if the edge's endpoint cannot be resolved to a known allowed
    type (e.g. a dangling dst id), the edge is DROPPED for the subagent."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="proj x", body="public project fact")
    # An edge to a non-existent memory id (no row to resolve the type from).
    _link(conn, src_id="proj", predicate="references", dst_id="ghost", dst_type=None)
    out = knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", audit=False)
    proj = [r for r in out if r["id"] == "proj"][0]
    assert "ghost" not in {l["dst_id"] for l in proj["links"]}
    conn.close()


# ---------------------------------------------------------------------------
# R3 bughunt FIX 5 — the links wall must FAIL-CLOSED on a None/missing dst_kind.
# `_endpoint_allowed` returned True immediately for any kind != 'memory', so an edge
# with dst_kind=None (or a missing dst_kind key → .get() is None) was treated as a
# safe knowledge endpoint and KEPT — leaking its dst_id to a type-scoped subagent
# even if that dst_id is actually a user/feedback memory. Only an EXPLICIT 'knowledge'
# kind may bypass the type wall; None/missing/unknown → treated as a memory endpoint
# (re-read, fail-closed on an unresolvable id).
# ---------------------------------------------------------------------------

def test_fix5_links_wall_drops_edge_with_none_dst_kind(tmp_path):
    """A subagent recall over a returned memory whose `links` contains an edge with
    dst_kind=None pointing at a real user/feedback memory id → the edge is DROPPED,
    not leaked. (Pre-fix the None kind bypassed the wall as a 'non-memory' endpoint.)"""
    conn = _db(tmp_path)
    _save(conn, id="fb", type="feedback", title="how to work", body="a feedback note")
    # An edge whose dst_kind is None but dst_id points at the forbidden feedback row.
    links = [{"predicate": "references", "src_type": "project",
              "dst_kind": None, "dst_id": "fb", "dst_type": None}]
    kept = knowledge_mcp.filter_links_for_caller(
        conn, links, caller_class="subagent")
    assert kept == [], "None-dst_kind edge to a feedback memory leaked to a subagent"
    conn.close()


def test_fix5_links_wall_drops_edge_with_missing_dst_kind_key(tmp_path):
    """A missing dst_kind KEY (.get('dst_kind') is None) is treated as a memory
    endpoint and re-read — an edge to a forbidden user memory is DROPPED."""
    conn = _db(tmp_path)
    _save(conn, id="usr", type="user", title="peter pref", body="a personal pref")
    links = [{"predicate": "references", "dst_id": "usr"}]  # NO dst_kind key
    kept = knowledge_mcp.filter_links_for_caller(
        conn, links, caller_class="subagent")
    assert kept == [], "missing-dst_kind edge to a user memory leaked to a subagent"
    conn.close()


def test_fix5_links_wall_none_dst_kind_to_allowed_memory_kept(tmp_path):
    """No over-filtering: a None-dst_kind edge whose dst_id resolves to an ALLOWED
    type (reference) is treated as a memory endpoint, re-read, and KEPT."""
    conn = _db(tmp_path)
    _save(conn, id="ref", type="reference", title="ref x", body="a reference pointer")
    links = [{"predicate": "references", "dst_kind": None, "dst_id": "ref",
              "dst_type": None}]
    kept = knowledge_mcp.filter_links_for_caller(
        conn, links, caller_class="subagent")
    assert {l["dst_id"] for l in kept} == {"ref"}
    conn.close()


def test_fix5_links_wall_keeps_explicit_knowledge_edge(tmp_path):
    """An edge with an EXPLICIT dst_kind='knowledge' still bypasses the type wall and
    is KEPT (knowledge wiki pages are not the secret-bearing user/feedback rows) —
    the fix must not over-filter legitimate knowledge endpoints."""
    conn = _db(tmp_path)
    links = [{"predicate": "grounded_in", "dst_kind": "knowledge",
              "dst_id": "some-wiki-slug", "dst_type": None}]
    kept = knowledge_mcp.filter_links_for_caller(
        conn, links, caller_class="subagent")
    assert {l["dst_id"] for l in kept} == {"some-wiki-slug"}, "knowledge edge over-filtered"
    conn.close()


# ---------------------------------------------------------------------------
# SP-8 bughunt FIX 3 — knowledge_recall's per-hit record_access audit-write must be
# best-effort. `record_access` goes through `_write_txn` which can raise (e.g.
# WriteSpooled under write contention) — that must NOT turn a SUCCEEDED read into a
# recall error on the read-only MCP. Mirrors unified_query._audit_hits' try/except.
# ---------------------------------------------------------------------------

def test_fix3_recall_survives_audit_write_failure(tmp_path, monkeypatch):
    """A raising record_access (audit-write hiccup / WriteSpooled) must NOT propagate:
    the read result still returns the hits, no exception, no {'error': ...}."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="p", body="proj fact")

    def _boom(*a, **kw):
        raise RuntimeError("write spooled / contention")

    monkeypatch.setattr(memory_lib, "record_access", _boom)
    out = knowledge_mcp.knowledge_recall(
        conn, "proj", caller_class="subagent", embedder=_flat_embedder(), dim=3,
        now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00", audit=True)
    assert {r["id"] for r in out} == {"proj"}  # the read survives the audit failure
    conn.close()


def test_db_path_from_env_derives_default(tmp_path):
    """Zero-config: the DB path DERIVES the FIXED global default instead of raising —
    explicit override wins, else ~/.ultra-memory/memory.db. NEVER cwd, and (since
    2026-06-01) NEVER project-local: CLAUDE_PROJECT_DIR is no longer consulted; the
    fabric always lives at one fixed user-path."""
    from pathlib import Path
    p = tmp_path / "memory.db"
    GLOBAL = Path.home() / ".ultra-memory" / "memory.db"
    # (i) explicit ULTRA_MEMORY_DB wins outright.
    assert knowledge_mcp.db_path_from_env({"ULTRA_MEMORY_DB": str(p)}) == p
    # (ii) unset → the fixed global user-path; CLAUDE_PROJECT_DIR is IGNORED now.
    assert knowledge_mcp.db_path_from_env({"CLAUDE_PROJECT_DIR": "/x"}) == GLOBAL
    # (iii) blank ULTRA_MEMORY_DB is treated as unset → global.
    assert knowledge_mcp.db_path_from_env({"ULTRA_MEMORY_DB": "   "}) == GLOBAL
    # (iv) empty env → global.
    assert knowledge_mcp.db_path_from_env({}) == GLOBAL
    # The safety property: NO resolution branch returns a cwd-relative path.
    for env in ({}, {"CLAUDE_PROJECT_DIR": "/x"}, {"ULTRA_MEMORY_DB": str(p)}):
        assert knowledge_mcp.db_path_from_env(env).is_absolute()


def test_open_db_for_mcp_creates_missing_parent_and_migrates(tmp_path):
    """Fresh-install release blocker (§5.2.1): the MCP starts on the post-install
    restart BEFORE /memory-setup, so ~/.ultra-memory/ may not exist yet.
    _open_db_for_mcp must mkdir the parent (so sqlite can open the file) AND migrate
    (the MCP writes access_log audit rows on recall), returning a usable empty store
    instead of crashing the server at startup so it silently never registers."""
    deep = tmp_path / "nope" / "deeper" / "memory.db"
    assert not deep.parent.exists()
    conn = knowledge_mcp._open_db_for_mcp(deep)
    try:
        assert deep.parent.is_dir()                                  # parent created
        assert conn.execute("PRAGMA user_version").fetchone()[0] > 0  # migrated
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"memories", "access_log"} <= names                   # tables the MCP uses
    finally:
        conn.close()


def test_open_db_for_mcp_idempotent_on_existing_dir(tmp_path):
    """Existing parent + re-open: mkdir is a no-op (exist_ok) and re-migrate is
    idempotent — opening twice yields the same usable migrated store."""
    p = tmp_path / "memory.db"
    knowledge_mcp._open_db_for_mcp(p).close()
    conn = knowledge_mcp._open_db_for_mcp(p)
    try:
        assert p.parent.is_dir()
        assert conn.execute("PRAGMA user_version").fetchone()[0] > 0
    finally:
        conn.close()


def test_open_db_for_mcp_logs_mkdir_failure_to_stderr(tmp_path, monkeypatch, capsys):
    """A mkdir failure is logged to stderr, NOT swallowed-then-fatal-silently: the
    diagnostic line is emitted before open_memory_db surfaces the real open error."""
    import pathlib

    def _boom(self, *a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(pathlib.Path, "mkdir", _boom)
    with pytest.raises(Exception):
        knowledge_mcp._open_db_for_mcp(tmp_path / "x" / "memory.db")
    assert "could not create" in capsys.readouterr().err


def test_knowledge_tools_declares_query_tool():
    tools = knowledge_mcp.knowledge_tools()
    assert any(t.name == "knowledge_query" for t in tools)
    qt = next(t for t in tools if t.name == "knowledge_query")
    assert "query" in qt.inputSchema.get("required", [])


def test_lazy_embedder_defers_build_until_first_call_and_memoizes():
    """Startup resilience: the MCP must NOT build the (heavy) fastembed model at
    startup — that raced the 30s connect timeout and a missing model file crashed
    the whole server (knowledge MCP failure, 2026-05-31). lazy_embedder defers the
    build to the first embed call, then reuses it (one build, warm thereafter)."""
    builds = []

    def factory():
        builds.append(1)

        def _embed(texts):
            return [[float(len(t))] for t in texts]

        return _embed

    embed = knowledge_mcp.lazy_embedder(factory=factory)
    assert builds == []  # constructing the wrapper must not build the model

    assert embed(["abc"]) == [[3.0]]
    assert builds == [1]  # built on first use

    assert embed(["de", "f"]) == [[2.0], [1.0]]
    assert builds == [1]  # memoized — no second build


# ---------------------------------------------------------------------------
# R4 FIX 4 — the knowledge MCP error payload must NOT leak the raw exception
# string (internal filesystem/DB paths) across the privilege boundary. The result
# ROWS are strip_secrets'd, but the ERROR payload was not — and strip_secrets does
# NOT redact paths anyway. Return a FIXED generic error; log the detail locally.
# ---------------------------------------------------------------------------

def test_fix4_recall_error_does_not_leak_internal_path(tmp_path, capsys):
    """An exception carrying an absolute path → the client-facing error string is a
    generic one (NEITHER the path NOR the db filename leaks), while the detail is
    logged locally (stderr) for server-side debugging."""
    import json
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="alpha", body="alpha fact")

    leaky_path = "/Users/USER/.cache/fastembed/models/secret-model-x/model.onnx"
    db_name = "memory.db"

    def boom(texts):
        raise RuntimeError(
            f"OperationalError: unable to open {leaky_path} (db {db_name})")

    res = knowledge_mcp.run_query_tool(
        {"query": "alpha", "top_k": 5}, conn=conn, embedder=boom,
        caller_class="subagent", dim=3, now_ts="2026-05-02T00:00:00", ts=None)
    payload = json.loads(res[0].text)
    assert "error" in payload
    # The client-facing error is the FIXED generic string.
    assert payload["error"] == "recall failed (internal error)"
    assert leaky_path not in payload["error"]
    assert db_name not in payload["error"]
    # The detail IS preserved server-side (stderr), so debugging info is not lost.
    captured = capsys.readouterr()
    assert leaky_path in captured.err
    conn.close()


def test_fix4_missing_query_error_is_unchanged(tmp_path):
    """The pre-existing structured 'missing query' error (no exception interpolated)
    is unaffected — only the exception-interpolating catch is genericized."""
    import json
    conn = _db(tmp_path)
    res = knowledge_mcp.run_query_tool(
        {}, conn=conn, embedder=_flat_embedder(), caller_class="subagent", dim=3)
    payload = json.loads(res[0].text)
    assert payload["error"] == "missing required 'query' string argument"
    conn.close()
