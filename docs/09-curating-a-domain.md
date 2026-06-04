# 9. Curating a domain

A knowledge wiki that nobody curates rots in a predictable way: near-duplicate pages pile up, links go stale, the indexes drift out of sync with the files, and the thing you wrote six months ago is unfindable. ultra-memory's answer is that curation is **mostly not your job** — a deterministic maintenance pipeline lints, dedups, cross-links, and re-indexes your topic on a schedule, and the few writes that *are* your job go through audited verbs that keep the structure correct *by construction*.

[Chapter 08](08-build-your-own-domain.md) stood your domain up. This chapter keeps it healthy. We continue with the `cooking` domain alongside the real `trading` reference, so every abstract verb has a concrete analogue.

---

## The shape you are curating

Before the verbs, fix the structure in your head — because every verb's job is to *preserve* it. The wiki is a **master-over-masters** browse tree:

```
wiki/index.md                       ← master-over-masters: ONE [[<topic>/index]] per topic
  wiki/trading/index.md             ← topic master: links this topic's theme-indexes
    wiki/trading/concepts/<slug>-index.md   ← a theme-index: links its atomics
      wiki/trading/concepts/<slug>.md       ← an atomic page: ONE idea
```

The discipline that keeps this readable:

- **Atomic pages** hold one idea each — soft cap **400 lines**, hard cap **800**.
- **Indexes** stay tight (one line per entry); a theme-index past ~**300 lines** gets sharded.
- Every page carries **YAML frontmatter** (at minimum `type` and `title`).
- Every cross-reference is a **`[[wikilink]]`**, never a raw path.

These defaults are the `WikiSchemaConfig` reference values; a domain that follows them needs no schema overrides. A `cooking` topic looks identical, only the content differs:

```
wiki/cooking/index.md
  wiki/cooking/concepts/braising-index.md
    wiki/cooking/concepts/low-and-slow-collagen-conversion.md
```

---

## Reading: the two complementary paths

You curate what you can find. There are two read paths, and they are complementary, not redundant.

**1 · Programmatic retrieval (the canonical path).** Ask a question; get a ranked answer. The cross-store fabric fuses BM25 + embeddings + the link graph and returns memory rows *and* wiki pages in one list, scoped by your caller class and topic. In the trading project this is surfaced by `scripts/wiki_query.py`:

```bash
uv run --script scripts/wiki_query.py "how does collagen break down when braising" --top 5
```

It emits JSON — per-hit `slug` / `path` / `snippet` / `match_loc`, plus `graph_context` (one-hop neighbours and backlinks). Cite what it surfaces with `[[wikilinks]]`. (The engine call beneath it is `unified_recall`; the script is a project-side convenience wrapper.)

**2 · Hand-browse (the master-over-masters).** When you want to *understand the lay of the land* rather than answer one question, browse top-down: open `wiki/index.md` → pick a topic master (`wiki/cooking/index.md`) → pick a `<slug>-index.md` theme-index → open the one atomic page you need. The index is engineered to be cheap to read; that is why entries are one line each.

