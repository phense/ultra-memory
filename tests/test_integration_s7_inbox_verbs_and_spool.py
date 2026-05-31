"""Integration tests for seam S7 — inbox verbs + write-spool/replay (A9).

Exercises the *real* interplay between three modules through their production
seam, with no unit-level mocking of the code under test:

    memory_inbox.import_inbox()         (parse `verify <id>` directive lines)
        -> memory_lib.set_verified()    (the contended write verb)
            -> memory_lib._write_txn()  (BEGIN IMMEDIATE + bounded retry)
                -> memory_lib._spool()  (WriteSpooled on retry exhaustion)
    memory_lib.replay_spool()           (drain the spool back into the DB)

Determinism technique (no threads, no network, no LLM; bounded backoff only):

* A *second* real sqlite3 connection holds an open ``BEGIN IMMEDIATE`` write
  transaction (with an actual UPDATE to pin the WAL write lock) on the same
  temp DB, so every competing ``BEGIN IMMEDIATE`` from the code under test
  gets SQLITE_BUSY.
* The working connection is dropped to ``PRAGMA busy_timeout=0`` so the busy
  error surfaces immediately instead of waiting out the busy_timeout that
  ``db.connect`` installs — this is what makes the 5 retries run fast.
* The public write verbs (``set_verified``) do NOT expose an injectable sleep,
  and ``_write_txn`` binds ``sleep=time.sleep`` as a *default value* at import
  (so a ``monkeypatch`` of ``memory_lib.time.sleep`` does not reach it). The
  retry backoff is therefore the real but tiny bounded sum
  ``0.05 * (1+2+4+8) = 0.75 s`` per spooling op — sub-second and deterministic,
  not a wall-clock race. The no-op ``time.sleep`` patch is kept to document
  intent and to stay correct if the product ever switches to call-time lookup.

Hermeticity is automatic: ``_spool_dir(conn)`` derives the spool directory as
``<dir-of-the-main-db-file>/memory_spool``. Because every test opens its DB at
``tmp_path / "m.db"``, the spool lands under ``tmp_path`` and the real
``data/memory_spool`` is never touched. (This mirrors the existing
``tests/test_memory_spool_replay.py`` convention of a ``tmp_path`` DB.)
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from pathlib import Path

import pytest

from ultra_memory import memory_inbox, memory_lib


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_embed(monkeypatch):
    """Keep writes hermetic + fast: never load the embedding model."""
    monkeypatch.setenv("ULTRA_MEMORY_NO_EMBED", "1")


@pytest.fixture
def conn(tmp_path):
    """An open, migrated memory.db at tmp_path/m.db with one seeded memory.

    The spool dir is tmp_path/memory_spool (derived by _spool_dir), so this is
    self-contained without touching the real data/ tree.
    """
    c = memory_lib.open_memory_db(tmp_path / "m.db")
    memory_lib.save_memory(
        c, id="foo-1", type="project", title="seed", body="body",
        ts="2026-05-01T00:00:00",
    )
    c.commit()
    yield c
    c.close()


@pytest.fixture
def no_backoff(monkeypatch):
    """Replace the module-level sleep the retry loop calls with a no-op."""
    monkeypatch.setattr(memory_lib.time, "sleep", lambda s: None)


def _lock_holder(tmp_path: Path) -> sqlite3.Connection:
    """A 2nd connection holding an exclusive write lock until released.

    An actual UPDATE inside BEGIN IMMEDIATE pins the WAL write lock, so any
    other writer's BEGIN IMMEDIATE gets SQLITE_BUSY.
    """
    holder = sqlite3.connect(str(tmp_path / "m.db"))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE memories SET title = 'held' WHERE id = 'foo-1'")
    return holder


def _spool_dir(conn: sqlite3.Connection) -> Path:
    return memory_lib._spool_dir(conn)


def _spool_files(conn: sqlite3.Connection) -> list[str]:
    sd = _spool_dir(conn)
    if sd is None or not sd.exists():
        return []
    return sorted(os.path.basename(f) for f in glob.glob(str(sd) + "/*.json"))


def _last_verified(conn: sqlite3.Connection, mem_id: str = "foo-1"):
    return conn.execute(
        "SELECT last_verified FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]


# --------------------------------------------------------------------------
# Test 1 — the production half of A9: set_verified under lock spools + raises
# --------------------------------------------------------------------------


def test_busy_write_spools_and_raises(conn, tmp_path, no_backoff):
    """A held write lock makes set_verified exhaust retries -> WriteSpooled.

    Asserts the spool side-effects deterministically: exactly one content-hash
    JSON exists and its decoded payload is the verify op for this id/ts.
    """
    holder = _lock_holder(tmp_path)
    conn.execute("PRAGMA busy_timeout=0")
    try:
        with pytest.raises(memory_lib.WriteSpooled):
            memory_lib.set_verified(conn, id="foo-1", ts="2026-06-01T00:00:00")

        files = _spool_files(conn)
        assert len(files) == 1

        payload = json.loads((_spool_dir(conn) / files[0]).read_text(encoding="utf-8"))
        assert payload["op"] == "set_verified"
        assert payload["id"] == "foo-1"
        assert payload["ts"] == "2026-06-01T00:00:00"
    finally:
        holder.rollback()
        holder.close()

    # authoritative read after the lock is released: the write did NOT land
    assert _last_verified(conn) is None


# --------------------------------------------------------------------------
# Test 2 — S7 + A9 end to end: inbox verify spools, then replay lands it
# --------------------------------------------------------------------------


def test_inbox_verify_under_busy_spools_then_replay_lands_it(
    conn, tmp_path, no_backoff
):
    """Full seam: import_inbox(verify) under contention -> spool; replay drains.

    1. Drop a ``verify foo-1`` directive line in the inbox file.
    2. Hold the write lock, run import_inbox -> the spooled write is durably
       written, last_verified still NULL, exactly one spool file exists.
    3. Release the lock and replay_spool -> the verify lands (last_verified set)
       and the spool drains.

    NOTE on the inbox summary: ``import_inbox`` does not special-case
    ``WriteSpooled`` — it is caught by the generic ``except Exception`` and the
    op is reported under ``summary["errors"]`` (NOT ``applied``), even though the
    write was durably spooled and is recoverable. We assert that *observed*
    behavior here; the observability gap is flagged in bugsFound for human
    review (no data is lost — the spool + replay make it eventually consistent).
    """
    inbox = tmp_path / "inbox.md"
    inbox.write_text("verify foo-1\n", encoding="utf-8")

    holder = _lock_holder(tmp_path)
    conn.execute("PRAGMA busy_timeout=0")
    try:
        summary = memory_inbox.import_inbox(conn, inbox, ts="2026-06-01T00:00:00")
    finally:
        # keep the lock until after we have inspected the spooled state
        spooled_files = _spool_files(conn)
        lv_during = _last_verified(conn)
        holder.rollback()
        holder.close()

    # the spooled write was NOT applied and NOT counted as "applied"
    assert summary["applied"] == 0
    # WriteSpooled surfaces as an error entry (the observability gap, see NOTE)
    assert len(summary["errors"]) == 1
    assert "foo-1" in summary["errors"][0]
    # but it WAS durably spooled and the DB was not mutated
    assert len(spooled_files) == 1
    assert lv_during is None

    # --- release contention, replay drains the spool into the DB ---
    replay = memory_lib.replay_spool(conn)
    assert replay["replayed"] == 1
    assert replay["failed"] == 0
    assert _last_verified(conn) is not None
    assert _spool_files(conn) == []


# --------------------------------------------------------------------------
# Test 3 — replay re-entrancy: still-busy replay re-spools, no duplicate
# --------------------------------------------------------------------------


def test_replay_reentry_still_busy_respools_not_duplicates(
    conn, tmp_path, no_backoff
):
    """A still-failing replay counts as failed, keeps the SAME file, no dupes.

    Verifies the replay_spool docstring's re-entrancy promise: a replay against
    a still-locked DB must re-spool to the identical content-hash path
    (failed==1, replayed==0, one file, not unlinked), and a later unobstructed
    replay must then land it and drain the spool.
    """
    holder = _lock_holder(tmp_path)
    conn.execute("PRAGMA busy_timeout=0")

    # --- produce exactly one spool file via a contended write ---
    with pytest.raises(memory_lib.WriteSpooled):
        memory_lib.set_verified(conn, id="foo-1", ts="2026-06-01T00:00:00")
    initial = _spool_files(conn)
    assert len(initial) == 1

    # --- replay while STILL busy: re-spool, do not duplicate or drain ---
    # The same conn (busy_timeout=0) is used internally by replay_spool, so the
    # replayed set_verified hits SQLITE_BUSY again and re-raises WriteSpooled,
    # which replay_spool catches as a failure and leaves the file in place.
    busy_replay = memory_lib.replay_spool(conn)
    assert busy_replay["replayed"] == 0
    assert busy_replay["failed"] == 1
    # same single content-hash file — re-spool overwrote in place, no duplicate
    assert _spool_files(conn) == initial
    assert _last_verified(conn) is None

    # --- release the lock; a fresh replay now lands and drains ---
    holder.rollback()
    holder.close()

    ok_replay = memory_lib.replay_spool(conn)
    assert ok_replay["replayed"] == 1
    assert ok_replay["failed"] == 0
    assert _spool_files(conn) == []
    assert _last_verified(conn) is not None
