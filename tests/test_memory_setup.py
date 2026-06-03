import sqlite3
from ultra_memory import setup, memory_lib


def _fake_which(present):
    """Build a shutil.which stand-in: returns a path for names in `present`, else None."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_check_prerequisites_reports_each_tool():
    res = setup.check_prerequisites(which=_fake_which({"uv", "git"}))
    assert res == {"uv": True, "git": True}


def test_check_prerequisites_flags_absent_tool():
    res = setup.check_prerequisites(which=_fake_which({"uv"}))
    assert res["uv"] is True and res["git"] is False


def test_missing_prerequisites_lists_only_absent():
    assert setup.missing_prerequisites(which=_fake_which({"uv", "git"})) == []
    assert setup.missing_prerequisites(which=_fake_which({"uv"})) == ["git"]
    assert sorted(setup.missing_prerequisites(which=_fake_which(set()))) == ["git", "uv"]


def test_required_tools_includes_git_and_uv():
    # git is load-bearing for the rollback model; uv provisions the Python runtime.
    assert "git" in setup.REQUIRED_TOOLS
    assert "uv" in setup.REQUIRED_TOOLS


def test_stamp_import_complete_is_idempotent(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()  # migrate so meta exists
    assert setup.mark_import_complete(str(db)) is True      # first call stamps
    assert setup.mark_import_complete(str(db)) is False     # already set => no-op
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT value FROM meta WHERE key='import_complete'").fetchone()[0]
    conn.close()
    assert val == "1"


def test_db_ready_true_after_stamp(tmp_path):
    from ultra_memory.hooks import common
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    assert common.db_ready(str(db)) is False                # migrated but not stamped
    setup.mark_import_complete(str(db))
    assert common.db_ready(str(db)) is True                 # now the hooks will activate


def test_should_import_legacy_skips_when_complete(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    assert setup.should_import_legacy(str(db)) is True      # fresh => import would run
    setup.mark_import_complete(str(db))
    assert setup.should_import_legacy(str(db)) is False     # complete => skip (idempotent)


# --- §5.2.9 cold-start backfill discoverability ----------------------------
# A consumer that ships a session-cache backfill runner (declared via
# ULTRA_MEMORY_BACKFILL_CMD) gets a one-time post-bootstrap hint from
# /memory-setup. The flag (meta.backfill_complete) is INDEPENDENT of
# import_complete: skipping the backfill must never disable the hooks.

def test_stamp_backfill_complete_is_idempotent(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    assert setup.mark_backfill_complete(str(db)) is True    # first call stamps
    assert setup.mark_backfill_complete(str(db)) is False   # already set => no-op
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT value FROM meta WHERE key='backfill_complete'").fetchone()[0]
    conn.close()
    assert val == "1"


def test_should_offer_backfill_requires_declared_runner(tmp_path):
    # Greenfield consumer ships no backfill => never offered, even unstamped.
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    assert setup.should_offer_backfill(str(db), "") is False
    assert setup.should_offer_backfill(str(db), None) is False


def test_should_offer_backfill_true_when_declared_and_unstamped(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    assert setup.should_offer_backfill(str(db), "./scripts/run_backfill.sh") is True


def test_should_offer_backfill_false_after_stamp(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    setup.mark_backfill_complete(str(db))
    assert setup.should_offer_backfill(str(db), "./scripts/run_backfill.sh") is False


def test_backfill_flag_independent_of_db_ready(tmp_path):
    # The backfill flag must NOT gate hook activation: stamping it leaves
    # db_ready False (only import_complete flips that), and stamping
    # import_complete leaves the backfill still offer-able.
    from ultra_memory.hooks import common
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    setup.mark_backfill_complete(str(db))
    assert common.db_ready(str(db)) is False                # backfill flag is not the gate
    setup.mark_import_complete(str(db))
    assert common.db_ready(str(db)) is True                 # only import_complete activates hooks


def test_import_stamp_leaves_backfill_offerable(tmp_path):
    db = tmp_path / "m.db"
    memory_lib.open_memory_db(str(db)).close()
    setup.mark_import_complete(str(db))                     # legacy import done
    assert setup.should_offer_backfill(str(db), "run.sh") is True   # backfill still pending


def test_backfill_hint_names_the_runner(tmp_path):
    hint = setup.backfill_hint("./scripts/run_backfill.sh")
    assert "./scripts/run_backfill.sh" in hint
    assert "backfill" in hint.lower()
