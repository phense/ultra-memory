"""SQLite-backed embedding cache for wiki ingest + consolidation pipelines.

Replaces scripts/.embed_cache.json (a 10 MB monolith at n≈1159 entries on
2026-05-28). Two reasons for the switch:

  1. Size — JSON-stringified float arrays are ~5× larger than raw
     float32 bytes. Migrating to BLOB cuts the on-disk footprint from
     ~10 MB to ~2 MB at current scale, and scales linearly instead of
     pathologically as the wiki grows past ~3k atomics.
  2. Correctness — the old `tmp + os.replace()` atomic-write pattern
     mitigated, but did not eliminate, race conditions between
     overlapping ingest and consolidation runs. SQLite WAL gives
     lock-free concurrent reads and serialized writes with retry
     semantics via busy_timeout. The `model_name` column auto-
     invalidates vectors on embedding-model swap (e.g. upgrade from
     BAAI/bge-small-en-v1.5 to bge-large), which was a silent-
     poisoning class of bug under the old cache.

Two tables in one DB file:

  * wiki_atomic_embeddings — path-keyed, sha256-validated. Used by
    scripts/youtube_to_wiki.py for the atomic-mechanism dedup pool.
  * text_embeddings — text-keyed (the text itself IS the key, so
    text changes naturally produce new cache entries; no sha256 needed).
    Used by scripts/consolidate_paraphrase_dup_atomics.py for transient
    candidate-text embeddings.

API split intentionally — the two use cases have different semantics
(sha256-validated file content vs. text-as-identity) and bundling them
into one `put`/`get` would force one to lie about its contract.
"""

from __future__ import annotations

import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


# Default DB path lives in the global ultra-memory store.
# All public functions accept db_path= overrides; this default is only
# used when no consumer passes an explicit path.
DB_PATH = Path.home() / ".ultra-memory" / "wiki_embeds.db"

EMBED_DIM = 384  # BAAI/bge-small-en-v1.5; must match scripts/youtube_to_wiki.py:EMBED_DIM


JUDGE_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS judge_decisions (
    pair_key              TEXT PRIMARY KEY,
    sha_a                 TEXT NOT NULL,
    sha_b                 TEXT NOT NULL,
    verdict               TEXT NOT NULL,
    shared_mechanism      TEXT,
    distinguishing_factor TEXT,
    model_name            TEXT NOT NULL,
    cosine_sim            REAL NOT NULL,
    called_at             TEXT NOT NULL,
    claim_a_preview       TEXT,
    claim_b_preview       TEXT
);
CREATE INDEX IF NOT EXISTS idx_judge_called_at ON judge_decisions(called_at);
CREATE INDEX IF NOT EXISTS idx_judge_model     ON judge_decisions(model_name);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_atomic_embeddings (
    path        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    embed_dim   INTEGER NOT NULL,
    model_name  TEXT NOT NULL,
    updated_at  REAL DEFAULT (julianday('now'))
) STRICT;

CREATE INDEX IF NOT EXISTS wae_by_model
    ON wiki_atomic_embeddings(model_name);

