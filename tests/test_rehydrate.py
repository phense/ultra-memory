from ultra_memory import memory_lib
from ultra_memory.hooks import rehydrate


def _db(tmp_path):
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    return p, conn


def test_gist_includes_pinned_rules(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="r6", type="feedback", title="Year-End Tax Fence",
                           body="Close all US options Dec 30.", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    conn.commit()
    g = rehydrate.build_gist(conn)
    assert "Year-End Tax Fence" in g


# ---------------------------------------------------------------------------
# SP-3 Stage 4 (D7): build_gist unions memory pins + knowledge pins into the one
# "## Pinned rules" section. The knowledge page's display title comes from
# unified_index (Stage 5 mirror); a pin with no unified_index row falls back to
# its slug so a pin is never silently dropped.
# ---------------------------------------------------------------------------

def test_gist_unions_knowledge_pins(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="r6", type="feedback", title="Year-End Tax Fence",
                           body="Close all US options Dec 30.", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    # A pinned wiki page + its unified_index mirror row (for the display title).
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="german-tax-fence",
                          pinned=True, ts="2026-05-01T00:00:00Z")
    conn.execute(
        "INSERT INTO unified_index (slug, topic, title, snippet, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("german-tax-fence", "trading", "German Tax Year-End Fence",
         "Close all US options at the 2nd-to-last NYSE day in December.",
         "2026-05-01T00:00:00Z"))
    conn.commit()
    g = rehydrate.build_gist(conn)
    # Both stores' pins land in the SINGLE "## Pinned rules" section.
    assert g.count("## Pinned rules") == 1
    assert "Year-End Tax Fence" in g          # memory pin
    assert "German Tax Year-End Fence" in g   # knowledge pin (title from unified_index)


def test_gist_knowledge_pin_falls_back_to_slug_without_index_row(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="orphan-slug",
                          pinned=True, ts="2026-05-01T00:00:00Z")
    conn.commit()
    g = rehydrate.build_gist(conn)
    assert "## Pinned rules" in g
    assert "orphan-slug" in g  # no unified_index title → slug, never dropped


