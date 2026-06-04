"""Shared SP-7 / SP-10 gate primitives (the blast-radius safety wall's read side).

The presence-based kill-switch reader and the per-period blast-radius budget counter
(with the 10**9 fail-CLOSED-to-safety read) were duplicated byte-for-byte between
``aggressive_bounds`` (per-action-class) and ``synthesize_bounds`` (a single 'skills'
class). Centralized here so the fail-closed invariant lives in ONE audited place.

The per-period ``meta`` keys are byte-identical to the per-module originals
(``<prefix>:<period>:<cls>``) — do NOT change the shape: a changed key would orphan
live budget state in the consumer's DB.

NOTE: this is the PRESENCE-based kill-switch convention (a var SET to anything, incl.
'', disables; the operator REMOVES it to enable). It is intentionally NOT the
VALUE-truthy SP-8 feature-flag reader (``aggressive_run._env_truthy``: only
'1'/'true'/'yes' enables) — opposite defaults, a different convention, not merged.
"""
from __future__ import annotations

import os

# Fail-CLOSED-to-safety sentinel: an unreadable counter is treated as "budget already
# exhausted", so a read error can never accidentally unlock more writes.
_BUDGET_EXHAUSTED = 10 ** 9


def is_env_present(name: str, env=None) -> bool:
    """True iff `name` is SET in the env (to anything, incl. ''). The presence-based
    kill-switch / dry-run convention."""
    env = os.environ if env is None else env
    return env.get(name) is not None


_OPTOUT_VALUES = ("0", "false", "no", "off")


def is_enabled_default_on(name, env=None) -> bool:
    """Opt-OUT reader: a feature is ON unless its env var is an explicit disable
    value ('0'/'false'/'no'/'off', case-insensitive). Unset ⇒ ON. The inverse of
    `is_env_present` (which is the opt-IN / kill-switch reader)."""
    src = env if env is not None else os.environ
    return str(src.get(name, "")).strip().lower() not in _OPTOUT_VALUES


def period_meta_key(prefix: str, period: str, cls: str) -> str:
    """The ``meta`` KV key for the (period, cls) per-period counter."""
    return f"{prefix}:{period}:{cls}"


def period_used(conn, *, prefix: str, period: str, cls: str) -> int:
    """How many `cls` actions were already applied in `period`. Missing counter → 0.
    Fail-CLOSED-to-safety: a read error → a huge sentinel (treat the budget as
    exhausted) so an unreadable counter can never unlock more writes."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (period_meta_key(prefix, period, cls),)).fetchone()
    except Exception:
        return _BUDGET_EXHAUSTED
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def add_period_usage(conn, *, prefix: str, period: str, cls: str, n: int) -> None:
    """Additively bump the (period, cls) counter by `n` (no-op when n<=0). Does NOT
    commit — the caller commits once after bumping all of its classes."""
    if n <= 0:
        return
    used = period_used(conn, prefix=prefix, period=period, cls=cls)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (period_meta_key(prefix, period, cls), str(used + n)))
