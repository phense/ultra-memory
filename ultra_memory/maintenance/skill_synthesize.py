"""SP-10 Stage 3 — the INDUCTION pass (planning only; no writes).

Clones the SP-5/6 ``consolidate_candidates`` shape (~80% reuse): bounded select →
dedup/delta pre-filter → ONE batched OAuth ``run_claude`` draft → grounded-or-
dropped parse. It produces a PLAN (a drafted ``GeneratedSkill`` + the per-domain
incumbent, if any); the orchestrator (``synthesize_run``) applies it inside the
SP-7 wall + checkpoint + bounds.

Fork-2 trigger: a cluster of graduated lessons (``node_type='learning'``,
``created_by ∈ ('agent','background_review')``, ``status='active'``) grouped by
``index_hook`` reaching ``N`` lessons with mean ``outcome_weight ≥ THETA_W``.
Fork-H: the slug is DERIVED from the domain (``gen-<slugify(domain)>``) → one skill
per domain; a re-qualifying domain re-drafts and the orchestrator supersedes the
incumbent (archive-never-delete). Every source lesson is funnelled through the
wall's ``assert_mutable`` before its body is read into the prompt — a human/pinned
source halts the whole run (zero tolerance).

OAuth-only: the single draft call routes through ``ultra_memory.claude_cli.run_claude``
(injectable runner for tests); NO anthropic SDK.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


from ultra_memory.claude_cli import run_claude  # noqa: E402  (the OAuth chokepoint)

from ultra_memory.maintenance import skill_fs  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import (  # noqa: E402
    ForbiddenTargetError,
    MemoryUnit,
    assert_mutable,
)

DEFAULT_N = 3
DEFAULT_THETA_W = 1.0
DEFAULT_LESSON_CAP = 40  # max lesson bodies pulled into one draft prompt
DEFAULT_MODEL = "claude-sonnet-4-6"  # OAuth-only via claude_cli; the beat passes config.model


# --------------------------------------------------------------------------- #
# Slug + ledger-id derivation (fork H — one skill per domain). The pure helpers
# live in skill_ids (stdlib-only) so a NO-LLM consumer can derive ids without
# importing this module's OAuth draft path; re-exported here for back-compat.
# --------------------------------------------------------------------------- #

from ultra_memory.maintenance.skill_ids import (  # noqa: E402,F401
    backing_memory_id,
    derive_slug,
    procedure_id,
    slugify_domain,
)


# --------------------------------------------------------------------------- #
# 1. SELECT — the induction trigger query.
# --------------------------------------------------------------------------- #

def select_induction_clusters(conn, *, n: int = DEFAULT_N,
                              theta_w: float = DEFAULT_THETA_W,
                              lesson_cap: int = DEFAULT_LESSON_CAP) -> list[dict]:
    """Adapt per_skill_outcome_rates: group graduated lessons by index_hook, keep
    domains with >=n lessons and mean outcome_weight >= theta_w. Returns clusters
    ranked (avg_w desc, n desc), each with its lesson bodies pulled. EXCLUDES the
    generated-skill domains' own backing rows (node_type='generated_skill')."""
    rows = conn.execute(
        """
        SELECT index_hook AS domain, COUNT(*) AS n, AVG(outcome_weight) AS avg_w
        FROM memories
        WHERE created_by IN ('agent','background_review')
          AND status = 'active'
          AND node_type = 'learning'
          AND index_hook IS NOT NULL
        GROUP BY index_hook
        HAVING COUNT(*) >= ? AND AVG(outcome_weight) >= ?
        ORDER BY AVG(outcome_weight) DESC, COUNT(*) DESC
        """,
        (n, theta_w),
    ).fetchall()
    clusters = []
    for r in rows:
        domain = r["domain"]
        lessons = conn.execute(
            """
            SELECT id, title, body, outcome_weight
            FROM memories
            WHERE created_by IN ('agent','background_review')
              AND status = 'active' AND node_type = 'learning' AND index_hook = ?
            ORDER BY outcome_weight DESC, updated_at DESC
            LIMIT ?
            """,
            (domain, lesson_cap),
        ).fetchall()
        clusters.append({
            "domain": domain,
            "slug": derive_slug(domain),
            "n": int(r["n"]),
            "avg_w": float(r["avg_w"]),
            "lesson_ids": [l["id"] for l in lessons],
            "lessons": [dict(l) for l in lessons],
        })
    return clusters


