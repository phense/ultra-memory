"""The AGGRESSIVE SAFETY WALL (project-agnostic; ported from Trading SP-7 §4a/§4b).

This module is the single chokepoint the three aggressive self-improvement
capabilities (auto-edit / self-reversion / contradiction quarantine) MUST funnel
every write through, plus the SP-10 generated-skill target. The capabilities are
CLIENTS of this wall and physically cannot execute an action the wall forbids.

THE WALL LIVES IN THE APPLY PATH (CODE), NEVER ONLY THE PROMPT
(the [[feedback-subagents-can-leak-secrets]] lesson: "build the constraint into
the TOOL, not the prompt"). The LLM *proposes*; this module *enforces*.
`assert_mutable` RE-READS the live row — it never trusts a `created_by`/`pinned`
field the LLM echoed back (a hallucinated "this unit is agent-authored" cannot
make a human/pinned row mutable).

§4a — PROVENANCE GATE (the non-negotiable wall).
  A unit is mutable ONLY IF:
      created_by IN ('agent','background_review')   (memory)  /
      frontmatter created_by IN (...)               (wiki page / generated skill)
    AND pinned = 0 (memory) / not in knowledge_pins (page) / not skill-protected.
  A 'human', an 'import'-of-human, OR any pinned/protected unit is IMMUTABLE.
  Fail-closed: a missing row, a missing frontmatter created_by, or any read
  error is treated as FORBIDDEN — refuse rather than risk an edit.
  A single ForbiddenTargetError is NOT a per-item skip — it is the §4a
  stop-the-world signal; the consumer (the eval hard-gate) turns it into a
  whole-run halt (zero tolerance).

§4b — ARCHIVE-NEVER-DELETE (reversibility, the 2026-05-24 lesson).
  Every aggressive verb maps to a NON-destructive engine primitive (auto-edit →
  new version + consolidate-redirect; quarantine → set_status('quarantined');
  revert → status flip + re-activate prior). NO rm / os.remove / shutil.rmtree /
  delete anywhere — a static guard test asserts this.

This module makes NO LLM call (it is the deterministic apply path) and imports no
anthropic SDK (OAuth-only by construction). The engine primitives it consumes
(set_status / consolidate / save_memory / record_link) are GENERIC; this module is
the POLICY (the "which rows are protected" wall the engine deliberately does not
enforce). PyYAML is soft-imported: when it is absent the frontmatter parse
fail-CLOSES to 'human' (immutable) — the safe degrade.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyYAML is the optional 'maintenance' extra; absent → fail-closed to 'human'.
    import yaml
except Exception:  # pragma: no cover - exercised only on a yaml-less install
    yaml = None

from ultra_memory.memory_lib import (
    consolidate,
    record_link,
    save_memory,
    set_status,
)

# The provenances the loop is ALLOWED to touch. Everything else — notably 'human'
# and 'import' (the bootstrap importer's stamp on human content) — is immutable.
MUTABLE_PROVENANCES = ("agent", "background_review")


class ForbiddenTargetError(Exception):
    """Raised by `assert_mutable` when an aggressive action targets a protected
    unit (human / import / pinned, or unprovable provenance). NOT a per-item skip
    — the §4a stop-the-world: the consumer turns one of these into a whole-run
    halt (zero tolerance). A loop attempting to edit a human/pinned rule is a bug
    or a prompt-injection, not routine."""


# --------------------------------------------------------------------------- #
# Unit descriptors — what an aggressive action targets.
# --------------------------------------------------------------------------- #

@dataclass
class MemoryUnit:
    """A `memories` row target. `echoed_*` are whatever the LLM CLAIMED the
    provenance/pin are — DELIBERATELY IGNORED by the gate (it re-reads the live
    row). They exist only so a caller can pass an LLM plan straight through; the
    wall never trusts them."""
    id: str
    echoed_created_by: str | None = field(default=None)
    echoed_pinned: bool | None = field(default=None)


@dataclass
class PageUnit:
    """A wiki-page target. `slug` keys the knowledge_pins check; `path` is the
    on-disk page whose frontmatter `created_by` is the source of truth."""
    slug: str
    path: Path
    echoed_created_by: str | None = field(default=None)


@dataclass
class SkillUnit:
    """SP-10 — a generated `SKILL.md` target. `slug` is the `gen-<…>` skill name;
    `path` is the on-disk SKILL.md (whose frontmatter `created_by` is the source of
    truth when it already exists). A SkillUnit is mutable iff it is structurally a
    generated-skill path (gen-prefixed, one level under the PROJECT `.claude/skills/`
    root, never the archive — so a static/human skill can NEVER be a target), AND
    (when the file already exists) its frontmatter created_by ∈ MUTABLE_PROVENANCES,
    AND it is not in the skill-protect registry. A NOT-yet-existing path is allowed
    (a fresh induction) because the structural gen- check already forbids writing
    over any static skill."""
    slug: str
    path: Path
    echoed_created_by: str | None = field(default=None)


# --------------------------------------------------------------------------- #
# §4a — the provenance gate (the single chokepoint).
# --------------------------------------------------------------------------- #

def assert_mutable(conn, unit) -> None:
    """The SINGLE chokepoint called immediately before EVERY aggressive write.

    Returns None if `unit` is mutable; raises `ForbiddenTargetError` otherwise.
    RE-READS the live row / live frontmatter — NEVER trusts an LLM-echoed field.
    Fail-closed on every uncertainty (missing row, missing created_by, read error).
    """
    if isinstance(unit, MemoryUnit):
        _assert_memory_mutable(conn, unit.id)
    elif isinstance(unit, PageUnit):
        _assert_page_mutable(conn, unit.slug, unit.path)
    elif isinstance(unit, SkillUnit):
        _assert_skill_mutable(conn, unit.slug, unit.path)
    else:
        # Unknown unit kind → fail-closed.
        raise ForbiddenTargetError(f"unknown unit kind: {type(unit).__name__}")
    return None


def assert_synthesis_source(conn, mem_id: str) -> None:
    """SP-10 source-eligibility gate — DISTINCT from the SP-7 mutation gate
    ``assert_mutable``. Synthesis READS a lesson to induce a NEW skill; it never mutates
    the lesson, so source eligibility is PROVENANCE-AGNOSTIC: ``backfill_import`` (the
    cold-start seed) / ``import`` / ``human`` / ``agent`` / ``background_review`` may all
    seed a skill — else the seed could never graduate (the bug that conflated SP-7
    mutability with SP-10 visibility a second time, at the draft funnel). The ONE
    protection kept: a PINNED source (a hard rule / explicitly-hot unit) is never folded
    into an auto-generated, auto-editable skill → ``ForbiddenTargetError``. Fail-closed: a
    missing row / read error → forbidden (a source must provably exist)."""
    try:
        row = conn.execute(
            "SELECT pinned FROM memories WHERE id=?", (mem_id,)).fetchone()
    except Exception as exc:
        raise ForbiddenTargetError(
            f"synthesis source {mem_id!r}: provenance read failed ({exc!r}) — refusing"
        ) from exc
    if row is None:
        raise ForbiddenTargetError(
            f"synthesis source {mem_id!r}: no live row — cannot confirm the source")
    if bool(row["pinned"]):
        raise ForbiddenTargetError(
            f"synthesis source {mem_id!r}: pinned — not folded into a generated skill")


def _assert_memory_mutable(conn, mem_id: str) -> None:
    """A memory is mutable iff created_by IN MUTABLE_PROVENANCES AND pinned=0.
    Re-reads the LIVE row. Fail-closed: a read error or a missing id → forbidden."""
    try:
        row = conn.execute(
            "SELECT created_by, pinned FROM memories WHERE id=?", (mem_id,)
        ).fetchone()
    except Exception as exc:  # fail-closed — refuse rather than risk an edit
        raise ForbiddenTargetError(
            f"memory {mem_id!r}: provenance read failed ({exc!r}) — refusing"
        ) from exc
    if row is None:
        # Cannot prove agent-authored → forbidden (NOT a free-to-edit blank).
        raise ForbiddenTargetError(
            f"memory {mem_id!r}: no live row — cannot prove agent provenance")
    created_by = str(row["created_by"])
    pinned = bool(row["pinned"])
    if created_by not in MUTABLE_PROVENANCES:
        raise ForbiddenTargetError(
            f"memory {mem_id!r}: created_by={created_by!r} is immutable to the loop")
    if pinned:
        raise ForbiddenTargetError(
            f"memory {mem_id!r}: pinned — immutable even though created_by={created_by!r}")


def _assert_page_mutable(conn, slug: str, path) -> None:
    """A wiki page is mutable iff its frontmatter created_by IN MUTABLE_PROVENANCES
    AND it is not pinned in knowledge_pins. Re-reads the LIVE frontmatter + the
    LIVE knowledge_pins row. Fail-closed on a missing/unreadable file, a missing
    created_by, or a read error."""
    # knowledge_pins is the page-pin source of truth (set_pinned source_kind=knowledge).
    try:
        pin = conn.execute(
            "SELECT pinned FROM knowledge_pins WHERE slug=?", (slug,)
        ).fetchone()
    except Exception as exc:
        raise ForbiddenTargetError(
            f"page {slug!r}: knowledge_pins read failed ({exc!r}) — refusing"
        ) from exc
    if pin is not None and bool(pin["pinned"]):
        raise ForbiddenTargetError(f"page {slug!r}: in knowledge_pins — immutable")

    # Frontmatter created_by is the page provenance source of truth.
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        raise ForbiddenTargetError(
            f"page {slug!r}: cannot read {path} ({exc!r}) — refusing") from exc
    created_by = _frontmatter_created_by(text)
    if created_by not in MUTABLE_PROVENANCES:
        # Missing created_by parses to 'human' (the engine's safe default) → forbidden.
        raise ForbiddenTargetError(
            f"page {slug!r}: frontmatter created_by={created_by!r} is immutable")


_GEN_PREFIX = "gen-"


def _under_generated_root(path) -> bool:
    """SP-10 structural guard (path-traversal defence). True iff `path` is
    `.../.claude/skills/gen-<slug>/SKILL.md` — gen-prefixed, exactly ONE level under
    a project skills root, NOT under the skills-archive. A static skill
    (risk-manager), a two-level `_generated/<slug>/` layout, and an archived skill
    all return False, so a generated-skill write can NEVER land on a static skill."""
    p = Path(path).resolve()
    sd = p.parent
    return (
        p.name == "SKILL.md"
        and sd.name.startswith(_GEN_PREFIX)
        and sd.parent.name == "skills"
        and sd.parent.parent.name == ".claude"
        and "skills-archive" not in p.parts
    )


def _skill_is_protected(conn, slug: str) -> bool:
    """A generated skill pinned via the `sp10_skill_protect:<slug>` meta flag is
    immutable to the loop (Peter's opt-out). Fail-closed: a read error → protected."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"sp10_skill_protect:{slug}",)).fetchone()
    except Exception:
        return True
    if row is None:
        return False
    return str(row[0]).strip() not in ("", "0", "false", "False")


def _assert_skill_mutable(conn, slug: str, path) -> None:
    """A generated SKILL.md is mutable iff (1) it is structurally a generated-skill
    path (`_under_generated_root` — never a static/human skill), (2) IF the file
    already exists, its frontmatter created_by ∈ MUTABLE_PROVENANCES (so the loop
    can never overwrite a hand-authored file even if one sat in a gen- dir), and
    (3) it is not in the skill-protect registry. A not-yet-existing path is allowed
    (a fresh induction) — the structural gen- check already forbids any static
    target. Fail-closed: an unreadable existing file → forbidden."""
    if not _under_generated_root(path):
        raise ForbiddenTargetError(
            f"skill {slug!r}: {path} is not a generated-skill path "
            f"(refusing — never write over a static skill)")
    if _skill_is_protected(conn, slug):
        raise ForbiddenTargetError(f"skill {slug!r}: protected (sp10_skill_protect)")
    p = Path(path)
    if p.exists():  # overwriting/superseding an existing generated skill
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as exc:
            raise ForbiddenTargetError(
                f"skill {slug!r}: cannot read {path} ({exc!r}) — refusing") from exc
        created_by = _frontmatter_created_by(text)
        if created_by not in MUTABLE_PROVENANCES:
            raise ForbiddenTargetError(
                f"skill {slug!r}: existing frontmatter created_by={created_by!r} "
                f"is immutable — refusing to overwrite")


def _frontmatter_created_by(text: str) -> str:
    """Parse the leading YAML frontmatter `created_by`. A page with NO frontmatter
    or NO created_by key defaults to 'human' — the safe-immutable default (mirrors
    the engine's `created_by` column default). Fail-closed: a parse error, or no
    YAML parser available, → 'human'."""
    if yaml is None:  # no parser → cannot prove agent provenance → immutable
        return "human"
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return "human"
    rest = text[4:]
    end = rest.find("\n---")
    if end == -1:
        return "human"
    block = rest[:end]
    try:
        fm = yaml.safe_load(block)
    except Exception:
        return "human"
    if not isinstance(fm, dict):
        return "human"
    return str(fm.get("created_by", "human"))


# --------------------------------------------------------------------------- #
# §4b — the non-destructive aggressive verb primitives.
# Each funnels through assert_mutable FIRST. NO destructive call anywhere.
# --------------------------------------------------------------------------- #

def _new_version_id(old_id: str, new_body: str) -> str:
    """A deterministic id for an auto-edited version (idempotent on old+new body)."""
    raw = f"sp7-edit:{old_id}:{new_body}"
    return "edit-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def apply_auto_edit(conn, *, old_id: str, new_body: str, new_title: str,
                    evidence: str, ts: str) -> str:
    """§5.1 auto-edit, archive-never-delete (§4b). Writes the NEW version as a
    fresh `save_memory(created_by='background_review')` row, then
    `consolidate(loser_id=old, canonical_id=new)` so the OLD row becomes
    status='redirect' with supersedes=new — its bytes preserved verbatim
    (recoverable, NOT deleted). Records a `superseded_by` edge old -> new.

    Provenance-gated: refuses (raises) if the OLD unit is human/import/pinned.
    Returns the new version id. Idempotent on (old_id, new_body)."""
    assert_mutable(conn, MemoryUnit(old_id))
    new_id = _new_version_id(old_id, new_body)
    save_memory(conn, id=new_id, type="learning", title=new_title, body=new_body,
                ts=ts, created_by="background_review")
    consolidate(conn, loser_id=old_id, canonical_id=new_id,
                reason="sp7-auto-edit", ts=ts)
    record_link(conn, src_kind="memory", src_id=old_id, predicate="superseded_by",
                dst_kind="memory", dst_id=new_id, evidence=evidence, ts=ts)
    return new_id


