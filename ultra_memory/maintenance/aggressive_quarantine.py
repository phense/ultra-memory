"""SP-7 §5.3 — the CONTRADICTION QUARANTINE track — Stage 7 of the SP-7 build
(spec §7 step 7). The THIRD and GENTLEST of the three aggressive self-improvement
capabilities, built ON TOP of the safety wall (Stages 1-4): it re-implements no
guard — it COMPOSES the wall (`aggressive_wall.apply_quarantine_pair`, the FSM-flip
apply path that re-reads the live row), the hard gate (`aggressive_eval.hard_gate`,
zero-tolerance provenance), and the bounds (`aggressive_bounds.MAX_QUARANTINES_PER_RUN`).

THE GENTLEST AGGRESSIVE VERB (spec §5.3): nothing is edited or deleted — two
agent-authored units that DISAGREE just stop being recalled (a `status='quarantined'`
FSM state, dropped out of `unified_recall`'s `status='active'` filter), are linked
by a `contradicts` edge, and are LISTED in the digest for the operator's adjudication. The
loop does NOT pick a winner (picking is an edit, gated separately) — it demotes BOTH
"these disagree, stop surfacing either until reviewed". Fully reversible (the wall's
`reactivate` flips a unit back to 'active').

THE TWO-STAGE DETECTION (spec §5.3, the `judge_borderline` pattern):
  1. EMBEDDING PRE-FILTER (NO LLM — the SP-3/wiki embedder, INJECTED): pair
     agent-authored units that are topically NEAR (cosine in a BAND — high enough to
     be about the same thing, low enough not to be a trivial restatement). This is
     deterministic + cheap; no model call.
  2. ONE BATCHED LLM ADJUDICATION (an INJECTED runner — the §5.5 one-call budget):
     label each near-pair contradicts | compatible | duplicate. The ONE call routes
     through the injected runner mimicking `ultra_memory.claude_cli.run_claude`'s
     CLI contract (NEVER the anthropic SDK; OAuth-only).

THE THREE ROUTES (spec §5.3):
  * contradicts → BOTH units `set_status('quarantined')` (via the wall's
    `apply_quarantine_pair`) + a `contradicts` link + a digest listing.
  * duplicate   → routes to the SP-6 CONSERVATIVE MERGE path (the engine
    `consolidate` redirect-stub: loser→'redirect', supersedes=canonical) — NOT
    quarantine. (A genuine duplicate is a merge, the conservative SP-6 verb, not an
    isolation.) Still provenance-gated: the wall re-reads the loser's live row.
  * compatible  → a NO-OP (two same-topic units that AGREE are fine to both recall).

THE WALL LIVES IN THE APPLY PATH (code), NEVER ONLY THE PROMPT (spec §4 design rule
+ the [[feedback-subagents-can-leak-secrets]] lesson: build the constraint into the
TOOL). The LLM *proposes* a label; the wall *enforces* provenance. `apply_quarantines`
runs the hard gate over the WHOLE plan FIRST (assert_mutable RE-READS each target's
live row — it never trusts an LLM-echoed `created_by`/`pinned`), so a single
forbidden (human/import/pinned) target HALTS the whole apply (the §4a stop-the-world,
zero tolerance, NOT a per-item skip) — and it is BOUNDED (halt-on-exceed).

OAUTH-ONLY (HARD): the ONE adjudication call routes through an INJECTED runner (the
precedent: score_news.py's injectable runner, judge_borderline's `runner=`). Tests
inject a fake runner + a stub embedder and NEVER spawn `claude` / NEVER load
fastembed. There is NO anthropic-SDK import, no API-key env read, no direct messages
API (a guard test asserts it). The default runner (a real run only) is the OAuth
`claude` CLI subprocess, imported lazily so the module imports clean.

ARCHIVE-NEVER-DELETE: every verb is a reversible FSM transition (quarantine) or a
conservative redirect-stub (the duplicate merge) via the wall + engine primitives —
NO rm / os.remove / memory_lib.delete anywhere (a guard test asserts it). FAIL-OPEN:
any error in pre-filter / adjudication / apply degrades to an EMPTY plan / a no-op —
it never raises out into the nightly / monthly maintenance run (the one exception is
a ForbiddenTargetError from the wall on the apply path, which is the §4a
zero-tolerance stop-the-world the hard gate enforces; it propagates as the halt).

The engine primitives the wall + the merge consume (set_status / record_link /
consolidate) are GENERIC + already on live master (ffcd414). The cosine BAND, the
adjudication prompt, the duplicate→merge routing, and the MAX_QUARANTINES policy are
the consumer's policy (e.g. a trading project).
"""
from __future__ import annotations

