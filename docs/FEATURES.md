# ultra-memory — Feature Catalog

This page is a comprehensive, human-readable catalog of everything ultra-memory does — grouped by theme, most foundational first. For the *why* behind each design choice, follow the handbook links; this page is the *what*.

---

## Two-store memory fabric — the foundation

Everything else in ultra-memory rests on one decision: keep two kinds of memory at once, because not everything an agent should remember ages at the same speed.

### Session Memory — the fast, volatile store

Session memory holds *how you work*: your preferences, your project's current state, the corrections you've given, and the references you've pointed at. It is the kind of knowledge that turns over quickly — a preference might change next week, "current state" changes constantly. It lives in a local SQLite database (`~/.ultra-memory/memory.db` by default, one store shared across every project) as typed, queryable rows that can be pinned, verified, superseded, and ranked. Each memory carries a stable `id`, a `type` (`user` · `feedback` · `project` · `reference`), a short title, and a body. The `type` is the most consequential field because it decides who is allowed to read the fact later — the private `user`/`feedback` tier never leaves your trusted sessions. (Handbook ch. 4; module `ultra_memory/memory_lib.py`.)

### The Expert-Knowledge LLM-Wiki — the slow, durable store

The wiki holds *what you've learned*: concepts, studies, findings, post-mortems — knowledge meant to outlive any single session or experiment. It follows Andrej Karpathy's "LLM Wiki" pattern and is stored as plain Markdown you can read with your own eyes, edit by hand, diff in a pull request, and keep in git forever. It is organized as a uniform **master-over-masters** tree: small *atomic* pages (one idea each, soft-capped at 400 lines and hard-capped at 800), gathered under *theme-indexes*, gathered under a per-topic *master index*, all under one top-level master that links every topic. Every page carries YAML frontmatter and every cross-reference is a `[[wikilink]]`. The two stores are *never* merged into one bucket — that would force a single expiry rule and a single way of writing onto two genuinely different things, and would throw away the wiki's readable, git-tracked form. (Handbook ch. 2, 9; modules `ultra_memory/wiki_sync.py`, `ultra_memory/wiki_gateway.py`.)

### Topics — walled-off subject areas

A topic is one top-level directory under the wiki root (`trading`, `programming`, `user`, …), and topics never bleed into each other — a query scoped to one topic never surfaces another's pages. A topic is deliberately cheap: creating one is a directory, an `index.md`, and a registry row, all generated with no LLM call. This is what lets the same single store serve many unrelated subject areas at once without cross-contamination, and it is the axis the privilege boundary uses to keep a subagent inside the knowledge it is bound to. (Handbook ch. 8.)

### The typed-link graph that ties the stores together

A small links table records typed relationships — memory↔memory and memory↔wiki-page — without copying one store into the other. The most important type is the **graduation** link (`validated_as`), which points from a session lesson to the durable memory it graduated into, so a graduated fact can always be traced back to the session where it was first learned. These edges do real ranking work (a page several proven lessons point to ranks more confidently) and are filtered by the same privilege wall as the rows they connect, so an edge to a private memory never leaks that endpoint to an untrusted caller. (Handbook ch. 2, 10.7; schema `links` table.)

---

## Unified Retrieval — one ranked search across both stores

Two stores would be a burden if you had to remember which one held what. You don't, because at retrieval time they behave as one fabric.

### `unified_recall` — the deterministic cross-store search

A single search runs your question against *both* stores and returns one ranked list that interleaves matching session memories and matching wiki pages by how well each matches. Under the hood it fuses several ranking signals — memory embedding-cosine, knowledge BM25, knowledge embedding-cosine, and the `## Signal` channel — with rank-based reciprocal-rank fusion (best-rank-per-backend RRF) and a fixed tie-break, then multiplies in each fact's outcome weight. Two properties make it trustworthy: it is **deterministic** (the same question gives the same order every run) and it uses **no LLM on the read path** (no model call sits between your question and your answer, so recall stays fast and reproducible). The intelligence lives in how things were organized on the way *in*, not in a slow inference on the way *out*. (Handbook ch. 2, 10.1; module `ultra_memory/unified_query.py`.)

### The privilege boundary — who is asking shapes what comes back

