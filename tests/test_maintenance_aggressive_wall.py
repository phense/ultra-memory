"""Tests for aggressive_wall.py — SP-7 §4a (provenance gate) + §4b
(archive-never-delete) — the SAFETY WALL the three aggressive capabilities
(§5.1 auto-edit / §5.2 self-reversion / §5.3 quarantine) are clients of.

THE WALL LIVES IN THE APPLY PATH (code), NEVER ONLY THE PROMPT
([[feedback-subagents-can-leak-secrets]]: build the constraint into the TOOL).

HARD INVARIANTS under test:
  * `assert_mutable` RAISES ForbiddenTargetError on a human / import / pinned
    memory target AND on a pinned / human / import wiki-page target;
  * `assert_mutable` PASSES (returns None) on an agent / background_review,
    unpinned, non-knowledge_pins target;
  * `assert_mutable` RE-READS the live row — it never trusts an LLM-echoed
    `created_by`/`pinned` field (a hallucinated "this is agent-authored" cannot
    flip a human row mutable);
  * a missing memory id is treated as FORBIDDEN (fail-closed — never edit a row
    the loop cannot prove is agent-authored);
  * any read error → FORBIDDEN (fail-closed);
  * the non-destructive verb primitives:
      - auto-edit  → save_memory(created_by='background_review') + consolidate
                     (old preserved as a `redirect` row — recoverable, NOT deleted);
      - quarantine → set_status('quarantined') — out of recall, reversible;
      - revert     → set_status FSM flip — reversible (flip back to active);
  * NO code path calls rm / delete(tier=...) — archive-never-delete;
  * the fence demonstrably BLOCKS (a single forbidden target = a raise, the
    §4a stop-the-world; the consumer turns it into a run-halt).

These tests NEVER touch the live memory.db, NEVER spawn `claude`, NEVER load a
real embedder, and NEVER make a real wiki write. They run against a temp DB +
synthetic agent-authored / human / pinned fixtures.
"""
from pathlib import Path

import pytest

from ultra_memory import memory_lib
from ultra_memory.maintenance import aggressive_wall as aw


TS = "2026-05-31T00:00:00Z"


def _open_temp_db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _save(conn, *, id, created_by, pinned=False, body="a lesson", title="L"):
    """Insert a memory with a given provenance + optional pin."""
    memory_lib.save_memory(
        conn, id=id, type="learning", title=title, body=body, ts=TS,
        created_by=created_by)
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=TS, reason="test pin")
    return id


def _pin_page(conn, slug):
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id=slug,
                          pinned=True, ts=TS, reason="test page pin")


# --------------------------------------------------------------------------- #
# 4a. Provenance gate — memory targets
# --------------------------------------------------------------------------- #

def test_assert_mutable_passes_agent(tmp_path):
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-agent", created_by="agent")
    # Returns None (does not raise).
    assert aw.assert_mutable(conn, aw.MemoryUnit("m-agent")) is None


def test_assert_mutable_passes_background_review(tmp_path):
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-bg", created_by="background_review")
    assert aw.assert_mutable(conn, aw.MemoryUnit("m-bg")) is None


def test_assert_mutable_raises_on_human(tmp_path):
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-human", created_by="human")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.MemoryUnit("m-human"))


def test_assert_mutable_raises_on_import(tmp_path):
    """`import` is human content the bootstrap importer stamped — immutable to the
    loop (spec §4a: created_by='import' of human content is NOT 'agent'/'bg')."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-import", created_by="import")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.MemoryUnit("m-import"))


def test_assert_mutable_raises_on_pinned_agent(tmp_path):
    """`pinned` is an INDEPENDENT second condition — even an agent-authored row,
    if pinned, is immutable (spec §4a)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-agent-pin", created_by="agent", pinned=True)
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.MemoryUnit("m-agent-pin"))


def test_assert_mutable_missing_id_is_forbidden(tmp_path):
    """Fail-closed: a missing row can't be proven agent-authored → forbidden."""
    conn = _open_temp_db(tmp_path)
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.MemoryUnit("nonexistent"))


def test_assert_mutable_read_error_is_forbidden():
    """Fail-closed: any read error → forbidden (refuse rather than risk an edit)."""
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(_Boom(), aw.MemoryUnit("x"))


