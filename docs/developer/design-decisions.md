# Design Decisions

This is the *why* behind ultra-memory — the rationale for each major architectural
choice, the trade-offs that were weighed, and what was rejected. It is the heart of
the developer docs: [`architecture.md`](architecture.md) tells you *what* the modules
do, [`../reference/api.md`](../reference/api.md) tells you *how* to call them, and
[`../reference/schema.md`](../reference/schema.md) tells you the table layout — this
file tells you *why it is shaped the way it is*, so that a future change does not
quietly violate a decision that was made on purpose.

The whole system rests on one principle: **separate durable concerns from volatile
ones, then unify them through a deterministic, auditable, OAuth-only fabric with zero
external dependencies.** Every decision below is an application of that principle. The
encouraging part — and it is honest, not marketing — is how much the system already
handles *by construction*: redaction at chokepoints, reversibility instead of deletion,
bounded blast-radius, fail-open vs. fail-closed chosen per-site rather than by default,
and a hard wall against ever touching an API key. These are not aspirations; they are
in code and under test (the engine ran a `go-after-fixes` adversarial audit on
2026-05-30 with every finding fixed, plus a strict-TDD bug-hunt arc that surfaced and
closed 20+ integrity/redaction/privilege bugs).

---

## 1. One global DB + one global wiki, partitioned by sub-topic (SP-9)

**Decision (executed 2026-06-01).** All Session Memory — `memories`, `session_events`,
`embeddings`, `access_log`, the edge graph — lives in **one canonical SQLite at
`~/.ultra-memory/memory.db`**, shared by every project. The Expert-Knowledge wiki
likewise lives in **one global tree at `~/.ultra-memory/wiki/`**, organized into
**topic subdirectories** (`trading`, `programming`, `user`) rather than per-project
silos.

### The why

The knowledge fabric must be *project-agnostic*, but the orchestrator (Claude, working
across many projects) needs *global* context. The operator's preferences live in the `user`
topic, coding/infra knowledge in `programming`, project knowledge in `trading`. When
Session Memory and Expert Knowledge were trapped per-project, a new project or a
non-project query (e.g. "fix this build") had no access to accumulated knowledge.

- **One global DB** means operational facts — *OAuth-only, commit-proactively* — travel
  everywhere. Operational rows stay `topic = NULL` (cross-topic, visible to all callers
  within the privilege wall).
- **One global wiki** means a pattern learned in one project becomes available to the
  next. Project-specific knowledge carries its topic and is scoped to callers with the
  matching binding.

### Topic-partitioning vs. flat storage (and the nested-topic fork)

The wiki is partitioned by **top-level topic dir** so that recall is naturally scoped:
a subagent bound to `topic IN {trading}` *cannot* see `programming` or `user` knowledge.
That is a **fail-closed privilege boundary** — a missing or empty binding yields the
empty set, which resolves to "only `NULL`-topic rows," never "everything."

Within a topic, sub-organization is **theme-indexes** (e.g.
`wiki/programming/concepts/claude-tooling-index.md`), **not nested topics**. This
resolves the fork directly: nested topics would fragment the recall-scope axis to
arbitrary depth, making "which topics can this caller see?" unanswerable. Keeping the
scope axis at exactly one level (the top-level topic) and pushing all finer structure
into theme-indexes keeps the access model legible.

### Zero-config resolution (and why one value, not many)

With no env var and no config file, the plugin defaults to `~/.ultra-memory/memory.db`
and `~/.ultra-memory/wiki`. An explicit `ULTRA_MEMORY_DB` override is still honored
(for testing, or a user who deliberately wants a per-project silo). A consumer who wants
to store elsewhere sets **one** value (`data_db_path`); the default is the global home.

**Rejected: require each project to declare its own roots, topic names, and env vars.**
That makes portability fragile and multi-project scenarios unmaintainable — every new
project duplicates config, and a typo in a root path silently splits the fabric. The
single, predictable home (`~/.ultra-memory/`) is the only topology that scales to a
second project without config duplication. Drop the plugin in, run `/ultra-memory:memory-setup`,
restart — the fabric auto-wires.