Recall is scoped by a fail-closed access wall that composes two orthogonal axes by AND: a *type* axis and a *topic* axis. A trusted caller (`orchestrator`/`owner`) sees all types; everything else — a subagent, a cron run, an unknown or unset caller — is treated as the untrusted `subagent` and sees only the shareable `project`/`reference` facts, never your private `user`/`feedback` tier, and only the topiced knowledge it is explicitly bound to. The boundary lives *inside* the search and fails closed: an unbound or unknown caller sees the *least*, not the most. It is enforced as a tool constraint, not a prompt instruction — the structural answer to the fact that a prose "do not print secrets" plea is ignored when secrets are central to a finding. Every recall also writes an access-log audit row. (Handbook ch. 7, 10.7; module `ultra_memory/knowledge_mcp.py`.)

### The read-only `knowledge` MCP tool

Subagents read memory through a dedicated read-only MCP tool (`knowledge_query`) rather than the trusted CLI, which is precisely where the privilege boundary is enforced. The tool never raises (it returns a structured error), runs every caller-facing title and snippet back through secret-stripping as defense-in-depth, and returns a fixed generic string on any exception so an internal filesystem path can never cross the boundary in an error message. This is how an agent you dispatch to summarize a file is structurally prevented from surfacing a private preference or a secret. (Handbook ch. 4, 7; module `ultra_memory/knowledge_mcp.py`.)

### Programmatic and hand-browse reading paths

Two complementary read paths exist. The canonical one is programmatic retrieval: ask a question, get a ranked JSON answer with per-hit slug/path/snippet plus one-hop graph context (surfaced in a consumer via a thin wrapper like `wiki_query.py`, the engine call being `unified_recall`). The second is hand-browse: when you want to understand the lay of the land rather than answer one question, open the master index, pick a topic master, pick a theme-index, open the one atomic you need — the indexes are engineered to be cheap to read, which is why entries are one line each. (Handbook ch. 9.)

---

## Recall-Reflex — the store recalls itself

A store you have to *remember to consult* is a store you will forget to consult. The most expensive failure isn't a missing fact — it's a fact that's there, findable, and never looked at, so the same problem gets solved twice. (That happened: the same fastembed cache bug was fixed twice, days apart, because the lesson lived in a code comment and wasn't findable by the error text.) The Recall-Reflex closes that gap from both ends.

### `recall()` — the one-line reflex primitive

`recall(signal_text)` is a thin, fail-open, privacy-scoped wrapper over the same `unified_recall` fusion used everywhere else — there is exactly one retrieval engine, and `recall()` only adds the policy a reflex needs. It defaults to the **subagent** privilege scope (so an automatic recall can never surface your private tier), over-fetches and then drops navigational index/redirect pages so prior art isn't crowded out, and is fail-open everywhere (a missing DB or a model-load failure returns an empty list plus one stderr line, never a raise). It is exposed in Python and as a CLI (`python -m ultra_memory.recall "<signal>" [--top --topic --caller-class --no-embed --json]`), the single shared entry point every consumer reflexes through. (Handbook ch. 4, 10.4a; module `ultra_memory/recall.py`.)

### The `UserPromptSubmit` recall hook — prior art before you read code

A `UserPromptSubmit` hook watches each prompt and, *only* when it spots a concrete error signature (a stacktrace, an exception name, an `Error:`, a `path/file.ext:123`, an OS error, a `panic`), quietly runs a recall and injects the top hits as a short "Recall-Reflex — prior art" block before Claude reads any code. It is deliberately conservative: precision over recall, never firing on a plain question, returning at most three hits, querying **knowledge-only** (the memory backend is dropped entirely, so no private `user`/`feedback` row can surface on a main-session prompt), and running BM25-only so no embedding model loads on every prompt. Like every hook it is fail-open and ships on by default; silence it with `RECALL_HOOK_DISABLE=1`. (Handbook ch. 4, 10.4; module `ultra_memory/hooks/recall_prompt.py`.)

### The `## Signal` channel — making knowledge findable by its symptom

Any wiki atomic may carry an optional `## Signal` H2 section: the observable condition under which the page should be recalled, *in the words it actually appears in* (an error like `onnxruntime NoSuchFile … model_optimized.onnx`, or a market state like `VIX spike + breadth collapse`). This closes a real gap — a page titled by its *insight* ("persistent model cache") is invisible to a search for its *symptom*. The gateway embeds the `## Signal` text as a *distinct* retrieval channel (`knowledge_signal`), and `unified_recall` fuses it as a separate ranked backend over exactly the slugs the scoped knowledge backends already admitted (so it inherits the privilege wall and never widens scope). A page whose recorded observable matches the query earns extra rank credit structurally, with no tuned weight. The same channel is the second axis the write gateway dedups on. Backfill is forward-only: new observable-bearing atomics get one at authoring time; existing pages gain one on their next edit. (Handbook ch. 4, 10.4a; `ultra_memory/wiki_sync.extract_signal_text`.)