# --------------------------------------------------------------------------- #
# 2. INCUMBENT + DELTA (fork H per-domain uniqueness).
# --------------------------------------------------------------------------- #

def active_generated_skill_for(conn, domain: str) -> dict | None:
    """The active generated skill for `domain` (≤1 by invariant), or None."""
    slug = derive_slug(domain)
    mrow = conn.execute(
        "SELECT id FROM memories WHERE node_type='generated_skill' "
        "AND index_hook=? AND status='active' LIMIT 1", (slug,)).fetchone()
    if mrow is None:
        return None
    prow = conn.execute(
        "SELECT steps, times_seen, created_at FROM procedures WHERE id=?",
        (procedure_id(slug),)).fetchone()
    steps = {}
    times_seen = 1
    created_at = None
    if prow is not None:
        times_seen = int(prow["times_seen"] or 1)
        created_at = prow["created_at"]
        try:
            steps = json.loads(prow["steps"]) if prow["steps"] else {}
        except Exception:
            steps = {}
    return {
        "slug": slug,
        "mem_id": mrow["id"],
        "proc_id": procedure_id(slug),
        "times_seen": times_seen,
        "source_lesson_ids": list(steps.get("source_lesson_ids", [])),
        "source_domain": steps.get("source_domain"),
        "created_at": created_at,
    }


def has_material_delta(incumbent: dict | None, lesson_ids) -> bool:
    """True iff there is NO active incumbent, or the incumbent was built from a
    different lesson set (a redraft with no delta is suppressed)."""
    if incumbent is None:
        return True
    return set(incumbent.get("source_lesson_ids", [])) != set(lesson_ids)


# --------------------------------------------------------------------------- #
# 3. DRAFT — ONE OAuth call (anti-capture + anti-hijack narrowness).
# --------------------------------------------------------------------------- #

def build_sys() -> str:
    return (
        "You synthesize a single NATIVE Claude Code skill (SKILL.md) from a cluster "
        "of matured, positively-scored engineering lessons about ONE task domain. "
        "Output STRICT JSON only.\n"
        "ANTI-CAPTURE (do NOT synthesize): environment-dependent failures, negative "
        "tool claims ('X is broken'), transient/resolved errors, or one-off task "
        "narratives — only durable, reusable procedure.\n"
        "ANTI-HIJACK: the `description` MUST be NARROW and specific to this domain's "
        "exact intent, and MUST NOT use the trigger verbs of the static skills listed "
        "(they are provided as negative space you must not shadow). Third person.\n"
        "GROUNDING: every claim must come from the provided lessons; cite the lesson "
        "ids you used in source_lesson_ids. If nothing durable is worth a skill, "
        'return {"skill": null}.'
    )


def build_prompt(cluster: dict, static_descriptions: list[str]) -> str:
    lessons = "\n\n".join(
        f"[{l['id']}] {l.get('title','')}\n{l.get('body','')}"
        for l in cluster["lessons"])
    statics = "\n".join(f"- {d}" for d in static_descriptions)
    return (
        f"DOMAIN: {cluster['domain']}\n"
        f"TARGET SKILL NAME (use verbatim): {cluster['slug']}\n\n"
        f"STATIC SKILL DESCRIPTIONS YOU MUST NOT SHADOW:\n{statics}\n\n"
        f"MATURED LESSONS ({cluster['n']}):\n{lessons}\n\n"
        "Return JSON: {\"skill\": {\"name\": \"" + cluster["slug"] + "\", "
        "\"description\": <narrow third-person trigger, <=1024 chars>, "
        "\"body\": <the SKILL.md markdown procedure>, "
        "\"paths\": [<glob patterns this skill is relevant to, optional>], "
        "\"source_lesson_ids\": [<the lesson ids you used>]}} "
        "or {\"skill\": null}."
    )


