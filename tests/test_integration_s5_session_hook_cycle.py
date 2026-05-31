"""Integration tests for seam S5-session-hook-cycle.

These exercise the real coupling between the two session hooks
(``ultra_memory.hooks.checkpoint`` + ``ultra_memory.hooks.rehydrate``),
``ultra_memory.hooks.common`` (the db-ready / role gates), and
``ultra_memory.memory_lib`` (the SQLite store) — through their real seams,
not unit mocks. Everything is hermetic: a temp SQLite DB per test, explicit
timestamps, no network, and no real ``claude`` CLI invocation (the session
hooks are deliberately LLM-free, so there is nothing to stub).

The repo ships no shared ``conftest.py`` / ``tmp_db`` fixture — the existing
hook tests (``tests/test_rehydrate.py`` / ``tests/test_checkpoint.py``) build a
ready DB inline with a local helper. We reuse exactly that pattern here (the
``_ready_db`` / ``_write_transcript`` helpers below mirror them) so the suite
stays consistent.

G9 note: the task forbids editing the shared conftest (none exists) and to keep
this a single NEW file, the env-scrubbing autouse fixture lives module-scoped
here rather than in a new ``tests/conftest.py``.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from ultra_memory import memory_lib
from ultra_memory.hooks import checkpoint, common, rehydrate


# ---------------------------------------------------------------------------
# G9 — env hermeticity. Strip every env var the run()/main() paths consult so
# a stray ambient value can never flip a test green or red. Autouse =>
# every test in this module starts from a clean slate.
# ---------------------------------------------------------------------------
_ENV_KEYS = (
    "ULTRA_MEMORY_DB",
    "ULTRA_MEMORY_SHADOW",
    "ULTRA_MEMORY_SHADOW_OUT",
    "ULTRA_MEMORY_REHYDRATE_BUDGET",
    "ULTRA_MEMORY_AGENT_ROLE",
)


@pytest.fixture(autouse=True)
def _hermetic_env():
    # Use a dedicated MonkeyPatch context (NOT the test's `monkeypatch` fixture)
    # so this scrub's teardown is fully independent of any setenv/delenv the test
    # body does on its own `monkeypatch`. Sharing one instance creates a
    # restore-ordering quirk that, because `main()` reads os.environ live, can
    # surface stale env between tests.
    mp = pytest.MonkeyPatch()
    for key in _ENV_KEYS:
        mp.delenv(key, raising=False)
    yield
    mp.undo()


# ---------------------------------------------------------------------------
# helpers (mirror tests/test_rehydrate.py + tests/test_checkpoint.py)
# ---------------------------------------------------------------------------
def _open_db(tmp_path) -> tuple[Path, "object"]:
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    return p, conn


def _stamp_ready(conn) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('import_complete','1')"
    )
    conn.commit()
    # Force a WAL checkpoint so a *separately-opened* reader connection (e.g. the
    # one rehydrate.main()/run() opens) is guaranteed to see these committed rows.
    # Without this, heavy same-process WAL connection churn from earlier tests can
    # leave a fresh reader on a stale snapshot -> empty gist -> no shadow write.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()


def _write_transcript(tmp_path, events) -> Path:
    p = tmp_path / "t.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def _tool_use(name, inp):
    return {"message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _create_result(task_id, subject):
    return {"message": {"content": [{
        "type": "tool_result", "tool_use_id": f"toolu_{task_id}",
        "content": f"Task #{task_id} created successfully: {subject}"}]}}


def _task_update(task_id, **inp):
    return _tool_use("TaskUpdate", {"taskId": str(task_id), **inp})


# ---------------------------------------------------------------------------
# G1 — checkpoint writes -> rehydrate surfaces, via the real summary-NULL ->
# Recent-activity branch. checkpoint + rehydrate + common + memory_lib, one DB.
# ---------------------------------------------------------------------------
def test_checkpoint_then_rehydrate_surfaces_recent_activity(tmp_path):
    p, conn = _open_db(tmp_path)
    _stamp_ready(conn)
    conn.close()

    transcript = _write_transcript(tmp_path, [
        _create_result(1, "Ship the S5 integration suite"),
        _task_update(1, status="completed"),
    ])

    # Checkpoint records the completed task as a session_event.
    out = checkpoint.run(
        {"session_id": "sess-1", "transcript_path": str(transcript)},
        db_path=p, ts="2026-05-30T16:00:00Z",
    )
    assert out == {}  # never blocks

    # No session summary set => build_gist must take the recent_session_events
    # branch (rehydrate.py:37-42) — couples checkpoint's write to that branch.
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                        ts="2026-05-30T16:05:00Z")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Recent activity" in ctx
    assert "Ship the S5 integration suite" in ctx


# ---------------------------------------------------------------------------
# G3 — record_access ordering feeds the gist "Hot memories" section.
# Couples memory_lib.record_access (access_count++) -> build_gist ordering.
# ---------------------------------------------------------------------------
def test_hot_memories_ordered_by_access_count(tmp_path):
    p, conn = _open_db(tmp_path)
    memory_lib.save_memory(conn, id="cold", type="note", title="COLD-rarely-touched",
                           body="b", ts="2026-05-01T00:00:00Z")
    memory_lib.save_memory(conn, id="hot", type="note", title="HOT-frequently-touched",
                           body="b", ts="2026-05-01T00:00:00Z")
    # Bump the "hot" one several times with explicit ts (deterministic).
    for i in range(5):
        memory_lib.record_access(conn, target_kind="memory", target_id="hot",
                                 ts=f"2026-05-02T00:00:0{i}Z")
    # Touch the cold one once so both are non-zero but ordering is unambiguous.
    memory_lib.record_access(conn, target_kind="memory", target_id="cold",
                             ts="2026-05-02T00:00:00Z")

    gist = rehydrate.build_gist(conn)
    conn.close()

    assert "## Hot memories" in gist
    hot_section = gist.split("## Hot memories", 1)[1]
    assert "HOT-frequently-touched" in hot_section
    assert "COLD-rarely-touched" in hot_section
    assert hot_section.index("HOT-frequently-touched") < hot_section.index(
        "COLD-rarely-touched"
    ), "higher access_count must sort first in Hot memories"


# ---------------------------------------------------------------------------
# G4 — resolved follow-ups are excluded from the gist's Open follow-ups.
# Couples the resolved=0 filter (rehydrate.py:44-50) to the DB state.
# ---------------------------------------------------------------------------
def test_resolved_followup_excluded_from_gist(tmp_path):
    p, conn = _open_db(tmp_path)
    memory_lib.record_session_event(conn, session_id="s1", kind="followup",
                                    title="OPEN-wire-the-cron", ts="2026-05-29T10:00:00Z")
    done_key = memory_lib.record_session_event(
        conn, session_id="s1", kind="followup",
        title="DONE-ship-the-export", ts="2026-05-29T11:00:00Z")
    conn.execute("UPDATE session_events SET resolved = 1 WHERE event_key = ?",
                 (done_key,))
    conn.commit()

    gist = rehydrate.build_gist(conn)
    conn.close()

    assert "## Open follow-ups" in gist
    followup_section = gist.split("## Open follow-ups", 1)[1]
    # Stop the slice at the next section so we don't catch a title that also
    # shows up under "## Recent activity".
    followup_section = followup_section.split("\n\n", 1)[0]
    assert "OPEN-wire-the-cron" in followup_section
    assert "DONE-ship-the-export" not in followup_section


# ---------------------------------------------------------------------------
# G5 — rehydrate.main(stdin, stdout) stdin/env/stdout hook contract.
# ---------------------------------------------------------------------------
def test_rehydrate_main_live_emits_sessionstart_contract(tmp_path, monkeypatch):
    p, conn = _open_db(tmp_path)
    memory_lib.save_memory(conn, id="r6", type="feedback", title="STDIN-pinned-rule",
                           body="OAuth only, never the API.", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    _stamp_ready(conn)
    conn.close()

    monkeypatch.setenv("ULTRA_MEMORY_DB", str(p))
    monkeypatch.setenv("ULTRA_MEMORY_SHADOW", "0")  # live

    stdin = io.StringIO(json.dumps({"source": "startup"}))
    stdout = io.StringIO()
    rc = rehydrate.main(stdin, stdout)
    assert rc == 0
    out = json.loads(stdout.getvalue())
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "STDIN-pinned-rule" in out["hookSpecificOutput"]["additionalContext"]


def test_rehydrate_main_shadow_default_no_stdout_injection(tmp_path, monkeypatch):
    # No ULTRA_MEMORY_SHADOW => shadow-by-default => stdout must be empty and the
    # gist goes to the shadow file instead.
    p, conn = _open_db(tmp_path)
    memory_lib.save_memory(conn, id="r6", type="feedback", title="SHADOW-pinned-rule",
                           body="Close Dec 30", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    _stamp_ready(conn)
    conn.close()

    shadow_out = tmp_path / "shadow" / "rehydration.md"
    monkeypatch.setenv("ULTRA_MEMORY_DB", str(p))
    monkeypatch.setenv("ULTRA_MEMORY_SHADOW_OUT", str(shadow_out))
    # deliberately DO NOT set ULTRA_MEMORY_SHADOW => defaults to "1" (shadow)

    stdin = io.StringIO(json.dumps({"source": "startup"}))
    stdout = io.StringIO()
    rc = rehydrate.main(stdin, stdout)
    assert rc == 0
    assert stdout.getvalue() == ""  # nothing injected in shadow
    assert shadow_out.exists()
    assert "SHADOW-pinned-rule" in shadow_out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# G6 — fail-open on a mid-flight exception (never block the session).
# ---------------------------------------------------------------------------
def test_rehydrate_fails_open_on_build_gist_exception(tmp_path, monkeypatch):
    p, conn = _open_db(tmp_path)
    _stamp_ready(conn)
    conn.close()

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic build_gist failure")

    monkeypatch.setattr(rehydrate, "build_gist", _boom)
    # Must swallow the exception and return {} (rehydrate.py:93-94).
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                        ts="2026-05-30T16:00:00Z")
    assert out == {}


def test_checkpoint_fails_open_on_record_event_exception(tmp_path, monkeypatch):
    p, conn = _open_db(tmp_path)
    _stamp_ready(conn)
    conn.close()

    transcript = _write_transcript(tmp_path, [
        _create_result(1, "trigger an event"),
        _task_update(1, status="completed"),
    ])

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic record_session_event failure")

    monkeypatch.setattr(memory_lib, "record_session_event", _boom)
    # Must swallow + return {} (checkpoint.py:123-125).
    out = checkpoint.run(
        {"session_id": "sess-1", "transcript_path": str(transcript)},
        db_path=p, ts="2026-05-30T16:00:00Z",
    )
    assert out == {}


# ---------------------------------------------------------------------------
# G7 — both hooks no-op on a migrated-but-unstamped DB (import_complete != 1).
# Covers the first-cutover transient: fail-open to legacy.
# ---------------------------------------------------------------------------
def test_hooks_noop_on_migrated_but_unstamped_db(tmp_path):
    # open_memory_db migrates + creates schema, but we deliberately do NOT
    # stamp meta.import_complete='1'.
    p, conn = _open_db(tmp_path)
    assert common.db_ready(p) is False  # gate is closed
    # Seed a pinned memory so a *ready* DB would have produced a non-empty gist;
    # this isolates the gate (not emptiness) as the reason for the no-op.
    memory_lib.save_memory(conn, id="r6", type="feedback", title="would-appear-if-ready",
                           body="b", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    conn.commit()
    conn.close()

    transcript = _write_transcript(tmp_path, [
        _create_result(1, "should not be recorded"),
        _task_update(1, status="completed"),
    ])

    assert rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                         ts="2026-05-30T16:00:00Z") == {}
    assert checkpoint.run(
        {"session_id": "sess-1", "transcript_path": str(transcript)},
        db_path=p, ts="2026-05-30T16:00:00Z") == {}

    # And the checkpoint must not have written anything to the gated DB.
    conn = memory_lib.open_memory_db(str(p))
    n_events = conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
    conn.close()
    assert n_events == 0


# ---------------------------------------------------------------------------
# G8 — pinned rules survive a tight budget; truncation marker appears.
#
# NOTE: build_gist's truncation is a naive `gist[:budget]` (rehydrate.py:61-62).
# It does NOT special-case the pinned section, so a budget smaller than the
# pinned block WOULD slice a hard-rule mid-string. We choose a budget
# comfortably larger than the pinned section (realistic: default 2000 >> a few
# pinned rules) so the always-present contract is honestly exercised. See the
# bugsFound note for the latent risk at pathologically small budgets.
# ---------------------------------------------------------------------------
def test_pinned_rules_survive_tiny_budget_and_truncation_marker(tmp_path):
    p, conn = _open_db(tmp_path)
    memory_lib.save_memory(conn, id="pin", type="feedback", title="PINNED-hard-rule",
                           body="OAuth only, never the API.", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='pin'")
    # Many hot memories to push the gist well past the budget.
    for i in range(60):
        memory_lib.save_memory(conn, id=f"m{i}", type="note",
                               title=f"hot-mem-{i:02d}-with-some-padding-text",
                               body="x" * 80, ts="2026-05-01T00:00:00Z")
    conn.commit()

    # Build the full gist to learn the pinned section length, then pick a budget
    # larger than it but smaller than the whole so truncation fires.
    full = rehydrate.build_gist(conn, budget_chars=10_000_000)
    hot_start = full.index("## Hot memories")
    pinned_section_len = hot_start  # pinned section is rendered first.
    assert pinned_section_len < len(full), "test setup must overflow the budget"
    budget = pinned_section_len + 50

    gist = rehydrate.build_gist(conn, budget_chars=budget)
    conn.close()

    assert "PINNED-hard-rule" in gist, "pinned hard-rule must survive truncation"
    assert "…(truncated)" in gist, "truncation marker must be present"
