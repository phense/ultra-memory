"""SP-10 Stage 4 — the TRIGGER-PROBE EVAL-GATE (the load-bearing deliverable).

Proves a generated skill does NOT hijack a static skill's auto-invocation triggers
BEFORE it is installed live. Two tiers (Stage-0 research grounded the mechanism):

  Tier-A (deterministic, no LLM, always runs): reject if the generated description
    is too close (token-cosine) to any static skill description — a cheap pre-filter
    that kills the obvious lexical hijacks before paying for Tier-B.

  Tier-B (behavioral, OAuth `claude -p`, the faithful proof): commands ≡ skills, so
    a `.claude/commands/<name>.md` proxy carrying the candidate description appears
    in the spawned subprocess's available_skills. For every probe whose intent
    SHOULD route to a STATIC skill, the candidate must fire ZERO times
    (`candidate_fp == 0`). A probe error/timeout fails CLOSED (treated as a fire →
    reject). Empty / coverage-gap corpus → HOLD (fail-closed: never install an
    un-probed skill). The probe subprocess is OAuth-sanitized via
    `claude_cli._child_env` (strip CLAUDECODE markers, refuse the metered API-key env).

Plus a listing-budget pre-apply check (§9 risk 8): a generated skill that would push
the skill-listing description budget over is HELD.

NO anthropic SDK; the only LLM path is `claude -p` through the OAuth-sanitized env.
The ephemeral probe command-file is throwaway scaffolding (NOT a knowledge artifact)
and is removed after each probe — archive-never-delete governs skills/memories/wiki,
not the probe proxy.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml


from ultra_memory.claude_cli import _child_env  # noqa: E402  (OAuth-sanitized env)

THETA_DESC = 0.6          # Tier-A: reject above this token-cosine to any static desc
RUNS_PER_QUERY = 3        # Tier-B: hijack-direction sample count (zero-tolerance — fire if ANY
                          # sample fires). The per-deployment auto-corpus probes ~50 skills; the
                          # probes now run CONCURRENTLY (§1.4.7), so the gate fits the timeout at a
                          # conservative 3 samples (~12 min) rather than the serial-era 2.
PROBE_MAX_WORKERS = int(os.environ.get("ULTRA_MEMORY_PROBE_WORKERS", "6") or "6")
                          # Tier-B concurrency: independent `claude -p` probes run in a bounded
                          # thread pool. Bounded so the OAuth CLI is not swamped (§1.4.7).
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- #
# Tier-A — deterministic token cosine.
# --------------------------------------------------------------------------- #

def _tokens(text: str) -> dict:
    counts: dict[str, int] = {}
    for t in _TOKEN_RE.findall(str(text).lower()):
        counts[t] = counts.get(t, 0) + 1
    return counts


def token_cosine(a: str, b: str) -> float:
    """Deterministic TF cosine over lowercased word tokens (no embedder/network)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    common = set(ta) & set(tb)
    dot = sum(ta[t] * tb[t] for t in common)
    na = math.sqrt(sum(v * v for v in ta.values()))
    nb = math.sqrt(sum(v * v for v in tb.values()))
    return dot / (na * nb) if na and nb else 0.0


def tier_a_reject(candidate_desc: str, static_descriptions: dict,
                  *, theta_desc: float = THETA_DESC, similarity_fn=None) -> str | None:
    """Return a reject reason if the candidate description is too close to any
    static skill description, else None."""
    sim = similarity_fn or token_cosine
    worst = None
    for name, desc in static_descriptions.items():
        s = sim(candidate_desc, desc)
        if worst is None or s > worst[1]:
            worst = (name, s)
        if s > theta_desc:
            return f"tier-A hijack: description cosine {s:.2f} > {theta_desc} vs {name!r}"
    return None


# --------------------------------------------------------------------------- #
# Static skill descriptions + corpus coverage.
# --------------------------------------------------------------------------- #