A safety property worth calling out: DB-path resolution is **`explicit ULTRA_MEMORY_DB`
→ global default**, and **never the cwd / never project-local**. A cwd-relative default
would mean the store silently changes identity when you `cd`. See
[`../reference/operations.md`](../reference/operations.md) for the full resolution chain.

---

## 2. Two stores, one fabric (unified at retrieval, never merged)

**Decision (SP-3).** Session Memory (`memory.db`, volatile, SQLite) and Expert Knowledge
(the wiki, durable, Markdown + git) are **never merged into one canonical store.** They
are unified *at retrieval time* by `unified_recall`, which fuses three rank streams —
memory-cosine, knowledge-BM25, knowledge-embedding — into one ranked list via
deterministic **Reciprocal-Rank Fusion (RRF)**. A typed-edge `links` table
(memory↔memory, memory↔knowledge, wiki↔wiki) lets a session learning reference the wiki
page it matured into.

### Why not collapse them into one table

| Axis | Session Memory | Expert Knowledge | Consequence of merging |
|---|---|---|---|
| **Half-life** | Fast (prefs, state, corrections churn session-to-session) | Slow (market patterns, post-mortems, lessons outlive sessions) | One forced expiry model; one store can't decay at two rates. |
| **Write authority** | Single writer `memory_lib`, transactional, row-level redaction, provenance | Domain gateway `wiki_gateway`, topic routing, page schema | A merge forces a compromise on write discipline. |
| **Canonical form** | Query-on-demand, no flat index | Human-readable Markdown, git-tracked, hand-browsable | A merge loses the human-readable form (DB blob) or redundantly re-exports every memory as Markdown (the old costly fallback). |
| **Coupling** | Engine imports nothing from the consumer (test-enforced) | Frontmatter, wikilinks, page-type enums are domain logic | If wiki pages were rows, the engine would parse Markdown — breaking the project-agnostic boundary. |

There is also a **warm-path NFR**: retrieval must be ≤2–3 s with **no LLM call**. One WAL
SQLite connection serving both stores via one embedder and one BM25 index meets that. A
merged store would mean either one larger DB (scaling risk) or multiple
connections/embedders (warming cost).

### Why `unified_recall` is the right *level* to unify

Not at **write time** — that would put an LLM scorer on the hot path and conflate
volatile and durable at the moment of persisting. Not in a **third DB** — that adds a
connection and breaks the one-warm-process budget. Retrieval time is where the two stores
naturally compose, and crucially it is **where the MCP privilege wall lives** (the
`type × topic` AND-gate). Unifying there means the composition *inherently respects the
caller's authority*: a subagent sees type-scoped memory hits and topic-scoped knowledge
hits ranked together — never `user`/`feedback` rows it shouldn't.

### Why the `links` table doesn't duplicate the wiki graph

The wiki's own `graph.sqlite` holds 5000+ wiki↔wiki edges (pure knowledge domain), read
by the wiki's own retrieval (`wiki_query`). The `links` table is the **cross-store
spine** only: memory↔memory (a learning linking the memory that inspired it),
memory↔wiki (a graduated lesson linking the page it matured into), and the rare
wiki↔memory. Mirroring the whole wiki graph into `memory.db` would bloat it and double the
sync cost; a **one-way mirror** (wiki → `links`, cross-store edges only) keeps the spine
without redundancy. Pure wiki↔wiki edges stay where they belong.

One subtlety that bit us and is now fixed: the `links` edge key
`(src_kind, src_id, predicate, dst_kind, dst_id)` has **no SQL `UNIQUE`** (the pre-existing
table never had one), so idempotency is enforced **in code** (SELECT-then-UPDATE-or-INSERT).
And the privilege wall was extended to **edge endpoints** (`filter_links_for_caller`,
fail-closed on unknown kinds) after a bug-hunt found forbidden user/feedback memory IDs
leaking through `links` even when the row type was correctly walled.

---

## 3. OAuth-only, never the API (a hard boundary, not a policy)

