import sqlite3
from ultra_memory import setup, memory_lib


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