import sys
from pathlib import Path

# The sibling SP-7 modules — the wall (the FSM-flip apply path + the reactivate
# reversibility primitive), the hard gate (zero-tolerance provenance), the bounds.
# This track COMPOSES them; it re-implements no guard.
from ultra_memory.maintenance.aggressive_bounds import MAX_QUARANTINES_PER_RUN  # noqa: E402
from ultra_memory.maintenance.aggressive_eval import hard_gate  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import (  # noqa: E402
    ForbiddenTargetError,
    MemoryUnit,
    apply_quarantine_pair,
    assert_mutable,
)

# The engine — generic, project-agnostic primitives (wiki_lib.py:24 precedent).
from ultra_memory import memory_lib, retrieval_core  # noqa: E402
# Shared OAuth-call + JSON-extract plumbing (the OAuth chokepoint lives there).
from ultra_memory.maintenance.aggressive_utils import (  # noqa: E402
    call_model,
    default_runner,
    extract_json,
)

# --------------------------------------------------------------------------- #
# §5.3 detection policy — the cosine BAND for the no-LLM pre-filter.
# --------------------------------------------------------------------------- #

# The OAuth model the ONE adjudication call uses (Sonnet-tier, like judge_borderline).
ADJUDICATE_MODEL = "claude-sonnet-4-6"

# The cosine BAND for "topically near but not a trivial restatement" (spec §5.3:
# "high enough to be about the same thing"). A pair is a near-candidate iff its
# cosine is in [BAND_LO, BAND_HI]. BAND_HI < 1.0 keeps a byte-identical restatement
# (which the conservative dedup already handles) out of the contradiction scan; the
# adjudicator decides duplicate-vs-contradicts within the band. Conservative
# (fork B): a fairly HIGH floor so only genuinely same-topic pairs are surfaced —
# the loop should rarely act, and only on real same-topic disagreement.
BAND_LO = 0.50
BAND_HI = 0.999

# Per-run cap (mirrors the bound the apply enforces). Default from aggressive_bounds.
MAX_QUARANTINES = MAX_QUARANTINES_PER_RUN

# How many near-pairs to surface to the ONE adjudication call (bounded so the
# batched prompt stays cheap — [[feedback-workflow-token-cost]]). Generous vs the
# apply bound: the adjudicator may label many as compatible/duplicate; the
# quarantine APPLY is what MAX_QUARANTINES caps.
MAX_NEAR_PAIRS = 50

_VALID_LABELS = frozenset({"contradicts", "compatible", "duplicate"})


# --------------------------------------------------------------------------- #
# 0. The agent-authored active set the loop may reason over.
# --------------------------------------------------------------------------- #

def _agent_authored_active(conn) -> list[dict]:
    """Every unit the loop may even reason over: agent-authored, active, unpinned.
    (The provenance WALL gates the WRITE; we ALSO restrict the SELECT so the loop
    never even reasons over human/pinned rows.) Returns {id, title, body}.
    Fail-closed-to-empty on a read error."""
    try:
        rows = conn.execute(
            "SELECT id, title, body FROM memories "
            "WHERE created_by IN ('agent','background_review') "
            "AND status='active' AND pinned=0"
        ).fetchall()
    except Exception:
        return []
    return [{"id": r["id"], "title": r["title"], "body": r["body"]} for r in rows]


# --------------------------------------------------------------------------- #
# 1. The embedding pre-filter (NO LLM) — cosine-band near pairs.
# --------------------------------------------------------------------------- #