def parse_skill_plan(stdout: str, cluster: dict) -> skill_fs.GeneratedSkill | None:
    """Parse the draft → a GeneratedSkill or None. GROUNDED-OR-DROPPED: drops the
    skill if the name != the derived slug, the description is invalid, or any cited
    source id is not in the cluster (a hallucinated citation)."""
    text = (stdout or "").strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    data = json.loads(text)  # JSONDecodeError → ValueError → caller fails closed
    if not isinstance(data, dict) or "skill" not in data:
        raise ValueError("draft JSON missing top-level 'skill'")
    sk = data["skill"]
    if sk is None:
        return None
    if not isinstance(sk, dict):
        raise ValueError("'skill' is not an object or null")
    name = sk.get("name")
    if name != cluster["slug"]:
        return None  # the model renamed it — drop (never trust an off-slug name)
    cited = sk.get("source_lesson_ids") or []
    if not isinstance(cited, list) or not cited:
        return None
    if not set(cited).issubset(set(cluster["lesson_ids"])):
        return None  # ungrounded citation → drop
    paths = sk.get("paths") or None
    skill = skill_fs.GeneratedSkill(
        slug=cluster["slug"], description=str(sk.get("description", "")),
        body=str(sk.get("body", "")), paths=paths, index_hook=cluster["slug"],
        source_lesson_ids=[c for c in cited])
    if skill_fs.validate_frontmatter(skill.slug, skill.description, skill.paths):
        return None  # invalid frontmatter → drop
    return skill


def draft(conn, *, repo_root, runner=subprocess.run, static_descriptions=None,
          n: int = DEFAULT_N, theta_w: float = DEFAULT_THETA_W, ts: str,
          model: str | None = None, claude_bin: str = "claude",
          timeout: int = 720, env=None) -> dict:
    """The induction pipeline (planning only). Picks the top eligible domain with a
    material delta, funnels every source lesson through the wall, makes ONE
    run_claude draft, parses grounded-or-dropped. Returns
    {skill, cluster, incumbent, reason}. Raises ForbiddenTargetError if a source
    lesson is human/pinned (the orchestrator turns it into a whole-run halt)."""
    static_descriptions = static_descriptions or []
    clusters = select_induction_clusters(conn, n=n, theta_w=theta_w)
    for cluster in clusters:
        # Funnel every source lesson through the provenance gate FIRST (re-reads the
        # live row; a human/pinned source → ForbiddenTargetError → whole-run halt).
        for lid in cluster["lesson_ids"]:
            assert_mutable(conn, MemoryUnit(lid))
        incumbent = active_generated_skill_for(conn, cluster["domain"])
        if (incumbent is not None and incumbent.get("source_domain")
                and incumbent["source_domain"] != cluster["domain"]):
            # Slug collision: a DIFFERENT domain already owns this gen-<slug>. NEVER
            # cross-domain supersede (it would thrash two domains over one skill) —
            # skip this domain (a diagnostic; the slug space is the conflict).
            continue
        if not has_material_delta(incumbent, cluster["lesson_ids"]):
            continue  # no change since the incumbent — skip this domain
        prompt = build_prompt(cluster, static_descriptions)
        stdout = run_claude(prompt, model=model or DEFAULT_MODEL,
                            system=build_sys(), claude_bin=claude_bin,
                            timeout=timeout, runner=runner, env=env)
        skill = parse_skill_plan(stdout, cluster)
        if skill is None:
            return {"skill": None, "cluster": cluster, "incumbent": incumbent,
                    "reason": "draft returned no durable skill"}
        return {"skill": skill, "cluster": cluster, "incumbent": incumbent,
                "reason": "drafted"}
    return {"skill": None, "cluster": None, "incumbent": None,
            "reason": "no eligible cluster with a material delta"}
