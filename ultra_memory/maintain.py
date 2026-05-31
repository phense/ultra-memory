"""Self-healing maintenance: prune session_events + export views, throttled.

Shared by the async SessionStart hook (via um-hook.cmd), the /memory-maintain
command, and a documented CLI. Pure Python — NO LLM, NO OAuth token (the memory
maintenance slice is prune + export only). Fail-open: a maintenance error must
never block a session.
"""
import datetime
import os

from ultra_memory import memory_lib, retention, memory_export

# Retention window for session_events (days). Conservative default; rolled into
# sessions.summary before deletion, so nothing is lost — only the raw rows are bounded.
_KEEP_DAYS = 90
# Throttle: skip if the last successful run was within this many hours.
_THROTTLE_HOURS = 20
_META_KEY = "last_maintenance"


def _now_z():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(conn, *, out_dir, ts=None, keep_days=_KEEP_DAYS, force=False):
    """Stage-3 fills in the throttle + prune + export body. Returns a summary dict."""
    raise NotImplementedError  # Implemented in Task 3.1.


def main(argv=None):  # pragma: no cover - exercised via the wrapper + Task 3.3
    """Stage-3 wires argv/env → run(). Stub returns 0 so the wrapper can dispatch."""
    return 0