def select_near_pairs(conn, *, embedder, band_lo: float = BAND_LO,
                      band_hi: float = BAND_HI,
                      max_pairs: int = MAX_NEAR_PAIRS) -> list[dict]:
    """The NO-LLM §5.3 pre-filter. Embed every agent-authored active unit (via the
    INJECTED embedder — never fastembed here) and return the pairs whose cosine is
    in the BAND [band_lo, band_hi] — topically NEAR but not a trivial restatement.

    Returns a list of {id_a, id_b, cosine}, highest-cosine first, capped at
    `max_pairs` (the batched-adjudication budget). NEVER pairs a human / pinned unit
    (the underlying scan restricts to created_by IN ('agent','background_review')).
    NO model call — the embedder is the only dependency (a stub in tests).

    Fail-open-to-empty on any error (a bad embed, a read failure) — never raises."""
    try:
        units = _agent_authored_active(conn)
        if len(units) < 2:
            return []
        texts = [f"{u['title']}\n{u['body']}" for u in units]
        vecs = embedder(texts)
        if not isinstance(vecs, list) or len(vecs) != len(units):
            return []
    except Exception:
        return []

    pairs: list[dict] = []
    n = len(units)
    for i in range(n):
        for j in range(i + 1, n):
            try:
                c = retrieval_core.cosine(vecs[i], vecs[j])
            except Exception:
                continue                      # fail-open per pair
            if band_lo <= c <= band_hi:
                pairs.append({"id_a": units[i]["id"], "id_b": units[j]["id"],
                              "cosine": c})
    pairs.sort(key=lambda p: p["cosine"], reverse=True)
    return pairs[:max_pairs]


# --------------------------------------------------------------------------- #
# 2. The ONE batched adjudication — contradicts | compatible | duplicate.
# --------------------------------------------------------------------------- #

_ADJUDICATE_SYSTEM = (
    "You are the contradiction-adjudication step of an autonomous knowledge-curation "
    "loop. For each near-duplicate pair of agent-authored learnings, you decide "
    "whether they CONTRADICT (assert opposing claims about the same situation), are "
    "DUPLICATE (the same claim restated), or are COMPATIBLE (same topic, no "
    "disagreement). You NEVER pick a winner between contradicting units — that is a "
    "human's call; you only label the relationship."
)


def build_adjudication_prompt(conn, near_pairs: list[dict]) -> str:
    """Build the ONE batched adjudication prompt over all near-pairs (spec §5.5: one
    batched call). For each pair it surfaces both units' title+body and asks for a
    label in {contradicts, compatible, duplicate}; the reply MUST be a single JSON
    object {\"adjudications\": [{id_a, id_b, label}, ...]} — nothing else.

    The label set is encoded in the PROMPT here AND enforced in CODE at parse
    (`_parse_adjudication` drops an unknown label) — defense-in-depth, never trusting
    the prompt alone. Fail-soft: a missing unit body is rendered as empty (the
    adjudicator then has nothing to disagree on → most likely 'compatible', the safe
    default)."""
    def _body(mem_id):
        row = conn.execute(
            "SELECT title, body FROM memories WHERE id=?", (mem_id,)).fetchone()
        if row is None:
            return "", ""
        return row["title"] or "", row["body"] or ""

    lines: list[str] = [
        "TASK: for each near-duplicate pair of agent-authored learnings below, label "
        "the relationship exactly one of: contradicts | duplicate | compatible.",
        "",
        "DEFINITIONS:",
        "  * contradicts — they assert OPPOSING claims about the same situation "
        "(e.g. 'sell premium on a VIX spike' vs 'buy premium on a VIX spike').",
        "  * duplicate   — the SAME claim restated (no new information).",
        "  * compatible  — same topic, but NO disagreement (they can both be true).",
        "",
        "HARD RULES:",
        "  * Do NOT pick a winner between contradicting units — only label the "
        "relationship. Picking a winner is a human's call.",
        "  * Reply with a SINGLE JSON object and nothing else: "
        '{"adjudications": [{"id_a": ..., "id_b": ..., "label": ...}, ...]}.',
        "",
        "PAIRS:",
    ]
    for p in near_pairs:
        ta, ba = _body(p["id_a"])
        tb, bb = _body(p["id_b"])
        lines.append(f"--- pair: id_a={p['id_a']} id_b={p['id_b']} "
                     f"(cosine={p.get('cosine', 0.0):.3f}) ---")
        lines.append(f"A.title: {ta}")
        lines.append(f"A.body: {ba}")
        lines.append(f"B.title: {tb}")
        lines.append(f"B.body: {bb}")
        lines.append("")
    return "\n".join(lines)


