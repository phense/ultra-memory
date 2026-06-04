# Usage

> Audience: **users** driving ultra-memory from a Claude Code project. This page is
> the practical guide — installing, the everyday verbs, how recall works, the wiki
> surface, and how the autonomous loop behaves day to day. For the mental model and
> the safety posture, read [overview.md](overview.md) first. For exact function
> signatures see [reference/api.md](../reference/api.md); for operational detail (DB
> paths, export format, rollback) see [reference/operations.md](../reference/operations.md).

## Install and set up

ultra-memory is a Claude Code plugin. Two steps:

1. **Install the plugin** (zero-config). Once installed it defaults to the global home
   `~/.ultra-knowledge/memory.db` and `~/.ultra-knowledge/wiki/` — no env vars, no
   config file needed.

2. **Run `/memory-setup`.** This one-time bootstrap:
   - builds the runtime virtual environment,
   - **optionally** imports an existing legacy memory directory (a one-time legacy
     import),
   - stamps the database as ready (the `import_complete` gate, below),
   - optionally prints a hint to run a cold-start backfill if you configured one,
   - and runs a sanity check.

   It is **idempotent** — safe to re-run any time.

3. **Restart Claude Code** so the read-only `knowledge` MCP registers.

Prerequisites (both required, and preflighted by `/memory-setup`): `uv` and `git`.

> **The `import_complete` gate.** Until `/memory-setup` stamps the database ready, the
> SessionStart and Stop hooks **no-op** (fail-open to the legacy path). This is a
> guardrail: the projection-style `Learnings.md` files and the rehydration gist cannot
> regenerate empty over un-imported content. You will not get rehydration or capture
> until setup has run.

For a deeper install walkthrough — DB-path resolution, `userConfig` keys
(`data_db_path`, `caller_class`, `rehydrate_budget`, `oauth_token`), the `.mcp.json`
and `hooks.json` wiring — see [reference/operations.md](../reference/operations.md).

## The everyday verbs

These are the slash commands you will use most. All of them write through the single
audited gateway: redacted, transactional, and logged to the audit trail. You never
write raw SQL.

| Command | What it does |
|---|---|
| `/memory-save` | Persist a **new** durable fact — a preference, a feedback directive, project state, or a reference. The canonical new-fact verb. |
| `/memory-recall "<query>"` | Recall durable memories matching a query — past decisions, accumulated project knowledge, preferences. Trusted full recall (all types). |
| `/memory-pin <id>` | Make a fact **always in context** — it gets injected into every SessionStart gist. Pin your hard rules. Append `unpin` to clear. |
| `/memory-verify <id>` | Mark a fact as reconfirmed-true today — resets its staleness signal. |
| `/memory-edit <id>` | Correct a fact's **body** in place. Type, title, and other fields are preserved. |
| `/memory-inbox` | Apply queued human-correction directives (pin/unpin/verify) you typed into the watched inbox file between sessions. |
| `/memory-setup` | The one-time bootstrap above. |
| `/memory-maintain` | Force a prune + export now (the same maintenance the SessionStart hook runs throttled). Pure Python, no LLM. |

### Saving a memory

Use `/memory-save` whenever something should be remembered durably — "the operator prefers
German for conversation", "the order-execution engine is Rust", "this strategy is
paper-only". Saving:

- routes the fact to the right **type** and **topic** (operational facts stay
  `topic = NULL`, i.e. visible everywhere; project facts get tagged),
- runs the text through the **secret redactor** before it is persisted,
- writes an **audit-log row**, and
- is an **upsert** keyed on a stable id, so re-saving an existing fact updates it
  *without* clobbering a `deleted`/`redirect` tombstone — and **without downgrading a
  human-authored row**: a re-import or agent re-save over a `created_by='human'` row
  keeps it `human`. Your manual edits are safe.

### Recalling

`/memory-recall "<query>"` is the trusted, full-access read path — it can see every
type, including your `user`/`feedback` memories. (Subagents and cron jobs get a
*type-scoped* read instead; see *How recall works* below.) Use it before answering
anything that depends on remembered context.

### Pinning hard rules

`/memory-pin <id>` is how you make a rule **un-missable**. A pinned memory is injected
into every SessionStart rehydration gist, so the agent always starts with it in
context — exactly what you want for hard rules (a tax fence, an OAuth-only rule, a
paper-only constraint). Pinning also makes a memory immutable to the autonomous
self-correction loop. Wiki pages can be pinned too (see the wiki surface below); pinned
memories and pinned wiki pages share one section of the gist.

