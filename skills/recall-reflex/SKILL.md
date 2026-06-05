---
name: recall-reflex
description: Use at the START of any debug/build task or when you observe an abnormal/unforeseen condition (an error signature, a stacktrace, a surprising market/data state) — BEFORE reading code or acting. The reflex is: recognise the situation → recall what we already know about it → act informed, so accumulated lessons are reused instead of re-derived. Also use when you just solved something non-obvious (bridge to capture). Pairs with the UserPromptSubmit recall hook + the recall() primitive.
---

# Recall-Reflex

> **Recognise a situation → recall what you know about it → act informed.**

ultra-memory exists not only to *store* knowledge but to *reuse* it. The failure this skill
prevents: re-deriving a solution that already lives in the wiki/memory (we once fixed the same
fastembed `$TMPDIR`-cache bug twice). The reflex fires on the **observable** — an error
signature or an abnormal condition — not after you've already decided to look something up.

## The loop

1. **Recognise.** A debug/build task starts, or you observe an abnormal condition (a stacktrace,
   `SomeError:`, `file.ext:line`, "No such file", or — for trading — a sector-wide drawdown, a
   vol spike, an unforeseen regime). That observable is your **signal**.
2. **Recall — BEFORE reading code / acting.**
   - For a concrete error signature in the user's prompt, the **UserPromptSubmit hook already
     ran** `recall()` and injected a "## Recall-Reflex — prior art" block. **Read those hits
     first** and open any `[[slug]]` that looks relevant (`uv run --script scripts/wiki_query.py
     "…"` in Trading, or browse the wiki).
   - Otherwise (or to go deeper), run a targeted recall yourself, querying with the **observable
     in the words it appears**:
     ```bash
     python -m ultra_memory.recall "onnxruntime NoSuchFile model_optimized.onnx temp purge" --top 5
     ```
     In Trading you can also use the richer agent CLI: `uv run --script scripts/wiki_query.py
     "<signal>" --top 5` (BM25 + embedding + graph).
3. **Act informed.** Treat recalled hits as **prior art / advisory context** — they tell you what
   we already learned. A recall **miss is never evidence of safety**, and a hit never replaces a
   gate (for trading, recall composes *before* the `risk-manager` / hard-rules check, never
   bypasses it).

## Formulating the query

- Use the literal observable: the error class, the failing path, the OS message, the market
  condition — the words a future occurrence would actually contain. Pages carry a `## Signal`
  section keyed to exactly these words.
- Keep it short and distinctive; drop boilerplate ("Traceback", line numbers) in favour of the
  identifying tokens (the exception name, the artifact name, the symptom).

## Bridge to capture (close the loop)

If you **solved** something non-obvious — a gotcha, a workaround, a strategy lesson — capture it
so the next occurrence is a 2-second recall hit, not a re-derivation:

- Write a `## Signal`-keyed atomic **through the gateway** (never hand-create pages):
  in Trading, `uv run scripts/wiki_lib.py create-page …` with a `## Signal` section whose body is
  the literal observable (see `wiki/SCHEMA.md` → "## Signal section"). The autonomous
  session-ingest/consolidate backstop also captures lessons, but the immediate author-written
  `## Signal` carries the highest-quality observable words.
- Anti-duplication is automatic: the gateway's `## Signal`-aware dedup merges a near-duplicate
  observable instead of creating a second page.

## Notes

- The hook is **Tier-2 only** (fires on a concrete error signature), **knowledge-only**, fail-open,
  and capped at 3 hits. Disable per-session with `RECALL_HOOK_DISABLE=1` if it ever gets noisy.
- Privacy: hook recall is `caller_class=subagent` + knowledge-only — it never surfaces
  `user`/`feedback` memory.