**Decision (enforced at runtime *and* design).** Every LLM call — daily briefing, news
scoring, session consolidation, aggressive self-correction, skill synthesis, attribution
— runs through the local **`claude` CLI** on the user's OAuth session. The engine never
imports `anthropic`, never calls `messages.create`, never has an `ANTHROPIC_API_KEY` on
the process. An `ANTHROPIC_API_KEY` present in the environment is a hard `OAuthViolation`
— **it is better to crash with a clear message than to silently fall through to the SDK.**

### The why

- **Metering control.** LLM cost is on the user's Claude Max subscription (the CLI), not
  a separately-metered API account (the SDK). A stray key invites accidental SDK usage and
  unpredictable billing.
- **Session isolation.** The CLI inherits the user's login — no key rotation, no
  key-per-project fragmentation, no key-sharing with subagents. A cron job runs
  `claude -p <prompt>` on the same subscription session as an interactive one.
- **Boundary clarity / auditability.** Every LLM call is a `subprocess.run(['claude', …])`
  — visible and grep-able. An SDK import would be caught immediately by code review and the
  project's static guards (`test_no_hardcoded_paths`, extensible to `test_no_sdk_imports`).
- **Zero keys on disk.** Git never commits a key; the harness stores none. A break is
  always a *visible* expired session or a code bug — never a leaked/misconfigured key.
- **Composability.** A new contributor learns "shells out to `claude` CLI" once and knows
  there is no hidden API account anywhere.

### The implementation

`claude_cli.py:run_claude()` is the single chokepoint: it validates the env (no
`ANTHROPIC_API_KEY`), raises on a missing `CLAUDE_CODE_OAUTH_TOKEN`, strips the ambient
session id on outbound calls (anti-recursion), and shells out. Tests inject a `runner`
kwarg to mock the subprocess. The token is **the OAuth token, never an API key** — that
distinction *is* the rule.

**Deferred / gated.** The LLM-maintenance beats (wiki curation, consolidation, aggressive
self-correction, synthesis) call Claude only when explicitly armed behind their
`SP*_ENABLE` gates. Until armed, all maintenance is **deterministic, no LLM**.

---

## 4. Gateway-only writes, with redaction at both persist *and* export

**Decision (SP-3).** Every mutation to memory, the wiki, and the edge graph goes through a
single audited gateway. For Session Memory that is `memory_lib`
(`save_memory`, `set_pinned`, `consolidate`, `delete`, `record_session_event`,
`record_access`, `record_link`); for Expert Knowledge it is `wiki_gateway` (a subclassable
base). Both strip secrets at the write chokepoint (`redact_secrets.strip_secrets`) **and
again over the entire export dump** committed to git.

### The why

- **Redaction completeness.** Secrets hide in any column — a token in `links.evidence`, a
  key in `meta.value`, a password in a memory `body`. Redacting *once* at write-time misses
  columns no single writer touched. Redacting **again over the whole export dump** (every
  column, every row) is the only way to guarantee nothing reaches git. This is defense-in-
  depth, and it was hardened by a four-leak bug-hunt round (see below): redaction is a
  **chokepoint discipline**, not a per-return-site concern.
- **Auditability.** Every write lands in `audit_log` (timestamp, user, target, action), so
  "who changed fact X, and when?" is answerable without grepping git diffs or the WAL. The
  `audit_log` is itself redacted on export.
- **Deterministic spool.** When the DB is locked (concurrent cron, slow query), a write is
  spooled to `memory_spool/<hash>.json` and replayed when the lock clears. The spool is
  **content-addressed on the input** (same args → same JSON hash), so a write never fires
  twice; the replay is logged separately. `maintain.run` is the **single serialized
  drainer** of the spool — exactly one production caller.
- **Transactional discipline.** Each write opens `BEGIN IMMEDIATE` (upfront exclusive lock
  — see §4.1), redacts, writes, logs to `audit_log`, commits, closes. A failure rolls the
  whole thing back; the spool catches a busy-casualty and the operator is told **loudly**
  (`WriteSpooled`, "the DB was locked"), never a silent drop. WAL guarantees rollback on a
  mid-transaction crash.