> **Privilege note.** Retrieval is scoped. A subagent caller sees `project` / `reference` knowledge but never `user` / `feedback` memories — fail-closed. The trusted top-level session gets full recall via the CLI. You don't configure this per-read; it comes from the caller's class. See [the privilege boundary](07-privacy-cost-control.md#the-privilege-boundary--subagents-cant-read-everything) for what it protects, or [Working with your memory](04-working-with-memory.md#the-privilege-boundary-from-where-you-sit) for how it looks from where you sit.

---

## Writing: the four gateway verbs

Here is the rule that makes everything else hold: **structured content is written ONLY through the gateway verbs.** You never hand-create an atomic or index page with a text editor, and you never run a raw ingest. Each verb routes, deduplicates, **redacts secrets**, and appends an audit row — skip the gateway and you lose all four.

Invoke them as subcommands of your gateway (the built-in `python -m ultra_memory.wiki_gateway`, or your subclass's CLI shim, or — in the trading project — `uv run scripts/wiki_lib.py <verb>`). Multi-line bodies come from a file via `--from-file`.

### `create-page` — graduate a matured idea into a new page

Use when an insight has matured enough to deserve its own atomic. It refuses to clobber an existing page, and it only writes under `<topic>/concepts/` or `<topic>/synthesis/`.

```bash
uv run scripts/wiki_lib.py create-page \
  --topic cooking \
  --path wiki/cooking/concepts/low-and-slow-collagen-conversion.md \
  --from-file /tmp/collagen.md \
  --source-label kitchen-notes
```

### `append-validation-log` — record empirical evidence on an existing page

Use when reality tests a claim and you want the page to carry the evidence. It appends a (redacted) entry to the page's `## Empirical Validation Log`, creating the section if absent, and bumps the page's `updated:` date. It is **idempotent** — a re-run of the same entry returns `already-logged` and writes nothing.

```bash
uv run scripts/wiki_lib.py append-validation-log \
  --topic cooking \
  --page wiki/cooking/concepts/low-and-slow-collagen-conversion.md \
  --from-file /tmp/brisket-result.md
```

In trading, this is how a backtest result lands on the strategy page that predicted it.

### `register-index` — file an atomic under its theme-index

Use when a new atomic needs to be discoverable through the browse tree. It registers the slug under its `<slug(theme)>-index.md`, and — critically — **when the theme is new, it links that theme-index into the topic's `index.md`**, and links the topic master into the master-over-masters. One verb wires all three tiers, so the hand-browse path never goes stale.

```bash
uv run scripts/wiki_lib.py register-index \
  --topic cooking \
  --slug low-and-slow-collagen-conversion \
  --theme braising \
  --summary "Collagen → gelatin above ~70°C over hours; the basis for tender braises."
```

### `log` — append a human run-summary line

Use to leave a one-line human-readable trace in `wiki/log.md` — what a curation or ingestion run did, for the next person reading the tree.

```bash
uv run scripts/wiki_lib.py log --message "Ingested 8 braising recipes; 2 merged as near-dupes."
```

> **The one documented exception.** A free-form prose amendment to an *existing* page — fixing a sentence, adding a cross-link mid-paragraph — is allowed as a direct edit. Lint + git are the control on that path. Everything that *creates structure* (new atomics, new index entries, validation logs) goes through the verbs.

Every verb call appends a row to `briefings/maintenance-logs/wiki-writes-<date>.jsonl`. That audit trail is your record of who wrote what, and how many secrets were redacted in the process.

---

## The maintenance pipeline: curation you don't run by hand

The verbs keep individual writes clean. The **maintenance pipeline** keeps the *whole tree* healthy over time — and it runs on a schedule, not on your attention. It is two tiers, both **fail-open** (a step that errors logs one diagnostic line and no-ops; it never wedges the run).

**Tier-1 (fast, no LLM).** Retention/prune, exports, index reconciliation. Pure Python — nothing here calls a model.

**Tier-2 (scheduled, one bundled LLM call).** This is where your topic gets curated:

- **Stage 1 — detect → worklist.** Deterministic detectors scan the active wiki roots and build a worklist of candidate actions: `detect_scope` (new atomics since the last run), `detect_dedup` (embedding-cosine near-duplicates), `detect_lint` (structural findings — broken links, missing frontmatter, oversize pages), `detect_graph` (orphans and clusters in the link graph), and `detect_stale` (pages marked `superseded`). Each worklist item is stamped with its owning root.
- **Stage 2 — adjudicate.** The worklist is handed to **one batched OAuth `claude` call** that decides each item — merge this near-dup, recategorize that page, add this cross-link. The decisions are applied **through your gateway verbs** (so they too are routed, redacted, and audited). One LLM call per run, not one per page.

You invoke a beat on demand like this:

```bash
python -m ultra_memory.maintenance --beat wiki_maintenance
```

but in practice it runs from a scheduled job. In the trading project, that is a launchd cron at 03:00 Europe/Berlin calling the stage-aware CLI (`--stage 1|2`) under separate timeouts. The pipeline operates over the `(global, project)` root pair once the global root is activated; today over the single project root.

### Two safety rails worth knowing

- **Maintenance never deletes an atomic.** Consolidation of a duplicate is a *redirect-stub* (the page becomes `type: redirect` pointing at the canonical page, with sources concatenated), never an `rm`. This is a hard rule, learned the hard way — a 2026 run once deleted most of a batch via false-positive matches. Your pages are safe.
- **The grey zone is conservative.** Dedup has a calibrated band (cosine `0.78`–`0.86`): below it, two pages are distinct and both kept; above it, they auto-merge. Inside the band, a domain can wire an optional `wiki_merge_decider` — a calibrated judge that decides "same idea?" — otherwise the pipeline merges only on clear matches.

---

## Linting and the index health you can run yourself

You don't have to wait for the nightly run to check structure. A consumer can supply its own linter via `config.toml`'s `wiki_linter` seam (the trading project does, because its area-stripped wikilinks and master-over-masters layout would over-flag under the engine's naive generic lint). The linter is just the *findings producer*; the pipeline's generic routing consumes its output unchanged. Absent a custom linter, the engine's built-in structural lint runs — it checks the same invariants: required frontmatter per page-type, resolvable wikilinks, the line caps, and index sizes.

The signal a linter gives you is the worklist of *fix-me* items: a broken `[[link]]`, a page that blew past 800 lines and needs splitting, a theme-index past 300 lines that should shard. Stage 2 adjudicates and applies the fixes; you read the result in the audit log and the human `wiki/log.md` line.

---

## A day in the life of a `cooking` curator

Putting it together, here is the full loop for one matured insight — the same loop trading runs for a strategy finding:

1. **Find what exists.** `uv run --script scripts/wiki_query.py "braising temperature collagen"` — is this already a page?
2. **It's new → graduate it.** `create-page --topic cooking --path wiki/cooking/concepts/low-and-slow-collagen-conversion.md --from-file /tmp/note.md`.
3. **Make it discoverable.** `register-index --topic cooking --slug low-and-slow-collagen-conversion --theme braising --summary "…"` — wires it into the braising index and, if braising is new, into the topic master.
4. **Reality tests it later.** `append-validation-log --page … --from-file /tmp/brisket-result.md` — the brisket came out tender at 75°C / 6h; the evidence now lives on the page.
5. **Leave a trace.** `log --message "Added collagen-conversion page; validated with brisket trial."`
6. **Let the pipeline curate.** Overnight, Tier-2 dedups it against your existing braising pages, cross-links related atomics, and lints the structure — no action from you.

That is curation as a *system*: you write the knowledge, the verbs keep each write correct, and the pipeline keeps the whole tree coherent.

---

## Recap

- **Read** two ways: programmatic retrieval (`wiki_query.py` / `unified_recall`, the canonical path) and the hand-browse master-over-masters.
- **Write** structured content **only** through the four verbs — `create-page`, `append-validation-log`, `register-index`, `log` — each routed, deduped, redacted, audited.
- The **maintenance pipeline** (Tier-1 no-LLM + Tier-2 one bundled OAuth call) lints, dedups, cross-links, and re-indexes your topic on a schedule, fail-open, **never deleting** an atomic.
- A domain that follows the reference conventions needs **no schema overrides**; divergences plug in through `config.toml` seams (`wiki_linter`, `wiki_merge_decider`, …).

To stand up a *new* domain in the first place — the topic, the gateway subclass, the config wiring, the ingestion adapter — go back to **[Build your own domain](08-build-your-own-domain.md)**.

---

This is the last of the user-facing chapters. **For developers:** [Architecture →](10-architecture.md) — the engine design, modules, and data flow under everything you've used so far.