def test_assert_mutable_rereads_live_row_ignores_echoed_field(tmp_path):
    """The gate RE-READS the live row; it never trusts a field the LLM echoed.
    A human row carrying an LLM-asserted created_by='agent' hint is STILL refused
    — the hint is ignored; only the live DB value counts."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-human2", created_by="human")
    # The unit echoes a (hallucinated) provenance — the gate must ignore it.
    unit = aw.MemoryUnit("m-human2", echoed_created_by="agent", echoed_pinned=False)
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, unit)
    # And the live value governs a passing case too: a genuinely agent-authored row
    # whose unit ECHOES created_by='human' is STILL mutable — the echoed hint is
    # ignored, only the live 'agent' value counts. We use a FRESH id rather than
    # re-stamping m-human2: save_memory's provenance guard (engine fix r4-E7) now
    # REFUSES to downgrade a human row's created_by, so a human row stays human (a
    # re-stamp to 'agent' is a no-op on the provenance field) — the correct invariant
    # the wall relies on. This still asserts the re-read-ignores-the-echo property.
    _save(conn, id="m-agent2", created_by="agent")
    unit2 = aw.MemoryUnit("m-agent2", echoed_created_by="human", echoed_pinned=True)
    assert aw.assert_mutable(conn, unit2) is None  # live agent, unpinned → mutable


# --------------------------------------------------------------------------- #
# 4a. Provenance gate — wiki-page targets
# --------------------------------------------------------------------------- #

def test_assert_mutable_page_passes_agent(tmp_path):
    conn = _open_temp_db(tmp_path)
    page = tmp_path / "p-agent.md"
    page.write_text("---\ntype: mechanism\ncreated_by: agent\n---\nbody\n")
    assert aw.assert_mutable(conn, aw.PageUnit("p-agent", path=page)) is None


def test_assert_mutable_page_raises_on_human_frontmatter(tmp_path):
    conn = _open_temp_db(tmp_path)
    page = tmp_path / "p-human.md"
    page.write_text("---\ntype: mechanism\ncreated_by: human\n---\nbody\n")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.PageUnit("p-human", path=page))


def test_assert_mutable_page_raises_on_missing_created_by(tmp_path):
    """A page WITHOUT a created_by frontmatter is treated as human (the safe
    default — the engine's created_by default is 'human'). Fail-closed."""
    conn = _open_temp_db(tmp_path)
    page = tmp_path / "p-bare.md"
    page.write_text("---\ntype: mechanism\n---\nbody\n")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.PageUnit("p-bare", path=page))


def test_assert_mutable_page_raises_on_knowledge_pin(tmp_path):
    """An agent-authored page that is in knowledge_pins is immutable (spec §4a:
    'not a member of the knowledge_pins set')."""
    conn = _open_temp_db(tmp_path)
    _pin_page(conn, "p-pinned")
    page = tmp_path / "p-pinned.md"
    page.write_text("---\ntype: mechanism\ncreated_by: agent\n---\nbody\n")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.PageUnit("p-pinned", path=page))


def test_assert_mutable_page_passes_unpinned_knowledge_pin_row(tmp_path):
    """A knowledge_pins row with pinned=0 does NOT protect the page."""
    conn = _open_temp_db(tmp_path)
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="p-unpin",
                          pinned=False, ts=TS, reason="explicitly unpinned")
    page = tmp_path / "p-unpin.md"
    page.write_text("---\ntype: mechanism\ncreated_by: agent\n---\nbody\n")
    assert aw.assert_mutable(conn, aw.PageUnit("p-unpin", path=page)) is None


def test_assert_mutable_page_missing_file_is_forbidden(tmp_path):
    """Fail-closed: a page whose file cannot be read is forbidden."""
    conn = _open_temp_db(tmp_path)
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.PageUnit("gone", path=tmp_path / "nope.md"))


# --------------------------------------------------------------------------- #
# 4b. Archive-never-delete — the non-destructive verb primitives
# --------------------------------------------------------------------------- #

def _row(conn, mem_id):
    return conn.execute(
        "SELECT status, supersedes, body, created_by FROM memories WHERE id=?",
        (mem_id,)).fetchone()


def test_auto_edit_preserves_old_version_as_redirect(tmp_path):
    """auto-edit writes the NEW version + consolidate(loser=old, canonical=new):
    the OLD row survives as status='redirect' with its bytes intact (recoverable,
    NOT deleted), and a superseded_by edge is recorded."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-old", created_by="agent", body="OLD lesson text")

    new_id = aw.apply_auto_edit(
        conn, old_id="m-old", new_body="SHARPER lesson text",
        new_title="L (sharpened)", evidence="trace:ev1", ts=TS)

    old = _row(conn, "m-old")
    new = _row(conn, new_id)
    # Old preserved verbatim, just redirected — NOT deleted.
    assert old["status"] == "redirect"
    assert old["body"] == "OLD lesson text"          # bytes preserved → recoverable
    assert old["supersedes"] == new_id
    # New version is active, agent-provenance via background_review.
    assert new["status"] == "active"
    assert new["body"] == "SHARPER lesson text"
    assert new["created_by"] == "background_review"
    # superseded_by edge old -> new.
    link = conn.execute(
        "SELECT predicate, dst_id FROM links WHERE src_id=? AND predicate='superseded_by'",
        ("m-old",)).fetchone()
    assert link is not None and link["dst_id"] == new_id


def test_auto_edit_refuses_protected_target(tmp_path):
    """auto-edit funnels through assert_mutable → a human target raises, and
    NOTHING is written (no new row, old untouched)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-h", created_by="human", body="HUMAN rule")
    before = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    with pytest.raises(aw.ForbiddenTargetError):
        aw.apply_auto_edit(conn, old_id="m-h", new_body="x", new_title="x",
                           evidence="e", ts=TS)
    after = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    assert after == before                       # no new version written
    assert _row(conn, "m-h")["status"] == "active"  # human row untouched