- **Single point of enforcement.** Routing *all* writes through the gateway means every
  writer — CLI user, agent, cron, maintenance — inherits the same redaction, audit, spool,
  and transactional rules. There is **no path to a raw `INSERT`** or a hand-edited `.md`
  that bypasses them.

### Why `wiki_gateway` is subclassable, not monolithic

`WikiGateway` is a base class. A project overrides only the **six hooks** it cares about
(`route`, `theme_for`, `render_frontmatter`, `dedup_check`, `derive_anchor`,
`confidence_label`); embedding, page loading, dedup mechanics, the `fcntl` write-lock,
redaction, and the audit row are all inherited. Trading's gateway adds topic routing
without reimplementing the write machinery; a new project extends it without forking the
engine. Wire a subclass with `wiki_gateway = "<module>:<Class>"` in
`.ultra-memory/config.toml`; unset → the built-in turnkey gateway; no config at all → a
pure-memory install with no wiki (all wiki beats no-op). See
[`../reference/api.md`](../reference/api.md) for the hook contracts.

### What "never delete" means in the gateway

Data is **soft-deleted** — a `status` field (`active`/`deleted`/`redirect`/`quarantined`/
`reverted`) or a redirect-stub — **never removed from the DB.** A superseded learning gets
`status='redirect'` and a pointer to its successor; the old row is never `DELETE`d (the
audit trail survives, and git can resurrect it). This ties directly into the self-learning
safety wall (§5–§6): even the most aggressive self-correction *cannot destroy data* — it
redirects and archives, and every action is git-checkpointed and reversible. In the graph,
this principle goes further: `retention.prune_session_events` **soft-tombstones** rather
than hard-deletes, because outcome edges (`validated_as`, `informed_by`) point at those
rows and a hard-delete would orphan them — edge traversals check liveness.

### 4.1 Why `BEGIN IMMEDIATE` (the WAL-locking decision)

