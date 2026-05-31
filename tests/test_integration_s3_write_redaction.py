"""Integration tests for seam S3: write-redaction.

Exercises the REAL seam between the audited write path and the secret
chokepoint + the export path:

    memory_lib.open_memory_db        (real schema bootstrap / migrate)
    memory_lib.save_memory           (real audited write; redacts 4 free-text cols)
    memory_lib.delete                (real soft-delete; reason is NOT redacted)
    memory_lib.replay_spool          (real spool drain)
    redact_secrets.strip_secrets     (the mandatory pre-persist chokepoint)
    memory_export.export_memory      (dump.sql + snapshot.db + views/<slug>.md)

The seam's invariant: a credential-shaped secret that enters through the write
path must never reach the git-tracked rollback artifacts.  The export VIEWS leg
(memory_export.py:84-89) and the SNAPSHOT leg (the raw VACUUM INTO,
memory_export.py:92-98) render/copy RAW columns with NO independent redaction
net — they inherit safety solely from the write-path chokepoint
(memory_lib.py:112-115).  Only the SQL dump has its own redaction pass
(memory_export.py:70), and that pass is the only net that catches
audit_log.reason, which the write path leaves cleartext (memory_lib.py:240).

Hermetic & deterministic: every test uses a tmp SQLite DB via pytest tmp_path,
passes an explicit ts=, seeds rows through the real audited write API.  No
network, no real data/memory.db, no ``claude`` CLI.

API ground-truth (read from ultra_memory/memory_lib.py + memory_export.py +
redact_secrets.py):
    conn = memory_lib.open_memory_db(str(path))                          # ml:87
    memory_lib.save_memory(conn, id=, type=, title=, body=, ts=,
                           description=, index_hook=)                     # ml:103
    memory_lib.delete(conn, id=, reason=, tier='durable', ts=)           # ml:228
    memory_lib.replay_spool(conn, spool_dir=) -> {replayed,failed,errors}# ml:274
    memory_export.export_memory(conn, out_dir, ts=, snapshot=True)       # mexp:43
    # the spool dir is <db_parent>/memory_spool/ (ml:34-39)
    # export views only render status='active' rows (mexp:76)
"""
from __future__ import annotations

import glob
import json
import sqlite3
from pathlib import Path

import pytest

from ultra_memory import memory_export, memory_lib

# A credential-shaped token that matches redact_secrets' ``sk-ant-...`` rule
# (redact_secrets.py:25). It contains no whitespace, survives markdown rendering
# verbatim, and redacts to "[REDACTED]" (which re-triggers no rule → idempotent).
SECRET = "sk-ant-abcdef0123456789ABCDEF"
TS = "2026-05-31T00:00:00Z"


@pytest.fixture()
def conn(tmp_path):
    c = memory_lib.open_memory_db(str(tmp_path / "memory.db"))
    yield c
    c.close()


def _views_text(out_dir: Path) -> str:
    files = glob.glob(str(out_dir / "views" / "**" / "*.md"), recursive=True)
    return "".join(Path(f).read_text(encoding="utf-8") for f in files)


# ---------------------------------------------------------------------------
# 1. Export VIEWS leg: write-path redaction must carry into views/<slug>.md.
# ---------------------------------------------------------------------------
def test_export_views_redacts_secret_in_body_and_description(conn, tmp_path):
    """The views leg renders raw `body`/`description` columns with no independent
    net — this proves the write-path chokepoint actually scrubbed them."""
    memory_lib.save_memory(
        conn, id="m_views", type="user",
        title="view title", body=f"body holds {SECRET}",
        description=f"desc holds {SECRET}", ts=TS,
    )
    out = tmp_path / "export"
    assert memory_export.export_memory(conn, out, ts=TS) is True

    views = _views_text(out)
    assert views, "expected at least one rendered view markdown file"
    assert SECRET not in views
    assert "[REDACTED]" in views


# ---------------------------------------------------------------------------
# 2. Write path: all 4 redacted free-text columns, asserted in the DB.
# ---------------------------------------------------------------------------
def test_write_redacts_title_description_index_hook_in_db(conn):
    """save_memory redacts title/body/description/index_hook (ml:112-115).
    Only body had prior coverage; this pins the other three too."""
    memory_lib.save_memory(
        conn, id="m_fields", type="user",
        title=f"title {SECRET}", body=f"body {SECRET}",
        description=f"description {SECRET}", index_hook=f"index_hook {SECRET}",
        ts=TS,
    )
    row = dict(conn.execute(
        "SELECT * FROM memories WHERE id=?", ("m_fields",)).fetchone())

    for col in ("title", "body", "description", "index_hook"):
        assert SECRET not in (row[col] or ""), f"{col} not redacted"
        assert "[REDACTED]" in (row[col] or ""), f"{col} missing [REDACTED]"