CREATE TABLE IF NOT EXISTS text_embeddings (
    text        TEXT NOT NULL,
    model_name  TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    embed_dim   INTEGER NOT NULL,
    updated_at  REAL DEFAULT (julianday('now')),
    PRIMARY KEY (text, model_name)
) STRICT;
"""


def init_db(db_path: Path = DB_PATH) -> None:
    """Create both tables (idempotent) and set WAL journal mode.

    WAL is a persistent PRAGMA stored in the SQLite header — set once,
    every subsequent open uses it. Required because the daily
    wiki-maintenance cron + YouTube ingest cron can run concurrently
    against this DB.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.executescript(JUDGE_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()


def _is_missing_table(exc: sqlite3.OperationalError) -> bool:
    """True iff this OperationalError is SQLite's 'no such table: <name>'.

    The cold-read path (r3 FIX 4): on a first run where the DB file does not yet
    exist, sqlite3.connect creates an EMPTY file (no schema) and a SELECT raises
    'no such table'. The read primitives treat that as an EMPTY cache (return
    {} / 0 / None) — consistent with the module's existing 'miss/corruption →
    re-embed' contract — instead of crashing. A read NEVER runs DDL (no write
    side-effect on a read path); only the heavy write entry points call init_db.
    """
    return "no such table" in str(exc).lower()


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection, set busy_timeout, guarantee close on exit.

    `with sqlite3.connect(...) as c` commits on exit but does NOT close
    the connection — that leaks file descriptors across many calls and
    blocks WAL checkpoints. This wrapper is the discipline boundary.

    busy_timeout=30000ms means a writer waiting on the write-lock will
    retry for 30s before raising SQLITE_BUSY — enough headroom for the
    longest realistic batch-embed transaction (~1s on 250 atomics).
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        yield conn
    finally:
        conn.close()


# ---------- BLOB encoding ---------------------------------------------------


def _pack(vec: list[float]) -> bytes:
    """List[float] → bytes (float32, little-endian). Caller guarantees
    `len(vec) == EMBED_DIM`."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes, expected_dim: int = EMBED_DIM) -> list[float] | None:
    """bytes → List[float], or None if length doesn't match.

    Length-mismatch returns None rather than raising so a single
    corrupted BLOB (truncated write, manual edit, model-dim drift)
    can't crash a batch load. youtube_to_wiki re-embeds anything that
    comes back as None.
    """
    if len(blob) != expected_dim * 4:
        return None
    return list(struct.unpack(f"<{expected_dim}f", blob))


# ---------- atomic (path-keyed) API -----------------------------------------


def put_atomic(
    path: str,
    sha256: str,
    vec: list[float],
    model_name: str,
    db_path: Path = DB_PATH,
) -> None:
    """Upsert one atomic-mechanism embedding."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO wiki_atomic_embeddings(path, sha256, embedding, embed_dim, model_name) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  sha256=excluded.sha256, "
            "  embedding=excluded.embedding, "
            "  embed_dim=excluded.embed_dim, "
            "  model_name=excluded.model_name, "
            "  updated_at=julianday('now')",
            (path, sha256, _pack(vec), len(vec), model_name),
        )
        conn.commit()


def put_many_atomics(
    rows: Iterable[tuple[str, str, list[float]]],
    model_name: str,
    db_path: Path = DB_PATH,
) -> None:
    """Bulk upsert. `rows` is an iterable of (path, sha256, vec).

    Single transaction → atomic across the whole batch and ~10× faster
    than per-row commits at n=250.
    """
    payload = [(p, s, _pack(v), len(v), model_name) for (p, s, v) in rows]
    if not payload:
        return
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO wiki_atomic_embeddings(path, sha256, embedding, embed_dim, model_name) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  sha256=excluded.sha256, "
            "  embedding=excluded.embedding, "
            "  embed_dim=excluded.embed_dim, "
            "  model_name=excluded.model_name, "
            "  updated_at=julianday('now')",
            payload,
        )
        conn.commit()


def get_atomic(
    path: str,
    model_name: str,
    db_path: Path = DB_PATH,
) -> tuple[str, list[float]] | None:
    """Single-row lookup: returns (sha256, vec) or None on miss/corruption.

    O(1) point query — use this in any path that doesn't already need
    the full cache. The bulk-load path (load_all_atomics) is reserved
    for batch-embedding scans where the full table is needed anyway.
    """
    with _connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT sha256, embedding, embed_dim FROM wiki_atomic_embeddings "
                "WHERE path = ? AND model_name = ?",
                (path, model_name),
            ).fetchone()
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return None  # cold cache → miss → caller re-embeds
            raise
    if row is None:
        return None
    sha, blob, embed_dim = row
    vec = _unpack(blob, expected_dim=embed_dim)
    if vec is None:
        return None
    return (sha, vec)


def load_all_atomics(
    model_name: str,
    db_path: Path = DB_PATH,
) -> dict[str, tuple[str, list[float]]]:
    """Return {path: (sha256, vec)} for all rows matching `model_name`.

    Corrupted BLOBs (wrong byte length) are silently skipped — the caller
    treats a missing path as "needs re-embed", which is the correct
    recovery action.
    """
    out: dict[str, tuple[str, list[float]]] = {}
    with _connect(db_path) as conn:
        try:
            cur = conn.execute(
                "SELECT path, sha256, embedding, embed_dim "
                "FROM wiki_atomic_embeddings WHERE model_name = ?",
                (model_name,),
            )
            for path, sha256, blob, embed_dim in cur:
                vec = _unpack(blob, expected_dim=embed_dim)
                if vec is None:
                    continue
                out[path] = (sha256, vec)
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return {}  # cold cache → empty pool → caller re-embeds all
            raise
    return out


def count_atomics(model_name: str, db_path: Path = DB_PATH) -> int:
    with _connect(db_path) as conn:
        try:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM wiki_atomic_embeddings WHERE model_name = ?",
                (model_name,),
            ).fetchone()
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return 0  # cold cache → empty
            raise
    return int(n)


