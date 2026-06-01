"""The Tier-2 maintenance ORCHESTRATOR (project-agnostic; ports run_maintenance.sh).

`run_pipeline(conn, config, registry=…)` drives the heavy self-learning beats
(consolidate → aggressive → synthesize) on the session-lifecycle clock. Each beat is:
  * gated by config (`config.beat_enabled(name)` — the autonomous posture defaults ON);
  * throttled by a per-beat `meta` clock (`cadence_for(name)` hours) so SessionStart/
    Stop can call this every session on any platform without re-running a weekly beat;
  * fail-open — a beat that raises degrades to a recorded error + one log line, never
    wedging the session or the other beats.

The beats themselves are supplied via a `registry` ({name: callable(conn, config,
ts, env)}) so (a) tests inject stubs, and (b) a beat that has not yet been migrated
into the package is simply absent → skipped. The migrated beats register their real
callables here as each subsystem lands. NO LLM / OAuth here — that lives in the beats
(through `claude_cli.run_claude`); the orchestrator is pure control flow.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from ultra_memory import memory_lib

# Beat order (mirrors the bash Stage 2b → 2c → 2d sequence).
BEAT_ORDER = ("consolidate", "aggressive", "synthesize")


def _now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_meta(conn, key):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _set_meta(conn, key, value) -> None:
    def work():
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    memory_lib._with_immediate_retry(conn, work)


def _hours_between(earlier_z: str, later_z: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = datetime.datetime.strptime(earlier_z, fmt)
    b = datetime.datetime.strptime(later_z, fmt)
    return (b - a).total_seconds() / 3600.0


def _clock_key(beat: str) -> str:
    return f"last_maintenance_beat:{beat}"


def is_due(conn, beat: str, cadence_hours: int, now_z: str) -> bool:
    """True iff the beat has never run or its last run is older than its cadence.
    Fail-open: an unparseable/missing clock → due (run it)."""
    last = _get_meta(conn, _clock_key(beat))
    if not last:
        return True
    try:
        return _hours_between(last, now_z) >= cadence_hours
    except Exception:
        return True


@dataclass
class PipelineResult:
    ran: list = field(default_factory=list)       # beats that executed
    skipped: dict = field(default_factory=dict)   # beat -> reason ('disabled'|'not-due'|'unregistered')
    errors: dict = field(default_factory=dict)    # beat -> repr(exc)
    results: dict = field(default_factory=dict)   # beat -> its return value


def run_pipeline(conn, config, *, registry, ts=None, env=None, force=False,
                 log=lambda _m: None) -> PipelineResult:
    """Run the due+enabled Tier-2 beats once. NEVER raises (fail-open per beat).

    `registry` maps a beat name to `callable(conn, config, ts, env)`. A beat absent
    from the registry is skipped ('unregistered') — so an un-migrated beat is a no-op.
    `force=True` ignores the throttle clock (the on-demand path)."""
    now_z = ts or _now_z()
    res = PipelineResult()
    for beat in BEAT_ORDER:
        try:
            if not config.beat_enabled(beat):
                res.skipped[beat] = "disabled"
                continue
            fn = registry.get(beat)
            if fn is None:
                res.skipped[beat] = "unregistered"
                continue
            if not force and not is_due(conn, beat, config.cadence_for(beat), now_z):
                res.skipped[beat] = "not-due"
                continue
            log(f"maintenance beat '{beat}' running ({now_z})")
            res.results[beat] = fn(conn, config, now_z, env)
            res.ran.append(beat)
            _set_meta(conn, _clock_key(beat), now_z)  # stamp only on success
        except Exception as exc:  # fail-open: record + continue to the next beat
            res.errors[beat] = repr(exc)
            try:
                log(f"maintenance beat '{beat}' FAILED (fail-open): {exc!r}")
            except Exception:
                pass
    return res