def _parse_adjudication(text: str, near_pairs: list[dict]) -> list[dict]:
    """Parse the model reply into labeled pairs, KEEPING only well-formed entries
    with a VALID label whose (id_a, id_b) match a SURFACED near-pair (an
    adjudication for an unsurfaced/hallucinated pair is dropped — the model cannot
    introduce a target the pre-filter did not surface). Order-insensitive on the
    pair ids. Fail-open to [] on any parse failure.

    Each kept entry is {id_a, id_b, label} using the SURFACED pair's canonical id
    order (so the apply path always uses the real ids, never a model re-ordering)."""
    surfaced = {frozenset((p["id_a"], p["id_b"])): (p["id_a"], p["id_b"])
                for p in near_pairs}
    obj = extract_json(text)
    if not isinstance(obj, dict):
        return []
    raw = obj.get("adjudications", [])
    if not isinstance(raw, list):
        return []
    kept: list[dict] = []
    seen: set = set()
    for a in raw:
        if not isinstance(a, dict):
            continue
        id_a = a.get("id_a")
        id_b = a.get("id_b")
        label = a.get("label")
        if not id_a or not id_b or label not in _VALID_LABELS:
            continue
        key = frozenset((str(id_a), str(id_b)))
        if key not in surfaced or key in seen:
            continue                          # hallucinated / duplicate adjudication
        seen.add(key)
        canon_a, canon_b = surfaced[key]
        kept.append({"id_a": canon_a, "id_b": canon_b, "label": label})
    return kept


def adjudicate_pairs(conn, near_pairs: list[dict], *, runner=None,
                     model: str = ADJUDICATE_MODEL, env=None) -> list[dict]:
    """The §5.3 adjudication: ONE batched OAuth call through the OAuth chokepoint
    `run_claude` (the INJECTED runner is threaded into it) over all near-pairs → a
    list of {id_a, id_b, label} with label in {contradicts, compatible, duplicate}.
    NEVER spawns `claude` in a test (the runner is injected); the default runner (a
    real run only) is the OAuth subprocess. `env` (None → os.environ) is threaded to
    the chokepoint so a real cron run sanitizes the inherited env and a test can
    inject a fake OAuth env.

    FAIL-OPEN: a runner error / a non-zero exit / an unparseable reply / an OAuth
    violation degrades to an EMPTY list — never raises out into the maintenance run."""
    if not near_pairs:
        return []
    runner = runner or default_runner()
    try:
        prompt = build_adjudication_prompt(conn, near_pairs)
        out = call_model(prompt, system=_ADJUDICATE_SYSTEM, runner=runner, model=model, env=env)
    except Exception:
        return []                             # fail-open
    return _parse_adjudication(out, near_pairs)


# --------------------------------------------------------------------------- #
# 3a. Apply — the CONTRADICTS route (both quarantined), gated + bounded.
# --------------------------------------------------------------------------- #

