# Overview

> Audience: **users** — people who install ultra-memory in a Claude Code project and
> rely on it day to day. For the everyday commands see [usage.md](usage.md); for the
> module-level design see [developer/architecture.md](../developer/architecture.md);
> for exact behaviour see [reference/api.md](../reference/api.md) and
> [reference/operations.md](../reference/operations.md).

## What ultra-memory is

**ultra-memory is a Claude Code plugin that gives your agent a durable, structured
memory and a curated knowledge base — and keeps both honest with itself over time.**

Concretely it ships three things that work as one:

1. **A DB-canonical memory store.** Instead of a pile of loose markdown files and
   ad-hoc session logs, your memories live in one SQLite database
   (`~/.ultra-knowledge/memory.db`). "DB-canonical" means *the database is the
   working truth* — every read, write, and recall hits the database, and git only
   tracks a clean, redacted *text dump* of it for history and rollback. You get
   typed memories, a per-session event log, and fast retrieval without losing the
   safety of version control.

2. **A topic-partitioned LLM-Wiki of durable knowledge.** Alongside memory sits an
   *Expert-Knowledge* wiki: human-readable, git-versioned markdown pages that hold
   the knowledge meant to outlive any single session (patterns, post-mortems,
   studies). It is organised by **topic** (e.g. `trading`, `programming`, `user`),
   one top-level directory per topic.

