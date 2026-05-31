"""Integration tests for seam S1: import -> export -> re-import roundtrip.

Exercises the REAL seam between three modules through their public surface — no
mocks of the units under test:

    memory_import.import_memory_dir   (parse legacy md + MEMORY.md -> save_memory)
    memory_lib.save_memory / open_memory_db   (the single audited write path + db)
    memory_export.export_memory       (views/ + memory.dump.sql + snapshot)

Two distinct rollback artifacts come out of an export and they do NOT carry the
same fidelity, which these tests pin down:

  * views/*.md + views/MEMORY.md  — the human-readable, git-tracked form. Built
    from `_frontmatter(row) + body` only; it deliberately drops several columns
    (notably `pinned`). Re-importing views/ is therefore LOSSY for those columns.
  * memory.dump.sql               — a full `conn.iterdump()` of every column, so
    it round-trips faithfully (it is the genuine restore artifact).

Hermetic: tmp_path SQLite DBs only, never the real data/memory.db; no network;
no `claude` CLI is touched anywhere on this path.
"""
import sqlite3

import pytest

from ultra_memory import db
from ultra_memory import memory_export as mx
from ultra_memory import memory_import as mi
from ultra_memory import memory_lib


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors tests/test_roundtrip.py + tests/test_memory_export.py)
# ---------------------------------------------------------------------------

def _write(p, name, typ, desc, body, sid="s-1"):
    """Write a legacy harness memory/*.md file in the FIXED shape the importer
    parses (matches the _write helper used across the existing test suite)."""
    p.write_text(
        f"---\nname: {name}\ndescription: \"{desc}\"\nmetadata: \n"
        f"  node_type: memory\n  type: {typ}\n  originSessionId: {sid}\n---\n\n{body}\n")


def _memory_columns(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}


def _has_pinned(conn):
    return "pinned" in _memory_columns(conn)


def _snapshot(conn, *, cols):
    """{id: tuple(selected cols)} over ALL rows, ordered for stable comparison."""
    select = ", ".join(cols)
    return {
        r["id"]: tuple(r[c] for c in cols)
        for r in conn.execute(f"SELECT {select} FROM memories ORDER BY id")
    }


# ---------------------------------------------------------------------------
# Pins: views drops them, dump preserves them
# ---------------------------------------------------------------------------