def test_gist_excludes_unpinned_knowledge(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.set_pinned(conn, source_kind="knowledge", source_id="was-pinned",
                          pinned=False, ts="2026-05-01T00:00:00Z")
    conn.commit()
    g = rehydrate.build_gist(conn)
    assert "was-pinned" not in g


def test_gist_byte_identical_with_zero_knowledge_pins(tmp_path):
    """SAFETY GATE for the eventual merge to ultra-memory master (Trading's LIVE
    SessionStart hook runs build_gist). Trading today has ZERO knowledge_pins rows;
    the Stage-4 change to build_gist MUST be byte-identical to the memory-pins-only
    output in that state, so going live can't perturb Trading's rehydration.

    Two DBs built identically EXCEPT one has the knowledge-pin machinery exercised
    then fully cleared (zero pinned knowledge rows). Their gists must be byte-equal.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # Reference DB: only memory pins, knowledge_pins never touched.
    pa, ca = _db(tmp_path / "a")
    # Comparison DB: the knowledge-pin path is reachable, but no knowledge pin is
    # set → knowledge_pins is empty (Trading's current production state).
    pb, cb = _db(tmp_path / "b")
    for conn in (ca, cb):
        memory_lib.save_memory(conn, id="r6", type="feedback",
                               title="Year-End Tax Fence",
                               body="Close all US options Dec 30.",
                               ts="2026-05-01T00:00:00Z")
        conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
        memory_lib.save_memory(conn, id="m2", type="project", title="Second rule",
                               body="Body two.", ts="2026-05-02T00:00:00Z")
        conn.execute("UPDATE memories SET pinned=1 WHERE id='m2'")
        conn.execute(
            "INSERT INTO sessions (id, started_at, summary) VALUES (?,?,?)",
            ("s1", "2026-05-29T10:00:00Z", "Did the thing."))
        conn.commit()
    # The comparison DB exercises set_pinned(knowledge) then unpins → zero pinned
    # knowledge rows remain (proves the gist is invariant when no knowledge pin is
    # active, not merely when the code path is unreached).
    memory_lib.set_pinned(cb, source_kind="knowledge", source_id="x",
                          pinned=True, ts="2026-05-03T00:00:00Z")
    memory_lib.set_pinned(cb, source_kind="knowledge", source_id="x",
                          pinned=False, ts="2026-05-04T00:00:00Z")
    cb.commit()
    assert cb.execute(
        "SELECT COUNT(*) FROM knowledge_pins WHERE pinned=1").fetchone()[0] == 0
    ga = rehydrate.build_gist(ca)
    gb = rehydrate.build_gist(cb)
    assert ga == gb, (
        "build_gist diverged with zero active knowledge_pins — the merge to "
        f"master would perturb Trading's live rehydration.\n--- ref ---\n{ga}\n"
        f"--- cmp ---\n{gb}")
    ca.close()
    cb.close()


def test_gist_includes_last_session_summary(tmp_path):
    p, conn = _db(tmp_path)
    conn.execute("INSERT INTO sessions (id, started_at, summary) VALUES (?,?,?)",
                 ("s-old", "2026-05-29T10:00:00Z", "Built the engine; 102 tests green."))
    conn.commit()
    g = rehydrate.build_gist(conn)
    assert "102 tests green" in g


def test_gist_lists_open_followups(tmp_path):
    p, conn = _db(tmp_path)
    memory_lib.record_session_event(conn, session_id="s1", kind="followup",
                                    title="Wire the MCP", ts="2026-05-29T10:00:00Z")
    g = rehydrate.build_gist(conn)
    assert "Wire the MCP" in g


def test_gist_respects_budget(tmp_path):
    p, conn = _db(tmp_path)
    for i in range(200):
        memory_lib.save_memory(conn, id=f"m{i}", type="project",
                               title=f"Memory title number {i} with padding text",
                               body="x" * 200, ts="2026-05-01T00:00:00Z")
    g = rehydrate.build_gist(conn, budget_chars=2000)
    assert len(g) <= 2200  # budget + small header slack


def test_gist_empty_db_is_safe(tmp_path):
    p, conn = _db(tmp_path)
    g = rehydrate.build_gist(conn)
    assert isinstance(g, str)


def _ready_db(tmp_path):
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    memory_lib.save_memory(conn, id="r6", type="feedback", title="Tax Fence",
                           body="Close Dec 30", ts="2026-05-01T00:00:00Z")
    conn.execute("UPDATE memories SET pinned=1 WHERE id='r6'")
    conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('import_complete','1')")
    conn.commit()
    conn.close()
    return p


def test_run_injects_when_live(tmp_path):
    p = _ready_db(tmp_path)
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                        ts="2026-05-30T16:00:00Z")
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Tax Fence" in out["hookSpecificOutput"]["additionalContext"]


def test_run_shadow_writes_file_and_injects_nothing(tmp_path):
    p = _ready_db(tmp_path)
    shadow_out = tmp_path / "shadow" / "rehydration.md"
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=True,
                        ts="2026-05-30T16:00:00Z", shadow_out=shadow_out)
    assert out == {}  # no injection in shadow
    assert "Tax Fence" in shadow_out.read_text()


def test_run_noops_for_cron(tmp_path, monkeypatch):
    p = _ready_db(tmp_path)
    monkeypatch.setenv("ULTRA_MEMORY_AGENT_ROLE", "cron")
    out = rehydrate.run({"source": "startup"}, db_path=p, shadow=False,
                        ts="2026-05-30T16:00:00Z")
    assert out == {}


def test_run_noops_when_db_not_ready(tmp_path):
    out = rehydrate.run({"source": "startup"}, db_path=tmp_path / "absent.db",
                        shadow=False, ts="2026-05-30T16:00:00Z")
    assert out == {}


def test_budget_from_env_default(monkeypatch):
    monkeypatch.delenv("ULTRA_MEMORY_REHYDRATE_BUDGET", raising=False)
    assert rehydrate._budget_from_env() == 2000


def test_budget_from_env_override(monkeypatch):
    monkeypatch.setenv("ULTRA_MEMORY_REHYDRATE_BUDGET", "4000")
    assert rehydrate._budget_from_env() == 4000


def test_budget_from_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ULTRA_MEMORY_REHYDRATE_BUDGET", "not-a-number")
    assert rehydrate._budget_from_env() == 2000