3. **A self-learning skill loop.** A background pipeline that watches what the agent
   learns while working, consolidates the durable lessons, and — conservatively, and
   only with explicit opt-in — can correct its own past notes or even draft new
   skills from matured lessons. Every autonomous action is bounded, reversible, and
   audited (see *What we've already considered* below).

You interact with all of this through slash commands (`/memory-save`,
`/memory-recall`, `/memory-pin`, …) and an automatic SessionStart context injection;
you rarely touch the database directly.

## The mental model: two stores, one fabric

The single most useful idea to hold onto: **there are two stores with different
half-lives, but you query them as one fabric.**

| | **Session Memory** | **Expert Knowledge (the wiki)** |
|---|---|---|
| **What it holds** | How you want to work, current project state, corrections, references | Durable knowledge: patterns, studies, post-mortems, matured lessons |
| **Half-life** | Short — changes session to session | Long — meant to outlive any single approach |
| **Form** | Rows in `memory.db` (query-on-demand) | Human-readable markdown, git-tracked, hand-browsable |
| **Written via** | `/memory-save` (the `memory_lib` gateway) | The wiki write gateway (`create-page`, `append-validation-log`, …) |
| **Rule of thumb** | *How the agent should behave* → here | *What we learned about the domain* → here |

The two are **never merged into one store** — that is a deliberate design choice, not
a missing feature. Merging them would force a single expiry model, a single write
discipline, and would lose the wiki's human-readable git form. Instead they are
**unified at retrieval time** by a single warm-path engine called **`unified_recall`**:
you ask one question, and it returns one ranked list that interleaves matching
memories *and* matching wiki pages. The ranking is deterministic (Reciprocal Rank
Fusion over embedding-cosine, BM25, and a graph signal) and uses **no LLM on the read
path**, so recall stays fast and reproducible.

A typed **edge layer** (`links`) ties the two stores together: a session lesson can
point at the wiki page it eventually matured into, so the graph spans both stores
without copying one into the other.

### One global store, shared by every project

ultra-memory keeps **one global home** for the whole fabric:

```
~/.ultra-knowledge/
├── memory.db        ← the single Session-Memory store for ALL projects
└── wiki/            ← the single Expert-Knowledge wiki (topic subdirectories)
    ├── trading/
    ├── programming/
    └── user/
```

Why one global store instead of a per-project silo? Because a lot of what the agent
knows is *cross-cutting*: your preferences (the `user` topic), general coding and
infrastructure knowledge (`programming`), and so on. A per-project memory would trap
those facts in whichever project happened to learn them first. One global store means
an operational rule learned in one project travels to the next, and a pattern written
to the wiki in one project is available everywhere.

Scoping is handled by **topics**, not by separate databases:

- **Operational facts** (e.g. "use OAuth only", "commit proactively") carry
  `topic = NULL` — cross-topic, visible everywhere.
- **Project-specific knowledge** is tagged with its topic and only surfaces to callers
  bound to that topic.

This makes the plugin genuinely portable: drop it into any project, run
`/memory-setup`, and it auto-wires to the global home with **no environment variables
or config files required**. (You *can* point it elsewhere with an explicit
`ULTRA_MEMORY_DB` override — for testing, or if you truly want a per-project silo —
but you never *have* to.)

## Headline capabilities

- **Typed, audited memories.** Each memory has a type (`feedback` / `project` /
  `reference` / `user` / …), a stable id used for cross-links, and a full audit
  trail. Memories are never hard-deleted — they are soft-tombstoned or replaced with a
  redirect-stub, so history and rollback always survive.
- **Per-session episodes.** A typed, idempotent event log records what happened in a
  session, so the next session can pick up where the last left off.
- **Fast, LLM-free retrieval.** Recall ranks by embedding cosine plus a title-match
  boost and staleness / strength / access signals. No model call on the read path,
  so it stays in the low-seconds range.
- **SessionStart rehydration.** At the start of each session the plugin can inject a
  compact "gist" — your pinned hard rules, hot memories, and where you left off — so
  the agent starts with the right context already in mind.
- **One unified recall across both stores.** `unified_recall` ranks memories and wiki
  pages together, scoped by the caller's privileges (see below).
- **Cross-store pins and links.** Pin a memory *or* a wiki page so it stays hot in
  recall; record a typed edge between any two stored things so the knowledge graph
  spans both stores.
- **A four-beat self-learning loop.** Capture (hot, no LLM) → Consolidate (weekly,
  one batched LLM call) → Self-Correct (conservative, opt-in) → Synthesize (drafts new
  skills, opt-in). All four are described in [usage.md](usage.md).
- **Git-tracked rollback.** Every export writes a redacted SQL dump and regenerated
  markdown views, committed to git — so you can always roll back to a known-good state.

## What we've already considered

ultra-memory was built with a strong bias toward *not surprising you* and *not losing
data*. These are the safeguards already in place, not aspirations:

### Privacy and redaction

- **Redaction at two chokepoints, not per-site.** Secrets are stripped once at the
  *write* chokepoint and again over the **entire** export dump before it touches git.
  Redacting the whole export — every column, every row — is what guarantees nothing
  leaks even from places a single writer never touched (a token hiding in an edge's
  evidence field, a value inside an audit-trail JSON blob). This "redact at the
  boundary" discipline was hardened by an adversarial bug hunt that found and closed
  several read-path leaks.
- **Fail-closed privilege wall.** Recall is scoped by an orthogonal **(type × topic)**
  access wall. An untrusted caller (a subagent, a cron job) defaults to the
  `subagent` class and can only ever see `project` / `reference` memories — **never**
  your `user` / `feedback` memories — and only the topics it is bound to. "Fail-closed"
  means the *absence* of an explicit grant denies access: an unknown caller class, or a
  caller with no topic binding, sees the least, not the most. The privilege filter
  lives **in the query itself**, not as an afterthought, so it can never be starved or
  bypassed by truncation.

### OAuth-only, never the API

Every LLM call the system makes — consolidation, self-correction, skill synthesis —
runs through your local `claude` CLI on your **OAuth subscription**, never the
Anthropic SDK and never an API key. This is a hard architectural boundary enforced in
code: there is a single chokepoint that **refuses to run** if an `ANTHROPIC_API_KEY` is
present in the environment. The benefit to you is concrete — your LLM usage stays on
your subscription, there is no separate metered API account to manage, and there is no
API key on disk to leak.

### Reversibility and bounded autonomy

- **Archive-never-delete.** No part of the system — not even the most aggressive
  self-correction beat — issues a destructive delete. A superseded note becomes a
  redirect-stub; a contradiction is *quarantined* (demoted out of recall), not removed.
  Every "delete" is really a reversible state transition you can roll back via git.
- **Provenance is sacred.** A `created_by='human'` (or `import`, or pinned) row is
  *immutable* to the autonomous loops — the code re-reads the live row and physically
  refuses to touch it. Your hand-written rules and the German-tax-fence-style hard
  rules cannot be auto-edited away.
- **Bounded blast radius.** The autonomous beats cap how much they can change per run
  (e.g. ≤3 edits, ≤3 reversions, ≤5 quarantines, ≤1 new skill) and *halt* if a run
  would exceed the cap — they never silently truncate.
- **Pre-run git checkpoints + audit digests.** The aggressive beats refuse to run on a
  dirty tree, tag a checkpoint before acting, and write a human-readable digest of
  everything they did. You are in the **audit loop** (you review the digest), not the
  write loop (you do not have to approve each action).
- **Eval-gated skill synthesis.** When the loop drafts a new skill, a behavioural
  probe verifies the generated skill does **not** hijack an existing skill's trigger
  before it is allowed to ship — with zero tolerance for a false positive.
- **Kill switches, disabled by default.** The aggressive self-correction and skill
  synthesis beats ship behind explicit enable flags. They do nothing until you turn
  them on.

### Robustness already designed in

- **Transactional, replay-safe migrations.** Schema upgrades run inside one explicit
  transaction with idempotent `ADD COLUMN … IF NOT EXISTS`, so a crash mid-migration
  rolls back cleanly and a replay never wedges the database.
- **Concurrency-safe writes.** Writes take an upfront lock with bounded
  exponential-backoff retry; a write that loses a race is spooled and replayed, never
  silently dropped — and you get a loud, named error rather than a quiet failure.
- **Crash-resilient knowledge MCP.** The embedding model loads *lazily* on first query
  from a persistent cache, so the knowledge service answers startup in a fraction of a
  second even if the OS purged its temp cache — it degrades a single query on a miss
  instead of crashing the connection.
- **Deterministic ranking.** Recall results are stable across runs: ties in the
  ranking break on a fixed secondary key, so the same query returns the same order
  every time.
- **Never silently lose data.** Import collisions fail loudly; malformed log headers
  are surfaced as warnings rather than folded into the previous block; the import
  count cannot over-report. This is enforced by a strong test suite (the engine is
  developed test-first; see [developer/contributing.md](../developer/contributing.md)).

The throughline: **the system is autonomous in *whether* it acts, but conservative in
*how*.** Full automation lives behind code-level guarantees — provenance gates, bounded
blast radius, archive-never-delete, eval-gates — so a mistake is rare *and* cheap to
undo.