The naive choice is `BEGIN DEFERRED` (SQLite's default), which acquires a lock only on
the *first write*. Under WAL with concurrent writers, that lock can fail **before** the
`busy_timeout` window even applies (the timeout governs contended *statements*, not the
implicit `BEGIN`). The symptom was `database is locked` raised immediately on `BEGIN
DEFERRED` despite a 30 s `busy_timeout`. The decision: acquire the lock **up-front** with
`BEGIN IMMEDIATE`, wrapped in a **bounded busy-retry** (exponential backoff, default 5
tries from a 0.05 s base). Upfront acquisition + bounded retry is what makes concurrent
cron + interactive sessions safe.

---

## 5. The self-learning loop: capture → consolidate → self-correct → synthesize

**Decision (SP-6/SP-7/SP-10).** The system learns autonomously through a four-beat
Hermes-style loop, each beat separated by **time and locus**. Every beat is **fail-open**,
every beat is **gated**, and the governing posture is **full autonomy in *whether* it runs,
conservatism in *how* it acts** (gentlest verbs first, bounded, reversible). The four beats:

| Beat | When | LLM? | Verb posture |
|---|---|---|---|
| **1. Capture** | Session-end (Stop hook) | No | Append-only; never blocks. |
| **2. Consolidate** | Weekly | One batched OAuth call | *Adds only* (graduate / merge / skip). |
| **3. Self-correct** | Weekly | OAuth, strict bounds | Edit / revert (propose) / quarantine. |
| **4. Synthesize** | Weekly | OAuth + eval-gate | Generate one new skill, eval-gated. |

### Beat 1 — Capture (hot, no LLM, deterministic)

At Stop, the checkpoint hook records a **deterministic** `outcome_signal`
(`tests_passed`, `trade_win`, `commit_landed`, …) and enqueues a
`skill_learning_candidate` for each tracked skill invoked without a meaningful
`Learnings.md` update. The signal is an *observable fact the session already knows* — not
"how happy are you?" but "did the test pass?" — so capture is pure Python, sub-millisecond,
and **never blocks the session**.

**Why deterministic capture.** Asking Claude "did this work?" at Stop time (context gone,
session ending, we want to exit fast) would slow the session and require re-context.
Instead the session captures the signal it *has* and leaves the judgment to Beat 2, where
context can be re-materialized. Candidates accumulate in `session_events` as an
append-only queue — *capture fast, never lose a learning, never wedge a session.*

### Beat 2 — Consolidate (slow, weekly, one OAuth call, conservative)

A throttled drain reads up to 50 un-resolved candidates, builds **one batched prompt**,
calls Claude **once**, and applies a per-candidate plan: **graduate** (write a durable
memory or wiki page), **merge** (append to an existing page), or **skip-transient** (the
nag was a false positive). Each graduation writes a `validated_as` edge and marks the
candidate `resolved=1`.

**Why conservative.** This beat *only adds* — it never rewrites. It is bounded
(per-run graduation cap). It **refuses any action targeting a `created_by='human'` or
`pinned` unit** — a human fact is immutable; only `agent`/`background_review` lessons can be
consolidated further. It is fail-open: a parse error or LLM failure leaves the candidate
unresolved for the next run. The operator is in the **audit loop** (a digest lands in
`briefings/`), never the **write loop** — the gate is *code-based*: provenance + bounds +
archive-never-delete.

### Beat 3 — Self-correct (slowest, aggressive verbs, six-mechanism wall)

The self-correct beat (`SP7_AGGRESSIVE_ENABLE`) reads **agent-authored** lessons and their
downstream usage outcomes (via `informed_by` edges from SP-8 attribution). Where evidence
is net-negative and below baseline it proposes **auto-edit** (sharpen/merge/correct),
**self-reversion** (revert a graduation — **propose-for-the-operator**, never auto-reverts, FORK A),
or **quarantine** (mark a contradictory pair disputed). It runs behind a **six-mechanism
safety wall, enforced in the APPLY PATH (code), not the prompt** — the LLM *proposes*, the
code *enforces*:

1. **Provenance gate** — re-reads the live row; refuses any action on
   `created_by='human'`/`'import'` or `pinned` (`MUTABLE_PROVENANCES = ('agent',
   'background_review')`). A single forbidden-target attempt halts the run.
2. **Archive-never-delete** — every verb is a reversible FSM transition (active →
   redirect-stub, active → quarantined); no `rm` anywhere.
3. **Bounded blast radius** — ≤3 edits / ≤3 reversions / ≤5 quarantines per run;
   halt-on-exceed (not truncate). A per-period (`YYYY-MM`) counter in `meta` caps the month.
4. **Pre-run git checkpoint** — `pre-sp7-aggressive-<date>` tag + a `memory_export` snapshot;
   refuses to apply on a dirty/untracked tree.
5. **Audit + human digest** — every action to `briefings/.../sp7-self-improvement-*.md`;
   the operator reviews, never gates.
6. **Kill switch** — `SP7_AGGRESSIVE_ENABLE` (+ a `SP7_AGGRESSIVE_DRYRUN` plan-only mode).

### Beat 4 — Synthesize (newest, generates skills, seventh mechanism added)

The synthesize beat (`SP10_SYNTHESIS_ENABLE`) selects clusters of matured,
positively-scored, agent-authored lessons (trigger: **≥3 graduated lessons per
`index_hook`, mean `outcome_weight ≥ 1.0`**) and induces a native skill
(`.claude/skills/gen-<slug>/SKILL.md`). It reuses the same six-mechanism wall (bounded to
**1 skill per run**, per-domain uniqueness, supersede-on-redraft) **plus a seventh: a
load-bearing eval-gate** (`skill_eval.py`).

The eval-gate proves a generated skill does **not hijack** a static skill's auto-trigger:
a deterministic **Tier-A** description-cosine pre-filter (reject if cosine to any static
description > `THETA_DESC=0.6`) and a behavioral **Tier-B** trigger-probe
(`claude -p` command-file-proxy through the OAuth CLI, `candidate_fp == 0` zero-tolerance).
Probe coverage is **complete by construction** — if no curated corpus is configured,
`build_probe_corpus(descriptions)` auto-derives one hijack-direction probe per discovered
skill (`coverage_gaps() == []`), never fail-closed by omission.

**Why synthesize *augments*, not competes.** Lessons learned while using `risk-manager`
are tagged `index_hook='risk-manager'`. A `gen-risk-manager` minted from them would compete
with the static `risk-manager` — and the eval-gate (correctly) rejects it as a hijack.
Those lessons instead render into `risk-manager/Learnings.md` (per-skill augmentation, live
and working); synthesis focuses on **genuinely new domains** with no static namesake. This
is a *hard learned lesson*: the cold-start backfill is seeded from existing skills, so every
backfill cluster is a same-domain competitor → always (correctly) rejected. The loop
**augments** existing skills and **creates** new ones from the forward loop's novel domains.

### 5.1 Provenance gates *mutability*, not *visibility* — the doubled category error

This is the single most important and most expensive lesson in the codebase, and it is now
load-bearing design.

`created_by` controls **whether a row may be *rewritten*** (SP-7 mutability gate:
`MUTABLE_PROVENANCES = ('agent', 'background_review')`). It does **not** control **whether
a row may be *learned from*** (SP-10 selects induction clusters by `node_type='learning'`
+ quality — **provenance-agnostic**).

The bug: `select_induction_clusters()` reused the SP-7 *mutability* predicate
(`created_by IN ('agent','background_review')`) as the SP-10 *visibility* gate, hiding the
137 cold-start backfill lessons (`created_by='backfill_import'`) from synthesis. The
`Learnings.md` projections came up empty despite the rows being live in the DB. The **same
bug struck twice** — (1) cluster selection, (2) the draft source-gate — both via the shared
`assert_mutable()`. The fix **decouples the gates by semantic intent**: cluster selection
filters by `node_type` (provenance-agnostic); the source-gate uses a new
`assert_synthesis_source()` that permits reads from any source except `pinned` rows.

**Why the separation is crucial.** Pinned human rules must stay **immutable** (no
auto-edit, ever) — but *lessons derived from* pinned rules **can** be synthesized into
skills, because synthesis is additive, evaluated, and reversible. Mutability and visibility
are different questions; collapsing them into one predicate is a category error.

> **The general lesson:** *predicate reuse across lifecycle stages — a write-wall predicate
> reused as a read-scope predicate — is a category error.* Decouple gates by what they
> mean, not by sharing a function.

---

## 6. Autonomy + a code safety wall (not human gates)

**Decision (posture set 2026-06-03).** The system runs **fully autonomous** — the
self-correct and synthesize beats are live, unattended, weekly. The safety net is **code**,
not human approval. Human oversight is the **audit loop** (the operator reads the digest), not the
**write loop** (the operator does not gate individual actions).

### Why autonomous over human-gated

- **Decision windows.** A pattern causing trade losses should be corrected *this* week, not
  next week when a human has time. An automated gate with a manual override (the operator can
  revert) beats a gate that waits on a schedule.
- **Reversibility is the real safety.** Because archive-never-delete makes a revert a
  schema-level pointer flip (not an `rm`), the *cost of a mistake is low*. Eval-gates and
  bounded blast-radius make mistakes *rare*; reversibility makes them *cheap to fix*.
- **Code bounds never miss.** `if num_edits >= 3: halt` is guaranteed; human attention is
  not. The provenance gate's decision is made by a **database query**, before any mutation,
  not by a subprocess that might crash.
- **Conservatism is structural.** Gentlest verbs first (edit before revert, quarantine
  before delete), skip familiar domains, reject eval-regressing changes — all *in code,
  baked into the defaults*. Even with the operator asleep, the system takes the safe path.

### Why "full autonomy + conservative defaults" beats "shipped disabled"

Shipping disabled (dry-run-first, cron-never-fires) is right for a *brand-new* capability.
But once the code is proven — mechanisms tested, operators trained, at least one observed
run — the disabled state becomes a **hidden user decision** ("do I trust this?") that
delays the benefit and must be re-confirmed each cycle. The 2026-06-03 posture resolves it:
the system is **armed**, but the *defaults* are conservative, so it acts and makes the
safest call first. If a conservative default is *too* safe (e.g. top-k=1 attribution
starves the signal), the operator loosens one config value and watches the effect in the
next digest — faster than an approval step every cycle.

### The wall, in one place

The six mechanisms (provenance gate · archive-never-delete · bounded blast radius · pre-run
git checkpoint · audit+digest · kill switch) plus the SP-10 **eval-gate** are the structural
guarantee. They are not trust-based: the code **cannot legally** corrupt a human fact or
delete data; the eval-gate rejects degrading changes; the bounds cap cascading mistakes.
Destructive operations (aggressive auto-edit / revert) deliberately stay **env-gated and
disabled by default**; benign signal-only features (SP-8 attribution) may surface a
user-facing `userConfig` option — a gentle asymmetry that keeps the dangerous verbs behind
an explicit flag.

---

## 7. Hard-won infrastructure decisions (the bug-hunt arc)

These are smaller than the architecture above, but each one is a *decision made on
evidence* — the kind a future change can quietly undo. They are recorded so it doesn't
happen twice.

### Redaction is a chokepoint, not a per-site concern

A bug-hunt round found four high-severity read-path leaks: `unified_recall` returned
unredacted text; `wiki_sync` mirrored wiki titles verbatim into `unified_index` (so a
free-form edit could copy a secret into the queryable DB); the privilege wall filtered row
*type* but not edge *endpoints*; and `record_session_event` redacted JSON *keys* but not
string *values*. The fix made `wiki_sync` and the read path **chokepoints** (redact
title/snippet/bm25_text/values once at the boundary), and extended the wall to edges. The
rule: **redact before `json.dumps`, not after; redact at the boundary, not per return
site** — privacy data multiplies across edges, related tables, and audit trails.

### Lazy + memoized resource init in MCP `main()`

The knowledge MCP crashed on restart with an onnxruntime `NoSuchFile` because `main()`
eagerly built the fastembed model from `$TMPDIR` (which macOS purges and always clears on
reboot), killing the stdio server before it could answer `initialize`. The fix: move the
cache to a **persistent** `~/.cache/ultra-memory/fastembed`, and make the embedder **lazy**
(`lazy_embedder()` defers the load to first query). Result: the server answers `initialize`
in 0.36 s even on a stale cache; a cache miss degrades *one query*, never the connection.
**Eager resource init in an MCP `main()` is fragile; defer to query time.**

### Migrations: one explicit transaction, idempotent DDL

`migrate()` originally used `executescript()` (auto-commits per statement), so a crash
mid-migration could leave the schema partway-applied while `PRAGMA user_version` had
already bumped, and a replay of `ADD COLUMN` crashed on "column already exists." The fix:
run each migration's statements **+ the version bump inside one explicit `BEGIN/COMMIT`**
(SQLite DDL *is* transactional within an explicit transaction), and make every `ADD COLUMN`
replay-tolerant (`IF NOT EXISTS`). `PRAGMA user_version` is appended to the dump so
restore→reopen round-trips the version without re-running migrations. **Replays are
inevitable in production; idempotence is not optional for DDL.** Migrations are forward-only.

### Privilege filters live in the query layer, not post-fetch

A type-scoped caller asking `top_k=10` could get 3 results because the allowlist ran *after*
truncation. The fix pushes the type allowlist **into the SQL** (`include_types`), so the
query returns only allowed rows and `top_k` and the allowlist are compatible. **Post-hoc
filtering breaks `top_k` contracts; filter in the query.**

### Determinism: tie-breaks and stable fingerprints

RRF ties reordered run-to-run because the fusion dict was built from a `set` (insertion
order `PYTHONHASHSEED`-dependent) and the final sort keyed only on score. The fix is a
**deterministic total order** `(-weighted_score, (kind, key))` — it changes only tie-order,
never which score ranks where. Relatedly, the BM25 corpus cache keys on a **stable sha1
fingerprint**, not a `PYTHONHASHSEED`-salted `hash()`, so the cache key is process-stable.
**Sorting on one key in the presence of ties is a latent bug; always add a secondary
tie-break.**

### Human provenance is sacred

A human edited a memory in the DB and re-imported the markdown; the engine reverted the body
and downgraded `created_by` from `human` to `agent`. The fix: `save_memory`'s `UPDATE`
**preserves `human`** (a non-human re-save over a `human` row keeps it `human`), and
`import_memory_dir` **skips** any live `human` row entirely. **Import flows must respect
human provenance.** (Separately, import now **fails loud on a duplicate frontmatter name**
rather than silently overwriting — silent data loss in import is the cardinal sin.)

### Type invariants checked at entry; LLM JSON gets a retry

`query_memories(embedder=None)` on a non-empty store raised a cryptic
`'NoneType' is not callable` deep inside — the docstring claimed a BM25 fallback that only
the *knowledge* side has. The fix raises a clear `ValueError` at entry. And the skill-draft
generator wraps JSON parsing in a single-pass `retry_on_parse` loop, because long
LLM-generated Markdown bodies occasionally produce unescaped newlines — **LLM-generated
structured output is fragile; design for one retry.**

### Eval loops need parallelization and unique scratch files

The SP-10 eval-gate ran ~265 serial `claude -p` probes and overran the 60 min maintenance
window (exit 124, killed before applying any skill). The proper fix parallelizes probes in
a bounded `ThreadPoolExecutor` (`PROBE_MAX_WORKERS=6`), each with a **unique per-probe
temp-file** (`<slug>-probe-<nonce>.md`) — 50 min → 12 min. A separate fix added
`.claude/commands/*-probe.md` to `.gitignore` after a killed run left an ephemeral probe
file that the auto-commit hook swept into git (it then loaded as a stray command next
session). **Tight-deadline eval loops need parallelization; temp artifacts in user-facing
dirs need defensive cleanup *and* a gitignore pattern.**

### Green tests can be hollow veneers

The wiki-gateway Phase-1 build passed **all** plugin tests (1115 green) and **all** Trading
tests (1193, 9-skip) — then the live cron broke immediately because the gateway resolver was
**never threaded into the Stage-2 beat** (the tests mocked the gateway or exercised only the
golden path). The fix wired it (and an orchestrator structural review now asks "is the
resolver threaded into beat X?", not just "does the test pass?"). **Green tests without
structural verification of integration points are veneers.** This is why a post-build
orchestrator review is a project norm, not optional.

### Other safety-by-construction choices, briefly

- **Soft-tombstone over hard-delete in the graph** — `prune_session_events` tombstones;
  edges referenced by attribution predicates (`validated_as`, `superseded_by`,
  `informed_by`) are excluded from prune entirely, and traversals check liveness.
- **Path-based orphan prune, not topic-based** — `wiki_sync` prunes by the synced roots'
  path-prefix (mirroring `memory_export`), so a deleted page is pruned even when its topic
  goes fully empty (a topic-based prune left phantom recall hits).
- **Index the fastest-growing table** — migration 0008 adds the composite
  `idx_access_log_session(session_id, target_kind)` because the never-pruned `access_log`
  full-scanned on every session-end attribution query.
- **Filter before the join** — `query_memories` now sorts + truncates to `top_k` *before*
  fetching links, bounding the per-recall link work to `top_k` (the old fetch-then-truncate
  was a classic N+1).
- **Surface malformed input as a warning, never a silent fold** — the today-file importer
  now accepts all dash variants and captures a non-time header as its own day-midnight block
  *with a warning*, instead of silently folding it into the prior block.

---

## See also

- [`architecture.md`](architecture.md) — module-by-module mechanics and the canonical
  model.
- [`variables.md`](variables.md) — the complete configuration + tunable-constant reference,
  with the env > config.toml > defaults resolution order.
- [`contributing.md`](contributing.md) — TDD discipline and the doc-lockstep rule.
- [`../reference/api.md`](../reference/api.md) — per-function signatures and contracts.
- [`../reference/schema.md`](../reference/schema.md) — tables, columns, migrations, invariants.
- [`../reference/operations.md`](../reference/operations.md) — install, wiring, rollback, spool.