### Correcting and verifying

- `/memory-edit <id>` rewrites a fact's body when it is wrong or outdated — and stamps
  it `created_by='human'`, which makes it immutable to the autonomous loops from then
  on.
- `/memory-verify <id>` says "I checked, this still holds" — it resets the staleness
  signal so the fact ranks normally again.
- `/memory-inbox` is the between-sessions path: type pin/unpin/verify directives into
  the watched inbox file whenever you think of them, then run `/memory-inbox` to apply
  them in one go. Free text it cannot parse is preserved under an "Unprocessed"
  section rather than dropped.

## How recall works

### Ranking (no LLM)

Recall is deterministic and **LLM-free**. A query is embedded, and candidates are
ranked by:

- **embedding cosine similarity**, plus
- a fixed **title-match boost** when the title appears as a whole token in the query,
  then
- **× strength**, **+ a bounded access boost** (things you use often), **− a staleness
  penalty** (things not verified in a while).

Deleted and redirect memories are excluded by default. Because there is no model on
the read path, recall stays in the low-seconds range and returns the **same order
every time** (ties break on a fixed secondary key).

### The privilege wall

Recall is scoped by an orthogonal **(type × topic)** access wall, and it is
**fail-closed**:

- **Trusted callers** (you, via the `/memory-recall` CLI) get full recall — all types.
- **Untrusted callers** (a subagent, a cron job — the default `subagent` class) get
  only `project` / `reference` memories, **never** `user` / `feedback`, and only the
  topics they are bound to. A caller with no topic binding sees only `topic IS NULL`
  operational memories.

The filter lives inside the SQL query, so a top-k request always returns up to k
*allowed* rows — the wall can never silently starve your results.

### SessionStart rehydration

When a session starts, the plugin can inject a compact **gist** so the agent begins
with the right context. The gist unions:

- your **pinned** memories and pinned wiki pages (one section),
- **hot** memories (frequently/recently used), and
- where you **left off**.

The gist is **character-budgeted** (default ~2000 chars; tail-cut if exceeded) and each
field is length-capped, so it stays compact and cannot be used to inject structure.
By default the plugin ships in **shadow mode** — it *logs* the gist to a file rather
than injecting it live — which lets you see exactly what would be injected before you
switch it on. Flip it to live injection when you are comfortable with the content.

## The wiki: read and write

The Expert-Knowledge wiki holds durable domain knowledge as human-readable, git-tracked
markdown pages, organised by topic under `~/.ultra-knowledge/wiki/<topic>/`.

### Reading

Two complementary paths:

- **Programmatic, ranked retrieval** is the canonical path — the same `unified_recall`
  surface that powers cross-store recall ranks wiki pages alongside memories in one
  list (BM25 + embedding + a graph signal, fused deterministically). A consumer
  typically wraps this behind a query command; ask one question, get one ranked list.
- **Hand-browsing** follows a master-over-masters hierarchy: a top-level index links
  one entry per topic → each topic's master index links its theme-indexes → a
  theme-index links the atomic pages. Cite the pages you used with `[[wikilinks]]`.

### Writing — through the gateway only

**Never hand-create wiki pages.** Every structured wiki write goes through the audited
write gateway, which routes the content to the right topic, dedups it against existing
pages, redacts secrets, and emits an audit row. The gateway verbs:

| Verb | Use |
|---|---|
| `create-page` | Graduate a matured lesson into a new concept/synthesis page. |
| `append-validation-log` | Add a tagged entry to a page's empirical validation log. |
| `register-index` | Register a new atomic page under its theme-index (and link the theme-index into the topic master when the theme is new). |
| `log` | Write a human-readable run line to the wiki's `log.md`. |

Free-form prose tweaks to an *existing* page (a sentence, a cross-link) can be done as
a direct edit — that is the one documented exception, with the linter and git as the
control. But *new pages and index entries* always go through the gateway.

If a project wants its own routing/dedup/frontmatter rules, the gateway is a
subclassable base class with six override hooks — see
[reference/operations.md](../reference/operations.md) and the developer docs. Most
users never need this.

## The optional cold-start backfill

If you are adopting ultra-memory in a project that already has a history of work, you
can seed the self-learning loop from that history. This is **separate from** the
one-time legacy memory import:

