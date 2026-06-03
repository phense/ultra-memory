"""SP-10 Stage 5b — the SYNTHESIZE orchestrator (Stage 2d entry point).

Composes the whole beat inside the SP-7 wall + checkpoint + bounds, exactly like
``aggressive_run.run_aggressive_pass``:

  gate (synthesize_bounds) → draft (skill_synthesize, ONE OAuth call) → cap
  (synthesize_bounds) → trigger-probe eval (skill_eval) → [live only] pre-run git
  checkpoint → bounded apply (incl. the fork-H supersede: archive incumbent +
  consolidate + superseded_by) → digest + audit.

DUAL representation (fork C): a backing ``memories`` row (created_by=
'background_review', node_type='generated_skill', index_hook=gen-slug) + a
``procedures`` ledger row + the SKILL.md file. ``synthesized_into`` edges per
source lesson. SHIPS DISABLED (SP10_SYNTHESIS_DISABLE default-present in cron).
FAIL-OPEN: any error → no-op + a digest/audit line, never raises, never wedges.
OAuth-only (the draft + probes via the injected runner / _child_env); no SDK.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


from ultra_memory import memory_export  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402

from ultra_memory.maintenance import skill_eval as se  # noqa: E402
from ultra_memory.maintenance import skill_fs  # noqa: E402
from ultra_memory.maintenance import skill_synthesize as ss  # noqa: E402
from ultra_memory.maintenance import synthesize_bounds as sb  # noqa: E402
from ultra_memory.maintenance.aggressive_bounds import pre_run_checkpoint  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import (  # noqa: E402
    ForbiddenTargetError,
    MemoryUnit,
    SkillUnit,
    assert_mutable,
)

DEFAULT_MODEL = "claude-sonnet-4-6"  # OAuth-only via claude_cli; the beat passes config.model


@dataclass
class RunResult:
    mode: str = "noop"
    drafted: str | None = None
    admitted: str | None = None
    applied: str | None = None
    superseded: str | None = None
    halted: bool = False
    verdict: str | None = None
    reason: str = ""
    rejected: list = field(default_factory=list)
    held: list = field(default_factory=list)
    digest_path: str | None = None
    audit_path: str | None = None


def digest_path_for(briefings_dir, date: str) -> Path:
    return Path(briefings_dir) / str(date)[:4] / f"sp10-synthesize-{date}.md"


def audit_path_for(briefings_dir, date: str) -> Path:
    return Path(briefings_dir) / "maintenance-logs" / f"sp10-{date}.jsonl"


# --------------------------------------------------------------------------- #
# Apply (incl. fork-H supersede). Files + DB writes; the git checkpoint is the
# orchestrator's separate step.
# --------------------------------------------------------------------------- #

def apply_plan(conn, skill, incumbent, cluster, *, repo_root, ts,
               audit_dir=None, scope: str = "project") -> dict:
    """Materialize one admitted generated skill (dual representation + fork-H
    supersede). Every write funnels through the wall first."""
    slug = skill.slug
    target = skill_fs.skill_md_path(repo_root, slug)
    # §4a wall — a static/escape target halts the whole run (caught by the caller).
    assert_mutable(conn, SkillUnit(slug=slug, path=target))

    # IDENTITY + DELTA key on the CLUSTER's lesson set (NOT the model-cited subset),
    # so the backing id can never collide with the incumbent's after a genuine delta
    # (the 2026-06-01 review blocker: a subset citation == incumbent set self-redirected
    # the only active row). The cited subset (skill.source_lesson_ids) is provenance
    # evidence → the synthesized_into edges; the cluster set is the skill's identity.
    cluster_ids = list(cluster["lesson_ids"])
    new_mem_id = ss.backing_memory_id(slug, cluster_ids)
    # A supersede happens only when the incumbent is a DIFFERENT backing identity.
    is_supersede = incumbent is not None and new_mem_id != incumbent["mem_id"]

    if is_supersede:
        # fork H: retire the incumbent FIRST (archive the dir → frees the stable
        # path), archive-never-delete. archive() tolerates a drifted-missing dir.
        skill_fs.archive(incumbent["slug"], repo_root=repo_root, ts=ts,
                         audit_dir=audit_dir)

    # Model B: SEED the managed auto-learnings block at create from the founding
    # cluster — the SAME union-blend renderer the weekly refresh uses (source domain
    # ∪ the gen-slug own-usage feed, de-duped), so the SKILL.md is substantive on day
    # 1 and never relies on a pre-existing Learnings.md. The frozen description/trigger
    # is untouched by this; only the body block is seeded.
    skill.auto_learnings_block = memory_export.render_union_blend_block(
        conn, hooks=[cluster["domain"], slug], now=ts)
    skill_fs.create(skill, repo_root=repo_root, ts=ts, audit_dir=audit_dir)

    memory_lib.save_memory(
        conn, id=new_mem_id, type="memory", title=slug,
        body=json.dumps({"description": skill.description,
                         "cluster_lesson_ids": cluster_ids,
                         "cited_lesson_ids": list(skill.source_lesson_ids)}),
        ts=ts, index_hook=slug, node_type="generated_skill",
        created_by="background_review")

    if is_supersede:
        # archive-never-delete on the memory side (old → redirect, supersedes=new).
        assert_mutable(conn, MemoryUnit(incumbent["mem_id"]))
        memory_lib.consolidate(conn, loser_id=incumbent["mem_id"],
                               canonical_id=new_mem_id, reason="sp10-supersede", ts=ts)
        memory_lib.record_link(conn, src_kind="memory", src_id=incumbent["mem_id"],
                               predicate="superseded_by", dst_kind="memory",
                               dst_id=new_mem_id, evidence="sp10-supersede", ts=ts)

    for lid in skill.source_lesson_ids:
        memory_lib.record_link(conn, src_kind="memory", src_id=lid,
                               predicate="synthesized_into", dst_kind="memory",
                               dst_id=new_mem_id, evidence="sp10-synthesize", ts=ts)

    times_seen = (incumbent["times_seen"] + 1) if is_supersede else (
        incumbent["times_seen"] if incumbent else 1)
    created_at = (incumbent.get("created_at") if incumbent else None) or ts
    steps = {"source_lesson_ids": cluster_ids, "cited_lesson_ids": list(skill.source_lesson_ids),
             "fsm_state": "active", "scope": scope, "skill_path": str(target),
             "source_domain": cluster["domain"], "created_at": created_at}
    conn.execute(
        "INSERT OR REPLACE INTO procedures "
        "(id,name,steps,trigger,source_sessions,times_seen,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ss.procedure_id(slug), slug, json.dumps(steps), skill.description,
         json.dumps([]), times_seen, created_at, ts))
    conn.commit()
    return {"slug": slug, "new_mem_id": new_mem_id,
            "superseded": incumbent["mem_id"] if is_supersede else None,
            "times_seen": times_seen}


# --------------------------------------------------------------------------- #
# Digest + audit.
# --------------------------------------------------------------------------- #

def render_digest(result: RunResult, *, date: str, rollback: str = "") -> str:
    lines = [f"# SP-10 SYNTHESIZE — {date}", "",
             f"- mode: **{result.mode}**",
             f"- drafted: {result.drafted or '—'}",
             f"- eval verdict: {result.verdict or '—'}",
             f"- applied: {result.applied or '—'}",
             f"- superseded: {result.superseded or '—'}",
             f"- halted: {result.halted}",
             f"- rejected: {result.rejected or '—'}",
             f"- held: {result.held or '—'}",
             f"- note: {result.reason}"]
    if rollback:
        lines += ["", f"Rollback: `{rollback}`"]
    return "\n".join(lines) + "\n"


def _finish(result: RunResult, *, briefings_dir, date, ts, rollback="") -> None:
    if briefings_dir is None:
        return                                  # pure-memory install: no digest/audit
    try:
        dp = digest_path_for(briefings_dir, date)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(render_digest(result, date=date, rollback=rollback),
                      encoding="utf-8")
        result.digest_path = str(dp)
    except Exception:
        pass
    try:
        ap = audit_path_for(briefings_dir, date)
        ap.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": ts, "mode": result.mode, "drafted": result.drafted,
               "applied": result.applied, "superseded": result.superseded,
               "verdict": result.verdict, "halted": result.halted,
               "rejected": result.rejected, "held": result.held,
               "reason": result.reason}
        with ap.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        result.audit_path = str(ap)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The pass.
# --------------------------------------------------------------------------- #

def run_synthesize_pass(conn, *, repo_root, date, ts, briefings_dir,
                        runner=subprocess.run, env=None, export_fn=lambda: None,
                        checkpoint_fn=None, git_env=None, period=None,
                        log=lambda _m: None, model=None, probe_fn=None,
                        budget_fn=None, static_descriptions=None, corpus=None,
                        skills_dir=None, corpus_path=None) -> RunResult:
    """The single entry the Stage-2d wrapper calls. NEVER raises."""
    result = RunResult()
    # Project-agnostic: no briefings_dir (pure-memory install) → no audit/digest dir.
    audit_dir = (Path(briefings_dir) / "maintenance-logs") if briefings_dir else None
    rollback = ""
    try:
        gate = sb.run_gate(log=log)
        result.mode = gate.mode
        if gate.mode == "noop":
            result.reason = gate.reason
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts)
            return result

        skills_dir = skills_dir or (Path(repo_root) / ".claude" / "skills")
        if static_descriptions is None:
            # §6.3 HARD precondition: the not-shadow set is the project skills PLUS
            # the auto-invocable plugin skills a generated description could shadow.
            static_descriptions = se.read_all_invocable_skill_descriptions(repo_root)
        if corpus is None:
            # Autonomy (§6.5): an explicit curated corpus wins; otherwise AUTO-BUILD a
            # self-validating per-deployment corpus from the same discovered descriptions
            # the coverage check uses — so the eval-gate is never fail-closed on an
            # uncovered skill (the old `else []` made a no-corpus install HOLD forever).
            corpus = (se.load_corpus(corpus_path) if corpus_path
                      else se.build_probe_corpus(static_descriptions))
        if budget_fn is None:
            budget_fn = lambda c: se.estimate_listing_budget_ok(  # noqa: E731
                c.description, static_descriptions)

        # DRAFT (ONE OAuth call). A forbidden source = whole-run halt.
        try:
            plan = ss.draft(conn, repo_root=repo_root, runner=runner, ts=ts, env=env,
                            model=model,
                            static_descriptions=list(static_descriptions.values()),
                            # skip domains whose name IS a static skill (a gen-<skill>
                            # would hijack its namesake → always rejected); SP-10 mints
                            # only net-new domains.
                            static_skill_names=set(static_descriptions.keys()))
        except ForbiddenTargetError as exc:
            result.halted = True
            result.reason = f"HALT (forbidden source): {exc}"
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts)
            return result

        skill, incumbent, cluster = plan["skill"], plan["incumbent"], plan["cluster"]
        result.drafted = skill.slug if skill else None
        if skill is None:
            result.reason = plan["reason"]
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts)
            return result

        # §4c cap (halt-on-exceed) — per-run AND per-period (blocks stacked re-runs).
        cap = sb.enforce_skill_cap({"skills": [skill]}, conn=conn, period=period,
                                   period_cap=sb.MAX_SKILLS_INDUCED_PER_PERIOD)
        if not cap["admitted"]:
            result.reason = f"bound: {cap['bound']}"
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts)
            return result

        # TRIGGER-PROBE EVAL-GATE (load-bearing).
        rep = se.run_trigger_gate(skill, static_descriptions=static_descriptions,
                                  corpus=corpus, repo_root=repo_root, runner=runner,
                                  env=env, probe_fn=probe_fn, budget_fn=budget_fn)
        result.verdict = rep.verdict
        if not rep.admit:
            (result.held if rep.verdict == "hold" else result.rejected).append(
                {"slug": skill.slug, "reason": rep.reason})
            result.reason = rep.reason
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts)
            return result
        result.admitted = skill.slug

        if not gate.may_apply:  # dryrun
            result.reason = f"DRY-RUN — would apply {skill.slug}"
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts)
            return result

        # §4d pre-run git checkpoint (clean-tree precondition). The SP-10 tag is
        # pre-sp10-synthesize-<date> (NOT the SP-7 default) so the rollback anchor is
        # distinct from a same-day SP-7 aggressive checkpoint.
        ck = (checkpoint_fn(date) if checkpoint_fn else
              pre_run_checkpoint(repo_root=repo_root, date=date,
                                 tag_prefix="pre-sp10-synthesize-",
                                 export_fn=export_fn, env=git_env))
        rollback = getattr(ck, "rollback_command", "")
        if not getattr(ck, "ok", False):
            result.reason = f"checkpoint not ok ({getattr(ck, 'reason', '?')}) — plan-only"
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts,
                    rollback=rollback)
            return result

        # APPLY (bounded, provenance-gated). A forbidden write target halts.
        try:
            applied = apply_plan(conn, skill, incumbent, cluster,
                                 repo_root=repo_root, ts=ts, audit_dir=audit_dir)
        except ForbiddenTargetError as exc:
            result.halted = True
            result.reason = f"HALT (forbidden write target): {exc}"
            _finish(result, briefings_dir=briefings_dir, date=date, ts=ts,
                    rollback=rollback)
            return result
        result.applied = applied["slug"]
        result.superseded = applied["superseded"]
        if period:
            sb.commit_period_usage(conn, period=period, applied_count=1, ts=ts)
        result.reason = f"applied {skill.slug} (times_seen={applied['times_seen']})"
    except Exception as exc:  # fail-open: never wedge maintenance
        result.reason = f"fail-open: {exc!r}"
        try:
            log(result.reason)
        except Exception:
            pass
    _finish(result, briefings_dir=briefings_dir, date=date, ts=ts, rollback=rollback)
    return result


# --------------------------------------------------------------------------- #
# CLI (Stage 2d wrapper entry).
# --------------------------------------------------------------------------- #

def _default_export_fn(db_path, repo_root):
    def _fn():
        try:
            from ultra_memory import memory_export  # noqa
            # best-effort snapshot; a failure fails the checkpoint (fail-soft skip).
            memory_export.export_views(db_path, str(Path(repo_root) / "data" /
                                                    "memory_export"))
        except Exception:
            pass
    return _fn


# --------------------------------------------------------------------------- #
# Registry adapter — the beat signature run_pipeline calls (the synthesize beat).
# --------------------------------------------------------------------------- #

def beat(conn, config, ts, env):
    """The `run_pipeline` registry entry for the SP-10 SYNTHESIZE beat — it induces a
    native `.claude/skills/gen-<slug>/SKILL.md` from a cluster of matured, positively
    scored, agent-authored lessons. Threads the config seam (project_dir /
    briefings_dir / probe_corpus / db_path / export_dir / model).

    GOVERNED — not ships-disabled here (the north-star active-on-install posture):
    the safety is the wall (the SkillUnit gen-path structural guard + the provenance
    gate — a generated skill can NEVER overwrite a static/human skill) + the
    load-bearing TRIGGER-PROBE eval-gate (a generated skill that would hijack a static
    skill's auto-trigger is rejected) + the per-domain skill cap (≤1 active generated
    skill per domain, fork H) + a clean-tree git checkpoint required before any apply
    + the SP10_SYNTHESIS_* run_gate (read from the process env). INERT at install: the
    trigger (≥3 graduated lessons + outcome_weight ≥1.0 per domain) has no live signal
    until the consolidate beat graduates background_review lessons and outcome
    attribution is armed."""
    date = ts[:10]
    corpus_path = str(config.probe_corpus) if config.probe_corpus else None

    def _export():
        try:
            from ultra_memory import memory_export
            memory_export.export_views(str(config.db_path), str(config.export_dir))
        except Exception:
            pass

    return run_synthesize_pass(
        conn, repo_root=str(config.project_dir), date=date, ts=ts,
        briefings_dir=config.briefings_dir, period=date[:7], model=config.model,
        corpus_path=corpus_path, env=env, git_env=env, export_fn=_export,
        log=lambda m: sys.stderr.write(f"[synthesize] {m}\n"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="SP-10 SYNTHESIZE (Stage 2d)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--briefings-dir", required=True)
    ap.add_argument("--skills-dir", default=None)
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--date", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args(argv)

    conn = memory_lib.open_memory_db(args.db)
    ts = f"{args.date}T00:00:00Z"
    period = args.date[:7]
    corpus_path = args.corpus or str(Path(args.repo_root) / "tests" / "fixtures" /
                                     "skill_trigger_probes.json")
    result = run_synthesize_pass(
        conn, repo_root=args.repo_root, date=args.date, ts=ts,
        briefings_dir=args.briefings_dir, period=period, model=args.model,
        skills_dir=args.skills_dir, corpus_path=corpus_path,
        export_fn=_default_export_fn(args.db, args.repo_root),
        log=lambda m: print(m, file=sys.stderr))
    print(json.dumps({"mode": result.mode, "applied": result.applied,
                      "superseded": result.superseded, "verdict": result.verdict,
                      "reason": result.reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
