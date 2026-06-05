"""UserPromptSubmit hook (Recall-Reflex Tier-2): recognise a concrete error
signature in the prompt -> recall prior art -> inject it as additionalContext, so
the lesson is present with ZERO reliance on the agent remembering to look.

Posture (all from the spec-review-gate):
  * **Tier-2 only.** The fuzzy Tier-1 "debug-intent" nag is deliberately NOT built —
    the SessionStart gist + the recall-reflex skill already cover "remember to
    recall"; Tier-2 fires only on the lowest-ambiguity, highest-value case.
  * **Knowledge-only + BM25.** recall(knowledge_only=True, build_embedder=False):
    no memory backend (privacy-safe by construction — no user/feedback can surface),
    no fastembed model load (snappy on every prompt), literal error text matches the
    page's full-body BM25 (which includes any ## Signal section).
  * **Conservative matcher.** detect_signature fires only on strong signals
    (stacktrace / ExceptionName / Error: / file.ext:line / OS error), never on a
    plain question.
  * **Fail-open + frugal + kill-switchable.** Any error -> {} (no injection, rc 0);
    <= 3 hits; RECALL_HOOK_DISABLE=1 turns it off.
"""
import json
import os
import re

from ultra_memory.hooks import common

_MAX_HITS = 3
_QUERY_CAP = 300
_MAX_SIG_LINES = 3

# Strong error signatures only (precision over recall — start conservative, measure).
_SIG_RES = [re.compile(p) for p in (
    r"Traceback \(most recent call last\)",          # python stacktrace
    r"\b[A-Za-z_][\w.]*(?:Error|Exception|Warning)\b",  # ValueError, pkg.Mod.FooException
    r"\b(?:Error|Errno|Exception)\s*:",              # "Error:", "Exception:"
    r"\bNo such file or directory\b",                # common OS error
    r"\b[\w./\-]+\.[A-Za-z]{1,6}:\d+\b",             # path/file.ext:123 source location
    r"\b(?:panic|segmentation fault|fatal error|core dumped)\b",
)]


def detect_signature(text):
    """Return a concise recall query from a prompt that contains a CONCRETE error
    signature, else None. Conservative: only fires on strong signals, never on a
    plain question. Returns the joined error-bearing lines (<= _MAX_SIG_LINES),
    capped — so distinctive tokens (paths, class names) make it into the query."""
    if not text:
        return None
    hits = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(rx.search(s) for rx in _SIG_RES):
            hits.append(s)
            if len(hits) >= _MAX_SIG_LINES:
                break
    if not hits:
        return None
    return " ".join(hits)[:_QUERY_CAP]


def _render(hits):
    """Render recall hits as a compact additionalContext block. Wiki hits get a
    `[[slug]]` so the agent can open the full page; advisory framing per the
    safety invariant (recall is context, not a gate)."""
    lines = [
        "## Recall-Reflex — prior art for this problem",
        "(auto-recalled from ultra-memory by error-signature; advisory context — "
        "verify before acting, it does not replace any gate)",
    ]
    for h in hits:
        title = (h.get("title") or "").strip()
        if h.get("source_kind") == "knowledge":
            line = f"- [[{h.get('slug')}]] — {title}"
            if h.get("path"):
                line += f"  ({h['path']})"
        else:
            line = f"- {title}"
        lines.append(line)
    return "\n".join(lines)


def run(payload, *, db_path):
    """Build the injection dict, or {} when nothing to inject. Fail-open."""
    try:
        if os.environ.get("RECALL_HOOK_DISABLE", "").strip():
            return {}
        if common.agent_role_optout(payload):
            return {}
        if not common.db_ready(db_path):
            return {}
        prompt = (payload or {}).get("prompt") or ""
        signature = detect_signature(prompt)
        if not signature:
            return {}
        from ultra_memory import recall as recall_mod
        hits = recall_mod.recall(
            signature, top_k=_MAX_HITS, caller_class="subagent", agent_topics=None,
            db_path=db_path, knowledge_only=True, build_embedder=False)
        if not hits:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": _render(hits)}}
    except Exception:
        return {}


def main(stdin, stdout):
    payload = common.read_payload(stdin)
    db_path = common.resolve_db_path()
    out = run(payload, db_path=db_path)
    if out:
        json.dump(out, stdout)
    return 0
