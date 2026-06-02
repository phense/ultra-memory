"""SP-10 Stage 5a — the SYNTHESIZE run-gate + the 'skills' blast-radius cap.

ADDITIVE (spec §4f): this leaves the shipped SP-7 ``aggressive_bounds.run_gate``
UNTOUCHED. It reuses the generic ``GateDecision`` dataclass but reads the SP10
env-var triad, and adds the highest-blast-radius cap (1 generated skill / run).

§4f kill switch (mirrors SP-7): SP10_SYNTHESIS_DISABLE (default PRESENT in the cron
env → no-op) outranks SP10_SYNTHESIS_DRYRUN (plan + eval + digest, apply nothing).
§4c bounded blast radius: MAX_SKILLS_INDUCED_PER_RUN=1 (the tightest cap in the
project — a generated skill shapes every future session's routing) + an optional
per-period aggregate cap under its OWN meta namespace. HALT-ON-EXCEED. FAIL-OPEN,
fail-CLOSED-to-safety everywhere. NO LLM, NO anthropic SDK.
"""
from __future__ import annotations

import sys
from pathlib import Path


from ultra_memory.maintenance import gate_commons  # noqa: E402
from ultra_memory.maintenance.aggressive_bounds import GateDecision  # noqa: E402  (reuse the generic dataclass)

MAX_SKILLS_INDUCED_PER_RUN = 1
# Per-period (YYYY-MM) aggregate ceiling — blocks stacked re-runs from inducing many
# skills in one month even though each run is per-run-legal (§4c). Conservative.
MAX_SKILLS_INDUCED_PER_PERIOD = 2
_DISABLE_ENV = "SP10_SYNTHESIS_DISABLE"
_DRYRUN_ENV = "SP10_SYNTHESIS_DRYRUN"
_PERIOD_META_PREFIX = "sp10_synthesis_period"


def is_disabled(env=None) -> bool:
    return gate_commons.is_env_present(_DISABLE_ENV, env)


def is_dry_run(env=None) -> bool:
    return gate_commons.is_env_present(_DRYRUN_ENV, env)


def run_gate(*, log=lambda _m: None) -> GateDecision:
    """Read the SP10 kill switch + dry-run flag → the run mode. DISABLE outranks
    DRYRUN. Fail-open to a SAFE 'noop' on any error (never raises, never unbounded)."""
    try:
        if is_disabled():
            msg = (f"SP-10 SYNTHESIZE pass DISABLED ({_DISABLE_ENV} present) — no-op "
                   f"(the SP-5/6/7 beats are unaffected).")
            log(msg)
            return GateDecision(mode="noop", may_apply=False, reason=msg)
        if is_dry_run():
            msg = (f"SP-10 SYNTHESIZE pass DRY-RUN ({_DRYRUN_ENV} present) — plan + "
                   f"eval + digest, applies NOTHING.")
            log(msg)
            return GateDecision(mode="dryrun", may_apply=False, reason=msg)
        msg = "SP-10 SYNTHESIZE pass LIVE — plan + eval + digest + bounded apply."
        log(msg)
        return GateDecision(mode="live", may_apply=True, reason=msg)
    except Exception as exc:
        msg = f"SP-10 synthesize gate errored — degrading to no-op ({exc!r})."
        try:
            log(msg)
        except Exception:
            pass
        return GateDecision(mode="noop", may_apply=False, reason=msg)


# --------------------------------------------------------------------------- #
# §4c — the 'skills' cap (halt-on-exceed) + per-period aggregate.
# --------------------------------------------------------------------------- #

# The single SP-10 budget class — modeled as one 'skills' class so it shares the
# generic per-period counter (the key stays "<prefix>:<period>:skills", byte-identical).
_SKILLS_CLS = "skills"


def _period_key(period: str) -> str:
    return gate_commons.period_meta_key(_PERIOD_META_PREFIX, period, _SKILLS_CLS)


def _period_used(conn, period: str) -> int:
    """Skills already induced in `period`. Missing → 0; a read error → a huge sentinel
    (fail-CLOSED-to-safety). Thin wrapper over the shared gate_commons primitive."""
    return gate_commons.period_used(
        conn, prefix=_PERIOD_META_PREFIX, period=period, cls=_SKILLS_CLS)


def enforce_skill_cap(plan, *, conn=None, period=None,
                      cap: int = MAX_SKILLS_INDUCED_PER_RUN,
                      period_cap=None) -> dict:
    """HALT-ON-EXCEED for the single 'skills' class. `plan` = {'skills': [...]}.
    Returns {'admitted': [...], 'bound': None|{proposed,cap,scope}}. An over-cap
    proposal admits NONE (stop-and-ask). FAIL-OPEN: any error → admit nothing +
    an 'error'-scope bound record."""
    try:
        proposed_list = plan.get("skills", []) if isinstance(plan, dict) else []
        if not isinstance(proposed_list, list):
            return {"admitted": [], "bound": {"proposed": None, "cap": cap,
                                              "scope": "error"}}
        proposed = len(proposed_list)
        if proposed > cap:
            return {"admitted": [], "bound": {"proposed": proposed, "cap": cap,
                                              "scope": "run"}}
        if conn is not None and period is not None and period_cap is not None:
            used = _period_used(conn, period)
            if used + proposed > period_cap:
                return {"admitted": [], "bound": {"proposed": proposed,
                                                  "cap": period_cap,
                                                  "scope": "period",
                                                  "already_used": used}}
        return {"admitted": list(proposed_list), "bound": None}
    except Exception as exc:
        return {"admitted": [], "bound": {"proposed": None, "cap": cap,
                                          "scope": "error", "detail": repr(exc)}}


def commit_period_usage(conn, *, period: str, applied_count: int, ts: str = "") -> None:
    """Add the applied count to the per-period 'skills' counter (idempotent-additive)."""
    if applied_count <= 0:
        return
    gate_commons.add_period_usage(
        conn, prefix=_PERIOD_META_PREFIX, period=period, cls=_SKILLS_CLS, n=applied_count)
    conn.commit()