def test_pins_lost_through_views_roundtrip(tmp_path):
    """REGRESSION SENTINEL (current, intentional-but-lossy behavior).

    The markdown views/ form has no slot for `pinned` (memory_export._frontmatter
    serializes name/description/node_type/type/originSessionId only). So a pin set
    on the DB survives an export into views/, but is LOST when those views/ are
    re-imported into a fresh DB. This test locks that in: if someone later teaches
    the views format to carry pins, this test will flip and force a deliberate
    decision rather than a silent semantic change.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_x.md", "feedback-x", "feedback", "one liner", "Body X.")
    (mem / "MEMORY.md").write_text("- [Feedback X](feedback_x.md) — hook X\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    if not _has_pinned(db1):
        db1.close()
        pytest.skip("schema has no `pinned` column; nothing to assert")
    mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")

    # Pin it through the audited write path (the importer never sets pinned itself).
    memory_lib.set_pinned(db1, id="feedback-x", pinned=True, ts="2026-05-30T11:00:00")
    assert db1.execute("SELECT pinned FROM memories WHERE id='feedback-x'").fetchone()[0] == 1

    out = tmp_path / "export"
    assert mx.export_memory(db1, out, ts="2026-05-30T12:00:00") is True
    db1.close()

    # The views md file genuinely carries no pin marker.
    view = (out / "views" / "feedback_x.md").read_text()
    assert "pinned" not in view

    # Re-import the regenerated views/ into a FRESH db.
    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    n = mi.import_memory_dir(db2, out / "views", index_path=out / "views" / "MEMORY.md",
                             ts="2026-05-30T13:00:00")
    assert n == 1
    # The pin did NOT survive the views roundtrip — defaults back to 0.
    assert db2.execute("SELECT pinned FROM memories WHERE id='feedback-x'").fetchone()[0] == 0
    db2.close()


def test_pins_survive_dump_restore_roundtrip(tmp_path):
    """Counterpart: the DUMP leg (the real rollback artifact) DOES preserve pins.

    memory.dump.sql is a full `iterdump()` of every column, so restoring it into a
    fresh sqlite db reproduces pinned==1 — proving the committed rollback artifact
    is faithful even though the md views are not.
    """
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    if not _has_pinned(conn):
        conn.close()
        pytest.skip("schema has no `pinned` column; nothing to assert")
    memory_lib.save_memory(conn, id="pinme", type="reference", title="Pin Me",
                           body="body", ts="2026-05-30T10:00:00")
    memory_lib.set_pinned(conn, id="pinme", pinned=True, ts="2026-05-30T11:00:00")

    out = tmp_path / "export"
    assert mx.export_memory(conn, out, ts="2026-05-30T12:00:00") is True
    conn.close()

    restored = sqlite3.connect(tmp_path / "restored.db")
    restored.executescript((out / "memory.dump.sql").read_text())
    assert restored.execute(
        "SELECT pinned FROM memories WHERE id='pinme'").fetchone()[0] == 1
    restored.close()


# ---------------------------------------------------------------------------
# Curated order (sort_order) end-to-end through the full loop
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_curated_order_nontrivial(tmp_path):
    """The curated MEMORY.md order (zzz BEFORE aaa, i.e. != id/alphabetical) must
    survive import -> export -> re-import end-to-end. The existing suite only
    checks the export leg; this closes the loop on the re-imported sort_order.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    # filenames + ids are reverse-alphabetical vs. their curated position.
    _write(mem / "zzz_first.md", "zzz-first", "project", "d", "B0.")
    _write(mem / "mmm_second.md", "mmm-second", "reference", "d", "B1.")
    _write(mem / "aaa_third.md", "aaa-third", "feedback", "d", "B2.")
    (mem / "MEMORY.md").write_text(
        "- [First](zzz_first.md) — h0\n"
        "- [Second](mmm_second.md) — h1\n"
        "- [Third](aaa_third.md) — h2\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    # The original curated sequence, expressed as (file_slug ordered by sort_order).
    original_seq = [
        r["file_slug"] for r in db1.execute(
            "SELECT file_slug FROM memories ORDER BY sort_order IS NULL, sort_order, id")
    ]
    assert original_seq == ["zzz_first", "mmm_second", "aaa_third"]

    out = tmp_path / "export"
    assert mx.export_memory(db1, out, ts="2026-05-30T12:00:00") is True
    db1.close()

    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    mi.import_memory_dir(db2, out / "views", index_path=out / "views" / "MEMORY.md",
                         ts="2026-05-30T13:00:00")
    reimported_seq = [
        r["file_slug"] for r in db2.execute(
            "SELECT file_slug FROM memories ORDER BY sort_order IS NULL, sort_order, id")
    ]
    assert reimported_seq == original_seq
    db2.close()


# ---------------------------------------------------------------------------
# Body fidelity through the loop
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_body_byte_for_byte(tmp_path):
    """Body must survive import -> export -> re-import EXACTLY (==, not .strip()).

    Includes an em-dash (—), a literal '---' line (must not be misread as a YAML
    fence), and content that would tempt whitespace drift. The existing roundtrip
    test compares with .strip(), which would mask such drift; this asserts the raw
    bytes match.
    """
    body = "First line — with em-dash.\n---\nAfter a dash rule.\nLast line."
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_body.md", "feedback-body", "feedback", "d", body)
    (mem / "MEMORY.md").write_text("- [Body](feedback_body.md) — hook\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    body1 = db1.execute("SELECT body FROM memories WHERE id='feedback-body'").fetchone()[0]

    out = tmp_path / "export"
    assert mx.export_memory(db1, out, ts="2026-05-30T12:00:00") is True
    db1.close()

    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    mi.import_memory_dir(db2, out / "views", index_path=out / "views" / "MEMORY.md",
                         ts="2026-05-30T13:00:00")
    body2 = db2.execute("SELECT body FROM memories WHERE id='feedback-body'").fetchone()[0]
    db2.close()

    # First the importer must have read the body intact off the source file.
    assert body in body1
    # Then the full loop must be byte-identical.
    assert body1 == body2


# ---------------------------------------------------------------------------
# Hook-less branch end-to-end
# ---------------------------------------------------------------------------

def test_roundtrip_hookless_entry(tmp_path):
    """A MEMORY.md line with NO ' — hook' suffix must round-trip: imported with
    index_hook NULL, exported WITHOUT a spurious ' — ' tail, re-imported identically.
    Covers the optional-hook branch of _INDEX_LINE + _frontmatter/index emission.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "feedback_nohook.md", "feedback-nohook", "feedback", "d", "Body.")
    # No ' — hook' tail on the index line.
    (mem / "MEMORY.md").write_text("- [No Hook](feedback_nohook.md)\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    assert db1.execute(
        "SELECT index_hook FROM memories WHERE id='feedback-nohook'").fetchone()[0] is None

    out = tmp_path / "export"
    assert mx.export_memory(db1, out, ts="2026-05-30T12:00:00") is True
    db1.close()

    # The exported index line must have no trailing ' — '.
    index = (out / "views" / "MEMORY.md").read_text()
    line = [l for l in index.splitlines() if "feedback_nohook" in l][0]
    assert line == "- [No Hook](feedback_nohook.md)"
    assert " — " not in line

    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    mi.import_memory_dir(db2, out / "views", index_path=out / "views" / "MEMORY.md",
                         ts="2026-05-30T13:00:00")
    assert db2.execute(
        "SELECT index_hook FROM memories WHERE id='feedback-nohook'").fetchone()[0] is None
    db2.close()


# ---------------------------------------------------------------------------
# Whole-pipeline fixed point (idempotent convergence)
# ---------------------------------------------------------------------------

def test_pipeline_reaches_fixed_point(tmp_path):
    """import -> export(A) -> re-import -> export(B): the views/ trees of A and B
    must be byte-identical (every atomic AND MEMORY.md). Proves the loop converges
    to a fixed point — extends the export-determinism guarantee to the full loop.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    _write(mem / "project_a.md", "project-a", "project", "desc a", "Body A.")
    _write(mem / "reference_b.md", "reference-b", "reference", "desc b", "Body B.")
    (mem / "MEMORY.md").write_text(
        "- [Proj A](project_a.md) — hook a\n"
        "- [Ref B](reference_b.md) — hook b\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md", ts="2026-05-30T10:00:00")
    out_a = tmp_path / "export_a"
    assert mx.export_memory(db1, out_a, ts="2026-05-30T12:00:00") is True
    db1.close()

    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    mi.import_memory_dir(db2, out_a / "views", index_path=out_a / "views" / "MEMORY.md",
                         ts="2026-05-30T13:00:00")
    out_b = tmp_path / "export_b"
    assert mx.export_memory(db2, out_b, ts="2026-05-30T14:00:00") is True
    db2.close()

    a_views = {p.name: p.read_text() for p in (out_a / "views").glob("*.md")}
    b_views = {p.name: p.read_text() for p in (out_b / "views").glob("*.md")}
    assert a_views == b_views


# ---------------------------------------------------------------------------
# N rows spanning every type value, full snapshot equality
# ---------------------------------------------------------------------------

def test_roundtrip_n_rows_all_types_full_snapshot(tmp_path):
    """Generalize the 2-row roundtrip to many rows spanning every memory `type`,
    asserting full snapshot-set equality of the stable, view-serialized columns
    across the loop. Cheap hardening against per-type handling drift.
    """
    types = ["user", "feedback", "project", "reference"]
    mem = tmp_path / "memory"
    mem.mkdir()
    index_lines = []
    for i in range(10):
        typ = types[i % len(types)]
        slug = f"{typ}_{i:02d}"
        name = f"{typ}-{i:02d}"
        _write(mem / f"{slug}.md", name, typ, f"desc {i}", f"Body number {i}.")
        index_lines.append(f"- [Title {i}]({slug}.md) — hook {i}")
    (mem / "MEMORY.md").write_text("\n".join(index_lines) + "\n")

    db1 = memory_lib.open_memory_db(tmp_path / "a.db")
    n1 = mi.import_memory_dir(db1, mem, index_path=mem / "MEMORY.md",
                              ts="2026-05-30T10:00:00")
    assert n1 == 10

    out = tmp_path / "export"
    assert mx.export_memory(db1, out, ts="2026-05-30T12:00:00") is True

    db2 = memory_lib.open_memory_db(tmp_path / "b.db")
    n2 = mi.import_memory_dir(db2, out / "views", index_path=out / "views" / "MEMORY.md",
                              ts="2026-05-30T13:00:00")
    assert n2 == 10

    # Compare the columns the views form actually carries (i.e. those a views
    # roundtrip is contractually expected to preserve).
    cols = ("id", "type", "title", "description", "index_hook",
            "node_type", "file_slug", "sort_order", "body")
    snap1 = _snapshot(db1, cols=cols)
    snap2 = _snapshot(db2, cols=cols)
    db1.close()
    db2.close()
    assert snap1 == snap2
    assert set(snap1) == {f"{types[i % len(types)]}-{i:02d}" for i in range(10)}
