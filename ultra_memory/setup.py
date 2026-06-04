"""/memory-setup support: the idempotency-critical bits, unit-tested.

The slash command does venv bootstrap (uv sync) + the optional legacy import in
shell; the decisions that must be deterministic + tested live here. Production
code (NOT just tests) stamps meta.import_complete — without it db_ready() is
False forever and the session hooks never activate (the §2 trap).
"""
import shutil

from ultra_memory import memory_lib

# External tools the plugin requires on PATH to function. `/memory-setup` checks
# these in a preflight and refuses to proceed if any is missing.
#   - uv:  provisions the Python 3.13 runtime venv + the optional retrieval/mcp
#          extras (the engine itself is pure Python 3.13 + SQLite — no other
#          binary is shelled).
#   - git: the rollback/safety model is git-backed. The deterministic export
#          (memory.dump.sql + VACUUM snapshot + markdown views) is "the sole
#          git-committed rollback artifact" (memory_export §7.1), and the
#          wiki/maintenance lifecycle is archive-never-delete *via git*. The
#          engine never shells git directly; the REQUIREMENT is on the rollback
#          model, not a runtime call — but without git there is no restore net,
#          so it is a hard prerequisite, not advisory.
REQUIRED_TOOLS = ("uv", "git")


def check_prerequisites(which=shutil.which):
    """Map each required external tool → bool(present on PATH). `which` is
    injectable (shutil.which by default) so tests need no real binaries. Pure —
    no side effects."""
    return {name: bool(which(name)) for name in REQUIRED_TOOLS}


def missing_prerequisites(which=shutil.which):
    """The REQUIRED_TOOLS not found on PATH, in REQUIRED_TOOLS order. Empty list
    => all present. The /memory-setup preflight aborts with a clear message when
    this is non-empty."""
    present = check_prerequisites(which=which)
    return [name for name in REQUIRED_TOOLS if not present[name]]


def _import_complete(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='import_complete'").fetchone()
    return bool(row) and str(row[0]) == "1"


def mark_import_complete(db_path):
    """Stamp meta.import_complete='1'. Returns True if newly stamped, False if
    it was already set (idempotent)."""
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        if _import_complete(conn):
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('import_complete', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True
    finally:
        conn.close()


def should_import_legacy(db_path):
    """True only when the one-time legacy import has not yet run (import_complete
    unset). Greenfield consumers with no legacy dir simply stamp directly."""
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        return not _import_complete(conn)
    finally:
        conn.close()


# --- cold-start session-cache backfill (§5.2.9) ---------------------------
# A consumer MAY ship a session-cache backfill that mines historical Claude
# Code transcripts into the store (memories + wiki). It is consumer-side and
# OPTIONAL, so /memory-setup only *offers* it — it never auto-runs it. The
# consumer opts in by declaring its runner in the ULTRA_MEMORY_BACKFILL_CMD
# env (mirroring ULTRA_MEMORY_HARNESS_DIR for the legacy import); greenfield
# consumers leave it unset and are never offered the backfill.
#
# The meta.backfill_complete flag is INDEPENDENT of import_complete: it is a
# pure idempotency/hint marker and is deliberately NOT wired into db_ready(),
# so declining the backfill never disables the session hooks.

def _backfill_complete(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='backfill_complete'").fetchone()
    return bool(row) and str(row[0]) == "1"


def mark_backfill_complete(db_path):
    """Stamp meta.backfill_complete='1'. Returns True if newly stamped, False if
    it was already set (idempotent). Independent of import_complete."""
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        if _backfill_complete(conn):
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('backfill_complete', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True
    finally:
        conn.close()


def should_offer_backfill(db_path, backfill_cmd):
    """True only when a consumer-declared backfill runner exists AND the one-time
    cold-start backfill has not been stamped. `backfill_cmd` is the consumer's
    declared runner (e.g. from ULTRA_MEMORY_BACKFILL_CMD); empty/None => the
    consumer ships no backfill => never offer (greenfield-safe)."""
    if not backfill_cmd:
        return False
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        return not _backfill_complete(conn)
    finally:
        conn.close()


def backfill_hint(backfill_cmd):
    """The one-line post-bootstrap hint naming the consumer's backfill runner.
    Pure (no DB) — the caller decides whether to print it via should_offer_backfill."""
    return (
        f"ultra-memory: this project ships a cold-start session-cache backfill "
        f"({backfill_cmd}) — run it to seed the store from historical sessions "
        f"(writes memories + wiki). Pilot-first: try it with --pilot --dry-run, "
        f"then stamp meta.backfill_complete via setup.mark_backfill_complete(db) "
        f"so this hint stops."
    )


# --- optional OS scheduler offer (§4 — deterministic cadence) -------------
# The self-learning loop advances whenever Claude Code opens (the async
# SessionStart `beats` hook). For a headless box that rarely opens Claude, a
# user MAY install an OS scheduler for deterministic cadence. /memory-setup
# only OFFERS it (prints the snippet); these helpers are pure — the caller
# prints, never installs.

def detect_scheduler_platform(platform: str) -> str | None:
    """Map sys.platform → the OS scheduler kind, or None if unsupported (→ no offer)."""
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "systemd"
    return None


def scheduler_offer_text(platform_kind: str | None, *, py: str) -> str:
    """A copy-paste OPTIONAL scheduler snippet running the heavy-beat dispatcher daily.
    Empty string for an unsupported platform. Never installs anything — the user pastes
    it if they want deterministic cadence (e.g. a headless box that rarely opens Claude)."""
    cmd = f"{py} -m ultra_memory.maintenance"
    if platform_kind == "launchd":
        return ("OPTIONAL — deterministic cadence via launchd (else the loop advances "
                "whenever you open Claude Code). Save as "
                "~/Library/LaunchAgents/ng.ultra-memory.maintenance.plist with a "
                f"daily StartCalendarInterval running:\n    {cmd}\n"
                "then: launchctl load ~/Library/LaunchAgents/ng.ultra-memory.maintenance.plist")
    if platform_kind == "systemd":
        return ("OPTIONAL — deterministic cadence via a systemd --user timer (else the "
                "loop advances when you open Claude Code). Create "
                "~/.config/systemd/user/ultra-memory.service running:\n"
                f"    ExecStart={cmd}\n"
                "and a matching .timer (OnCalendar=daily), then: "
                "systemctl --user enable --now ultra-memory.timer")
    return ""
