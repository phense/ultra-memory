"""Regression tests for the MEDIUM-severity bugs found in the 2026-05-31 audit.

Hermetic: tmp SQLite DBs, no network, no real claude CLI.
See docs/audit/2026-05-31/reports/SUMMARY.md for the findings.
"""
from ultra_memory import memory_export as mx
from ultra_memory import memory_inbox, memory_lib, retention

TS = "2026-05-02T00:00:00"
SECRET = "sk-ant-abcdef0123456789ABCDEF"


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


# --- memory_lib: created_at `is None` (not `or`) preserves a falsy override -------

def test_save_memory_preserves_falsy_created_at(tmp_path):
    conn = _db(tmp_path)
    # epoch 0 is falsy: the OLD `created_at or ts` would discard it and stamp `ts`,
    # defeating the bootstrap mtime override. `is None` must keep the explicit value.
    # (created_at has TEXT affinity, so 0 -> "0"; assert affinity-agnostically.)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts=TS, created_at=0, updated_at=0)
    row = conn.execute(
        "SELECT created_at, updated_at FROM memories WHERE id='m'").fetchone()
    assert str(row["created_at"]) == "0" and row["created_at"] != TS
    assert str(row["updated_at"]) == "0" and row["updated_at"] != TS
    conn.close()


# --- memory_lib: event idempotency key computed on RAW (pre-redaction) text ------

def test_event_key_computed_on_raw_text(tmp_path):
    conn = _db(tmp_path)
    title = f"tok {SECRET}"
    detail = f"secret detail {SECRET}"
    memory_lib.record_session_event(conn, session_id="s", kind="note",
                                    title=title, ts=TS, detail=detail)
    stored = conn.execute("SELECT event_key FROM session_events").fetchone()[0]
    assert stored == memory_lib._event_key("s", TS, "note", title, detail)
    conn.close()


# --- memory_lib: delete reason redacted before the git-exported audit_log --------

def test_delete_reason_redacted_in_audit(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="user", title="t", body="b", ts=TS)
    memory_lib.delete(conn, id="m", reason=f"leak {SECRET}", tier="durable", ts=TS)
    reason = conn.execute(
        "SELECT reason FROM audit_log WHERE op='soft_delete'").fetchone()[0]
    assert SECRET not in reason and "[REDACTED]" in reason
    conn.close()


# --- memory_export: orphan views pruned when a memory is deleted -----------------

def test_export_prunes_orphan_views(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="keep", type="project", title="Keep", body="b", ts=TS)
    memory_lib.save_memory(conn, id="drop", type="project", title="Drop", body="b", ts=TS)
    out = tmp_path / "exp"
    mx.export_memory(conn, out, ts=TS)
    assert (out / "views" / "drop.md").exists()
    memory_lib.delete(conn, id="drop", reason="x", tier="volatile",
                      ts="2026-05-03T00:00:00")
    mx.export_memory(conn, out, ts="2026-05-03T00:00:01")
    assert not (out / "views" / "drop.md").exists()  # phantom view pruned
    assert (out / "views" / "keep.md").exists()
    conn.close()


# --- retention: returns the DELETE rowcount + bounds the summary -----------------

def test_prune_returns_rowcount_and_bounds_summary(tmp_path):
    conn = _db(tmp_path)
    for i in range(5):
        memory_lib.record_session_event(conn, session_id="s", kind="task_done",
                                        title=f"old {i}", ts=f"2026-01-0{i+1}T00:00:00Z")
    deleted = retention.prune_session_events(conn, keep_days=30, ts="2026-05-30T12:00:00Z")
    assert deleted == 5
    again = retention.prune_session_events(conn, keep_days=30, ts="2026-05-30T12:00:00Z")
    assert again == 0
    summary = conn.execute("SELECT summary FROM sessions WHERE id='s'").fetchone()[0]
    assert summary.count("\n") <= retention._SUMMARY_MAX_LINES
    conn.close()


# --- memory_inbox: a failed directive is re-emitted, never silently lost ---------

def test_inbox_reemits_failed_directive(tmp_path):
    conn = _db(tmp_path)
    inbox = tmp_path / "inbox.md"
    inbox.write_text("pin ghost-1\n", encoding="utf-8")
    summary = memory_inbox.import_inbox(conn, inbox, ts=TS)
    after = inbox.read_text(encoding="utf-8")
    assert "pin ghost-1" in after  # failed directive preserved for retry
    assert summary["errors"]
    conn.close()