### The `recall-reflex` skill — recognise → recall → act

A bundled skill teaches the discipline: at the start of a debug/build task, or when an abnormal condition shows up, it formulates a recall query from the *observable*, reads the injected prior art, and runs a deeper recall if needed. It holds one boundary firmly: a recall **hit is advisory context, never a gate**, and a recall **miss is never evidence of safety** — on a real-money or otherwise gated path, recall composes *before* the risk check and never replaces it. (Handbook ch. 4; `skills/recall-reflex/`.)

---

## The Self-Learning Organism — a loop that curates itself

This is the pillar that makes ultra-memory feel like an organism rather than a filing cabinet. A background loop runs in beats, advancing automatically whenever you open Claude Code via a throttled async session-start hook (each beat on its own clock, so opening ten sessions in a day doesn't re-run a weekly job). It is **on by default** and **safe by construction** — the guardrails (below) are enforced in code, not asked for in a prompt. The beats run in a fixed order, gentlest-blast-radius first. (Handbook ch. 5, 10.5; package `ultra_memory/maintenance/`.)

### Session capture — mining transcripts into durable candidates

Once per session (throttled to ~daily), one OAuth `claude` call mines the finished session's redacted transcript digest into durable memory candidates, `feedback` corrections, skill-tagged learnings, and `atomic_candidate` markers. The digest deliberately *excludes* raw tool-output bodies — the large, secret-bearing surface — keeping only user/assistant prose and tool *names*, so a tool that returned a credential never has that body fed to the call. It is the input the other beats feed on, which is why it runs first. (Handbook ch. 5; `maintenance/session_ingest.py`; toggle `SESSION_INGEST_ENABLE`.)

### Atomic graduation — auto-capturing lessons as findable wiki pages

The capture-findably backstop, and the boldest write the loop makes. It drains each `atomic_candidate` (a durable engineering gotcha or trading/strategy lesson, each carrying its literal observable) into a `## Signal`-keyed wiki page through the same audited gateway every other write uses. Crucially it needs **no AI call of its own** — capture's single call already did the reasoning, so the apply is deterministic. It rides on capture's coat-tails (same 24h clock, ordered right after it) and is fenced the hardest: a three-way `## Signal` dedup-gate (a clear match *merges* into the existing page, a grey-zone match is *skipped* for a future run, only a genuinely novel signal *creates* a page), and an eval-gate that re-recalls each new page by its own observable and *quarantines* (never deletes) any page that can't find itself. It is create-only, capped per run (default three, retunable via `ATOMIC_GRADUATE_CAP`), and disable-only (`ATOMIC_GRADUATE_DISABLE`). (Handbook ch. 2, 5, 10.5; `maintenance/atomic_graduate.py`.)

### Consolidate — promoting proven lessons, merging duplicates

Weekly, one batched OAuth call reads the unresolved learning candidates, dedups them via `unified_recall` (no LLM pre-filter), and for each one either *graduates* it into a durable memory or wiki page, *merges* it into an existing page via the validation-log verb, or *skips* it as transient. It is ADD-only — it never rewrites and refuses any human-authored or pinned target — and a graduation into a memory records a `validated_as` edge so the lesson stays connected to its new durable home. This is the beat that makes the store *better organized*, not just bigger. (Handbook ch. 5; `maintenance/consolidate.py`.)

### Outcome attribution — crediting the facts that actually helped

A no-LLM step woven through the loop that joins the memories a session recalled to that session's outcome signal via `informed_by` edges, then folds them into an `outcome_weight` that multiplies into every future ranking — so as outcome signals arrive, the facts that actually helped rise and the dead ones fade, all from deterministic bookkeeping with no model call. The edge-and-weight machinery ships built and armed; it feeds on the session outcomes a consumer marks, and the weight sits at a neutral 1.0 until those signals flow. (Handbook ch. 5; module `ultra_memory/attribution.py`; toggle `SP8_ATTRIBUTION_ENABLE`.)

### Self-correct — the loop fixing its *own* earlier notes

Monthly, the loop revisits its own earlier agent-authored notes, folds the attribution evidence into an EWMA, and can *auto-edit*, *flag a self-reversion*, or *quarantine* a lesson whose evidence has gone net-negative. It can never touch a fact you authored or pinned — those are physically immutable to it. The riskiest verb, reverting a past graduation that later regressed, is **propose-only**: the loop flags it in the digest for you to confirm, never applying it autonomously. This is the highest-blast-radius beat and lives behind the full safety wall (below). (Handbook ch. 5, 10.5; `maintenance/aggressive_run.py`; toggle `SP7_AGGRESSIVE_DISABLE`.)

### Synthesize — inventing a new reusable skill from clustered lessons

The most cautious beat (monthly). When a cluster of three or more graduated, positively-scored lessons keeps recurring under the same index hook, the loop drafts a brand-new native skill (`.claude/skills/gen-<slug>/SKILL.md`) from them. It will *not* create a skill that would hijack one you already have: a load-bearing **trigger-probe eval-gate** proves the generated skill doesn't steal an existing skill's auto-trigger (a description-cosine pre-filter plus a behavioral probe, zero-tolerance) before the skill is allowed to exist. At most one new skill per run. Generated `gen-*` skills are first-class self-learning skills themselves. (Handbook ch. 5, 10.5; `maintenance/skill_synthesize.py`; toggle `SP10_SYNTHESIS_DISABLE`.)

### The safety wall — autonomy in *whether*, conservatism in *how*

The loop is safe to leave unattended because five (for the two boldest beats, seven) properties are enforced in the apply path, not the prompt. **Archive-never-delete:** no beat ever runs `rm` — a superseded page becomes a redirect, a retired skill moves to an archive directory, every change is a reversible step. **It can never touch what's yours:** the apply path re-reads the live record and halts the whole run on a single attempt to touch a human-authored or pinned target. **git is the undo button:** the two boldest beats take a tagged git checkpoint before acting and refuse to run on a dirty tree. **Bounded per run:** at most a few edits, a few reversions, a handful of quarantines, and at most one new skill — a run that would exceed a cap halts rather than blasting through. **A written digest:** every bold run writes you a short human-readable summary naming what it changed, what it deliberately didn't, and the exact one-command rollback handle, so you sit in the *audit* loop, never the *write* loop. And the whole loop is **fail-open**: any error in any beat becomes one log line and a no-op. (Handbook ch. 5, 7, 10.5.)

### The session-lifecycle driver

The heavy beats no longer need an OS scheduler. A project-agnostic driver (`python -m ultra_memory.maintenance`) runs all due-and-enabled beats in fixed order, each gated by config (defaulting *on*), throttled by its own meta clock, and fail-open. It is wired into the async `beats` arm of the SessionStart hook so it advances every session on any platform without re-running a weekly beat — but it can also be invoked directly (`--beat <name>`, `--force`), and `/ultra-memory:memory-setup` will print an OS-scheduler snippet for headless boxes where sessions rarely open. (Handbook ch. 5, 10.5; `maintenance/run.py`.)

---

## Curation & Maintenance — keeping the wiki healthy

Beyond the self-learning loop, a deterministic maintenance pipeline keeps the *whole tree* coherent over time, on a schedule rather than on your attention. Both tiers are fail-open.

### The wiki-maintenance pipeline (detect → adjudicate)

The Tier-2 curation beat runs deterministic detectors over the active wiki roots to build a worklist — `detect_scope` (new atomics), `detect_dedup` (embedding-cosine near-duplicates), `detect_lint` (broken links, missing frontmatter, oversize pages), `detect_graph` (orphans and clusters), `detect_stale` (superseded pages) — then hands the whole worklist to **a small number of batched OAuth calls** (the worklist is chunked, a handful of items per call) that decide each item (merge this near-dup, recategorize that page, add this cross-link). The decisions are applied *through the gateway verbs*, so they too are routed, redacted, and audited. A few batched calls per run, never one per page. Two safety rails: it **never deletes** an atomic (a duplicate becomes a redirect stub with sources concatenated — a hard rule learned the hard way), and dedup uses a calibrated grey-zone band (cosine 0.78–0.86) where an optional consumer-supplied judge decides "same idea?" rather than guessing. (Handbook ch. 9; package `ultra_memory/wiki_maintenance/`.)

### Tier-1 light maintenance (no LLM)

A daily, model-free housekeeping slice behind a single ~20h throttle: drain the write spool, prune old session events (rolling them into a per-session summary first so nothing is lost), refresh the readable git-trackable export, run the wiki→index mirror sync, and rebuild the per-skill `Learnings.md` projections. It costs no token at all and runs about once a day from the session-start hook; `/ultra-memory:memory-maintain` triggers it on demand. (Handbook ch. 4, 10.3; modules `ultra_memory/maintain.py`, `ultra_memory/retention.py`.)

### The session hooks — rehydration and checkpoint

Two hooks bracket every interactive session. On **SessionStart**, the rehydration hook composes a budgeted gist from the DB — every pinned rule, "where we left off", open follow-ups, and the hottest relevant memories — and injects it directly into Claude's context with *no LLM and no embedder* (so it costs almost nothing and stays under a character budget, default 2000). It is structure-injection-sanitized so no stored value can forge a header inside the trusted context, and pinned rules are exempt from the budget tail-cut. On **Stop**, the checkpoint hook derives completed tasks from the raw transcript on disk (not the compacted in-context view, so mid-session compaction can't truncate it), records them as idempotent events, and enqueues the session for capture. Both hooks are fail-open and role-scoped (a no-op for cron/subagent runs). (Handbook ch. 4, 10.4; package `ultra_memory/hooks/`.)

