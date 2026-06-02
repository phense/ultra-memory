"""Pure, stdlib-only id derivation for generated skills (slug + ledger ids).

Extracted from ``skill_synthesize`` so the Tier-1, NO-LLM consumers (the
``import_learnings`` projection beat) can derive a slug's procedure id WITHOUT
transitively importing ``claude_cli`` / the OAuth draft path. ``skill_synthesize``
re-exports these names for back-compat, so ``ss.procedure_id`` etc. keep resolving.
No LLM, no SDK, no network — only ``re`` + ``hashlib``.
"""
from __future__ import annotations

import hashlib
import re


def slugify_domain(domain: str) -> str:
    # NOTE: do NOT strip a leading 'gen-' — that made derive_slug non-injective
    # (derive_slug('backtest') == derive_slug('gen-backtest')), collapsing distinct
    # domains onto one skill (the 2026-06-01 review finding). A recursive 'gen-<x>'
    # domain legitimately maps to a distinct 'gen-gen-<x>' slug; residual slugify
    # collisions are caught by the source_domain cross-domain guard in the
    # orchestrator (a slug collision → skip + diagnostic, never cross-domain supersede).
    s = re.sub(r"[^a-z0-9]+", "-", str(domain).strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "domain"


def derive_slug(domain: str) -> str:
    return "gen-" + slugify_domain(domain)


def procedure_id(slug: str) -> str:
    """One stable ledger row per slug (upserted on each redraft)."""
    return "skill-" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:24]


def backing_memory_id(slug: str, lesson_ids) -> str:
    """A fresh backing-memory id per (slug, lesson-set) — so each redraft is a new
    row and exactly one is status='active' at a time (supersede leaves the prior)."""
    key = slug + "|" + "|".join(sorted(lesson_ids))
    return "genskill-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
