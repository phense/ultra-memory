"""The single audited write path into memory.db.

Every function takes an open `conn` (the caller owns the short-lived connection,
per spec §6 single-writer discipline) and wraps its write in BEGIN IMMEDIATE/COMMIT.
All persisted text passes through redact_secrets first; every mutation writes an
audit_log row. No automatic fuzzy-batch deletion: consolidate = redirect-stub,
delete = soft tombstone.
"""
import hashlib
import json
from pathlib import Path

from . import db
from .redact_secrets import strip_secrets

_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def open_memory_db(path, migrations_dir=_MIGRATIONS):
    """Open + migrate a memory.db. Caller is responsible for closing it."""
    conn = db.connect(path)
    db.migrate(conn, migrations_dir)
    return conn


def _audit(conn, *, op, target_kind, target_id, reason, prior, ts):
    conn.execute(
        "INSERT INTO audit_log (ts, op, target_kind, target_id, reason, prior_state) "
        "VALUES (?,?,?,?,?,?)",
        (ts, op, target_kind, target_id, reason,
         json.dumps(prior) if prior is not None else None),
    )


def save_memory(conn, *, id, type, title, body, ts, origin_session_id=None):
    """Upsert a memory through the redact chokepoint + audit. Returns id."""
    title = strip_secrets(title)
    body = strip_secrets(body)
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if prior is None:
            conn.execute(
                "INSERT INTO memories (id, type, title, body, created_at, updated_at, "
                "origin_session_id) VALUES (?,?,?,?,?,?,?)",
                (id, type, title, body, ts, ts, origin_session_id),
            )
            _audit(conn, op="save", target_kind="memory", target_id=id,
                   reason="create", prior=None, ts=ts)
        else:
            conn.execute(
                "UPDATE memories SET type=?, title=?, body=?, updated_at=? WHERE id=?",
                (type, title, body, ts, id),
            )
            _audit(conn, op="save", target_kind="memory", target_id=id,
                   reason="update", prior=dict(prior), ts=ts)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return id


def _event_key(session_id, ts, kind, title):
    return hashlib.sha256(f"{session_id}|{ts}|{kind}|{title}".encode("utf-8")).hexdigest()


def record_session_event(conn, *, session_id, kind, title, ts,
                         detail=None, files=None, refs=None, session_fields=None):
    """Append a typed session event idempotently (UNIQUE event_key). Ensures the
    session row exists first (FK). Returns the event_key."""
    title = strip_secrets(title)
    detail = strip_secrets(detail)
    key = _event_key(session_id, ts, kind, title)
    sf = session_fields or {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, started_at, status) VALUES (?,?,?)",
            (session_id, sf.get("started_at", ts), sf.get("status", "active")),
        )
        conn.execute(
            "INSERT OR IGNORE INTO session_events "
            "(session_id, ts, kind, title, detail, files, refs, event_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_id, ts, kind, title, detail,
             json.dumps(files) if files is not None else None,
             json.dumps(refs) if refs is not None else None, key),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return key
