"""Canonical UTC timestamp helpers — one source of truth for the Zulu wire format.

Stored timestamps across the engine use the second-resolution Zulu form
(``YYYY-MM-DDTHH:MM:SSZ``); :func:`now_utc_zulu` produces it and
:func:`hours_between` diffs two such stamps. Stdlib-only, so this is safe to
import from any module (no circular-import risk).

NOTE: this is intentionally NOT the same as ``datetime.isoformat()``
(microseconds + numeric offset). Call sites that emit isoformat use a
deliberately distinct format and are NOT routed through here.
"""
import datetime

# The canonical second-resolution Zulu wire format used for stored timestamps.
ZULU_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc_zulu() -> str:
    """Current UTC time in the canonical Zulu wire format."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(ZULU_FMT)


def hours_between(earlier_z: str, later_z: str) -> float:
    """Hours between two Zulu-format stamps.

    Raises ``ValueError`` on an unparseable stamp; callers treat that as
    fail-open / self-heal (proceed as if due)."""
    a = datetime.datetime.strptime(earlier_z, ZULU_FMT)
    b = datetime.datetime.strptime(later_z, ZULU_FMT)
    return (b - a).total_seconds() / 3600.0