def apply_quarantines(conn, pairs: list, *, ts: str,
                      max_quarantines: int = MAX_QUARANTINES) -> list[dict]:
    """Apply the `contradicts` pairs via the wall's `apply_quarantine_pair` — BOTH
    members flip to status='quarantined' (out of recall) + a `contradicts` link.
    Each call funnels through the wall's assert_mutable (RE-READS each member's live
    row) — provenance is enforced in the apply path, never a prompt.

    BOUNDED to `max_quarantines`, HALT-ON-EXCEED (§4c): a batch LARGER than the cap
    applies NONE of the class (not the first N) — a volume far over the cap is a
    signal something is wrong, so stop-and-ask. Returns the list of applied pairs
    (each {id_a, id_b}) — empty when the bound halts.

    A ForbiddenTargetError from the wall PROPAGATES (the §4a zero-tolerance stop-the-
    world): a PRE-FLIGHT hard gate re-reads EVERY target's live row BEFORE any write,
    so a single forbidden target halts the batch with NOTHING applied (not even the
    legal pairs earlier in the list) — the true stop-the-world, not a per-item skip.

    Accepts either raw {id_a, id_b, label='contradicts'} adjudications or already
    {id_a, id_b} dicts; any non-contradicts entry is ignored here (the route is the
    caller's; this verb only quarantines)."""
    pairs = pairs if isinstance(pairs, list) else []

    # Normalize to the well-formed contradicts pairs we will actually apply.
    batch: list[dict] = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        if p.get("label") is not None and p.get("label") != "contradicts":
            continue                          # not a contradicts entry — skip
        id_a = p.get("id_a")
        id_b = p.get("id_b")
        if not id_a or not id_b:
            continue
        batch.append({"id_a": str(id_a), "id_b": str(id_b)})

    # HALT-ON-EXCEED: do not even start if the batch is over the cap.
    if len(batch) > max_quarantines:
        return []

    # PRE-FLIGHT provenance check over the WHOLE batch (the §4a stop-the-world): the
    # hard gate re-reads each member's live row; a single forbidden target makes it
    # propagate a ForbiddenTargetError HERE, before any write — so NOTHING in the
    # batch is applied. We funnel through the SAME hard_gate the eval uses, then
    # raise on its forbidden_targets, so the wall's assert_mutable is the one
    # authority and a test can catch ForbiddenTargetError just like the edit track.
    _assert_batch_mutable(conn, batch)

    # All members cleared the wall → quarantine both of each pair.
    applied: list[dict] = []
    for p in batch:
        apply_quarantine_pair(
            conn, id_a=p["id_a"], id_b=p["id_b"],
            reason="sp7-contradiction-quarantine", ts=ts)
        applied.append({"id_a": p["id_a"], "id_b": p["id_b"]})
    return applied


def _assert_batch_mutable(conn, batch: list[dict]) -> None:
    """PRE-FLIGHT the whole batch through the hard gate (zero-tolerance provenance).
    The hard gate re-reads each pair member's LIVE row via assert_mutable; a single
    forbidden target makes it raise a ForbiddenTargetError HERE, before any write
    (the §4a stop-the-world). We re-derive the wall's own exception so the apply
    path's authority is the wall (never an LLM-echoed field)."""
    plan = {"quarantines": [
        {"id_a": p["id_a"], "id_b": p["id_b"]} for p in batch
    ]}
    report = hard_gate(conn, plan)
    if report["gate_hard_pass"]:
        return
    # Re-derive the FIRST forbidden target as the wall's own exception so the caller
    # (and the test) catches a ForbiddenTargetError — the same contract the edit +
    # revert tracks use (they let assert_mutable raise directly).
    for p in batch:
        for tid in (p["id_a"], p["id_b"]):
            try:
                assert_mutable(conn, MemoryUnit(tid))
            except ForbiddenTargetError:
                raise
    # Defensive: the hard gate failed but no per-target raise reproduced it. Fail-
    # closed: raise the stop-the-world.
    raise ForbiddenTargetError(
        f"quarantine batch failed the hard gate: {report['forbidden_targets']}")


# --------------------------------------------------------------------------- #
# 3b. Apply — the DUPLICATE route (the SP-6 CONSERVATIVE MERGE), gated + bounded.
# --------------------------------------------------------------------------- #

def apply_merges(conn, pairs: list, *, ts: str,
                 max_merges: int = MAX_QUARANTINES) -> list[dict]:
    """Route `duplicate` pairs to the SP-6 CONSERVATIVE MERGE path (the engine
    `consolidate` redirect-stub: the loser becomes status='redirect' with
    supersedes=canonical — bytes preserved, recoverable; NOT quarantine, NOT delete).
    The CANONICAL is the keep; the LOSER is redirected to it. We keep id_a as the
    canonical and redirect id_b (a deterministic, order-stable choice — a true
    duplicate has no winner to compute, the conservative merge just collapses them).

    Provenance-gated: the loser (the row being demoted to a redirect) is funneled
    through assert_mutable (the wall RE-READS its live row) — a forbidden loser
    HALTS the batch (the §4a stop-the-world), NOTHING merged. Bounded + halt-on-
    exceed like the quarantine route. Returns the applied merges (each
    {canonical_id, loser_id})."""
    pairs = pairs if isinstance(pairs, list) else []

    batch: list[dict] = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        if p.get("label") is not None and p.get("label") != "duplicate":
            continue
        id_a = p.get("id_a")
        id_b = p.get("id_b")
        if not id_a or not id_b or id_a == id_b:
            continue
        # canonical = id_a (keep), loser = id_b (redirect to canonical).
        batch.append({"canonical_id": str(id_a), "loser_id": str(id_b)})

    if len(batch) > max_merges:
        return []                             # halt-on-exceed

    # PRE-FLIGHT: the LOSER (the demoted row) must be mutable. The canonical is only
    # a redirect TARGET, not mutated — but we gate the loser (the one that changes
    # status). A forbidden loser raises the §4a stop-the-world before any write.
    for p in batch:
        assert_mutable(conn, MemoryUnit(p["loser_id"]))

    applied: list[dict] = []
    for p in batch:
        memory_lib.consolidate(
            conn, loser_id=p["loser_id"], canonical_id=p["canonical_id"],
            reason="sp7-contradiction-duplicate-merge (conservative)", ts=ts)
        applied.append({"canonical_id": p["canonical_id"],
                        "loser_id": p["loser_id"]})
    return applied