- The **legacy import** (gated by `import_complete`) pulls an existing markdown memory
  tree into the database — a one-time event run by `/memory-setup`.
- The **cold-start backfill** is independent and optional: if you point the plugin at a
  backfill runner, `/memory-setup` prints a *hint* to run it (it never auto-runs). It
  populates the session-event cache from prior sessions so the consolidate beat has
  material to work with.

A useful thing to know about the backfill: because every backfilled lesson was learned
*while using an existing skill*, the backfill can only **augment existing skills** (via
their per-skill `Learnings.md` projections) — it cannot mint brand-new generated
skills. That is by design, not a limitation: brand-new skills come from the *forward*
loop discovering genuinely novel domains, and the eval-gate correctly rejects any
generated skill that would compete with a skill that already exists.

## The self-learning loop, day to day

The loop runs **autonomously** in the background on a maintenance schedule. You do not
drive it; you review what it did. It has four beats, each separated by time and locus,
and each progressively more conservative in *how* it acts.

### Beat 1 — Capture (every session end, hot, no LLM)

When a session ends, a hook records a **deterministic** outcome signal (e.g.
`tests_passed`, `trade_win`, `commit_landed` — observable facts, not "how do you
feel?") and enqueues a learning-candidate for each skill that was used without a
meaningful update to its notes. This is pure Python, sub-millisecond, and **never
blocks the session**. Candidates simply accumulate as an append-only queue.

### Beat 2 — Consolidate (weekly, one batched LLM call)

A throttled weekly drain reads the un-resolved candidates, dedups them against existing
knowledge (no LLM — a cosine/BM25 pre-filter), builds **one batched prompt**, calls the
`claude` CLI **once**, and applies a per-candidate plan: **graduate** a durable lesson
into the store, **merge** it into an existing page, or **skip** it as transient. This
beat **only adds** — it never rewrites or deletes. It refuses to touch any
`human`/`pinned` row, caps how much it graduates per run, and is fail-open: a parse or
runner error just leaves the candidate for next week. A digest lands in your briefings
directory.

### Beat 3 — Self-Correct (opt-in, conservative, the safety wall)

The third beat can *sharpen, merge, revert, or quarantine* the system's own
**agent-authored** notes — never your human-authored ones. Because this is the
highest-blast-radius autonomous verb, it lives behind a six-mechanism code wall:

1. **Provenance gate** — re-reads the live row and physically refuses to touch a
   `human` / `import` / `pinned` unit.
2. **Archive-never-delete** — every verb is a reversible state transition (active →
   redirect-stub, active → quarantined); nothing is ever removed.
3. **Bounded blast radius** — ≤3 edits / ≤3 reversions / ≤5 quarantines per run; it
   *halts* if a run would exceed the cap.
4. **Pre-run git checkpoint** — refuses to run on a dirty tree; tags a checkpoint and
   snapshots the DB first.
5. **Audit + human digest** — every action is logged for you to review. You are in the
   audit loop, not the write loop.
6. **Kill switch** — disabled by default; you opt in explicitly.

A reversion is *proposed for you*, never applied silently — the loop flags it in the
digest and you confirm.

### Beat 4 — Synthesize (opt-in, drafts new skills)

The newest beat can draft an entirely new Claude skill from a cluster of matured,
positively-scored, agent-authored lessons. It reuses the same six-mechanism wall
(bounded to **at most one new skill per run**) **plus a seventh**: an **eval-gate**.
The eval-gate runs a behavioural probe — through the OAuth `claude` CLI, no human input
needed — that proves the generated skill does **not** hijack an existing skill's
auto-trigger, with **zero tolerance** for a false positive. It only ever creates skills
for *new* domains; a domain that already has a static skill is augmented through that
skill's `Learnings.md` instead of cloned. Like Beat 3, it is **disabled by default**.

### What you actually do

In practice, day to day:

- **Save, recall, and pin** as you work (the everyday verbs above).
- **Pin your hard rules** so they ride in every session's gist and stay immutable.
- **Review the weekly digest** in your briefings directory to see what the loop
  consolidated.
- **Leave Beats 3 and 4 off** until you have watched the loop run and are comfortable
  arming them — they ship disabled precisely so this is your call.

Everything the loop does is bounded, reversible, archived, and audited. The design goal
is that mistakes are both **rare** (provenance gates, eval-gates, bounded caps) and
**cheap to undo** (archive-never-delete plus a git checkpoint before every aggressive
run).
