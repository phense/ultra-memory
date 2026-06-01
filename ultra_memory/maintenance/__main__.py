"""`python -m ultra_memory.maintenance` — the throttled Tier-2 pipeline entry.

This is the project-agnostic entry a consumer wires into its session lifecycle
(a SessionStart/Stop hook or a thin cron shim). It resolves the consumer's
`MaintenanceConfig` (`.ultra-memory/config.toml` + ULTRA_MEMORY_* env), opens the
memory DB, and runs the due+enabled beats once (each throttled by its own meta
clock — so calling this every session is cheap and safe). Fail-open throughout.

    python -m ultra_memory.maintenance                 # all due+enabled beats
    python -m ultra_memory.maintenance --beat consolidate   # one beat only
    python -m ultra_memory.maintenance --force         # ignore the throttle clocks
    python -m ultra_memory.maintenance --project-dir /path/to/project
"""
from __future__ import annotations

import argparse
import os
import sys

from ultra_memory import memory_lib
from ultra_memory.maintenance.config import load_config
from ultra_memory.maintenance.run import run_pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ultra_memory.maintenance",
        description="Run the throttled Tier-2 self-learning maintenance beats once.")
    ap.add_argument("--project-dir", default=None,
                    help="consumer project root (default: $CLAUDE_PROJECT_DIR or cwd)")
    ap.add_argument("--beat", action="append", default=None,
                    help="restrict to this beat (repeatable); default: all due+enabled")
    ap.add_argument("--force", action="store_true",
                    help="ignore the per-beat throttle clocks (the on-demand path)")
    args = ap.parse_args(argv)

    config = load_config(project_dir=args.project_dir, env=os.environ)
    try:
        conn = memory_lib.open_memory_db(str(config.db_path))
    except Exception as exc:  # fail-open: a missing/locked DB must never wedge a session
        sys.stderr.write(f"[maintenance] cannot open DB {config.db_path}: {exc!r} — skipping\n")
        return 0
    try:
        result = run_pipeline(
            conn, config, force=args.force, only=args.beat, env=os.environ,
            log=lambda m: sys.stderr.write(f"[maintenance] {m}\n"))
    finally:
        conn.close()

    sys.stderr.write(
        f"[maintenance] ran={result.ran} skipped={result.skipped} "
        f"errors={list(result.errors)}\n")
    # Exit 0 even on per-beat errors (fail-open: never wedge the caller / launchd).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
