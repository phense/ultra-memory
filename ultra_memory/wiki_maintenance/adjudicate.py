"""adjudicate — the ONE batched OAuth Stage-2 decision (project-agnostic).

Reads the Stage-1 worklist, makes ALL decisions, THEN applies writes. Hard invariant:
no LLM call ever runs inside a write. Flow:

  1. read worklist; ``is_empty`` (or zero items) → exit 0, ZERO LLM/judge calls.
  2. Phase 1 — grey-zone dedup: an injected ``merge_decider(cosine, claim, cand_text)``
     decides each ``greyzone-dedup`` item; a merge → a deterministic ``redirect-stub``.
     The default decider auto-merges only a pair at/above ``schema.dedup_upper`` (the
     conservative no-LLM default); a consumer injects its calibrated judge.
  3. Phase 2 — bundled LLM: items partitioned by owning root, chunked, ONE
     ``claude_call`` per chunk (the OAuth chokepoint by default = ``run_claude``).
     Parse ``{"actions": [...]}``; a chunk that errors is logged + skipped (partial
     progress); ALL chunks failing → return 1, write nothing.
  4. Apply phase: dispatch each action through ``apply_fns[op]`` (the ONLY writes).
     ``create-page``/``log`` shell the CONSUMER gateway (``config.wiki_gateway``); the
     ``edit``/``redirect-stub`` ops write files directly. Unknown/malformed ops skip.

The decision prompt, the redirect-stub frontmatter, the topic derivation and the merge
threshold are schema/config seams. OAuth-only: the default ``claude_call`` is
``claude_cli.run_claude`` (the chokepoint that strips any metered-API key and requires
the OAuth token); never the metered SDK surface.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ultra_memory.wiki_maintenance import worklist
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig
from ultra_memory.wiki_maintenance.wiki_util import today_iso

MAX_ITEMS = 60       # bundled-prompt safety cap (lowest priority int = highest priority)
CHUNK_SIZE = 6       # items per bundled LLM call (OAuth CLI ~23s/item at effort=low)
DEFAULT_TIMEOUT = 300
DEFAULT_EFFORT = "low"
OPS = ("edit", "create-page", "log", "redirect-stub")


# ── system prompt (schema-driven, content-free) ──────────────────────────────

def build_sys(schema: WikiSchemaConfig, index_stems: list[str], *, wiki_dir: str = "wiki") -> str:
    """Build the Stage-2 adjudicator system prompt from the wiki schema. The
    link-hygiene clause names the CURRENT theme-index stems (passed in) as the valid
    [[wikilink]] example, so the engine stays content-free — no consumer literal."""
    atomics = schema.atomics_subdir
    synth = schema.synthesis_subdir
    master = schema.topic_master_index
    index_example = schema.index_filename("<theme>")
    sample = sorted(set(index_stems))[:40]
    if sample:
        example = ("index pages are named per the index template (e.g. "
                   + ", ".join(f"[[{s}]]" for s in sample)
                   + "); use ONLY a CURRENT index slug, never a renamed one. ")
    else:
        example = "use ONLY a CURRENT index slug. "
    return (
        "You are the Stage-2 wiki-maintenance adjudicator. You receive a JSON list "
        "of worklist items (cross-link, recategorize, recalibrate, contradiction, "
        "synthesis-candidate, summarize, stale-archive, index-create, index-split). "
        "Decide the concrete edits and return ONLY a JSON object "
        '{"actions": [...]} — no prose, no markdown fences. Each action is one of:\n'
        f'  - "edit": {{"op":"edit","page":<rel path under {wiki_dir}/>,'
        '"old_string":<verbatim text that appears EXACTLY ONCE in the page>,'
        '"new_string":<replacement>,"reason":<str>} — used for cross-link, '
        "recategorize (move an index bullet = remove edit + insert edit), "
        "recalibrate, summarize, contradiction.\n"
        f'  - "create-page": {{"op":"create-page","path":<{wiki_dir}/{atomics} or '
        f'{wiki_dir}/{synth} path>,"content":<full markdown incl. YAML frontmatter>,'
        '"reason":<str>}.\n'
        '  - "log": {"op":"log","message":<str>} — a human run-log line.\n'
        "Per-kind mapping (use the EXACT kind token each item carries):\n"
        f"  - index-create -> a create-page action whose path is "
        f"{wiki_dir}/<topic>/{atomics}/{index_example} (the theme lowered, with / and "
        f"spaces replaced by -), `type: {schema.index_types[0]}` frontmatter, PLUS a "
        f"paired edit action that inserts a `- [[<index-slug>]]` bullet into the TOPIC "
        f"MASTER {wiki_dir}/<topic>/{master} so the new index is linked the SAME run "
        f"(old_string = a verbatim unique anchor line already in the master; new_string "
        "= that line followed by the new bullet).\n"
        "  - index-split -> edit action(s) on the oversized index page.\n"
        f"  - synthesis-candidate -> a create-page action under {wiki_dir}/<topic>/{synth}/.\n"
        "  - recalibrate / contradiction / cross-link / recategorize / summarize / "
        "stale-archive -> edit action(s) (or a log action if no safe edit exists).\n"
        "Rules: NEVER delete a page; consolidation/merges are handled separately. "
        "Prefer minimal, surgical edits. The old_string MUST be copied verbatim from "
        "the page and be unique; if you cannot produce a safe verbatim old_string, emit "
        "a `log` action noting it needs manual review instead of guessing.\n"
        "Link hygiene (avoid broken links): every [[wikilink]] you write MUST target an "
        "EXISTING wiki page by its CURRENT slug — " + example +
        "Non-wiki project paths are DIRECTORIES, not wiki pages — reference them with "
        "backticks, NEVER as [[wikilinks]]. When unsure a target exists, omit the link "
        "or use plain text."
    )


# ── prompt building ──────────────────────────────────────────────────────────

def _build_prompt(items: list[dict], wiki_root: str) -> str:
    payload = {"wiki_root": wiki_root, "items": items}
    return (
        "Adjudicate the following wiki-maintenance worklist items. For each, decide the "
        'concrete write(s) per your system instructions and return ONLY {"actions": [...]}.\n\n'
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


_OP_REQUIRED_STR_KEYS = {
    "edit": ("page", "old_string", "new_string"),
    "create-page": ("path", "content"),
    "log": ("message",),
    "redirect-stub": ("page", "canonical"),
}


def _action_malformed_reason(action: dict) -> str | None:
    """A one-line reason an action is malformed (must be skipped), or None. Validates
    only the known-op required-key + str-type contract so a bad action never crashes
    the non-transactional apply loop and drops every action after it."""
    op = action.get("op")
    required = _OP_REQUIRED_STR_KEYS.get(op)
    if required is None:
        return None
    for key in required:
        if key not in action:
            return f"missing required key {key!r}"
        if not isinstance(action[key], str):
            return f"key {key!r} is {type(action[key]).__name__}, expected str"
    return None


def _parse_actions(stdout: str) -> list[dict]:
    """Parse the bundled response into actions. Robust to a leading ```json fence;
    raises ValueError on any deviation (caller fails closed)."""
    text = stdout.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict) or "actions" not in data:
        raise ValueError("response JSON missing top-level 'actions' list")
    actions = data["actions"]
    if not isinstance(actions, list):
        raise ValueError("'actions' is not a list")
    return actions


# ── apply implementations ────────────────────────────────────────────────────

def _read_sources_lines(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("**Sources**") or s.startswith("Sources:"):
            out.append(ln)
    return out


def _frontmatter_field(text: str, key: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text[3:].find("\n---")
    if end == -1:
        return None
    for ln in text[3:3 + end].splitlines():
        s = ln.strip()
        if s.startswith(f"{key}:"):
            return s[len(key) + 1:].strip()
    return None


def _action_base(action: dict, *, fallback: Path) -> Path:
    """The repo base an action's path resolves against: ``<action root>.parent`` (each
    worklist item carries its owning wiki root; detectors emit ``<root>.parent``-relative
    ``<wiki_dir>/...`` paths), else *fallback* (single-root / back-compat)."""
    root = action.get("root")
    if root:
        return Path(root).parent
    return fallback


def _topic_from_path(rel: str, *, wiki_dir: str, default_topic: str) -> str:
    """Derive the topic from a wiki-relative path (``<wiki_dir>/<topic>/<subdir>/...``);
    a path with no topic segment falls back to *default_topic*."""
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == wiki_dir:
        return parts[1]
    return default_topic


def real_apply_fns(*, gateway=None, gateway_prefix: list[str] | None = None,
                   runner=subprocess.run, cwd: Path | None = None,
                   today: str | None = None, schema: WikiSchemaConfig | None = None,
                   default_topic: str = "default", wiki_dir: str = "wiki") -> dict:
    """Build the real apply dispatch dict. ``create-page``/``log`` shell the consumer
    gateway with ``cwd=<action root>.parent``; ``edit``/``redirect-stub`` write files
    directly. Each action resolves against its OWN source root (``action['root']``),
    else *cwd* (default cwd of the process).

    The gateway argv is built from *gateway_prefix* (the RESOLVED argv prefix from
    ``wiki_curate._resolve_gateway`` — e.g. ``["python", "-m",
    "ultra_memory.wiki_gateway"]`` for the built-in turnkey, or a ``--gateway-class``
    prefix, or ``["uv", "run", <path>]``) + ``[verb, …args, "--from-file", tmp]``. When
    *gateway_prefix* is None (back-compat / direct callers that pass only *gateway*) it
    falls back to the legacy ``["uv", "run", str(gateway)]`` prefix."""
    schema = schema or WikiSchemaConfig()
    fallback = Path(cwd) if cwd is not None else Path.cwd()
    if today is None:
        today = today_iso()
    if gateway_prefix is None:
        gateway_prefix = ["uv", "run", str(gateway)]
    else:
        gateway_prefix = list(gateway_prefix)

    def _resolve(action: dict, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else _action_base(action, fallback=fallback) / p

    def _run_gateway_verb(verb_args: list[str], content: str, *, suffix: str,
                          run_cwd: Path, fail_label: str) -> None:
        """Write `content` to a temp file, shell ``<gateway_prefix> <verb_args…>
        --from-file <tmp>`` in *run_cwd*, warn on a non-zero exit, always unlink. The
        ``getattr`` reads tolerate an injected test `runner` that returns a non-
        ``CompletedProcess`` shape."""
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                         encoding="utf-8") as tf:
            tf.write(content)
            tmp = tf.name
        try:
            proc = runner([*gateway_prefix, *verb_args, "--from-file", tmp],
                          capture_output=True, text=True, cwd=str(run_cwd))
            if getattr(proc, "returncode", 0) != 0:
                print(f"warning: {fail_label}: "
                      f"{(getattr(proc, 'stderr', '') or '')[:300]}", file=sys.stderr)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def apply_edit(action: dict) -> None:
        page = _resolve(action, action["page"])
        old, new = action["old_string"], action["new_string"]
        if not page.is_file():
            print(f"warning: edit target not found, skipping: {page}", file=sys.stderr)
            return
        text = page.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            print(f"warning: edit old_string occurs {count} time(s) (need exactly 1) "
                  f"in {action['page']}; skipping", file=sys.stderr)
            return
        page.write_text(text.replace(old, new, 1), encoding="utf-8")

    def apply_create_page(action: dict) -> None:
        content, path = action["content"], action["path"]
        topic = _topic_from_path(path, wiki_dir=wiki_dir, default_topic=default_topic)
        _run_gateway_verb(["create-page", "--path", path, "--topic", topic], content,
                          suffix=".md", run_cwd=_action_base(action, fallback=fallback),
                          fail_label=f"create-page failed for {path}")

    def apply_log(action: dict) -> None:
        _run_gateway_verb(["log"], action["message"], suffix=".txt",
                          run_cwd=_action_base(action, fallback=fallback),
                          fail_label="log write failed")

    def apply_redirect_stub(action: dict) -> None:
        page = _resolve(action, action["page"])
        canonical = action["canonical"]
        if not page.is_file():
            print(f"warning: redirect-stub target not found, skipping: {page}", file=sys.stderr)
            return
        text = page.read_text(encoding="utf-8")
        title = _frontmatter_field(text, schema.title_field) or page.stem
        created = _frontmatter_field(text, "created")
        sources = _read_sources_lines(text)
        fm_lines = ["---", f"{schema.type_field}: {schema.redirect_type}",
                    f"{schema.title_field}: {title}", f"redirect_to: [[{canonical}]]"]
        if created:
            fm_lines.append(f"created: {created}")
        fm_lines.append(f"{schema.updated_field}: {today}")
        fm_lines.append("---")
        body = [f"Merged into [[{canonical}]]."]
        if sources:
            body.append("")
            body.extend(sources)
        page.write_text("\n".join(fm_lines) + "\n\n" + "\n".join(body) + "\n", encoding="utf-8")
        # D10-1: keep the merged page's source attribution on the RETRIEVABLE surface.
        # recall()/wiki_query drop redirect stubs, so sources left ONLY in the stub fall
        # out of the warm surface — the never-delete contract is about retrievability,
        # not just bytes-on-disk. When the canonical's resolved path is known (the
        # deterministic dedup path threads `canonical_path`; an LLM-emitted stub carries
        # only the slug and keeps the prior stub-only behavior), append the dup's
        # not-already-present Sources line(s) onto the canonical so the attribution
        # survives in a page that recall can actually return.
        canonical_path = action.get("canonical_path")
        if sources and canonical_path:
            canon = _resolve(action, canonical_path)
            if canon.is_file() and canon.resolve() != page.resolve():
                canon_text = canon.read_text(encoding="utf-8")
                existing = set(_read_sources_lines(canon_text))
                merged = [s for s in sources if s not in existing]
                if merged:
                    base = canon_text if canon_text.endswith("\n") else canon_text + "\n"
                    canon.write_text(base + "\n".join(merged) + "\n", encoding="utf-8")

    return {"edit": apply_edit, "create-page": apply_create_page,
            "log": apply_log, "redirect-stub": apply_redirect_stub}


def _noop(_action: dict) -> None:
    return None


NOOP_APPLY = {op: _noop for op in OPS}


# ── grey-zone dedup (Phase 1) ────────────────────────────────────────────────

def _greyzone_actions(w: dict, *, merge_decider) -> list[dict]:
    """Deterministic redirect-stub actions for grey-zone dedup items the *merge_decider*
    decides to merge. ZERO disk writes; the NEW atomic redirects to the candidate."""
    actions: list[dict] = []
    for item in w["items"]:
        if item.get("kind") != "greyzone-dedup":
            continue
        cand_text = item.get("candidate_text")
        cosine = item.get("cosine")
        cand_path = item.get("candidate_path")
        if cand_text is None or cosine is None or cand_path is None:
            continue
        if merge_decider(cosine, item.get("claim", ""), cand_text):
            actions.append({
                "op": "redirect-stub", "page": item["atomic_path"],
                "canonical": Path(cand_path).stem,
                "canonical_path": cand_path,
                "reason": f"dedup cosine={cosine}", "root": item.get("root")})
    return actions


# ── default LLM call (OAuth chokepoint) ──────────────────────────────────────

def _default_claude_call(prompt, *, model, system, timeout, env, claude_bin, effort, runner):
    from ultra_memory.claude_cli import run_claude
    return run_claude(prompt, model=model, system=system, claude_bin=claude_bin,
                      timeout=timeout, runner=runner, env=env, effort=effort)


# ── orchestration ────────────────────────────────────────────────────────────

def adjudicate(worklist_path, *, gateway=None, gateway_prefix=None, model,
               claude_call=None, runner=subprocess.run, apply_fns=None,
               schema: WikiSchemaConfig | None = None, merge_decider=None,
               sys_prompt=None, index_stems=None, timeout=DEFAULT_TIMEOUT,
               effort=DEFAULT_EFFORT, env=None, claude_bin="claude",
               default_topic="default", wiki_dir="wiki", fallback_cwd=None) -> int:
    """Run Stage-2 adjudication over the worklist. Returns 0 on success, 1 if EVERY
    bundled chunk failed (writes nothing). The default *claude_call* is the OAuth-only
    ``run_claude``; the default *merge_decider* auto-merges only a pair at/above
    ``schema.dedup_upper``.

    *gateway_prefix* is the RESOLVED gateway argv prefix (from
    ``wiki_curate._resolve_gateway``); when None the apply path falls back to the
    legacy ``["uv", "run", str(gateway)]`` so direct callers that pass only *gateway*
    keep working."""
    schema = schema or WikiSchemaConfig()
    if apply_fns is None:
        apply_fns = real_apply_fns(gateway=gateway, gateway_prefix=gateway_prefix,
                                   runner=runner, cwd=fallback_cwd, schema=schema,
                                   default_topic=default_topic, wiki_dir=wiki_dir)
    if merge_decider is None:
        upper = schema.dedup_upper
        merge_decider = lambda cosine, claim, cand: cosine is not None and cosine >= upper  # noqa: E731
    if claude_call is None:
        def claude_call(prompt, **kw):
            return _default_claude_call(
                prompt, model=model, system=kw["system"], timeout=timeout, env=env,
                claude_bin=claude_bin, effort=effort, runner=runner)

    w = worklist.read_worklist(Path(worklist_path))

    if worklist.is_empty(w):
        print(f"adjudicate: worklist {worklist_path} is empty — skip (no LLM call)", file=sys.stderr)
        return 0
    if not w["items"]:
        print("[adjudicate] worklist has no items (only auto-fixes/graph-findings) — "
              "nothing to adjudicate", file=sys.stderr)
        return 0

    actions: list[dict] = []
    actions.extend(_greyzone_actions(w, merge_decider=merge_decider))

    non_dedup = [i for i in w["items"] if i.get("kind") != "greyzone-dedup"]
    if non_dedup:
        groups: dict[object, list[dict]] = {}
        for item in non_dedup:
            groups.setdefault(item.get("root"), []).append(item)

        if sys_prompt is None:
            sys_prompt = build_sys(schema, index_stems or [], wiki_dir=wiki_dir)

        chunks_attempted = 0
        chunks_errored = 0
        for group_root, group_items in groups.items():
            ranked = sorted(group_items, key=lambda i: i.get("priority", 99))
            if len(ranked) > MAX_ITEMS:
                dropped = len(ranked) - MAX_ITEMS
                print(f"adjudicate: {len(ranked)} non-dedup items; capping to top {MAX_ITEMS} "
                      f"by priority — dropping {dropped} lowest-priority item(s)", file=sys.stderr)
                ranked = ranked[:MAX_ITEMS]
            prompt_root = group_root if group_root is not None else w.get("wiki_root", "wiki")
            for start in range(0, len(ranked), CHUNK_SIZE):
                chunk = ranked[start:start + CHUNK_SIZE]
                chunks_attempted += 1
                prompt = _build_prompt(chunk, str(prompt_root))
                try:
                    stdout = claude_call(prompt, system=sys_prompt)
                    group_actions = _parse_actions(stdout)
                except Exception as exc:  # noqa: BLE001 — chunk-isolated fail-open
                    chunks_errored += 1
                    print(f"adjudicate: chunk {start // CHUNK_SIZE} of group ({len(chunk)} "
                          f"item(s)) failed ({exc}); skipping it, continuing.", file=sys.stderr)
                    continue
                for act in group_actions:
                    if group_root is not None and act.get("root") is None:
                        act["root"] = group_root
                actions.extend(group_actions)

        if chunks_attempted and chunks_errored == chunks_attempted:
            print(f"adjudicate: all {chunks_attempted} bundled chunk(s) failed; writing nothing.",
                  file=sys.stderr)
            return 1

    for action in actions:
        op = action.get("op")
        fn = apply_fns.get(op)
        if fn is None:
            print(f"warning: unknown action op {op!r}; skipping", file=sys.stderr)
            continue
        reason = _action_malformed_reason(action)
        if reason is not None:
            print(f"warning: action {op!r} malformed ({reason}); skipping", file=sys.stderr)
            continue
        try:
            fn(action)
        except Exception as exc:  # noqa: BLE001 — per-action fail-open
            print(f"warning: action {op!r} failed ({exc}); skipping", file=sys.stderr)
            continue

    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stage-2 bundled wiki-maintenance adjudicator (generic).")
    ap.add_argument("--worklist", required=True)
    ap.add_argument("--gateway", required=True, help="consumer wiki write gateway (wiki_lib.py).")
    ap.add_argument("--model", required=True)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)
    return adjudicate(args.worklist, gateway=args.gateway, model=args.model, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
