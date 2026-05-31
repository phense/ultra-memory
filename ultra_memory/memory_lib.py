"""The single audited write path into memory.db.

Every function takes an open `conn` (the caller owns the short-lived connection,
per spec §6 single-writer discipline) and wraps its write in BEGIN IMMEDIATE/COMMIT.
All persisted text passes through redact_secrets first; every mutation writes an
audit_log row. No automatic fuzzy-batch deletion: consolidate = redirect-stub,
delete = soft tombstone.
"""
import hashlib
import json
import re
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


def _safe_rollback(conn):
    """Roll back an active txn without letting the cleanup error mask the original
    failure — a COMMIT/ROLLBACK can itself hit SQLITE_BUSY, which previously
    propagated out of the except block and skipped the retry/spool path."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


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
            _safe_rollback(conn)
            if not _is_busy(exc):
                raise
            last = exc
            sleep(base_delay * (2 ** attempt))
        except Exception:
            _safe_rollback(conn)
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
    # `reason` is caller-supplied free text that lands in the git-exported audit_log;
    # redact it too (prior is a snapshot of an already-redacted row, so it's clean).
    conn.execute(
        "INSERT INTO audit_log (ts, op, target_kind, target_id, reason, prior_state) "
        "VALUES (?,?,?,?,?,?)",
        (ts, op, target_kind, target_id, strip_secrets(reason),
         json.dumps(prior) if prior is not None else None),
    )


# ---------------------------------------------------------------------------
# Topic on the write path (SP-3 Stage 2, D2/D3/D11). The engine is PROJECT-
# AGNOSTIC: a topic is just a stored TEXT string; the router is a GENERIC
# keyword/caller_class/origin_session heuristic (NO LLM, NO wiki import); and
# the "unknown topic genesis" is an OPTIONAL injectable hook (default no-op).
# The CONSUMER (Trading) wires wiki_topics.ensure_topic into the hook and supplies
# the keyword map — the engine never references wiki_topics.
# ---------------------------------------------------------------------------

# Operational rows are cross-topic by nature (D11) — see _TOPIC_EXEMPT_TYPES,
# defined below alongside backfill_topic; reused by the router + write path.


def make_keyword_router(keyword_map):
    """Build a deterministic, GENERIC fallback topic router from a
    `{topic: (kw, kw, ...)}` map (caller-supplied — content-free in the engine).

    The returned callable has the router contract:
        router(*, type, title, body, origin_session_id, caller_class) -> str | None
    It lowercases title+body and returns the FIRST topic whose map order contains a
    matching whole-word keyword; abstains to None on no hit. Operational types
    (user/feedback) always abstain (D11 — they are cross-topic). No LLM, no wiki dep,
    no network — pure string matching, safe on the hot path.

    The map insertion order is the priority order (Python dicts are ordered), so the
    consumer controls tie-breaking deterministically.
    """
    # Pre-compile one whole-word regex per keyword so 'ibkr' doesn't match inside a
    # larger token and matching stays O(text) per keyword.
    compiled = {
        topic: [re.compile(rf"(?<!\w){re.escape(kw.lower())}(?!\w)") for kw in kws]
        for topic, kws in dict(keyword_map).items()
    }

    def router(*, type, title, body, origin_session_id=None, caller_class=None):
        if type in _TOPIC_EXEMPT_TYPES:
            return None
        hay = f"{title or ''}\n{body or ''}".lower()
        for topic, patterns in compiled.items():
            if any(p.search(hay) for p in patterns):
                return topic
        return None

    return router


def _resolve_topic(*, topic, topic_router, type, title, body, origin_session_id,
                   caller_class):
    """Resolve the topic to persist (D3/D11). Precedence:
      1. operational types (user/feedback) → ALWAYS NULL (cross-topic, D11) —
         a hard invariant that overrides even an explicit topic= arg.
      2. explicit topic= arg (the writer knows its context, north-star §6.1).
      3. the generic fallback router, if one is enabled.
      4. None (the router abstained / none supplied) → NULL.
    No LLM. The router is a plain in-process callable."""
    if type in _TOPIC_EXEMPT_TYPES:
        return None
    if topic is not None:
        return topic
    if topic_router is not None:
        return topic_router(type=type, title=title, body=body,
                            origin_session_id=origin_session_id,
                            caller_class=caller_class)
    return None


def save_memory(conn, *, id, type, title, body, ts, origin_session_id=None,
                description=None, index_hook=None, node_type="memory",
                file_slug=None, sort_order=None, created_at=None, updated_at=None,
                topic=None, topic_router=None, genesis_hook=None, caller_class=None):
    """Upsert a memory through the redact chokepoint + audit. Returns id.

    `ts` is the action time (always the audit-row timestamp). `created_at`/
    `updated_at` default to `ts` but can be overridden so a bootstrap import can
    stamp the file's real age (mtime) — otherwise every imported memory looks
    freshly written and the §8 staleness signal never fires.

    `topic` (SP-3 Stage 2, D1/D3) is the topic string to persist. If omitted and a
    `topic_router` is supplied, the deterministic generic router assigns one (no
    LLM); if it abstains the topic stays NULL. user/feedback rows ALWAYS stay NULL
    (D11), even if a topic= is passed. `genesis_hook(topic)` is an OPTIONAL,
    injectable, best-effort callback (default no-op) the CONSUMER wires its topic
    genesis (e.g. wiki_topics.ensure_topic) into — fired only when a non-NULL topic
    is resolved; a raising hook never aborts the write (fail-open). The engine
    itself has NO wiki dependency."""
    title = strip_secrets(title)
    body = strip_secrets(body)
    description = strip_secrets(description)
    index_hook = strip_secrets(index_hook)
    # `is None` (not `or`): an explicit falsy override (e.g. epoch 0) must be kept,
    # otherwise the bootstrap mtime-stamping the override exists for is silently lost.
    created = ts if created_at is None else created_at
    updated = ts if updated_at is None else updated_at

    resolved_topic = _resolve_topic(
        topic=topic, topic_router=topic_router, type=type, title=title, body=body,
        origin_session_id=origin_session_id, caller_class=caller_class)

    def work():
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if prior is None:
            conn.execute(
                "INSERT INTO memories (id, type, title, body, description, index_hook, "
                "node_type, file_slug, sort_order, created_at, updated_at, "
                "origin_session_id, topic) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id, type, title, body, description, index_hook, node_type,
                 file_slug, sort_order, created, updated, origin_session_id,
                 resolved_topic),
            )
            _audit(conn, op="save", target_kind="memory", target_id=id,
                   reason="create", prior=None, ts=ts)
        else:
            conn.execute(
                "UPDATE memories SET type=?, title=?, body=?, description=?, "
                "index_hook=?, node_type=?, file_slug=?, sort_order=?, updated_at=?, "
                "topic=? WHERE id=?",
                (type, title, body, description, index_hook, node_type,
                 file_slug, sort_order, updated, resolved_topic, id),
            )
            _audit(conn, op="save", target_kind="memory", target_id=id,
                   reason="update", prior=dict(prior), ts=ts)

    _write_txn(conn, work, spool={
        "op": "save_memory", "id": id, "type": type, "title": title, "body": body,
        "ts": ts, "origin_session_id": origin_session_id, "description": description,
        "index_hook": index_hook, "node_type": node_type, "file_slug": file_slug,
        "sort_order": sort_order, "created_at": created, "updated_at": updated,
        # Spool the RESOLVED topic, not the router — replay must be deterministic and
        # the router/hook are in-process callables that don't serialize.
        "topic": resolved_topic})

    # Genesis fires only AFTER a successful, durable write and only for a non-NULL
    # topic. Best-effort + fail-open: the consumer's genesis (e.g. the wiki) is not
    # allowed to break the canonical memory write. The engine stays agnostic — it
    # neither knows nor cares what the hook does.
    if resolved_topic is not None and genesis_hook is not None:
        try:
            genesis_hook(resolved_topic)
        except Exception:
            pass
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
    # Key on the RAW (pre-redaction) text so a future redaction-rule change can't
    # shift the idempotency key and un-dedupe a replayed / re-imported event.
    key = _event_key(session_id, ts, kind, title, detail)
    title = strip_secrets(title)
    detail = strip_secrets(detail)
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


def set_pinned(conn, *, id, pinned, ts, reason="manual pin"):
    """Set/clear a memory's pinned flag. Pinned memories are injected into every
    SessionStart rehydration gist, so this is human-settable (spec §14). Audited."""
    def work():
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if prior is None:
            raise KeyError(f"set_pinned: no memory with id {id!r}")
        conn.execute("UPDATE memories SET pinned=? WHERE id=?", (1 if pinned else 0, id))
        _audit(conn, op="pin" if pinned else "unpin", target_kind="memory",
               target_id=id, reason=reason, prior=dict(prior), ts=ts)

    _write_txn(conn, work, spool={
        "op": "set_pinned", "id": id, "pinned": bool(pinned), "ts": ts, "reason": reason})


def set_verified(conn, *, id, ts, reason="manual verify"):
    """Stamp last_verified=ts (a human reconfirmed the memory is still true). Audited."""
    def work():
        prior = conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone()
        if prior is None:
            raise KeyError(f"set_verified: no memory with id {id!r}")
        conn.execute("UPDATE memories SET last_verified=? WHERE id=?", (ts, id))
        _audit(conn, op="verify", target_kind="memory", target_id=id,
               reason=reason, prior=dict(prior), ts=ts)

    _write_txn(conn, work, spool={"op": "set_verified", "id": id, "ts": ts, "reason": reason})


# ---------------------------------------------------------------------------
# The `links` cross-store edge spine (SP-3 Stage 3, D5/D6). `record_link` is the
# FIRST writer the `links` table ever gets (north-star Risk §14.8: defined + read
# via memory_query._links_for, but never written). The edge KEY is
# (src_kind, src_id, predicate, dst_kind, dst_id); the writer is idempotent on it
# (an upsert that refreshes the sub-types / evidence / confidence rather than
# duplicating the row). `links` has no UNIQUE on the key (0001:41 predates this and
# a UNIQUE add would need a destructive table rebuild), so idempotency is enforced
# in code: SELECT-then-UPDATE-or-INSERT inside the same BEGIN IMMEDIATE txn.
# ---------------------------------------------------------------------------

_LINK_KEY_COLS = ("src_kind", "src_id", "predicate", "dst_kind", "dst_id")


def record_link(conn, *, src_kind, src_id, predicate, dst_kind, dst_id,
                src_type=None, dst_type=None, evidence=None, confidence=None, ts):
    """Upsert a cross-store edge into `links` (idempotent on the edge key) + audit.

    The edge KEY is (src_kind, src_id, predicate, dst_kind, dst_id). Re-recording
    the same edge is a no-op/upsert — it refreshes `src_type`/`dst_type`/`evidence`/
    `confidence`/`created_at` on the existing row, never appends a duplicate. A
    different predicate (or any differing key field) is a DISTINCT edge.

    `src_kind`/`dst_kind` are the store side (`memory`/`knowledge`); `src_type`/
    `dst_type` (migration 0004) are the within-kind sub-type (e.g. `feedback`,
    `mechanism`) so a reader can filter without a join. Through the redact/audit
    chokepoint + `_write_txn` (BEGIN IMMEDIATE + bounded retry + durable spool) like
    every other write; registered in `replay_spool`'s dispatch.
    """
    evidence = strip_secrets(evidence)

    def work():
        key_vals = (src_kind, src_id, predicate, dst_kind, dst_id)
        where = " AND ".join(f"{c}=?" for c in _LINK_KEY_COLS)
        prior = conn.execute(
            f"SELECT rowid, * FROM links WHERE {where}", key_vals).fetchone()
        if prior is None:
            conn.execute(
                "INSERT INTO links (src_kind, src_id, src_type, predicate, "
                "dst_kind, dst_id, dst_type, evidence, confidence, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (src_kind, src_id, src_type, predicate, dst_kind, dst_id,
                 dst_type, evidence, confidence, ts),
            )
        else:
            conn.execute(
                f"UPDATE links SET src_type=?, dst_type=?, evidence=?, "
                f"confidence=?, created_at=? WHERE {where}",
                (src_type, dst_type, evidence, confidence, ts, *key_vals),
            )
        # Audit against the source unit (the edge "belongs" to its source).
        _audit(conn, op="link", target_kind=src_kind, target_id=src_id,
               reason=f"{predicate} -> {dst_kind}:{dst_id}",
               prior=dict(prior) if prior is not None else None, ts=ts)

    _write_txn(conn, work, spool={
        "op": "record_link", "src_kind": src_kind, "src_id": src_id,
        "src_type": src_type, "predicate": predicate, "dst_kind": dst_kind,
        "dst_id": dst_id, "dst_type": dst_type, "evidence": evidence,
        "confidence": confidence, "ts": ts})


def mirror_cross_store_links(conn, wiki_edges, *, ts):
    """Lift CROSS-STORE wiki-graph edges into `links` (D6) via `record_link`.

    PROJECT-AGNOSTIC (hard NFR): `wiki_edges` is a CONSUMER-FED iterable — the
    consumer (Trading) reads them out of its OWN `wiki/graph/graph.sqlite` and hands
    them in. The engine NEVER opens / imports that DB (the agnostic-import test stays
    green). Each edge is a mapping with keys: `src_kind`, `src_id`, `predicate`,
    `dst_kind`, `dst_id`, and optionally `src_type`/`dst_type`/`evidence`/
    `confidence`.

    Only edges that CROSS stores (one side `memory`, the other `knowledge`) are
    mirrored. A pure wiki<->wiki edge (both sides `knowledge`) is skipped — those
    stay in the consumer's `graph.sqlite` (D6). In practice the consumer feeds only
    cross-store edges; the skip is defense-in-depth. Idempotent (each lift goes
    through the idempotent `record_link`). Returns
    {mirrored, skipped_wiki_internal}.
    """
    summary = {"mirrored": 0, "skipped_wiki_internal": 0}
    for e in wiki_edges:
        sk, dk = e["src_kind"], e["dst_kind"]
        crosses = ("memory" in (sk, dk)) and ("knowledge" in (sk, dk))
        if not crosses:
            summary["skipped_wiki_internal"] += 1
            continue
        record_link(
            conn, src_kind=sk, src_id=e["src_id"], predicate=e["predicate"],
            dst_kind=dk, dst_id=e["dst_id"],
            src_type=e.get("src_type"), dst_type=e.get("dst_type"),
            evidence=e.get("evidence"), confidence=e.get("confidence"), ts=ts)
        summary["mirrored"] += 1
    return summary


_BACKFILL_FLAG = "topic_backfill_complete"
# Operational rows are cross-topic by nature (OAuth-only / commit-proactively apply
# in every topic — D11) → they stay topic=NULL so the §5 topic wall renders them
# visible regardless of an agent's binding (the type-scope still hides them from
# subagents). Topic + type stay orthogonal.
_TOPIC_EXEMPT_TYPES = ("user", "feedback")


def backfill_topic(conn, *, default_topic, ts, reason="topic backfill (D4)"):
    """Stamp `topic = default_topic` on every existing `memories` row whose topic is
    NULL and whose type is NOT operational (D11 keeps user/feedback rows NULL). The
    default topic is consumer-supplied (content-free in the engine: Trading → 'trading').

    Guarded + idempotent: a `meta.topic_backfill_complete` flag short-circuits a
    re-run (mirrors `import_complete`). Audited per stamped row. Reversible — the
    git-tracked export + audit_log + clearing the flag undo it. Returns a summary
    dict: {stamped, skipped_already_complete}.

    NOTE: a one-time touch of the live canonical store — gated on Peter's sign-off
    (spec §10). NEVER run on a live DB without that gate; the suite runs it on tmp DBs.
    """
    if not default_topic:
        raise ValueError("backfill_topic: default_topic must be a non-empty string")

    flag = conn.execute(
        "SELECT value FROM meta WHERE key=?", (_BACKFILL_FLAG,)).fetchone()
    if flag is not None and str(flag[0]) == "1":
        return {"stamped": 0, "skipped_already_complete": True}

    summary = {"stamped": 0, "skipped_already_complete": False}

    def work():
        placeholders = ",".join("?" * len(_TOPIC_EXEMPT_TYPES))
        # Snapshot the rows we are about to touch (for per-row audit prior-state) so
        # the change is fully reconstructable from audit_log.
        targets = conn.execute(
            f"SELECT * FROM memories WHERE topic IS NULL "
            f"AND type NOT IN ({placeholders})",
            tuple(_TOPIC_EXEMPT_TYPES),
        ).fetchall()
        for row in targets:
            conn.execute(
                "UPDATE memories SET topic=? WHERE id=?", (default_topic, row["id"]))
            _audit(conn, op="backfill_topic", target_kind="memory",
                   target_id=row["id"], reason=reason, prior=dict(row), ts=ts)
        summary["stamped"] = len(targets)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_BACKFILL_FLAG,))

    _write_txn(conn, work, spool={
        "op": "backfill_topic", "default_topic": default_topic, "ts": ts,
        "reason": reason})
    return summary


def replay_spool(conn, *, spool_dir=None):
    """Drain memory_spool/: re-apply each spooled write (a prior SQLITE_BUSY casualty)
    via its op, deleting the file on success. A still-failing op re-spools to the SAME
    content-hash file (no duplicate) and is left in place + recorded. Unknown/corrupt
    records are kept (never silently dropped). Returns {replayed, failed, errors}."""
    import inspect

    target = Path(spool_dir) if spool_dir is not None else _spool_dir(conn)
    summary = {"replayed": 0, "failed": 0, "errors": []}
    if target is None or not target.is_dir():
        return summary

    dispatch = {
        "save_memory": save_memory, "record_session_event": record_session_event,
        "record_access": record_access, "consolidate": consolidate, "delete": delete,
        "set_pinned": set_pinned, "set_verified": set_verified,
        "backfill_topic": backfill_topic, "record_link": record_link,
    }
    for f in sorted(target.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            fn = dispatch.get(rec.get("op"))
            if fn is None:
                raise ValueError(f"unknown spooled op {rec.get('op')!r}")
            accepted = set(inspect.signature(fn).parameters)
            kwargs = {k: v for k, v in rec.items() if k != "op" and k in accepted}
            fn(conn, **kwargs)
            f.unlink()
            summary["replayed"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{f.name}: {exc!r}")
    return summary
