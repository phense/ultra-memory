"""Self-learning `Learnings.md` import + projection driver (project-agnostic).

Migrated from the Trading-side `scripts/maintenance/import_learnings.py` into the
plugin so installing ultra-memory ships the whole self-learning projection loop. Two
responsibilities:

  1. The ONE-TIME, idempotent, NO-LLM IMPORTER (SP-6 §6.5 / SP-5 D5 DATA-LOSS guard):
     lifts each hand-written `Learnings.md` into `memories` rows
     (`index_hook=<skill>`, `created_by='import'`) BEFORE the file is switched to a
     regenerated `export_learnings_projection` view — else the projection regenerates
     EMPTY and overwrites the hand-written learnings. The hard ordering invariant
     (D6): import-THEN-switch, per file, atomic, idempotent. `switch_to_projection`
     REFUSES (`ImportIncompleteError`) until that skill's import is complete — the
     in-code fence, not just a prompt.

  2. The WEEKLY projection regen (Tier-1, no-LLM): rebuilds each per-skill
     `Learnings.md` from the store AND — for Model B (projection-coupled skill
     evolution, spec 2026-06-02) — refreshes each ACTIVE generated skill's managed
     `<!-- BEGIN/END auto-learnings -->` block from the union-blend of its
     source-domain + own-usage lessons. The frozen frontmatter trigger is never
     touched; the refresh is provenance-gated through the SP-7 wall and costs zero
     LLM tokens.

PROJECT-AGNOSTIC (hard NFR): the registry of which files to project is the
CONSUMER's `config.self_learning_files` ((rel_path, skill_tag) pairs) ∪ the
generated-skill glob — there is NO project literal, no hardcoded skill name. A
fresh, registry-less install simply projects its generated skills.

Fail-open (Tier-1 invariant): a missing/unreadable file or a regen/refresh error
degrades to a no-op + one diagnostic line on stderr; it NEVER raises into the caller
and NEVER overwrites a source file with empty content.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from ultra_memory import memory_lib
# Aliased through a module attribute so a fail-open test can monkeypatch it.
from ultra_memory.memory_export import (
    export_learnings_projection as _export_learnings_projection,
    render_union_blend_block,
    BLEND_CAP,
    BLEND_HALFLIFE_DAYS,
)
from ultra_memory.maintenance import skill_fs
# skill_ids is stdlib-only (no claude_cli) — keeps this Tier-1 no-LLM beat free of
# the OAuth draft path even transitively.
from ultra_memory.maintenance.skill_ids import procedure_id
from ultra_memory.maintenance.aggressive_wall import (
    ForbiddenTargetError,
    SkillUnit,
    assert_mutable,
)


# --------------------------------------------------------------------------- #
# The registry — CONSUMER-fed (config.self_learning_files) ∪ generated skills.
# --------------------------------------------------------------------------- #

def discover_generated_skill_files(repo_root):
    """Every ACTIVE generated skill (`.claude/skills/gen-*/SKILL.md`) is a
    first-class self-learning skill — it gets its own `Learnings.md` projection
    (from `memories WHERE index_hook='gen-<slug>'`) so its captured lessons can
    themselves graduate and re-feed synthesis. Archived skills (under
    `.claude/skills-archive/`) are excluded (not on the scan path). Fail-open: any
    error → no generated files (the static registry still regenerates)."""
    out = []
    try:
        skills = Path(repo_root) / ".claude" / "skills"
        for sub in sorted(skills.glob("gen-*")):
            if sub.is_dir() and (sub / "SKILL.md").is_file():
                out.append((f".claude/skills/{sub.name}/Learnings.md", sub.name))
    except Exception:
        pass
    return out


def all_self_learning_files(repo_root, self_learning_files):
    """The consumer registry ∪ the dynamically-discovered generated skills. The
    registry is `config.self_learning_files` ((rel_path, skill_tag) pairs); the
    engine names no project file of its own."""
    return list(self_learning_files or []) + discover_generated_skill_files(repo_root)


# --------------------------------------------------------------------------- #
# Section heading → node_type. A heading not in the map falls back to "learning".
# --------------------------------------------------------------------------- #
_SECTION_NODE_TYPE = {
    "what has worked": "worked",
    "what has failed": "failed",
    "patterns and preferences": "pattern",
    "open questions": "open-question",
    "trading-outcome feedback (promoted to wiki)": "trading-outcome",
}

_PLACEHOLDER = "(none yet)"
# Sentinel substrings that mark a file as a genuinely-empty placeholder (no real
# learning prose): the `(none yet)` per-section sentinel and the projection's
# empty-store sentinel.
_EMPTY_SENTINELS = (_PLACEHOLDER, "_no learnings recorded yet._")
_META_PREFIX = "learnings_import_complete"


class ImportIncompleteError(RuntimeError):
    """Raised by `switch_to_projection` when the skill's import has not completed —
    the SP-5 D5 DATA-LOSS fence (refuse rather than regenerate an empty projection)."""


def _warn(msg):
    print(f"[import_learnings] {msg}", file=sys.stderr)


def _node_type_for(heading):
    return _SECTION_NODE_TYPE.get(heading.strip().lower(), "learning")


def _first_sentence(body, *, limit=120):
    """Title = the first sentence of the bullet body, bounded. Falls back to the
    first line / a truncation when there is no sentence terminator."""
    flat = " ".join(body.split())
    for term in (". ", "! ", "? "):
        idx = flat.find(term)
        if idx != -1:
            return flat[: idx + 1].strip()
    if flat.endswith("."):
        return flat
    return flat[:limit].strip()


def _is_content_full(text):
    """True when the file carries REAL learning prose beyond the H1 title, the
    `---` horizontal rule, and the empty-placeholder sentinels. Conservative: when
    in doubt (real prose present) → True, so a malformed-but-content-full file never
    silently disarms the SP-5 D5 data-loss fence."""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            continue
        if set(stripped) <= {"-"} and len(stripped) >= 3:
            continue
        low = stripped.lower()
        if any(sentinel in low for sentinel in _EMPTY_SENTINELS):
            continue
        return True
    return False


def parse_learnings(text):
    """Split a Learnings.md into per-learning entries (no LLM, section-aware).

    Recognizes BOTH source formats so the parse<->export round-trip is idempotent:
    the ORIGINAL hand-written format (`## ` sections + column-0 `- ` bullets) and the
    PROJECTION format emitted by `export_learnings_projection` (`### {title}` entry
    headings + prose bodies, no `## ` sections). Returns a list of dicts:
    {title, body, node_type, section}.
    """
    lines = text.splitlines()
    learnings = []

    section = None
    node_type = "learning"
    bullet = None
    prose = []
    entry_title = None
    entry_body = None

    def flush_bullet():
        nonlocal bullet
        if bullet is None:
            return
        body = "\n".join(bullet).rstrip()
        bullet = None
        stripped = body.strip()
        if not stripped or _PLACEHOLDER in stripped.lower():
            return
        learnings.append({
            "title": _first_sentence(stripped),
            "body": stripped,
            "node_type": node_type,
            "section": section,
        })

    def flush_prose():
        nonlocal prose
        joined = "\n".join(prose).strip()
        prose = []
        if not joined or _PLACEHOLDER in joined.lower():
            return
        learnings.append({
            "title": _first_sentence(joined),
            "body": joined,
            "node_type": node_type,
            "section": section,
        })

    def flush_entry():
        nonlocal entry_title, entry_body
        if entry_title is None:
            return
        title = entry_title.strip()
        body = "\n".join(entry_body).strip()
        entry_title = None
        entry_body = None
        if _PLACEHOLDER in (body or "").lower() or _PLACEHOLDER in title.lower():
            return
        if not title and not body:
            return
        learnings.append({
            "title": title or _first_sentence(body),
            "body": body or title,
            "node_type": node_type,
            "section": section,
        })

    def flush_all():
        flush_bullet()
        flush_prose()
        flush_entry()

    for raw in lines:
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()

        if stripped.startswith("### "):
            flush_all()
            entry_title = stripped[4:].strip()
            entry_body = []
            continue

        if entry_title is not None:
            entry_body.append(raw)
            continue

        if stripped.startswith("## "):
            flush_all()
            section = stripped[3:].strip()
            node_type = _node_type_for(section)
            continue

        if section is None:
            continue

        if indent == 0 and stripped.startswith("- "):
            flush_bullet()
            flush_prose()
            bullet = [stripped[2:]]
            continue

        if bullet is not None:
            if not stripped:
                flush_bullet()
            else:
                bullet.append(stripped)
            continue

        if stripped:
            prose.append(stripped)

    flush_all()
    return learnings


def learning_id(skill_tag, learning):
    """A deterministic content-hash id so a re-parse of the SAME bullet produces the
    SAME row id (no duplicate on re-import even without the flag)."""
    raw = f"learnings:{skill_tag}:{learning['section']}:{learning['body']}"
    return "lrn-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _meta_key(skill_tag):
    return f"{_META_PREFIX}:{skill_tag}"


def import_complete(conn, skill_tag):
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (_meta_key(skill_tag),)
    ).fetchone()
    return bool(row) and str(row[0]) == "1"


def _mark_import_complete(conn, skill_tag):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_meta_key(skill_tag),),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def import_file(conn, path, *, skill_tag, ts):
    """Import ONE Learnings.md into `memories` rows. Idempotent + fail-open.

    Returns the number of rows written (0 if the per-skill flag already set, or on a
    fail-open no-op). Each row: id=content-hash, index_hook=skill_tag,
    created_by='import', node_type from the section. The per-skill
    `meta.learnings_import_complete:<skill>` flag short-circuits re-runs.
    """
    if import_complete(conn, skill_tag):
        return 0
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, IsADirectoryError) as exc:
        _warn(f"cannot read {p}: {exc} — no-op")
        return 0
    try:
        learnings = parse_learnings(text)
    except Exception as exc:  # pragma: no cover — defensive fail-open
        _warn(f"parse failed for {p}: {exc} — no-op")
        return 0

    parsed = len(learnings)
    written = 0
    failed = 0
    for learning in learnings:
        try:
            memory_lib.save_memory(
                conn,
                id=learning_id(skill_tag, learning),
                type="memory",
                title=learning["title"],
                body=learning["body"],
                ts=ts,
                index_hook=skill_tag,
                node_type=learning["node_type"],
                created_by="import",
            )
            written += 1
        except Exception as exc:  # per-row fail-open
            failed += 1
            _warn(f"save failed for one learning in {p}: {exc} — skipping")

    # Stamp completion only when the import has legitimately + fully run. REFUSE to
    # stamp on a ZERO-capture of a CONTENT-FULL file or a PARTIAL save failure — both
    # would silently disarm the SP-5 D5 data-loss fence.
    if parsed == 0 and _is_content_full(text):
        _warn(
            f"zero-capture of a CONTENT-FULL file {p} (parsed 0 learnings from real "
            f"prose) — NOT stamping import complete for '{skill_tag}' (SP-5 D5 guard)"
        )
        return written
    if written < parsed:
        _warn(
            f"PARTIAL import of {p}: {written}/{parsed} learnings saved ({failed} "
            f"failed) — NOT stamping import complete for '{skill_tag}' (SP-5 D5 guard)"
        )
        return written
    _mark_import_complete(conn, skill_tag)
    return written


def regenerate_projection(conn, path, *, skill_tag, title=None):
    """Rebuild a Learnings.md from the store (D14, Tier-1, no LLM).

    DATA-LOSS-SAFE (SP-5 D5): SKIPS — leaving the target file UNTOUCHED — unless that
    skill's import is complete. A gen-* tag is a projection FROM BIRTH (no hand-written
    prose to lose) so the fence does not apply to it. Fail-open: on any engine error
    it returns None + leaves the file UNTOUCHED. Returns the projected-learning count
    on a successful regen."""
    if not skill_tag.startswith("gen-") and not import_complete(conn, skill_tag):
        _warn(f"projection regen SKIPPED for {skill_tag} → {path}: import not "
              f"complete — leaving the hand-written file untouched (SP-5 D5 guard)")
        return None
    try:
        return _export_learnings_projection(conn, path, skill_tag=skill_tag, title=title)
    except Exception as exc:
        _warn(f"projection regen failed for {skill_tag} → {path}: {exc} — no-op")
        return None


def switch_to_projection(conn, path, *, skill_tag, title=None):
    """Switch a hand-written Learnings.md to a regenerated projection view.

    THE DATA-LOSS FENCE (SP-5 D5): REFUSES (raises ImportIncompleteError) unless that
    skill's import is complete — else the projection would regenerate EMPTY and
    overwrite the hand-written learnings."""
    if not import_complete(conn, skill_tag):
        raise ImportIncompleteError(
            f"refusing to switch {path} to a projection: import for skill "
            f"'{skill_tag}' is not complete (would regenerate an EMPTY file — SP-5 D5)"
        )
    return regenerate_projection(conn, path, skill_tag=skill_tag, title=title)


# --------------------------------------------------------------------------- #
# Model B — the weekly generated-skill managed-block refresh (no LLM).
# --------------------------------------------------------------------------- #

def _resolve_block_hooks(conn, slug):
    """The union feed for a generated skill's managed block: its source domain (read
    from the procedures ledger `steps.source_domain`, stored by apply_plan) ∪ the
    `gen-<slug>` own-usage feed. Fail-open: an unknown / unparseable procedure → the
    own-usage feed only. render_union_blend_block de-dups, so order is irrelevant."""
    hooks = []
    try:
        row = conn.execute("SELECT steps FROM procedures WHERE id=?",
                           (procedure_id(slug),)).fetchone()
        if row and row[0]:
            steps = json.loads(row[0])
            sd = steps.get("source_domain")
            if sd:
                hooks.append(sd)
    except Exception:
        pass
    hooks.append(slug)
    return hooks


def refresh_generated_skill_blocks(conn, repo_root, *, ts, cap=BLEND_CAP,
                                   halflife_days=BLEND_HALFLIFE_DAYS,
                                   audit_dir=None, log=_warn):
    """For each ACTIVE generated skill on disk (`.claude/skills/gen-*/SKILL.md`),
    re-render the union-blend managed block and splice it into the SKILL.md marked
    region (no LLM, frozen frontmatter). Provenance-gated per skill via the wall's
    `assert_mutable(SkillUnit)` — a protected / hand-authored gen skill is SKIPPED (a
    Tier-1 fail-open skip; a ForbiddenTargetError here is the correct protect signal,
    NOT the aggressive-loop whole-run halt). Returns the count of skills refreshed."""
    refreshed = 0
    try:
        skills = skill_fs.skills_root(repo_root)
        gen_dirs = sorted(skills.glob("gen-*")) if skills.exists() else []
    except Exception:
        gen_dirs = []
    for sub in gen_dirs:
        slug = sub.name
        md = sub / "SKILL.md"
        if not md.is_file():
            continue
        try:
            assert_mutable(conn, SkillUnit(slug=slug, path=md))
        except ForbiddenTargetError as exc:
            log(f"skill block refresh SKIPPED for {slug}: {exc}")
            continue
        try:
            hooks = _resolve_block_hooks(conn, slug)
            block = render_union_blend_block(conn, hooks=hooks, now=ts, cap=cap,
                                             halflife_days=halflife_days)
            skill_fs.rewrite_auto_block(repo_root, slug, block, ts=ts,
                                        audit_dir=audit_dir)
            refreshed += 1
        except Exception as exc:  # per-skill fail-open
            log(f"skill block refresh failed for {slug}: {exc} — no-op")
    return refreshed


# --------------------------------------------------------------------------- #
# The learnings beat (run_pipeline registry entry; Tier-1, NO LLM).
# --------------------------------------------------------------------------- #

def beat(conn, config, ts, env):
    """The `run_pipeline` registry entry for the LEARNINGS projection-regen beat
    (Tier-1, NO LLM, weekly). Rebuilds each consumer-declared per-skill Learnings.md
    + each generated skill's own Learnings.md from the store (D5-fenced for static
    skills, bypassed for gen-* projections), then refreshes each active generated
    skill's Model B managed block. Threads the config seam (self_learning_files
    registry + project_dir + briefings_dir audit). Fail-open per file."""
    repo_root = Path(config.project_dir)
    audit_dir = (Path(config.briefings_dir) / "maintenance-logs"
                 if getattr(config, "briefings_dir", None) else None)
    regenerated = 0
    for rel, skill_tag in all_self_learning_files(repo_root, config.self_learning_files):
        n = regenerate_projection(conn, repo_root / rel, skill_tag=skill_tag)
        if n is not None:
            regenerated += 1
    refreshed = refresh_generated_skill_blocks(conn, repo_root, ts=ts,
                                               audit_dir=audit_dir)
    return {"regenerated": regenerated, "blocks_refreshed": refreshed}


# --------------------------------------------------------------------------- #
# CLI — the orchestrator runs the LIVE one-time import (cron-paused + checkpointed)
# + the nightly --regen-only path. The registry resolves from the consumer config.
# --------------------------------------------------------------------------- #

def _now_z():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    import argparse
    from ultra_memory.maintenance.config import load_config

    ap = argparse.ArgumentParser(
        description="Import + project the self-learning Learnings.md files.")
    ap.add_argument("--db", required=True, help="path to memory.db")
    ap.add_argument("--repo-root",
                    default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
                    help="project root for resolving registry paths")
    ap.add_argument("--switch", action="store_true",
                    help="after import, switch each file to a projection (import-then-switch)")
    ap.add_argument("--regen-only", action="store_true",
                    help="regenerate projections + refresh gen-skill blocks only (Tier-1 nightly)")
    args = ap.parse_args(argv)

    conn = memory_lib.open_memory_db(args.db)
    repo = Path(args.repo_root)
    cfg = load_config(project_dir=repo, env=os.environ)
    ts = _now_z()
    try:
        files = all_self_learning_files(repo, cfg.self_learning_files)
        if args.regen_only:
            for rel, skill_tag in files:
                n = regenerate_projection(conn, repo / rel, skill_tag=skill_tag)
                print(f"regen {skill_tag}: {n} learnings")
            audit_dir = (repo / cfg.briefings_dir / "maintenance-logs"
                         if cfg.briefings_dir else None)
            r = refresh_generated_skill_blocks(conn, repo, ts=ts, audit_dir=audit_dir)
            print(f"refreshed {r} generated-skill blocks")
            return 0
        for rel, skill_tag in files:
            n = import_file(conn, repo / rel, skill_tag=skill_tag, ts=ts)
            print(f"import {skill_tag}: {n} rows")
            if args.switch:
                try:
                    m = switch_to_projection(conn, repo / rel, skill_tag=skill_tag)
                    print(f"  switched {skill_tag} → projection ({m} learnings)")
                except ImportIncompleteError as exc:
                    print(f"  BLOCKED: {exc}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