# ---------------------------------------------------------------------------
# 3. Canonical seam test: secret absent from DB row, dump, view AND snapshot.
# ---------------------------------------------------------------------------
def test_end_to_end_secret_absent_from_db_dump_view_and_snapshot(conn, tmp_path):
    memory_lib.save_memory(
        conn, id="m_e2e", type="user",
        title=f"title {SECRET}", body=f"body {SECRET}",
        description=f"description {SECRET}", index_hook=f"index_hook {SECRET}",
        ts=TS,
    )
    out = tmp_path / "export"
    assert memory_export.export_memory(conn, out, ts=TS) is True

    # (a) live DB row
    row = dict(conn.execute(
        "SELECT * FROM memories WHERE id=?", ("m_e2e",)).fetchone())
    assert SECRET not in json.dumps(row)

    # (b) SQL dump
    dump = (out / "memory.dump.sql").read_text(encoding="utf-8")
    assert SECRET not in dump

    # (c) views
    assert SECRET not in _views_text(out)

    # (d) snapshot binary DB — read the actual memories row, not just bytes.
    snap = sqlite3.connect(str(out / "memory.snapshot.db"))
    snap.row_factory = sqlite3.Row
    try:
        snap_row = dict(snap.execute(
            "SELECT * FROM memories WHERE id=?", ("m_e2e",)).fetchone())
    finally:
        snap.close()
    assert SECRET not in json.dumps(snap_row)


# ---------------------------------------------------------------------------
# 4. Spool/replay composed with redaction.
# ---------------------------------------------------------------------------
class _BusyOnBegin:
    """Delegates everything to a real sqlite3 connection but raises SQLITE_BUSY
    on every ``BEGIN IMMEDIATE`` — driving _write_txn to exhaust its retries and
    spool (ml:80-84).  A duck-typed wrapper is required because
    ``sqlite3.Connection.execute`` is read-only and cannot be monkeypatched
    directly; save_memory takes ``conn`` positionally, so a wrapper works.
    ``PRAGMA database_list`` (used by _spool_dir, ml:34-39) and the rollback
    path delegate through, so the spool still lands beside the real db file.
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def execute(self, sql, *args):
        if isinstance(sql, str) and sql.strip().upper().startswith("BEGIN IMMEDIATE"):
            raise sqlite3.OperationalError("database is locked")
        return self._inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_spooled_write_replays_redacted_into_db(tmp_path, monkeypatch):
    """Force save_memory to exhaust retries and spool, then replay.

    The spooled JSON payload must already be redacted (strip_secrets runs before
    persistence, ml:112-115, and the spool dict carries the already-redacted
    `body`, ml:143).  After replay, the DB row is redacted too.
    """
    db_path = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        # Zero the backoff so the retry loop is instant + deterministic.
        monkeypatch.setattr(memory_lib.time, "sleep", lambda *_a, **_k: None)

        with pytest.raises(memory_lib.WriteSpooled):
            memory_lib.save_memory(
                _BusyOnBegin(conn), id="m_spool", type="user",
                title="t", body=f"spooled {SECRET}", ts=TS,
            )

        # Spool file landed under <db_parent>/memory_spool/ and is ALREADY redacted.
        spool_dir = db_path.parent / "memory_spool"
        files = sorted(spool_dir.glob("*.json"))
        assert len(files) == 1, "the busy write must have spooled exactly one op"
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["op"] == "save_memory"
        assert SECRET not in payload["body"]
        assert "[REDACTED]" in payload["body"]

        # Replay the spool into the DB through a healthy connection.
        summary = memory_lib.replay_spool(conn, spool_dir=str(spool_dir))
        assert summary["replayed"] == 1 and summary["failed"] == 0, summary
        assert not list(spool_dir.glob("*.json")), "replayed file must be unlinked"

        got = dict(conn.execute(
            "SELECT * FROM memories WHERE id=?", ("m_spool",)).fetchone())
        assert SECRET not in got["body"]
        assert "[REDACTED]" in got["body"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Characterization: audit_log.reason is cleartext in the live DB + snapshot
#    but scrubbed in the git-tracked SQL dump.
# ---------------------------------------------------------------------------
def test_audit_reason_cleartext_in_db_but_scrubbed_in_dump(conn, tmp_path):
    """Pins the real boundary: the write path does NOT redact the delete reason
    (ml:240 stores f"[{tier}] {reason}" verbatim), so the secret lives in
    audit_log.reason in BOTH the live DB and the raw VACUUM snapshot.  Only the
    SQL dump's whole-text redaction pass (mexp:70) scrubs it.  This makes the
    silent live-DB/snapshot leak boundary explicit and locks the dump behavior.
    """
    memory_lib.save_memory(
        conn, id="m_audit", type="user",
        title="t", body="body no secret", ts=TS,
    )
    memory_lib.delete(conn, id="m_audit", reason=f"leak {SECRET}",
                      tier="durable", ts=TS)

    out = tmp_path / "export"
    assert memory_export.export_memory(conn, out, ts=TS) is True

    # (a) live DB: reason is cleartext (boundary, NOT a bug — the live .db is
    #     local & git-ignored; the dump is the only git artifact).
    reason = conn.execute(
        "SELECT reason FROM audit_log WHERE op='soft_delete'").fetchone()[0]
    assert SECRET in reason, "write path is expected NOT to redact the delete reason"

    # (b) snapshot binary copy: also cleartext (raw VACUUM INTO of the live DB).
    snap = sqlite3.connect(str(out / "memory.snapshot.db"))
    try:
        snap_reason = snap.execute(
            "SELECT reason FROM audit_log WHERE op='soft_delete'").fetchone()[0]
    finally:
        snap.close()
    assert SECRET in snap_reason, "raw snapshot inherits the cleartext reason"

    # (c) SQL dump: scrubbed by export_memory's whole-dump strip_secrets pass.
    dump = (out / "memory.dump.sql").read_text(encoding="utf-8")
    assert SECRET not in dump
    assert "[REDACTED]" in dump
