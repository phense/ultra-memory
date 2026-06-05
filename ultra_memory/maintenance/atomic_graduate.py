"""Atomic Graduation (Recall-Reflex 5.2) — the autonomous capture-findably backstop.

A fenced maintenance beat: drain `atomic_candidate` markers (captured by `session_ingest`)
and turn each durable lesson into a `## Signal`-keyed wiki atomic via the consumer gateway.

Posture (feedback_ship_autonomous_no_dead_flags): ships ON by default; kill-switch
`ATOMIC_GRADUATE_DISABLE` (never an enable-flag). The apply is DETERMINISTIC — the lesson +
observable already came from `session_ingest`'s single OAuth call; this beat makes no LLM call.

Intrinsic safety wall: a THREE-way `## Signal` dedup-gate (merge / skip-grey / create), a
blast-radius cap, create-only (archive-never-delete), `created_by='background_review'`, and
per-candidate fail-open. (Task 5 adds the eval-gate: findable-or-quarantine.)
"""
import hashlib
import re
from pathlib import Path

from ultra_memory.maintenance.session_ingest import (
    pending_atomic_candidates, resolve_atomic_candidate)

DEFAULT_CAP = 3
DEFAULT_DEDUP_UPPER = 0.86
DEFAULT_DEDUP_LOWER = 0.78
DEFAULT_EVAL_TOP_N = 5
_GOTCHA_THEME = "tooling"
_LESSON_THEME = "strategy-methodology"
_SOURCE_LABEL = "atomic-graduate"


def _enabled(env) -> bool:
    """Default ON; disabled only when `ATOMIC_GRADUATE_DISABLE` is set (the kill-switch)."""
    return not (env.get("ATOMIC_GRADUATE_DISABLE") or "").strip()


def _slugify(title: str, signal: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60] or "atomic"
    h = hashlib.sha1((title + "\x00" + signal).encode("utf-8")).hexdigest()[:4]
    return f"{base}-{h}"


def _theme_for(cand) -> str:
    return _GOTCHA_THEME if cand.get("kind") == "gotcha" else _LESSON_THEME


def _render_atomic(cand, *, slug, ts) -> str:
    """The atomic markdown: frontmatter + body + a `## Signal` section. A `lesson` (e.g.
    trading/strategy) carries an unvalidated confidence label + the entry-trigger
    disambiguation so a real-money path never treats an auto-created lesson as established."""
    theme = _theme_for(cand)
    kind = cand.get("kind")
    tags = "[engineering, tooling, auto-graduated]" if kind == "gotcha" \
        else "[strategy, auto-graduated]"
    conf = ("\n\nConfidence: [Recent-Regime] — auto-graduated + UNVALIDATED; not an "
            "established rule. (The `## Signal` below is the RECALL trigger, NOT a "
            "strategy entry-trigger.)" if kind == "lesson" else "")
    date = str(ts)[:10]
    fm = (f"---\ntype: mechanism\ntitle: {cand['title']}\ntags: {tags}\n"
          f"created: {date}\nupdated: {date}\ntheme: {theme}\nanchor: {slug}\n"
          f"created_by: background_review\n---\n")
    body = (f"\n# {cand['title']}\n\n{cand['body']}{conf}\n\n"
            f"## Signal\n\n{cand['signal']}\n\n"
            f"Sources: auto-graduated by atomic_graduate from session "
            f"{cand.get('session_id', '?')} ({date}).\n")
    return fm + body


def _page_path_for_slug(conn, slug):
    """The stored `unified_index.path` for `slug` (the merge target), or the bare slug
    when the row is absent (fail-soft — the gateway resolves what it can)."""
    row = conn.execute("SELECT path FROM unified_index WHERE slug=?", (slug,)).fetchone()
    return row["path"] if row else slug


def run_atomic_graduate_pass(conn, *, ts, env, gateway_run, signal_match, wiki_root,
                             recall_fn=None, embedder=None, cap=DEFAULT_CAP,
                             dedup_upper=DEFAULT_DEDUP_UPPER, dedup_lower=DEFAULT_DEDUP_LOWER,
                             eval_top_n=DEFAULT_EVAL_TOP_N, log=lambda _m: None) -> dict:
    """Drain `atomic_candidate` markers → create / merge / skip `## Signal`-keyed atomics,
    capped + fenced + fail-open. `gateway_run(verb, args:list[str], content:str|None)` is
    injected (production: shells the consumer gateway; test: a recorder)."""
    if not _enabled(env):
        return {"mode": "disabled", "created": 0, "merged": 0, "skipped": 0, "quarantined": 0}
    created = merged = skipped = quarantined = 0
    pend = pending_atomic_candidates(conn, limit=max(cap * 10, 50))
    for c in pend:
        if created >= cap:
            log(f"atomic_graduate: blast-radius cap {cap} reached — "
                f"{len(pend) - cap} candidate(s) left for next run")
            break
        topic = c.get("topic") or "trading"
        try:
            m = signal_match(conn, c["signal"], embedder=embedder, topic=topic)
            cos = m[1] if m else 0.0
            if m and cos >= dedup_upper:
                # MERGE — reinforce the existing page; never a duplicate.
                gateway_run(
                    "append-validation-log",
                    ["--page", _page_path_for_slug(conn, m[0]), "--topic", topic,
                     "--source-label", _SOURCE_LABEL],
                    f"- {str(ts)[:10]} [atomic_graduate] (n=1): **MERGE** — recurring "
                    f"observable: {c['signal'][:160]}.")
                resolve_atomic_candidate(conn, event_id=c["event_id"])
                merged += 1
                continue
            if m and dedup_lower <= cos < dedup_upper:
                # SKIP-conservative — uncertain; neither a maybe-dup nor a forced merge.
                log(f"atomic_graduate: grey-zone ({cos:.3f}) for {c['title']!r} vs "
                    f"[[{m[0]}]] — skip, retry next run")
                skipped += 1
                continue
            # NOVEL → create a `## Signal`-keyed atomic.
            slug = _slugify(c["title"], c["signal"])
            path = str(Path(wiki_root) / topic / "concepts" / f"{slug}.md")
            gateway_run("create-page",
                        ["--path", path, "--topic", topic, "--source-label", _SOURCE_LABEL],
                        _render_atomic(c, slug=slug, ts=ts))
            gateway_run("register-index",
                        ["--slug", slug, "--theme", _theme_for(c),
                         "--summary", c["title"][:160], "--topic", topic,
                         "--source-label", _SOURCE_LABEL], None)
            # (Task 5 inserts the eval-gate here: findable-or-quarantine.)
            resolve_atomic_candidate(conn, event_id=c["event_id"])
            created += 1
        except Exception as exc:   # per-candidate fail-open: leave unresolved → retry
            log(f"atomic_graduate failed for {c.get('title')!r}: {exc!r} — retry next run")
    return {"mode": "ran", "created": created, "merged": merged, "skipped": skipped,
            "quarantined": quarantined}