# --------------------------------------------------------------------------- #
# The track entry — pre-filter → adjudicate → route (quarantine / merge / no-op).
# --------------------------------------------------------------------------- #

def run_quarantine_track(conn, *, ts: str, embedder, runner=None,
                         max_quarantines: int = MAX_QUARANTINES,
                         apply: bool = True, env=None) -> dict:
    """Run the contradiction-quarantine track (spec §5.3).

    1. PRE-FILTER (no-LLM, the INJECTED embedder): pair agent-authored active units
       that are topically NEAR (cosine in the band).
    2. ADJUDICATE (ONE batched OAuth call, the INJECTED runner): label each near-pair
       contradicts | compatible | duplicate.
    3. ROUTE + APPLY (only through the wall + bounded; only if `apply` and not
       halted):
         * contradicts → BOTH quarantined (`apply_quarantines`) + a `contradicts`
           link + listed in the digest for the operator's adjudication;
         * duplicate   → the conservative MERGE path (`apply_merges` → engine
           consolidate redirect-stub) — NOT quarantine;
         * compatible  → a no-op.
       `apply=False` is the DRY-RUN path (spec §4f / §7 step 8): plan + adjudicate +
       digest, apply NOTHING.

    Returns {quarantined, merged, compatible, halt, forbidden_targets} — the digest
    payload (`quarantined` is the list the operator adjudicates). FAIL-OPEN: any error in
    pre-filter / adjudication / routing degrades to an EMPTY result; never raises out
    into the maintenance run. (A ForbiddenTargetError on the apply propagates as the
    §4a zero-tolerance stop-the-world — the orchestrator turns it into a run halt.)"""
    result = {"quarantined": [], "merged": [], "compatible": [],
              "halt": False, "forbidden_targets": []}
    try:
        near = select_near_pairs(conn, embedder=embedder)
        labeled = adjudicate_pairs(conn, near, runner=runner, env=env)
    except Exception:
        return result                         # fail-open: empty plan

    contradicts = [p for p in labeled if p["label"] == "contradicts"]
    duplicates = [p for p in labeled if p["label"] == "duplicate"]
    compatible = [p for p in labeled if p["label"] == "compatible"]
    # `compatible` is a no-op route — recorded only so the digest can report it.
    result["compatible"] = [{"id_a": p["id_a"], "id_b": p["id_b"]}
                            for p in compatible]

    if not apply:
        # DRY-RUN: surface the PROPOSED quarantines/merges without applying.
        result["quarantined"] = [{"id_a": p["id_a"], "id_b": p["id_b"]}
                                 for p in contradicts]
        result["merged"] = [{"canonical_id": p["id_a"], "loser_id": p["id_b"]}
                            for p in duplicates]
        return result

    # APPLY through the wall + the bound. (A ForbiddenTargetError here is the §4a
    # stop-the-world; it propagates — the hard gate inside the apply enforces it.)
    result["quarantined"] = apply_quarantines(
        conn, contradicts, ts=ts, max_quarantines=max_quarantines)
    result["merged"] = apply_merges(
        conn, duplicates, ts=ts, max_merges=max_quarantines)
    return result