def apply_quarantine_pair(conn, *, id_a: str, id_b: str, reason: str,
                          ts: str) -> None:
    """§5.3 contradiction quarantine, archive-never-delete (§4b). Both units of a
    contradictory pair flip to status='quarantined' (dropped out of recall by the
    engine's status='active' filter — fully reversible via `reactivate`), and a
    `contradicts` edge connects them. The loop does NOT pick a winner (that is a
    gated edit) — it demotes BOTH for Peter's adjudication.

    Provenance-gated for BOTH members FIRST (zero-tolerance: if either is
    protected the whole quarantine raises and NEITHER unit is touched)."""
    assert_mutable(conn, MemoryUnit(id_a))
    assert_mutable(conn, MemoryUnit(id_b))
    set_status(conn, id=id_a, status="quarantined", ts=ts, reason=reason)
    set_status(conn, id=id_b, status="quarantined", ts=ts, reason=reason)
    record_link(conn, src_kind="memory", src_id=id_a, predicate="contradicts",
                dst_kind="memory", dst_id=id_b, evidence=reason, ts=ts)


def apply_revert(conn, *, regressed_id: str, prior_id: str | None,
                 ts: str) -> None:
    """§5.2 self-reversion mechanism, archive-never-delete (§4b). Demotes the
    regressed unit to status='reverted' (out of recall) and — if a prior version
    exists — re-activates it (a pure FSM flip, reversible). A graduated-then-
    regressed unit with NO prior (prior_id=None) demotes to 'quarantined' instead
    of reverting to nothing. Records a `reverted_from` edge.

    Provenance-gated on the regressed unit FIRST. (The orchestrator decides
    whether to CALL this — fork A leans propose-for-Peter — but when it is called,
    the apply path is the same reversible FSM transition.)"""
    assert_mutable(conn, MemoryUnit(regressed_id))
    if prior_id is None:
        # No prior to fall back to — quarantine (out of recall) rather than delete.
        set_status(conn, id=regressed_id, status="quarantined", ts=ts,
                   reason="sp7-revert: regressed graduation, no prior version")
        return
    assert_mutable(conn, MemoryUnit(prior_id))
    set_status(conn, id=regressed_id, status="reverted", ts=ts,
               reason="sp7-revert: regressed auto-edit")
    set_status(conn, id=prior_id, status="active", ts=ts,
               reason="sp7-revert: re-activate prior version")
    record_link(conn, src_kind="memory", src_id=regressed_id,
                predicate="reverted_from", dst_kind="memory", dst_id=prior_id,
                evidence="sp7-revert", ts=ts)


def reactivate(conn, *, id: str, ts: str, reason: str) -> None:
    """Flip a quarantined/reverted unit back to status='active' — the reversibility
    primitive (Peter adjudicates a quarantine, or a mistaken revert is undone).
    Provenance-gated (a human/pinned row should never have been demoted, but the
    gate is belt-and-suspenders here too)."""
    assert_mutable(conn, MemoryUnit(id))
    set_status(conn, id=id, status="active", ts=ts, reason=reason)