def test_quarantine_demotes_out_of_recall_and_is_reversible(tmp_path):
    """quarantine flips status='quarantined' (drops out of recall) — reversible by
    flipping back to active; both units of a pair get quarantined + a contradicts
    edge; nothing is deleted."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-a", created_by="agent", body="claim A")
    _save(conn, id="m-b", created_by="agent", body="claim B (opposes A)")

    aw.apply_quarantine_pair(conn, id_a="m-a", id_b="m-b",
                             reason="contradiction", ts=TS)
    assert _row(conn, "m-a")["status"] == "quarantined"
    assert _row(conn, "m-b")["status"] == "quarantined"
    # bodies preserved (nothing deleted).
    assert _row(conn, "m-a")["body"] == "claim A"
    # contradicts edge between them.
    edge = conn.execute(
        "SELECT predicate FROM links WHERE src_id='m-a' AND dst_id='m-b' "
        "AND predicate='contradicts'").fetchone()
    assert edge is not None
    # Reversible: flip back to active.
    aw.reactivate(conn, id="m-a", ts=TS, reason="adjudicated correct")
    assert _row(conn, "m-a")["status"] == "active"


def test_quarantine_refuses_protected_member(tmp_path):
    """If EITHER member of the pair is protected, the whole quarantine raises and
    NEITHER unit is touched (zero-tolerance, the §4a stop-the-world)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-ok", created_by="agent")
    _save(conn, id="m-pinned", created_by="agent", pinned=True)
    with pytest.raises(aw.ForbiddenTargetError):
        aw.apply_quarantine_pair(conn, id_a="m-ok", id_b="m-pinned",
                                 reason="x", ts=TS)
    assert _row(conn, "m-ok")["status"] == "active"      # untouched
    assert _row(conn, "m-pinned")["status"] == "active"  # untouched


def test_revert_flips_status_and_is_reversible(tmp_path):
    """A regressed auto-edited unit reverts: the regressed NEW version → 'reverted'
    (out of recall), the prior version (a redirect) re-activates. Both flips are
    pure FSM transitions — reversible — and a reverted_from edge is recorded."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-prior", created_by="agent", body="PRIOR good lesson")
    new_id = aw.apply_auto_edit(
        conn, old_id="m-prior", new_body="EDIT that later regressed",
        new_title="L'", evidence="ev", ts=TS)
    # Now the edit regressed — revert it.
    aw.apply_revert(conn, regressed_id=new_id, prior_id="m-prior", ts=TS)
    assert _row(conn, new_id)["status"] == "reverted"     # demoted out of recall
    assert _row(conn, "m-prior")["status"] == "active"    # prior re-activated
    edge = conn.execute(
        "SELECT predicate FROM links WHERE src_id=? AND predicate='reverted_from'",
        (new_id,)).fetchone()
    assert edge is not None
    # Reversible: a mistaken revert can flip back.
    aw.reactivate(conn, id=new_id, ts=TS, reason="revert was itself wrong")
    assert _row(conn, new_id)["status"] == "active"


def test_revert_demotes_no_prior_unit_to_quarantine(tmp_path):
    """A graduated-then-regressed unit with NO prior version (prior_id=None) is
    demoted to 'quarantined' (out of recall) rather than reverting to nothing —
    archive-never-delete (spec §5.2)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-grad", created_by="background_review", body="graduated lesson")
    aw.apply_revert(conn, regressed_id="m-grad", prior_id=None, ts=TS)
    assert _row(conn, "m-grad")["status"] == "quarantined"


def test_revert_refuses_protected_target(tmp_path):
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-h", created_by="human")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.apply_revert(conn, regressed_id="m-h", prior_id=None, ts=TS)
    assert _row(conn, "m-h")["status"] == "active"


# --------------------------------------------------------------------------- #
# Archive-never-delete: no destructive call exists anywhere in the module
# --------------------------------------------------------------------------- #

def test_module_never_calls_delete_or_rm():
    """Static guard: the wall module never calls memory_lib.delete, never `rm`,
    never `os.remove`/`unlink`/`rmtree` — archive-never-delete is structural."""
    src = (Path(aw.__file__)).read_text()
    for forbidden in ("memory_lib.delete(", ".delete(tier", "os.remove(",
                      "shutil.rmtree(", ".unlink(", "subprocess", "os.system(",
                      'rm -rf', '"rm"', "'rm'"):
        assert forbidden not in src, f"destructive call {forbidden!r} found in wall"


# --------------------------------------------------------------------------- #
# OAuth-only guard — the wall module imports no anthropic SDK / API
# --------------------------------------------------------------------------- #

def test_wall_module_no_anthropic_sdk_import():
    src = (Path(aw.__file__)).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com"):
        assert forbidden not in src, f"OAuth-only violation: {forbidden!r} in wall"
