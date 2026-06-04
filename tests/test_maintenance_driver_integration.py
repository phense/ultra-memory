import os, pathlib
from ultra_memory.maintenance.__main__ import main


def test_maintenance_main_is_failopen_on_fresh_store(tmp_path, monkeypatch):
    db = tmp_path / ".ultra-memory" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)  # store dir exists → the beats actually run
    monkeypatch.setenv("ULTRA_MEMORY_DB", str(db))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))   # no .ultra-memory/config.toml → safe defaults
    # No OAuth, empty store, no git: every heavy beat must self-skip / no-op, exit 0.
    # This exercises the real per-beat fail-open contract (default-on session_ingest
    # drains an empty transcript; the aggressive/synthesize beats self-skip on the
    # non-git tree behind the safety wall) — not just the DB-open early return.
    rc = main([])
    assert rc == 0