def read_static_skill_descriptions(skills_dir) -> dict:
    """Read every non-generated `<name>/SKILL.md` frontmatter description under a
    skills root. Excludes gen-* (generated skills are not the not-shadow targets)."""
    out: dict[str, str] = {}
    root = Path(skills_dir)
    if not root.is_dir():
        return out
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith("gen-"):
            continue
        md = sub / "SKILL.md"
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8")
            if text.startswith("---\n"):
                block = text[4:text.index("\n---", 4)]
                fm = yaml.safe_load(block) or {}
                out[sub.name] = str(fm.get("description", ""))
        except Exception:
            continue
    return out


def load_corpus(path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("probe corpus must be a JSON list of {query, should_trigger, expect}")
    return data


def coverage_gaps(skill_names, corpus: list[dict]) -> list[str]:
    """Skills with NO HIJACK-DIRECTION probe (`expect == name` AND
    `should_trigger`). A negative-only probe does NOT cover a skill, because the
    gate only proves no-hijack via the should_trigger probes (`hijack_probes`). A
    non-empty list means the corpus cannot prove no-hijack for those skills →
    fail-closed (the should_trigger predicate is the SAME one `run_trigger_gate`
    uses to build hijack_probes, so the two can never diverge)."""
    covered = {p.get("expect") for p in corpus
               if isinstance(p, dict) and p.get("should_trigger")}
    return [n for n in skill_names if n not in covered]


def build_probe_corpus(descriptions: dict) -> list[dict]:
    """Auto-derive a SELF-VALIDATING per-deployment trigger-probe corpus (§6.5): one
    hijack-direction probe per discoverable skill, with `query` = the skill's own
    description (its routing spec; the name as a fallback for a blank description).

    This is the AUTONOMOUS default — fed the same `descriptions` dict the coverage check
    uses, ``coverage_gaps(list(descriptions), build_probe_corpus(descriptions)) == []`` by
    construction, so the eval-gate is never fail-closed on an uncovered skill and needs no
    hand-maintained corpus file that would go stale as a deployment's skill set changes. A
    consumer MAY still supply a curated corpus (richer, more diverse queries) via
    `config.probe_corpus` to override."""
    corpus = []
    for name, desc in descriptions.items():
        query = str(desc).strip() or str(name)
        corpus.append({"query": query, "should_trigger": True, "expect": name})
    return corpus


def _plugin_skill_roots():
    """Best-effort roots that hold installed plugin skills (cache + marketplaces)."""
    base = Path.home() / ".claude" / "plugins"
    return [base / "cache", base / "marketplaces"]


def read_all_invocable_skill_descriptions(repo_root, *, include_plugins=True) -> dict:
    """The FULL not-shadow set the spec (§6.3) makes a HARD arming precondition:
    the project `.claude/skills/*` skills PLUS the auto-invocable plugin skills a
    generated description could shadow (superpowers:*, markov-regime, skill-creator,
    ultra-memory verbs). Keys are skill names; a `disable-model-invocation: true`
    skill is excluded (it can never auto-fire, so it cannot be hijacked). Fail-open
    per-source: a missing/unreadable plugin tree degrades to what it could read —
    but the coverage-gap lint then HOLDS on any uncovered discoverable skill, so an
    incomplete enumeration can never *admit* a shadower (it only ever holds)."""
    out = dict(read_static_skill_descriptions(Path(repo_root) / ".claude" / "skills"))
    if not include_plugins:
        return out
    for root in _plugin_skill_roots():
        try:
            if not root.is_dir():
                continue
            for md in root.glob("**/skills/*/SKILL.md"):
                try:
                    text = md.read_text(encoding="utf-8")
                    if not text.startswith("---\n"):
                        continue
                    fm = yaml.safe_load(text[4:text.index("\n---", 4)]) or {}
                    if fm.get("disable-model-invocation") is True:
                        continue
                    name = str(fm.get("name") or md.parent.name)
                    desc = str(fm.get("description", ""))
                    if name and name not in out:
                        out[name] = desc
                except Exception:
                    continue
        except Exception:
            continue
    return out


def estimate_listing_budget_ok(candidate_description: str, descriptions: dict,
                               *, budget_chars: int = 60000) -> bool:
    """§9 risk 8 (description-budget crowding). A coarse deterministic proxy for the
    skill-listing budget (the real signal is `/doctor`): admitting the candidate
    must keep the total description char count under `budget_chars`. Generous by
    default (it catches a flood, not a single skill)."""
    total = sum(len(str(d)) for d in descriptions.values()) + len(str(candidate_description))
    return total <= budget_chars


# --------------------------------------------------------------------------- #
# Tier-B — the behavioral probe (OAuth-sanitized claude -p command-file proxy).
# --------------------------------------------------------------------------- #

def _iter_tool_use_blocks(ev):
    """Yield the tool_use content blocks of a stream-json event — an assistant
    `message.content[*]` block, or a `content_block` on a content_block_start, at the
    top level OR under a `stream_event` `event` wrapper."""
    if not isinstance(ev, dict):
        return
    nodes = [ev]
    if isinstance(ev.get("event"), dict):
        nodes.append(ev["event"])
    for node in nodes:
        msg = node.get("message")
        if isinstance(msg, dict):
            for b in (msg.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    yield b
        cb = node.get("content_block")
        if isinstance(cb, dict) and cb.get("type") == "tool_use":
            yield cb


def _stream_mentions(stdout: str, cmd_name: str) -> bool:
    """True iff the model actually ENGAGED the candidate skill — a `tool_use`
    content block named `Skill`/`Read` whose input targets `cmd_name`.

    CRITICAL (the 2026-06-01 review finding): a blob/substring match is WRONG — the
    real `claude -p --output-format stream-json --verbose` emits a `type:"system"`
    init event whose `slash_commands` lists the just-written probe and whose `tools`
    array contains the string "Skill", so a substring match fires on EVERY probe and
    the gate becomes vacuous. We therefore (a) skip `type:"system"` events entirely
    and (b) match ONLY inside a tool_use block's `input.skill` / `input.file_path`
    (the structured parse the upstream skill-creator/run_eval.py uses)."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system":
            continue  # init/result enumeration is NOT a fire
        for block in _iter_tool_use_blocks(ev):
            name = block.get("name")
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name == "Skill" and cmd_name in str(inp.get("skill", "")):
                return True
            if name == "Read" and cmd_name in str(inp.get("file_path", "")):
                return True
    return False


def probe_fires(query: str, skill_name: str, skill_description: str, *,
                repo_root, runner=subprocess.run, env=None, timeout: int = 120,
                claude_bin: str = "claude") -> bool:
    """Does the candidate skill auto-fire on `query`? Writes an ephemeral
    `.claude/commands/<name>-probe.md` proxy (commands ≡ skills), spawns
    `claude -p <query> --output-format stream-json` through the OAuth-sanitized env,
    detects a candidate tool-use, then removes the proxy. Raises OAuthViolation if
    the env would route to the metered API."""
    child_env = _child_env(env)  # OAuth-only by construction (refuses API key / no token)
    cmds = Path(repo_root) / ".claude" / "commands"
    cmds.mkdir(parents=True, exist_ok=True)
    # UNIQUE per-call filename so CONCURRENT probes (§1.4.7) never race on one shared file
    # (the old fixed `<skill>-probe.md` collided under the thread pool). Detection matches the
    # STABLE prefix `<skill>-probe`, so any concurrent identical-proxy copy the model picks
    # still counts as the candidate firing.
    detect = f"{skill_name}-probe"
    cmd_name = f"{detect}-{uuid.uuid4().hex[:8]}"
    cmd_file = cmds / f"{cmd_name}.md"
    cmd_file.write_text(
        f"---\ndescription: {json.dumps(skill_description)}\n---\n\n(probe)\n",
        encoding="utf-8")
    try:
        proc = runner([claude_bin, "-p", query, "--output-format", "stream-json",
                       "--verbose"], capture_output=True, text=True,
                      timeout=timeout, env=child_env)
        return _stream_mentions(getattr(proc, "stdout", "") or "", detect)
    finally:
        try:
            cmd_file.unlink()  # ephemeral probe scaffolding — not a knowledge artifact
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# The gate.
# --------------------------------------------------------------------------- #

@dataclass
class EvalReport:
    admit: bool
    verdict: str            # 'admit' | 'reject' | 'hold'
    reason: str
    candidate_fp: int = 0
    tier_a_hit: str | None = None
    probes_evaluated: int = 0
    coverage_gaps: list = field(default_factory=list)


def _probe_outcome(probe, candidate, fn, runs_per_query):
    """Evaluate ONE hijack probe: fire if the candidate triggers on the probe's query
    within `runs_per_query` samples (early-break on the first fire). A probe error fails
    CLOSED (treated as a fire → reject). Returns (fired: bool, calls: int). Pure per-probe
    so the probes parallelize trivially."""
    calls = 0
    for _ in range(max(1, runs_per_query)):
        calls += 1
        try:
            if fn(probe["query"], candidate):
                return True, calls
        except Exception:
            return True, calls  # fail-closed — never admit an un-evaluated skill
    return False, calls


def run_trigger_gate(candidate, *, static_descriptions, corpus,
                     repo_root=None, runner=subprocess.run, env=None,
                     probe_fn=None, similarity_fn=None, theta_desc: float = THETA_DESC,
                     runs_per_query: int = RUNS_PER_QUERY, budget_fn=None) -> EvalReport:
    """The load-bearing gate. Returns an EvalReport; admit iff Tier-A passes, the
    corpus covers every static skill, the listing budget is OK, and the candidate
    fires on ZERO static-skill-intent probes (candidate_fp == 0). HOLD (fail-closed)
    on empty/gap coverage; REJECT on a Tier-A hit or any candidate fire; a probe
    error fails CLOSED to reject."""
    static_names = list(static_descriptions.keys())

    # Tier-A deterministic pre-filter.
    ta = tier_a_reject(candidate.description, static_descriptions,
                       theta_desc=theta_desc, similarity_fn=similarity_fn)
    if ta:
        return EvalReport(admit=False, verdict="reject", reason=ta, tier_a_hit=ta)

    # Coverage: every static skill must have ≥1 probe → else fail-closed.
    gaps = coverage_gaps(static_names, corpus)
    if gaps:
        return EvalReport(admit=False, verdict="hold",
                          reason=f"coverage gap: no probe for {gaps}",
                          coverage_gaps=gaps)

    hijack_probes = [p for p in corpus
                     if isinstance(p, dict) and p.get("expect") in static_names
                     and p.get("should_trigger")]
    if not hijack_probes:
        return EvalReport(admit=False, verdict="hold",
                          reason="no hijack-direction probes (empty → fail-closed)")

    # Listing-budget pre-apply check (§9 risk 8).
    if budget_fn is not None:
        try:
            ok = budget_fn(candidate)
        except Exception:
            ok = False  # fail-closed
        if not ok:
            return EvalReport(admit=False, verdict="hold",
                              reason="listing-budget would overflow — held")

    # Tier-B behavioral gate.
    fn = probe_fn or (lambda q, c: probe_fires(
        q, c.slug, c.description, repo_root=repo_root, runner=runner, env=env))
    # Probes are INDEPENDENT `claude -p` calls → run them CONCURRENTLY in a bounded pool
    # (§1.4.7: serial was ~50min/run and overran the maintenance timeout). Semantics are
    # identical to the serial loop — candidate_fp is the count of probes that fired (order-
    # independent), each probe early-breaks on its first fire, a probe error fails CLOSED.
    workers = max(1, min(PROBE_MAX_WORKERS, len(hijack_probes)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(
            lambda p: _probe_outcome(p, candidate, fn, runs_per_query), hijack_probes))
    candidate_fp = sum(1 for fired, _ in outcomes if fired)
    evaluated = sum(calls for _, calls in outcomes)
    if candidate_fp > 0:
        return EvalReport(admit=False, verdict="reject",
                          reason=f"trigger hijack: candidate fired on "
                                 f"{candidate_fp} static-skill probe(s)",
                          candidate_fp=candidate_fp, probes_evaluated=evaluated)
    return EvalReport(admit=True, verdict="admit",
                      reason="no hijack (candidate_fp==0); Tier-A + budget clear",
                      candidate_fp=0, probes_evaluated=evaluated)