# ---------- text-keyed API --------------------------------------------------


def put_text_vec(
    text: str,
    vec: list[float],
    model_name: str,
    db_path: Path = DB_PATH,
) -> None:
    """Upsert one text-keyed embedding. (text, model_name) is the
    compound primary key."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO text_embeddings(text, model_name, embedding, embed_dim) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(text, model_name) DO UPDATE SET "
            "  embedding=excluded.embedding, "
            "  embed_dim=excluded.embed_dim, "
            "  updated_at=julianday('now')",
            (text, model_name, _pack(vec), len(vec)),
        )
        conn.commit()


def get_text_vec(
    text: str,
    model_name: str,
    db_path: Path = DB_PATH,
) -> list[float] | None:
    """Return the cached embedding for `text` under `model_name`, or None
    on miss/corruption."""
    with _connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT embedding, embed_dim FROM text_embeddings "
                "WHERE text = ? AND model_name = ?",
                (text, model_name),
            ).fetchone()
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return None  # cold cache → miss → caller re-embeds
            raise
    if row is None:
        return None
    blob, embed_dim = row
    return _unpack(blob, expected_dim=embed_dim)


def load_all_texts(
    model_name: str,
    db_path: Path = DB_PATH,
) -> dict[str, list[float]]:
    """Return {text: vec} for all rows under `model_name`. Mirrors
    load_all_atomics; consolidate_paraphrase_dup_atomics.py primes its
    in-memory cache from this at the start of a run.

    Corrupted BLOBs silently skipped.
    """
    out: dict[str, list[float]] = {}
    with _connect(db_path) as conn:
        try:
            cur = conn.execute(
                "SELECT text, embedding, embed_dim FROM text_embeddings "
                "WHERE model_name = ?",
                (model_name,),
            )
            for text, blob, embed_dim in cur:
                vec = _unpack(blob, expected_dim=embed_dim)
                if vec is None:
                    continue
                out[text] = vec
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return {}  # cold cache → empty pool → caller re-embeds all
            raise
    return out


def count_texts(model_name: str, db_path: Path = DB_PATH) -> int:
    with _connect(db_path) as conn:
        try:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM text_embeddings WHERE model_name = ?",
                (model_name,),
            ).fetchone()
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return 0  # cold cache → empty
            raise
    return int(n)


# ---------- judge-decision API ---------------------------------------------


def put_judge_decision(
    *,
    pair_key: str,
    sha_a: str,
    sha_b: str,
    verdict: str,
    shared_mechanism: str | None,
    distinguishing_factor: str | None,
    model_name: str,
    cosine_sim: float,
    claim_a_preview: str | None,
    claim_b_preview: str | None,
    db_path: Path = DB_PATH,
) -> None:
    """Upsert one judge verdict. called_at is stamped now (UTC ISO-8601)."""
    from datetime import datetime, timezone
    called_at = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO judge_decisions(pair_key, sha_a, sha_b, verdict, "
            "shared_mechanism, distinguishing_factor, model_name, cosine_sim, "
            "called_at, claim_a_preview, claim_b_preview) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pair_key) DO UPDATE SET "
            "  verdict=excluded.verdict, shared_mechanism=excluded.shared_mechanism, "
            "  distinguishing_factor=excluded.distinguishing_factor, "
            "  model_name=excluded.model_name, cosine_sim=excluded.cosine_sim, "
            "  called_at=excluded.called_at",
            (pair_key, sha_a, sha_b, verdict, shared_mechanism, distinguishing_factor,
             model_name, cosine_sim, called_at, claim_a_preview, claim_b_preview),
        )
        conn.commit()


def get_judge_decision(pair_key: str, db_path: Path = DB_PATH) -> dict | None:
    """Return the verdict row as a dict, or None on miss.

    Model-agnostic lookup (verdicts are treated as stable across model
    versions — see spec 'Model invalidation policy').
    """
    with _connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT pair_key, sha_a, sha_b, verdict, shared_mechanism, "
                "distinguishing_factor, model_name, cosine_sim, called_at, "
                "claim_a_preview, claim_b_preview FROM judge_decisions WHERE pair_key = ?",
                (pair_key,),
            ).fetchone()
        except sqlite3.OperationalError as e:
            if _is_missing_table(e):
                return None  # cold cache → miss
            raise
    if row is None:
        return None
    cols = ("pair_key", "sha_a", "sha_b", "verdict", "shared_mechanism",
            "distinguishing_factor", "model_name", "cosine_sim", "called_at",
            "claim_a_preview", "claim_b_preview")
    return dict(zip(cols, row))
