"""Project-agnostic Tier-2 maintenance pipeline (the autonomous self-learning beats).

This package is the portable home of the heavy maintenance beats — consolidate,
self-correct (aggressive), and skill-synthesis — that previously lived consumer-side
(Trading's scripts/maintenance/). It extends the engine's light Tier-1 `maintain.py`
(prune + export + wiki_sync) with the LLM-driven Tier-2 beats, driven by a single
project-agnostic config seam (`config.MaintenanceConfig`).

Design invariants (the north-star, 2026-06-01):
  * project-agnostic — NO consumer literals; a project supplies its topics, probe
    corpus, hard rules, and wiki roots via `.ultra-memory/config.toml` + ULTRA_MEMORY_*
    env overrides (the same seam `maintain.py` already uses for wiki roots);
  * autonomous + self-governing — the beats run inside the safety wall (provenance
    gate, trigger-probe eval-gate, archive-never-delete, bounded blast radius,
    OAuth-only), failing closed; no human in the write loop;
  * session-lifecycle as the scheduler — no launchd; each beat is throttled by a
    `meta` clock so SessionStart/Stop can drive it on any platform;
  * fail-open — a beat error degrades to a no-op + one diagnostic line, never wedging
    a session.
"""
