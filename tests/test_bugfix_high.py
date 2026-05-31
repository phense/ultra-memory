"""Regression tests for the HIGH-severity bugs found in the 2026-05-31 audit.

Each test fails against the pre-fix code and passes after the fix. Hermetic:
tmp SQLite DBs, injected embedders/runners, no network, no real claude CLI.
See docs/audit/2026-05-31/reports/SUMMARY.md for the findings.
"""
import json
import subprocess

import pytest

from ultra_memory import (
    claude_cli,
    knowledge_mcp,
    memory_cli,
    memory_import as mi,
    memory_lib,
    memory_query,
)
from ultra_memory.claude_cli import ClaudeCliError
from ultra_memory.redact_secrets import strip_secrets

R = "[REDACTED]"


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _fake_embedder(dim=3):
    def _embed(texts):
        return [[1.0] + [0.0] * (dim - 1) for _ in texts]
    return _embed


# --- H1: memory_import name-collision must fail loud, not silently overwrite ----

def test_import_name_collision_raises(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    # Two DIFFERENT files whose frontmatter `name:` collides (the harness strips
    # type prefixes, so this happens in real data).
    for fn in ("a_email_routing.md", "b_email_routing.md"):
        (mem / fn).write_text(
            "---\nname: email-routing\ndescription: \"d\"\nmetadata: \n"
            "  node_type: memory\n  type: feedback\n  originSessionId: s\n---\n\nBODY\n")
    conn = _db(tmp_path)
    with pytest.raises(ValueError, match="duplicate memory id"):
        mi.import_memory_dir(conn, mem, index_path=None, ts="2026-05-30T10:00:00")
    conn.close()


# --- H2: type-scoped recall must scope in SQL (no starvation) -------------------

def test_query_memories_include_types_scopes_in_sql(tmp_path):
    conn = _db(tmp_path)
    # Many user rows + few project rows, all equally relevant.
    for i in range(20):
        memory_lib.save_memory(conn, id=f"u{i}", type="user", title=f"u{i}",
                               body="same body", ts="2026-05-01T00:00:00")
    for i in range(3):
        memory_lib.save_memory(conn, id=f"p{i}", type="project", title=f"p{i}",
                               body="same body", ts="2026-05-01T00:00:00")
    out = memory_query.query_memories(
        conn, "same body", embedder=_fake_embedder(), dim=3, top_k=5,
        include_types=("project", "reference"), now_ts="2026-05-02T00:00:00")
    assert out, "scoped query must still return the project rows, not be starved"
    assert {r["type"] for r in out} == {"project"}
    conn.close()


# --- H2 (M): query embedding dim mismatch must raise, not silently mis-score -----

def test_query_dim_mismatch_raises(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="m", body="doc body",
                           ts="2026-05-01T00:00:00")

    def emb(texts):
        # docs embed at dim 3; the query ("QX") embeds at dim 2 -> mismatch.
        return [[1.0, 0.0] if t == "QX" else [1.0, 0.0, 0.0] for t in texts]

    with pytest.raises(ValueError, match="dim"):
        memory_query.query_memories(conn, "QX", embedder=emb, dim=3,
                                    now_ts="2026-05-02T00:00:00")
    conn.close()


# --- H2 (M): top_k clamp -------------------------------------------------------

def test_query_negative_top_k_clamped(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="m", body="b",
                           ts="2026-05-01T00:00:00")
    out = memory_query.query_memories(conn, "b", embedder=_fake_embedder(), dim=3,
                                      top_k=-1, now_ts="2026-05-02T00:00:00")
    assert out == []
    conn.close()


# --- H2 (M): staleness signal is live even when now_ts is omitted ---------------

def test_query_default_now_ts_makes_staleness_live(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="old", type="project", title="old",
                           body="old doc", ts="2026-01-01T00:00:00")
    # No now_ts passed -> must default to "now" so the old memory is flagged stale
    # (pre-fix: now_ts=None short-circuited and stale was always False).
    out = memory_query.query_memories(conn, "old doc", embedder=_fake_embedder(),
                                      dim=3, staleness_days=90)
    assert out and out[0]["stale"] is True
    conn.close()


# --- H2 (M): knowledge MCP degrades a failing recall instead of crashing --------

def test_run_query_tool_degrades_on_embedder_failure(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="p", type="project", title="p", body="body",
                           ts="2026-05-01T00:00:00")

    def boom(texts):
        raise RuntimeError("model down")

    res = knowledge_mcp.run_query_tool(
        {"query": "body"}, conn=conn, embedder=boom, caller_class="subagent",
        now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00")
    payload = json.loads(res[0].text)
    assert "error" in payload  # structured error, not a raised exception
    conn.close()


# --- H5 / M: memory_cli error handling -----------------------------------------

def _seed_cli(tmp_path):
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(db)
    memory_lib.save_memory(conn, id="x", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    conn.close()
    return db


def test_cli_pin_missing_id_returns_1(tmp_path, capsys):
    db = _seed_cli(tmp_path)
    rc = memory_cli.main(["pin", "--id", "ghost"], db_path=str(db),
                         ts="2026-05-02T00:00:00")
    assert rc == 1
    assert "no memory" in capsys.readouterr().err


def test_cli_verify_missing_id_returns_1(tmp_path, capsys):
    db = _seed_cli(tmp_path)
    rc = memory_cli.main(["verify", "--id", "ghost"], db_path=str(db),
                         ts="2026-05-02T00:00:00")
    assert rc == 1
    assert "no memory" in capsys.readouterr().err


def test_cli_edit_bad_from_file_returns_1(tmp_path, capsys):
    db = _seed_cli(tmp_path)
    rc = memory_cli.main(["edit", "--id", "x", "--from-file", str(tmp_path / "nope.txt")],
                         db_path=str(db), ts="2026-05-02T00:00:00")
    assert rc == 1
    assert "cannot read" in capsys.readouterr().err


def test_cli_inbox_returns_nonzero_on_errors(tmp_path):
    db = _seed_cli(tmp_path)
    inbox = tmp_path / "inbox.md"
    inbox.write_text("pin ghost-id\n", encoding="utf-8")  # unknown id -> error
    rc = memory_cli.main(["inbox", "--path", str(inbox)], db_path=str(db),
                         ts="2026-05-02T00:00:00")
    assert rc != 0


# --- H6 / M: claude_cli wraps subprocess failures + validates model -------------

def test_claude_cli_missing_binary_raises_claudeclierror():
    def runner(cmd, **kw):
        raise FileNotFoundError("no such file: claude")

    with pytest.raises(ClaudeCliError, match="not found"):
        claude_cli.run_claude("p", model="m", runner=runner,
                              env={"CLAUDE_CODE_OAUTH_TOKEN": "t"})


def test_claude_cli_timeout_raises_claudeclierror():
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)

    with pytest.raises(ClaudeCliError, match="timed out"):
        claude_cli.run_claude("p", model="m", runner=runner,
                              env={"CLAUDE_CODE_OAUTH_TOKEN": "t"})


def test_claude_cli_empty_model_raises_valueerror():
    with pytest.raises(ValueError):
        claude_cli.run_claude("p", model="  ", runner=lambda *a, **k: None,
                              env={"CLAUDE_CODE_OAUTH_TOKEN": "t"})


# --- H4: redact_secrets — short credential value + proxy USERNAME ---------------

def test_redact_short_keyvalue_credential():
    # 9-char value after a recognized keyword used to slip through the {12,} floor.
    assert R in strip_secrets("password=p4ssvalue")


def test_redact_proxy_username():
    out = strip_secrets("WEBSHARE_PROXY_USERNAME=zk8f-rotate")
    assert "zk8f-rotate" not in out and R in out
