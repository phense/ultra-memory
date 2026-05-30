"""The single audited write path into memory.db.

Every function takes an open `conn` (the caller owns the short-lived connection,
per spec §6 single-writer discipline) and wraps its write in BEGIN IMMEDIATE/COMMIT.
All persisted text passes through redact_secrets first; every mutation writes an
audit_log row. No automatic fuzzy-batch deletion: consolidate = redirect-stub,
delete = soft tombstone.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from . import db
from .redact_secrets import strip_secrets

_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class WriteSpooled(Exception):
    """Raised LOUDLY when a write could not be committed after the bounded retries
    (db stayed busy) and was therefore written to the durable spool for replay
    rather than silently dropped (spec §6 + §15)."""


def _is_busy(exc):
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _spool_dir(conn):
    """The memory_spool/ dir beside the main db file. None for an in-memory db."""
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return (Path(file).parent / "memory_spool") if file else None
    return None


def _spool(conn, record):
    """Persist a failed write's intent for later replay. Keyed by content hash so a
    retried-then-spooled op writes one stable file, not duplicates."""
    if record is None:
        return
    target = _spool_dir(conn)
    if target is None:
        return
    target.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (target / f"{key}.json").write_text(payload)


def _write_txn(conn, work, *, spool=None, retries=5, base_delay=0.05, sleep=time.sleep):
    """Run work() inside BEGIN IMMEDIATE/COMMIT with bounded retry-with-backoff on
    SQLITE_BUSY (spec §6). work() must be re-runnable (it re-executes from scratch
    each attempt; the prior attempt was rolled back). A non-busy error surfaces
    immediately. On retry exhaustion the op is spooled durably and WriteSpooled is
    raised loudly — never a silent drop."""
    last = None
    for attempt in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            work()
            conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if not _is_busy(exc):
                raise
            last = exc
            sleep(base_delay * (2 ** attempt))
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    _spool(conn, spool)
    raise WriteSpooled(
        f"write failed after {retries} retries (database busy); spooled for replay: "
        f"{(spool or {}).get('op', '?')}"
    ) from last


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


def save_memory(conn, *, id, type, title, body, ts, origin_session_id=None,
                description=None, index_hook=None, node_type="memory",
                file_slug=None, sort_order=None, created_at=None, updated_at=None):
    """Upsert a memory through the redact chokepoint + audit. Returns id.

    `ts` is the action time (always the audit-row timestamp). `created_at`/
    `updated_at` default to `ts` but can be overridden so a bootstrap import can
    stamp the file's real age (mtime) — otherwise every imported memory looks
    freshly written and the §8 staleness signal never fires."""
    title = strip_secrets(title)
    body = strip_secrets(body)
    description = strip_secrets(description)
    index_hook = strip_secrets(index_hook)
    created = created_at or ts
    updated = updated_at or ts

    def work():
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if prior is None:
            conn.execute(
                "INSERT INTO memories (id, type, title, body, description, index_hook, "
                "node_type, file_slug, sort_order, created_at, updated_at, "
                "origin_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (id, type, title, body, description, index_hook, node_type,
                 file_slug, sort_order, created, updated, origin_session_id),
            )
            _audit(conn, op="save", target_kind="memory", target_id=id,
                   reason="create", prior=None, ts=ts)
        else:
            conn.execute(
                "UPDATE memories SET type=?, title=?, body=?, description=?, "
                "index_hook=?, node_type=?, file_slug=?, sort_order=?, updated_at=? "
                "WHERE id=?",
                (type, title, body, description, index_hook, node_type,
                 file_slug, sort_order, updated, id),
            )
            _audit(conn, op="save", target_kind="memory", target_id=id,
                   reason="update", prior=dict(prior), ts=ts)

    _write_txn(conn, work, spool={
        "op": "save_memory", "id": id, "type": type, "title": title, "body": body,
        "ts": ts, "origin_session_id": origin_session_id, "description": description,
        "index_hook": index_hook, "node_type": node_type, "file_slug": file_slug,
        "sort_order": sort_order, "created_at": created, "updated_at": updated})
    return id


def _event_key(session_id, ts, kind, title, detail=None):
    """Content-addressed idempotency key. Includes detail so two events sharing
    session/ts/kind/title but differing in body are distinct (not silently merged);
    byte-identical events still collide → genuine dedupe."""
    raw = f"{session_id}|{ts}|{kind}|{title}|{detail or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_session_event(conn, *, session_id, kind, title, ts,
                         detail=None, files=None, refs=None, session_fields=None):
    """Append a typed session event idempotently (UNIQUE event_key). Ensures the
    session row exists first (FK). Returns the event_key."""
    title = strip_secrets(title)
    detail = strip_secrets(detail)
    key = _event_key(session_id, ts, kind, title, detail)
    sf = session_fields or {}

    def work():
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

    _write_txn(conn, work, spool={
        "op": "record_session_event", "session_id": session_id, "kind": kind,
        "title": title, "ts": ts, "detail": detail, "files": files, "refs": refs,
        "event_key": key})
    return key


def record_access(conn, *, target_kind, target_id, ts, context=None):
    """Append-only access log + atomic access_count increment (memory targets only)."""
    def work():
        conn.execute(
            "INSERT INTO access_log (target_kind, target_id, ts, context) VALUES (?,?,?,?)",
            (target_kind, target_id, ts, context),
        )
        if target_kind == "memory":
            conn.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed=? "
                "WHERE id=?",
                (ts, target_id),
            )

    _write_txn(conn, work, spool={
        "op": "record_access", "target_kind": target_kind, "target_id": target_id,
        "ts": ts, "context": context})


def consolidate(conn, *, loser_id, canonical_id, reason, ts):
    """Redirect-stub: mark loser status='redirect' + supersedes=canonical. Never deletes."""
    def work():
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (loser_id,)).fetchone()
        if prior is None:
            raise KeyError(f"consolidate: no memory with id {loser_id!r}")
        conn.execute(
            "UPDATE memories SET status='redirect', supersedes=?, updated_at=? WHERE id=?",
            (canonical_id, ts, loser_id),
        )
        _audit(conn, op="redirect", target_kind="memory", target_id=loser_id,
               reason=reason, prior=dict(prior), ts=ts)

    _write_txn(conn, work, spool={
        "op": "consolidate", "loser_id": loser_id, "canonical_id": canonical_id,
        "reason": reason, "ts": ts})


_DELETE_TIERS = ("durable", "volatile")


def delete(conn, *, id, reason, tier, ts):
    """Soft-delete tombstone (status='deleted') + audit. Single-id only; no fuzzy batch.
    Hard purge is a separate, later step."""
    if tier not in _DELETE_TIERS:
        raise ValueError(f"unknown tier: {tier!r} (expected one of {_DELETE_TIERS})")

    def work():
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if prior is None:
            raise KeyError(f"delete: no memory with id {id!r}")
        conn.execute("UPDATE memories SET status='deleted', updated_at=? WHERE id=?", (ts, id))
        _audit(conn, op="soft_delete", target_kind="memory", target_id=id,
               reason=f"[{tier}] {reason}", prior=dict(prior), ts=ts)

    _write_txn(conn, work, spool={
        "op": "delete", "id": id, "reason": reason, "tier": tier, "ts": ts})