### The memory verbs — saving, recalling, and keeping the store honest

A complete set of slash commands manages the session store without ever hand-editing the database. `/ultra-memory:memory-save` creates a durable fact (the gateway picks nothing for you — Claude chooses id/type/title, and the body is redacted and audited). `/ultra-memory:memory-recall` is the trusted full-recall read path. `/ultra-memory:memory-pin` keeps a hard rule in the rehydration gist of *every* session (and makes it immutable to the self-learning loop). `/ultra-memory:memory-verify` reconfirms a fact flagged stale, resetting its age penalty. `/ultra-memory:memory-edit` corrects a wrong body while preserving every other field. `/ultra-memory:memory-inbox` applies pin/unpin/verify directives you jotted into a watched file *between* sessions (free text it doesn't recognize is preserved, never auto-applied). And `/ultra-memory:memory-maintain` runs the no-AI cleanup now. (Handbook ch. 4; `commands/`.)

### The four wiki gateway verbs

Structured wiki content is written *only* through four audited verbs, each of which routes, deduplicates, redacts secrets, and appends an audit row. `create-page` graduates a matured idea into a new atomic (refusing to clobber, only under `concepts/` or `synthesis/`). `append-validation-log` records empirical evidence on an existing page (idempotent — a re-run returns `already-logged`). `register-index` files an atomic under its theme-index and, when the theme is new, wires that theme-index into the topic master and the topic into the master-over-masters in one call, so the browse tree never goes stale. `log` leaves a one-line human trace in `wiki/log.md`. The one documented exception is a free-form prose amendment to an *existing* page, where lint and git are the control. (Handbook ch. 9; `ultra_memory/wiki_gateway.py`.)

### The readable, recoverable export

A human-readable, git-trackable export of your store is kept under `memory_export/` and refreshed by light maintenance: a redacted SQL dump (the canonical rollback source, carrying the schema version), a binary VACUUM snapshot (gitignored fast-path), and regenerated Markdown views. That export, plus the per-run git checkpoints the bold beats take, is your audit trail and your undo button — you can read what the engine knows in plain text and roll back any change. (Handbook ch. 7, 10.1; module `ultra_memory/memory_export.py`.)

---

## Privacy & Cost Control — boring on purpose

A memory that watches your sessions and rewrites itself should make you suspicious, so the privacy posture is enforced in code rather than promised in prose.

### OAuth-only — it refuses to start with a paid API key

Every LLM call goes through exactly one chokepoint that shells out to your local `claude` command on your own Claude subscription. There is deliberately no metered API path: the chokepoint *raises and refuses to run* if `ANTHROPIC_API_KEY` is set in the environment, refuses if no OAuth token is available rather than silently falling back, and drops an empty key from the child environment so it can't be reintroduced downstream. Never the Anthropic SDK, `api.anthropic.com`, `messages.create`, or `cache_control`. The practical consequence: the self-learning loop costs you nothing beyond the subscription you already pay for — no second bill to watch, no key on disk to leak. (Handbook ch. 7, 10.2; module `ultra_memory/claude_cli.py`.)

### Local-first and content-free

Nothing leaves your machine. The store is a local SQLite file; the wiki is local Markdown; transcripts are read once from disk and never persisted (only the extracted, redacted knowledge is saved). The plugin's *own repository* ships code only — no content — and a test enforces that the entire published surface contains no personal paths or data. Your database, notes, paths, and any secrets live in *your* project and are passed in by configuration; nothing personal is ever committed to the plugin itself. The only outbound traffic is the LLM call to Claude over your existing authenticated session — no telemetry, no analytics, no upload. (Handbook ch. 1, 7, 10.8.)

### Two-pass secret stripping

Every persisted text field is redacted at the write chokepoint, and the *entire* export dump is redacted again over the whole snapshot before it touches git — so nothing leaks even from a corner a single writer never touched. The stripper catches recognizable credential shapes (API keys for many providers, GitHub/GitLab/AWS/Google/Slack/Stripe/etc. tokens, JWTs and `Bearer` tokens, PEM private-key blocks, URI userinfo, credential-shaped `key=value` pairs, and anything wrapped in `<private>…</private>`) while leaving ordinary prose intact. It is a strong safety net, not a license to paste credentials — the stronger guarantee is upstream, where the capture digest never includes tool-output bodies in the first place. (Handbook ch. 7, 10.2; module `ultra_memory/redact_secrets.py`.)

### Subagent scoping (the privilege boundary, restated for privacy)

A subagent, a cron, or any unknown caller is limited to `project`/`reference` facts and the topics it is bound to — never your `user`/`feedback` tier, never another project's knowledge. It is fail-closed (anything not explicitly trusted is treated as the untrusted subagent), enforced in SQL and re-checked as defense-in-depth, extended to the *links* hanging off an allowed memory, and audited on every read. A fresh install is locked down until you deliberately mark a trusted top-level session as `orchestrator`. (Handbook ch. 7, 10.7.)

### Per-beat kill switches and dry-run

You are never locked in. Every step has an individual off switch — from the `/plugin` config UI or a matching environment variable — and they are *kill-switches, not enable-flags* (everything ships on; a toggle only ever disengages a beat). Session capture, outcome attribution, self-correction, skill synthesis, atomic graduation, the recall hook, and any single beat can each be disabled independently; a memory-only install simply leaves the wiki root unset and the wiki steps no-op. The two bold beats also have a dry-run presence switch (`SP7_AGGRESSIVE_DRYRUN`, `SP10_SYNTHESIS_DRYRUN`) so they plan, run the eval-gate, and write the digest while applying nothing — a way to watch the loop's judgment before handing it the pen. By default every bold beat ships **armed**, acting autonomously behind the full safety wall. (Handbook ch. 5, 7.)

---

## Extensibility — build your own knowledge domain

The engine ships content-free: it knows *how* to route, dedup, frontmatter, anchor, and audit a page, but knows *nothing* about what your pages are about. The subject matter is supplied by you in two small, well-defined places.

### Turnkey base gateway — zero code if the defaults fit

The base `WikiGateway` is turnkey. Out of the box it gives every domain correct no-LLM defaults (a page lands at `<topic>/concepts/<slug>.md`, joins a theme from the claim or `"general"`, gets minimal frontmatter, dedup off, no anchor, a `"Standard"` confidence label). If those fit, you write zero Python — point the engine at a wiki root and start writing through the verbs. You only reach for a subclass when your domain has an opinion the defaults don't encode. (Handbook ch. 8.)

### The six-hook subclass + scaffold command

When you do customize, you override only six small methods — `route`, `theme_for`, `render_frontmatter`, `dedup_check`, `derive_anchor`, `confidence_label` — each taking a claim dict and returning a small value. A scaffold command (`python -m ultra_memory.wiki_gateway scaffold …`, or the `/ultra-memory:wiki-gateway-scaffold` slash command) deterministically writes a ready-to-edit subclass with all six hooks present and documented inline plus the config snippet; you keep the ones that differ and delete the rest. Everything below those six hooks — the verb materializers, the embedding dedup machinery, the write-lock, redaction, the audit row — is inherited and load-bearing, never to be re-implemented (extend a verb by overriding and calling `super()`). The worked reference is the trading project's own `TradingWikiGateway`, a thin subclass overriding five hooks and using the extend-pattern for `create_page`. (Handbook ch. 8; `ultra_memory/wiki_gateway.py`; skill `using-wiki-gateway`.)

### Config-toml seams

A subclass is wired in with one line (`wiki_gateway = "module:Class"`) in the *project's* `.ultra-memory/config.toml`, never in the plugin. The same `[maintenance]` table is where a domain plugs all its project-specifics into the otherwise project-agnostic engine — known topics, the audit/digest output directory, an optional consumer linter, a grey-zone merge decider, a graph extractor, a failure notifier, the OAuth model — each optional with a sensible default. The engine degrades gracefully: with no wiki configured the wiki steps simply do nothing, so a memory-only install just works. (Handbook ch. 8.)

### Ingestion adapters — repeatable source pipelines

For a domain with a *source* (a folder of PDFs, a YouTube channel, a notes export), the ingestion-adapter contract is deliberately tiny: one source = one adapter = three methods (`fetch`, `extract`, `to_proposals`). A shared driver, `run_adapter`, owns stage ordering — it materializes the whole batch before deduping it, then hands proposals to the same routing/dedup/redaction/write/audit path the gateway already defines — so a new adapter cannot get the ordering wrong. The trading project's first adapter is YouTube; a new source reuses `run_adapter` verbatim. One invariant carries through: nothing in the adapter layer calls an LLM directly — any LLM step routes through the OAuth chokepoint. (Handbook ch. 8.)

### The project-agnostic boundary

The engine imports nothing from any consumer — enforced by a test across both the package and the published Markdown surface. The cross-store fabric is *fed, not coupled*: `wiki_sync` takes consumer-supplied root paths and derives topics generically; cross-store link mirroring takes consumer-read edges; topic genesis and routing are injectable callables; the wiki roots come from an environment seam (unset ⇒ a pure-memory deployment is byte-identically unaffected). This is why `unified_query` re-implements BM25 + cosine + RRF engine-side rather than importing a consumer's query module — the agnostic boundary forbids the import, and parity is checked by a consumer-side test that *can* import both. (Handbook ch. 10.8.)

---

## Engine internals — the discipline that holds it all together

These are the structural invariants that make the guarantees above hold across a thousand sessions, a hundred crashes, two concurrent crons, and one autonomous self-edit.

### Single-writer discipline (never lose a write)

Every mutation funnels through one writer that opens its own short-lived `BEGIN IMMEDIATE … COMMIT`, retries on `SQLITE_BUSY` with exponential backoff, and — on retry exhaustion — *spools* the operation to disk and raises loudly rather than silently dropping it. The spool self-heals on the next maintenance pass. No feature code runs a raw `INSERT`/`UPDATE`, `record_access` uses an atomic increment so there are no lost updates, and no LLM call ever happens inside a write transaction. Every write also appends an `audit_log` row. (Handbook ch. 10.2, 10.6; module `ultra_memory/memory_lib.py`.)

### Transactional, forward-only migrations

The schema evolves through ordered SQL migrations, each applied with its `user_version` bump inside one transaction (SQLite DDL and the pragma are both transactional), so a crash partway rolls back fully — version and schema never desync. The version is mirrored into a `meta` row that survives the export dump, so a restored snapshot round-trips its version. (Handbook ch. 10.6; `ultra_memory/migrations/`.)

### Embedding cache and lazy local model

Retrieval uses a small local embedding model (about the size of bge-small) loaded lazily and cached under `$HOME` — never the OS temp dir, the precise fix for the cache-purge bug that motivated the whole Recall-Reflex. Embeddings are cached (single and batch) so repeated text isn't re-embedded, and the read path falls back to BM25-only when no model is available. No embedding work touches a network or a paid API. (Handbook ch. 10.3; modules `ultra_memory/retrieval_core.py`, `ultra_memory/wiki_embed_cache.py`.)

### One-command setup and zero-config install

ultra-memory is a drop-in Claude Code plugin: add the marketplace, install, run `/ultra-memory:memory-setup` (which builds the runtime venv, prepares the database, optionally imports a legacy memory dir once, stamps the DB ready, and sanity-checks), restart. It is idempotent and safe to re-run. By default the store lives at `~/.ultra-memory/memory.db`, shared across all projects, with no editing of `.mcp.json` or `settings.json` by hand — a few optional `ULTRA_MEMORY_*` settings exist but nothing is required. The whole loop adds a few milliseconds at session start and is otherwise invisible. (Handbook ch. 3; README; `ultra_memory/setup.py`.)
