"""The AGGRESSIVE pass's BOUNDS + CHECKPOINT + KILL SWITCH (project-agnostic;
ported from the reference SP-7 implementation §4c/§4d/§4f).

This is the second pillar of the safety wall (alongside `aggressive_wall.py`, the
§4a/§4b provenance-gate + archive-never-delete primitives). Where the wall decides
*which rows are touchable*, this module decides *how many* may be touched, *whether
the run is recoverable* before it starts, and *whether the run runs at all*. Like
the wall, every guard here lives in the APPLY PATH (code), never only the prompt:
the LLM *proposes* a plan; this module *enforces* the caps, the clean-tree
precondition, and the kill switch. The LLM cannot talk its way past a bound.

§4c — BOUNDED BLAST RADIUS + HALT-ON-EXCEED.
  Per-run caps (conservative defaults): MAX_EDITS_PER_RUN=3, MAX_REVERSIONS_PER_RUN=3,
  MAX_QUARANTINES_PER_RUN=5. A small bound is the structural defense against the
  2026-05-24 95%-blast. HALT-ON-EXCEED, NOT truncate-and-continue: a plan with MORE
  actions of a class than the cap applies NONE of that class (not the first N),
  logs 'bound exceeded: proposed=K cap=N', and surfaces it in the digest. A GLOBAL
  per-period aggregate cap (a `meta` counter, reset per period) blocks stacked
  re-runs.

§4d — PRE-RUN GIT CHECKPOINT (the recoverability anchor).
  Before any aggressive write, tag the root + a memory_export snapshot
  (`pre-sp7-aggressive-<date>`). CLEAN-TREE PRECONDITION: a dirty tree makes the
  checkpoint ambiguous (the 2026-05-24 untracked-files gap), so the pass REFUSES TO
  START (fail-soft skip). The result carries the exact one-command rollback.

§4f — KILL SWITCH + DRY-RUN + FAIL-OPEN.
  SP7_AGGRESSIVE_DISABLE (absent by default → the beat runs; a consumer SETS it to
  disable) short-circuits the whole pass to a no-op. SP7_AGGRESSIVE_DRYRUN runs
  reflection + eval + the full plan + the digest, but applies NOTHING. FAIL-OPEN:
  any error degrades to a SAFE no-op + one diagnostic line — never wedges the run,
  never proceeds unbounded.

This module makes NO LLM call and imports no anthropic SDK (OAuth-only by
construction). It is GENERIC-consuming POLICY: the engine primitives (the `meta`
table) are content-free; the caps + cadence are the consumer's. The git verbs it
runs are read-only status checks + `tag`/`add` — never a destructive `reset --hard`
of content (archive-never-delete).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ultra_memory.maintenance import gate_commons

# --------------------------------------------------------------------------- #
# §4c — per-run caps (conservative defaults; spec §4c / fork B "conservative").
# Overridable per-call so a future on-demand run can tighten, never loosen blind.
# --------------------------------------------------------------------------- #

MAX_EDITS_PER_RUN = 3
MAX_REVERSIONS_PER_RUN = 3
MAX_QUARANTINES_PER_RUN = 5

# The plan classes, in the order they are reported. Each maps to a per-run cap.
_CLASS_CAPS = {
    "edits": "MAX_EDITS_PER_RUN",
    "reversions": "MAX_REVERSIONS_PER_RUN",
    "quarantines": "MAX_QUARANTINES_PER_RUN",
}

# The `meta` key prefix for the global per-period aggregate counter. The engine's
# `meta` table is a generic (key, value) KV — we namespace our counters under it.
_PERIOD_META_PREFIX = "sp7_aggressive_period"


# --------------------------------------------------------------------------- #
# §4c — bounds enforcement result
# --------------------------------------------------------------------------- #

@dataclass
class CapResult:
    """The outcome of `enforce_caps`.

    `admitted`  — the plan partitioned by class, with each over-cap class EMPTIED
                  (halt-on-exceed) and each under-cap class passed through whole.
    `bounds_hit`— a list of bound records, each a dict
                  {cls, proposed, cap, scope}.  `scope` is 'run' (per-run cap),
                  'period' (global aggregate cap), or 'error' (fail-open record).
                  An empty list means no bound was hit.
    """
    admitted: dict = field(default_factory=dict)
    bounds_hit: list = field(default_factory=list)


def format_bound_message(bound: dict) -> str:
    """The exact §4c log line: 'bound exceeded: proposed=K cap=N' (+ scope/class
    context).  Stable shape so the digest + the log can both parse it."""
    scope = bound.get("scope", "run")
    return (f"bound exceeded: proposed={bound.get('proposed')} "
            f"cap={bound.get('cap')} (class={bound.get('cls')}, scope={scope})")


def enforce_caps(plan, *, conn=None, period=None, period_cap_edits=None,
                 period_cap_reversions=None, period_cap_quarantines=None,
                 caps=None) -> CapResult:
    """Apply the per-run caps (+ optional global per-period aggregate cap) to a
    proposed plan, HALT-ON-EXCEED.

    `plan` is a dict {edits: [...], reversions: [...], quarantines: [...]} of
    opaque action records.  Returns a `CapResult`: each over-cap class is EMPTIED
    (the §4c stop-and-ask, not truncate-the-first-N) and recorded in `bounds_hit`;
    each under-cap class passes through unchanged.

    If `conn` + `period` are supplied, an ADDITIVE second gate applies: the global
    per-period aggregate cap (a `meta` counter).  Even a per-run-legal plan is
    halted for a class whose already-used period budget + this run would exceed
    the period cap — so stacked re-runs cannot accumulate past the budget.

    FAIL-OPEN: any error (a malformed plan, a meta read failure) degrades to an
    EMPTY admitted set for the offending class + an 'error'-scope bound record;
    it NEVER raises out into the maintenance run.
    """
    run_caps = {
        "edits": MAX_EDITS_PER_RUN,
        "reversions": MAX_REVERSIONS_PER_RUN,
        "quarantines": MAX_QUARANTINES_PER_RUN,
    }
    if caps:
        run_caps.update(caps)

    period_caps = {
        "edits": period_cap_edits,
        "reversions": period_cap_reversions,
        "quarantines": period_cap_quarantines,
    }

    res = CapResult(admitted={}, bounds_hit=[])

    for cls in _CLASS_CAPS:
        try:
            proposed_list = plan.get(cls, []) if isinstance(plan, dict) else []
            # A malformed (non-list) class → fail-open: halt the class.
            if not isinstance(proposed_list, list):
                res.admitted[cls] = []
                res.bounds_hit.append({
                    "cls": cls, "proposed": None, "cap": run_caps[cls],
                    "scope": "error"})
                continue

            proposed = len(proposed_list)
            cap = run_caps[cls]

            # --- per-run cap (halt-on-exceed) ---
            if proposed > cap:
                res.admitted[cls] = []
                res.bounds_hit.append({
                    "cls": cls, "proposed": proposed, "cap": cap, "scope": "run"})
                continue

            # --- global per-period aggregate cap (additive second gate) ---
            pcap = period_caps[cls]
            if conn is not None and period is not None and pcap is not None:
                used = _period_used(conn, period=period, cls=cls)
                if used + proposed > pcap:
                    res.admitted[cls] = []
                    res.bounds_hit.append({
                        "cls": cls, "proposed": proposed, "cap": pcap,
                        "scope": "period", "already_used": used})
                    continue

            # under both caps → admit whole.
            res.admitted[cls] = list(proposed_list)
        except Exception as exc:  # fail-open per class — never wedge the run
            res.admitted[cls] = []
            res.bounds_hit.append({
                "cls": cls, "proposed": None, "cap": run_caps.get(cls),
                "scope": "error", "detail": repr(exc)})

    return res


# --------------------------------------------------------------------------- #
# §4c — the global per-period aggregate counter (the `meta` KV)
# --------------------------------------------------------------------------- #

def _period_key(period: str, cls: str) -> str:
    return gate_commons.period_meta_key(_PERIOD_META_PREFIX, period, cls)


def _period_used(conn, *, period: str, cls: str) -> int:
    """Read how many actions of `cls` were already applied in `period`. Missing
    counter → 0; a read error → a huge sentinel (fail-CLOSED-to-safety). Thin wrapper
    over the shared gate_commons primitive (the one audited fail-closed read)."""
    return gate_commons.period_used(conn, prefix=_PERIOD_META_PREFIX, period=period, cls=cls)


def period_remaining(conn, *, period: str, cls: str, period_cap: int) -> int:
    """How many MORE actions of `cls` may be applied in `period` before the global
    per-period aggregate cap (§4c) is hit. = max(0, period_cap - already_used).

    Used to gate a track (e.g. the quarantine track) whose apply happens INSIDE the
    track's own call — the orchestrator passes this as the track's `max_*` so the
    track's own halt-on-exceed enforces the PERIOD budget too, not only the per-run
    cap. Fail-CLOSED-to-safety: `_period_used` returns a huge sentinel on a read
    error (treat budget as exhausted), so this returns 0 — the safe refusal."""
    used = _period_used(conn, period=period, cls=cls)
    rem = period_cap - used
    return rem if rem > 0 else 0


def commit_period_usage(conn, *, period: str, applied: dict, ts: str) -> None:
    """After a run applies its admitted actions, increment the per-period counters
    by the count actually applied — so a later same-period run sees the consumed
    budget.  `applied` is the admitted dict {cls: [...]}.  Idempotent-additive: it
    ADDS to the existing counter.  (`ts` is accepted for symmetry / future audit;
    the `meta` KV is intentionally lightweight and not itself audited.)"""
    for cls in _CLASS_CAPS:
        n = len(applied.get(cls, []) or [])
        gate_commons.add_period_usage(
            conn, prefix=_PERIOD_META_PREFIX, period=period, cls=cls, n=n)
    conn.commit()


# --------------------------------------------------------------------------- #
# §4d — pre-run git checkpoint + clean-tree precondition
# --------------------------------------------------------------------------- #

@dataclass
class CheckpointResult:
    """The outcome of `pre_run_checkpoint`.

    `ok`               — True iff the checkpoint was made cleanly; the caller MUST
                         NOT apply any aggressive write when ok is False.
    `tag`              — the tag name created (or that would be created).
    `reason`           — why ok is False (dirty tree / git error / export error).
    `rollback_command` — the documented one-command undo (spec §4d).
    """
    ok: bool
    tag: str
    reason: str | None = None
    rollback_command: str = ""


def _tree_is_clean(repo_root: Path, env=None) -> bool:
    """A tree is clean iff `git status --porcelain` is empty — NO modified, staged,
    OR UNTRACKED files (untracked is the exact 2026-05-24 gap: the deleted atomics
    were untracked, so a checkpoint could not have restored them).  Any git error
    propagates (the caller treats it as not-clean / fail-soft)."""
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root), capture_output=True, text=True, env=env, check=True)
    return out.stdout.strip() == ""


def pre_run_checkpoint(*, repo_root, date: str, export_fn, env=None,
                       tag_prefix: str = "pre-sp7-aggressive-"
                       ) -> CheckpointResult:
    """Create the pre-run recoverability anchor (spec §4d), with the CLEAN-TREE
    precondition.  Returns a `CheckpointResult`; the caller applies aggressive
    writes ONLY if `ok` is True.

    1. Verify the tree is clean (no modified/staged/UNTRACKED) — else fail-soft
       skip (the 2026-05-24 gap).
    2. Run the `memory_export` snapshot (`export_fn`, supplied by the orchestrator
       so this module stays free of the export import path / live-store coupling).
    3. Tag `<tag_prefix><date>`.

    FAIL-SOFT on EVERY failure (dirty tree, git error, export error): ok=False, a
    reason, NO partial checkpoint left in a state that would mislead a rollback.
    NEVER raises out — a checkpoint that cannot be made cleanly just skips the
    aggressive pass.
    """
    repo_root = Path(repo_root)
    tag = f"{tag_prefix}{date}"
    rollback = (
        f"cd {repo_root} && git reset --soft {tag}   "
        f"# undo the self-improvement commits back to the checkpoint; "
        f"then re-run memory_import on the {tag} memory_export snapshot")

    try:
        clean = _tree_is_clean(repo_root, env=env)
    except Exception as exc:
        return CheckpointResult(
            ok=False, tag=tag,
            reason=f"git status failed (not a repo / git error): {exc!r}",
            rollback_command=rollback)
    if not clean:
        return CheckpointResult(
            ok=False, tag=tag,
            reason="working tree is dirty (modified/staged/untracked) — refusing "
                   "to checkpoint (the 2026-05-24 untracked-files gap)",
            rollback_command=rollback)

    # Export snapshot FIRST (so the tag and the export reflect the same state).
    try:
        export_fn()
    except Exception as exc:
        return CheckpointResult(
            ok=False, tag=tag,
            reason=f"memory_export snapshot failed: {exc!r}",
            rollback_command=rollback)

    # Tag the checkpoint.
    try:
        subprocess.run(
            ["git", "tag", "-f", tag],
            cwd=str(repo_root), capture_output=True, text=True, env=env,
            check=True)
    except Exception as exc:
        return CheckpointResult(
            ok=False, tag=tag,
            reason=f"git tag failed: {exc!r}",
            rollback_command=rollback)

    return CheckpointResult(ok=True, tag=tag, reason=None,
                            rollback_command=rollback)


# --------------------------------------------------------------------------- #
# §4f — kill switch + dry-run + the single run gate
# --------------------------------------------------------------------------- #

# Presence-based per the spec ('default present in the cron env'): the var being
# SET (to any value, incl. empty) disables; an operator REMOVES it to enable.
_DISABLE_ENV = "SP7_AGGRESSIVE_DISABLE"
_DRYRUN_ENV = "SP7_AGGRESSIVE_DRYRUN"


def is_disabled(env=None) -> bool:
    """True iff SP7_AGGRESSIVE_DISABLE is present (set to anything, incl. '')."""
    return gate_commons.is_env_present(_DISABLE_ENV, env)


def is_dry_run(env=None) -> bool:
    """True iff SP7_AGGRESSIVE_DRYRUN is present (set to anything, incl. '')."""
    return gate_commons.is_env_present(_DRYRUN_ENV, env)


@dataclass
class GateDecision:
    """The single decision the orchestrator reads before doing ANYTHING aggressive.

    `mode`      — 'noop' (disabled or errored → do nothing at all),
                  'dryrun' (plan + eval + digest, apply NOTHING),
                  'live' (plan + eval + digest + APPLY within the wall + bounds).
    `may_apply` — True ONLY in 'live'. The orchestrator must gate every write on it.
    `reason`    — a one-line human note for the digest.
    """
    mode: str
    may_apply: bool
    reason: str


def run_gate(*, env=None, log=lambda _m: None) -> GateDecision:
    """The single entry the orchestrator calls FIRST.  Reads the kill switch +
    dry-run flag and returns the run mode.  DISABLE takes precedence over DRYRUN
    (a disabled pass does not even plan).

    FAIL-OPEN, fail-CLOSED-to-safety: any error reading the env → a SAFE 'noop'
    decision (do nothing) + one diagnostic line; it never raises out, and an error
    can only ever make the pass do LESS, never proceed unbounded.
    """
    try:
        if is_disabled(env):
            msg = (f"SP-7 aggressive pass DISABLED ({_DISABLE_ENV} present) — "
                   f"no-op (the conservative consolidate drain is unaffected).")
            log(msg)
            return GateDecision(mode="noop", may_apply=False, reason=msg)
        if is_dry_run(env):
            msg = (f"SP-7 aggressive pass DRY-RUN ({_DRYRUN_ENV} present) — "
                   f"plan + eval + digest, applies NOTHING.")
            log(msg)
            return GateDecision(mode="dryrun", may_apply=False, reason=msg)
        msg = "SP-7 aggressive pass LIVE — plan + eval + digest + bounded apply."
        log(msg)
        return GateDecision(mode="live", may_apply=True, reason=msg)
    except Exception as exc:  # fail-open to the safe no-op
        msg = f"SP-7 aggressive gate errored — degrading to no-op ({exc!r})."
        try:
            log(msg)
        except Exception:
            pass
        return GateDecision(mode="noop", may_apply=False, reason=msg)
