# 10. Architecture

> A memory plugin is mostly a discipline problem, not an algorithm problem. The hard
> part is not *how* to embed a sentence — it is making sure that across a thousand
> sessions, a hundred crashes, two concurrent crons, and one autonomous self-edit, the
> store never silently loses a fact, never leaks a secret, and never charges you for an
> API call you didn't authorize. ultra-memory's architecture is built around four
> chokepoints that make those guarantees structural rather than aspirational: **one
> writer**, **one LLM gate**, **one secret filter**, **one privilege wall**. Everything
> else hangs off them.

This chapter is the map of the engine for someone who is about to read or change the
code. It moves from the abstract shape (the canonical model and the data flow) down to
the concrete modules, the session hooks, the maintenance beats, and the privilege
boundary internals.

If you only want to *use* the plugin, you don't need this chapter — start at the
[handbook index](./README.md). If you want to *change* it, read this, then
[Chapter 11 — Reference: API & schema](./11-reference-api-schema.md) for the exact
surface, then [Chapter 12 — Contributing](./12-contributing.md) for the rules of the
road.

---

## 10.1 The canonical model

There are **two stores with different half-lives**, and the architecture's first job is
to keep them straight while letting a reader treat them as one.

```
   memory.db (SQLite, WAL)  =  WORKING TRUTH        the wiki (Markdown files)  =  DURABLE TRUTH
     volatile "how you work"                          slow "what you've learned"
     │ writes: ONLY short-lived memory_lib txns        │ writes: ONLY the wiki gateway
     │ reads : memory_query / unified_recall           │ reads : unified_recall (via a mirror)
     ▼                                                  ▼
   memory_export.export_memory  (deterministic)       wiki_sync  (idempotent, no-LLM)
     ▼                                                  ▼
   memory_export/                                     unified_index  (a derived, rebuildable
     memory.dump.sql  = redacted text dump,            mirror row per page, inside memory.db)
                        carries user_version
                        → the ROLLBACK source
     memory.snapshot.db = VACUUM INTO binary (gitignored)
     views/*.md         = regenerated human/harness views
```

Two rules fall straight out of this picture, and most of the engine exists to enforce
them:

1. **The database is the working truth, but git tracks only a consistent *text dump*,
   never the live `.db`.** Rollback means: restore `memory.dump.sql`, reopen. The
   binary snapshot is a fast-path convenience and is gitignored; the markdown views are
   lossy (no embeddings, no audit log) and are never a rollback source for those fields.

2. **Files stay canonical for the wiki; `memory.db` stays canonical for memory.** They
   are never merged. The bridge is a *derived* mirror: `wiki_sync` walks the wiki files
   and upserts a lightweight row per page into the `unified_index` table inside
   `memory.db`, so a single warm connection can rank both stores. Delete the mirror and
   nothing is lost — re-run `wiki_sync` and it rebuilds.

The payoff is **unified recall**: `unified_recall` ranks memory rows and wiki pages in
one list, scoped by type, topic, and caller class, with no LLM on the path. (See
[Chapter 11 §`unified_query`](./11-reference-api-schema.md#unified_query) for the exact
fusion math.)

---

## 10.2 The four chokepoints

Before the module list, internalize the four invariants — they explain *why* the code
is shaped the way it is. Each is a single point every relevant operation must pass
through, which is what makes the guarantee hold across every call site.

| Chokepoint | Module | The guarantee it makes structural |
|---|---|---|
| **One writer** | `memory_lib._write_txn` | Every mutation opens its own short-lived `BEGIN IMMEDIATE … COMMIT`, retries on `SQLITE_BUSY`, spools to disk + raises loudly on exhaustion (never a silent drop), and writes an `audit_log` row. No feature code runs raw `INSERT`/`UPDATE`. |
| **One LLM gate** | `claude_cli.run_claude` | Every model call shells out to the local `claude` CLI on your own OAuth subscription. It raises `OAuthViolation` if `ANTHROPIC_API_KEY` is set or the OAuth token is missing. Never the Anthropic SDK, `api.anthropic.com`, `messages.create`, or `cache_control`. |
| **One secret filter** | `redact_secrets.strip_secrets` | Every persisted text field is redacted at the write chokepoint, and the *entire* export dump is redacted again. A secret cannot enter the queryable store or the git-committed dump. |
| **One privilege wall** | `knowledge_mcp` + `unified_query` | Recall is scoped by `(type × topic × caller_class)`, **fail-closed**: an untrusted subagent never sees `user`/`feedback` rows or topiced knowledge it isn't bound to. |

Hold those four in mind and the rest of the architecture reads as machinery that feeds
them.

---

## 10.3 The engine modules

The engine is `ultra_memory/`. Every module is pure-ish: it takes a `conn`, a timestamp
`ts`, and (where needed) an injected embedder — no globals, no clock reads, no hidden
I/O. That is what makes the suite deterministic and offline.

| Module | Responsibility |
|---|---|
| `db.py` | Connection discipline (WAL, `busy_timeout`, `foreign_keys=ON`, autocommit + explicit `BEGIN IMMEDIATE`) and the **forward-only, transactional, idempotent migration runner**. |
| `_time.py` | One source of truth for the canonical Zulu wire format (`now_utc_zulu` / `hours_between` / `ZULU_FMT`). Stdlib-only, import-safe everywhere. |
| `migrations/*.sql` | Ordered `NNNN_name.sql`. Applied + version-bumped in one transaction each. See [Chapter 11 §Migrations](./11-reference-api-schema.md#migrations). |
| `memory_lib.py` | **The only writer.** Every mutation: redact → `_write_txn` (retry + spool) → `audit_log`. Owns `save_memory`, `record_link`, `set_pinned`, `set_status`, `set_outcome_weight`, `record_session_event`, `record_access`, the topic write path, and the spool replay. |
| `redact_secrets.py` | The pure secret-stripper — the pre-persist + pre-export chokepoint. |
| `retrieval_core.py` | cosine, RRF, vector (de)serialise, content hash, the embedding cache (single + batch), and the lazy fastembed loader (model cached under `$HOME`, never the OS temp dir). |
| `memory_query.py` | The memory read side: candidates → cosine → title boost → ranking → top-k → 1-hop links. No LLM, no BM25 (memory ranks on embedding-cosine only). |
| `memory_import.py` | One-time bootstrap: parse a legacy Markdown memory tree → write rows via `memory_lib`. Edit-safe (skips live `human` rows on re-import). |
| `memory_export.py` | The rollback artifact: redacted dump + binary snapshot + regenerated views, atomic, skip-if-unchanged. Also `export_learnings_projection` (the per-skill `Learnings.md` view). |
| `wiki_sync.py` | Tier-1 wiki→`unified_index` mirror: walk consumer-fed roots, upsert pages, reconcile orphans, embed into the shared cache. Project-agnostic, idempotent (sha-skip), fail-open, **a write-time redaction chokepoint**. Also `extract_signal_text` — pulls a page's optional `## Signal` H2 and embeds it as the distinct `knowledge_signal` channel (the Recall-Reflex observable). |
| `unified_query.py` | The warm cross-store retrieval surface: `unified_recall` (memory cosine + knowledge BM25/cosine + the `## Signal` boost, fused with best-rank-per-backend RRF, × `outcome_weight`) + the fail-closed `topic_scope_from_env` + `best_signal_match` (the Atomic-Graduation dedup-gate). No LLM. |
| `recall.py` | The Recall-Reflex public primitive: `recall(signal_text)` — a thin, fail-open, privacy-scoped wrapper over `unified_recall` + a CLI. The single shared entry point every consumer reflexes through — §10.4a. |
| `knowledge_mcp.py` | The read-only MCP tool + the type axis of the access wall (`allowed_types_for`, `caller_class_from_env`) + the single DB-path resolver `db_path_from_env`. |
| `attribution.py` | The deterministic, no-LLM usage→outcome join: links the memories a session recalled to that session's outcome signal via `informed_by` edges. |
| `retention.py` | Bounds `session_events`: roll rows older than `keep_days` into `sessions.summary`, then delete (preserving any event an attribution edge still references). |
| `claude_cli.py` | The single OAuth-sanitised LLM chokepoint (the engine's read/recall path uses no LLM; this is for the maintenance beats and future agents). |
| `maintain.py` | The light Tier-1 maintenance slice: spool drain → prune → export → `wiki_sync`, all behind one ~20h throttle. No LLM. |
| `wiki_gateway.py` | The subclassable wiki **write**-gateway base class (the 6 override hooks + inherited verb materializers). See [Chapter 11 §`wiki_gateway`](./11-reference-api-schema.md#wiki_gateway). |
| `hooks/` | The session hooks (`common`, `rehydrate`, `checkpoint`) + the `UserPromptSubmit` recall hook (`recall_prompt`) — §10.4. |
| `maintenance/` | The heavy Tier-2 self-learning beats + their config + the session-lifecycle driver — §10.5. |
| `wiki_maintenance/` | The project-agnostic LLM-wiki curation engine the `wiki_maintenance` beat drives. |

---

## 10.4 The session hooks — the capture/replay edge

Two hooks bracket every interactive session, two more fire the maintenance beats, and a
fifth fires on each prompt (the recall reflex). **All are fail-open** (a hook error, or a
not-yet-bootstrapped DB, never blocks the operator) and **role-scoped** (a no-op for
cron/subagent runs, gated on `ULTRA_MEMORY_AGENT_ROLE` or a non-interactive SessionStart
`source`).

The plugin registers them in `hooks/hooks.json`, dispatched through the path-free,
fail-open `hooks/um-hook.cmd` wrapper:

```
SessionStart (startup|resume|clear|compact):
  ├─ rehydrate   (sync)   → inject the ≤2k-char gist as additionalContext
  ├─ maintain    (async)  → the light Tier-1 slice (drain/prune/export/wiki_sync)
  └─ beats       (async)  → the throttled heavy Tier-2 self-learning beats
UserPromptSubmit:
  └─ recall      (sync)   → on a concrete error signature, inject prior art as additionalContext
Stop:
  └─ checkpoint  (sync)   → record completed tasks; enqueue the session for ingest
```

**SessionStart → `rehydrate.run`.** Composes a budgeted gist from the DB — pinned
rules, "where we left off", open follow-ups, hot memories, a pull-on-demand pointer —
with **no LLM and no embedder**. `memory_lib` imports in ~15 ms and pulls in no
fastembed, so this hot path stays fast. The gist is structure-injection-sanitized
(every rendered field is collapsed to one line so no stored value can forge a `## …`
header inside the trusted context), pinned rules are exempt from the budget tail-cut,
and the live-vs-shadow mode is a flag (`ULTRA_MEMORY_SHADOW`; the plugin runs live).

**Stop → `checkpoint.run`.** Derives completed tasks from the **raw transcript JSONL on
disk** — not the compacted in-context view, so mid-session compaction can't truncate it
— and records each as an idempotent `task_done` session event. It also enqueues the
finished session for the `session_ingest` beat (the capture beat of the self-learning
loop). It **never blocks**: it always returns `{}`.

**SessionStart → `maintain` + `beats` (async).** These are the two maintenance arms.
`maintain` is the light no-LLM slice. `beats` is the heavy self-learning driver
(§10.5). Both are async in `hooks.json` and throttled by their own meta clocks, so
firing them every session is cheap and safe.

**`UserPromptSubmit` → `recall_prompt.run` — the recall reflex.** The fifth hook is the
engineering arm of the Recall-Reflex (§10.4a): *recognise a situation → recall what you
know → act informed*, fired off the observable instead of off "you decided to look".
`detect_signature` scans the submitted prompt for a **concrete error signature**
(stacktrace / `FooError` / `Error:` / `path/file.ext:123` / OS error / `panic`) and is
deliberately **conservative** — precision over recall, never fires on a plain question
(this is "Tier-2 only": the fuzzy Tier-1 debug-intent nag is *not* built, because the
SessionStart gist + the `recall-reflex` skill already cover "remember to recall"). On a
hit it calls `recall(signature, knowledge_only=True, build_embedder=False)` and injects
the top hits (`≤3`) as `additionalContext`. The two flags are load-bearing:
`knowledge_only` drops the memory backend so **no `user`/`feedback` row can surface on a
main-session prompt** (privacy-safe by construction, not by trust), and
`build_embedder=False` keeps the path BM25-only so no fastembed model loads on every
prompt — the literal error text still matches the page body (and any `## Signal` it
carries) by BM25. Like the others it is **fail-open** (`detect_signature` miss, recall
error, or `RECALL_HOOK_DISABLE=1` → `{}`, no injection, rc 0) and role-scoped
(`common.agent_role_optout` + `common.db_ready`). The injected block is advisory framing,
not a gate — recall is context, never the `risk-manager`/hard-rules wall.

All hooks gate on `common.db_ready` (`meta.import_complete == '1'`): until the one-time
bootstrap import is done, they fail-open to the legacy path rather than touching an
un-migrated store.

---

## 10.4a The Recall-Reflex retrieval path — `recall()` and the `## Signal` channel

`recall.py` is the engine's *recall reflex*: the single, fail-open, privacy-scoped entry
point that turns the warm retrieval surface (§10.1, §10.7) into something a consumer can
fire reflexively off an observed signal — an error signature in the engineering hook
above, or an abnormal market condition on the trading observation surfaces. It is a
**thin wrapper, not a second retrieval engine**: there is exactly one fusion algorithm
(`unified_recall`), and `recall()` only adds the policy a reflex needs.

```
recall(signal_text)
  ├─ caller_class='subagent'  (default)  → the fail-closed type wall (§10.7): SAFE_TYPES only
  ├─ knowledge_only=…                    → include_memory pass-through (below)
  ├─ build_embedder=…                    → lazy fastembed, fail-soft to BM25-only
  ▼
unified_recall(conn, signal_text, …)     → the ONE fusion (memory ⊕ BM25 ⊕ cosine ⊕ ## Signal)
  ▼
over-fetch (top_k×3, ≤50)  →  drop navigational page-types  →  trim to top_k  →  uniform hits
```

Three architectural choices distinguish it from a bare `unified_recall` call:

**The `## Signal` channel is a *separate* RRF backend — the "boost".** An atomic may carry
an optional `## Signal` H2 = the observable condition under which its knowledge should be
recalled (distinct from a strategy's *entry* trigger; the schema and `wiki/SCHEMA.md` say
so). `wiki_sync.extract_signal_text` pulls that section and embeds it under its **own
embedding `target_kind='knowledge_signal'`**, kept apart from the page's main
`target_kind='knowledge'` vector. Inside `unified_recall`, `_signal_candidates` ranks the
signal channel **over exactly the slugs the type/topic-scoped knowledge backends already
admitted** — so it inherits the privilege wall and never widens scope — and is fused as a
**fourth, independent backend** alongside memory-cosine, knowledge-BM25, and
knowledge-cosine. Because the fusion is best-rank-per-backend RRF (rank-based,
scale-invariant), a page whose recorded *observable* matches the query earns an extra
`1/(k+rank)` credit with no new scoring math — the boost is structural, not a tuned
weight. This is the fix for the originating failure: existing engineering atomics were
titled by *lesson*, so a search by the *observable* scored 0; the signal channel makes the
lesson findable by the words the error actually appears in. (Fusion math:
[Chapter 11 §`unified_query`](./11-reference-api-schema.md#unified_query).)

**`include_memory` is the privacy knob, not a performance knob.** `unified_recall(…,
include_memory=False)` skips the memory backend *entirely* — no `query_memories` call —
so the result is knowledge-only and, critically, **no `user`/`feedback` memory can
surface even by accident**. `recall(knowledge_only=True)` is the pass-through, and it is
what the `UserPromptSubmit` hook uses: a hook that fires on a *main-session* prompt must
not be able to leak the operator's preferences past the type wall, and the cleanest way to
guarantee that is to never query memory at all. It also drops the embedder requirement
(the memory backend has no BM25-only fallback; the knowledge backends do), which is why
`knowledge_only=True` composes with `build_embedder=False`.

**The page-type filter trims navigational noise.** Recall returns *prior art*, so
index/redirect pages (`theme-index`/`master-index`/`index`/`redirect`) are dropped. To
keep the filter from starving the result below `top_k`, `recall()` over-fetches
(`top_k×3`, capped at 50), filters, then trims — the fusion never sees the policy.

Everything is **fail-open**: a missing DB, a fastembed load failure, or any exception
returns `[]` plus one stderr line, never a raise. The CLI
(`python -m ultra_memory.recall "<signal>" [--top --topic --caller-class --no-embed
--json]`) is the same path with `rc 0` always. Exact signature:
[Chapter 11 §`recall`](./11-reference-api-schema.md#recall--the-recall-reflex-primitive).

---

## 10.5 The maintenance beats + the session-lifecycle driver

The self-learning loop is **five beats** plus two no-LLM housekeeping beats. The whole
thing is **autonomous by default, conservative in how it acts** — autonomy in *whether*
a beat fires, conservatism in *how* it changes the store (gentlest verb first, bounded,
archived-never-deleted, eval-gated). The earlier "ships-disabled / dry-run-first"
posture is superseded. This posture is deliberate to the point of a rule: every beat
**ships ON with a kill-switch, never behind an enable-flag** — a default-off `*_ENABLE` is
a dead flag nobody flips, i.e. a feature that never runs. The config knobs are opt-*out*.

### The driver (new in 0.0.4): `python -m ultra_memory.maintenance`

The heavy beats no longer need an OS scheduler. The **session-lifecycle driver** is the
project-agnostic entry a consumer wires into its session lifecycle — here, the async
`beats` arm of the SessionStart hook:

```bash
python -m ultra_memory.maintenance                 # all due + enabled beats
python -m ultra_memory.maintenance --beat consolidate   # one beat only
python -m ultra_memory.maintenance --force         # ignore the throttle clocks
```

`run_pipeline` (in `maintenance/run.py`) drives the beats in a fixed order. Each beat
is:

- **gated** by config — `config.beat_enabled(name)`, which defaults **ON** (the
  autonomous posture); a consumer can switch any beat off in `.ultra-memory/config.toml`;
- **throttled** by a per-beat `meta` clock (`last_maintenance_beat:<name>`) at
  `cadence_for(name)` hours, so SessionStart can call this every session on any
  platform without re-running a weekly beat;
- **fail-open** — a beat that raises degrades to a recorded error + one log line, never
  wedging the session or the other beats. The clock is stamped only on success.

The beats are supplied through a `registry` (`{name: callable(conn, config, ts, env)}`)
so tests inject stubs and an un-migrated beat is simply absent → skipped. **No LLM lives
in the orchestrator** — that is inside the individual beats, always through
`claude_cli.run_claude`.

### Beat order and cadence

```
BEAT_ORDER = (session_ingest, atomic_graduate, consolidate, aggressive, synthesize, learnings, wiki_maintenance)
```

| Beat | Cadence (default) | LLM? | What it does |
|---|---|---|---|
| `session_ingest` | 24 h | OAuth | Mines each finished session's redacted transcript digest in ONE call → durable memories + `feedback` corrections + skill-tagged learnings **+ `atomic_candidate` markers** (the 4th output — engineering gotchas, *wanted* here unlike the env-specific exclusion on the others, + durable trading/strategy lessons, each with its literal observable). Runs **first** so its output feeds the downstream beats. Opt-out via `SESSION_INGEST_ENABLE`. |
| `atomic_graduate` | 24 h | none (deterministic) | **Capture-findably backstop (Recall-Reflex 5.2).** Clusters the pending `atomic_candidate`s **together with existing-page `## Signal` seed vectors** (cosine `ATOMIC_GRADUATE_CLUSTER_COS`, default 0.80) so each incident graduates once — a seeded cluster MERGEs into the existing page, a seedless cluster CREATEs one `## Signal`-keyed atomic via the consumer gateway. The apply is deterministic — the lesson + observable already came from `session_ingest`'s OAuth call. Same 24 h clock as `session_ingest` and ordered **right after** it, so on a pass where both are due (or a consumer cron forces the beats) a freshly-mined candidate graduates the same run. No embedder → falls open to a per-candidate dedup-gate. Behind its own intrinsic wall (below). Kill-switch `ATOMIC_GRADUATE_DISABLE`. |
| `consolidate` | 168 h (weekly) | OAuth (one batched call) | **Conservative.** Reads un-resolved learning candidates, dedups via `unified_recall` (no LLM pre-filter), then per candidate **graduates** (a durable memory or wiki page), **merges** (append-validation-log), or **skip-transients**. ADD-only — never rewrites; refuses any `human`/`pinned` target. Each graduation writes a `validated_as` edge. |
| `aggressive` | 720 h (monthly) | OAuth | **Self-correct.** Folds attribution edges into an EWMA, then proposes **auto-edit**, **self-reversion** (flagged for the operator, never auto-reverted), or **quarantine** on `agent`/`background_review` lessons whose evidence went net-negative. Behind the 6-mechanism wall (below). |
| `synthesize` | 720 h | OAuth + eval-gate | Induces a native skill (`.claude/skills/gen-<slug>/SKILL.md`) from a cluster of ≥3 graduated `node_type='learning'` lessons grouped by `index_hook` with mean `outcome_weight ≥ 1.0`. Net-new domains only. Reuses the wall + a 7th eval-gate mechanism. |
| `learnings` | 168 h | none | Tier-1 projection-regen: rebuild each per-skill `Learnings.md` view from the store and refresh any generated skill's managed block. Runs **last**. |
| `wiki_maintenance` | 24 h | OAuth | The two-stage LLM-wiki curation (detectors → worklist → one batched adjudication → apply via the consumer gateway). No-op with no wiki roots. |

### The safety wall (the two aggressive beats)

`aggressive` and `synthesize` are the highest-blast-radius autonomous verbs in the
system, so they live behind a **6-mechanism wall enforced in the apply path (code), not
the prompt** — the LLM *proposes*, the apply path *enforces*:

1. **Provenance gate** — re-reads the live row; only `('agent','background_review')`
   rows are mutable. A `human`/`import`/`backfill_import`/`pinned` target halts the run
   (zero tolerance).
2. **Archive-never-delete** — every verb is a reversible FSM transition or a redirect
   stub. No `rm` anywhere. A retired generated skill moves to `.claude/skills-archive/`.
3. **Bounded blast radius** — ≤3 edits / ≤3 reversions / ≤5 quarantines per run, max 1
   skill induced per run, plus per-month global caps tracked in `meta`. Halt-on-exceed.
4. **Pre-run git checkpoint** — a tag + an export snapshot; refuses to act on a
   dirty/untracked tree.
5. **Audit + human digest** — every decision lands in a digest the operator reads
   (Peter in the *audit* loop, none in the *write* loop).
6. **Kill switch** — present-by-default env switches (`SP7_AGGRESSIVE_DISABLE`,
   `SP10_SYNTHESIS_DISABLE`), wired from the plugin's userConfig opt-out toggles.

`synthesize` adds a **7th mechanism**: a load-bearing **trigger-probe eval-gate**
(`skill_eval`) that proves a generated skill does NOT hijack a static skill's
auto-trigger — a Tier-A description-cosine pre-filter plus a behavioral `claude -p`
command-file-proxy probe, zero-tolerance (`candidate_fp == 0`), with a probe corpus
auto-derived per deployment so coverage is complete by construction.

### The atomic_graduate wall (auto-creating findable pages)

`atomic_graduate` writes *new* wiki pages without a human in the loop — a different
blast-radius shape from the aggressive beats (it never *edits* an existing unit; it
creates or merges), so it carries its **own intrinsic wall**, again **in the apply path,
not the prompt** (and here there is no prompt at all — the apply is deterministic). Each
unresolved `atomic_candidate` runs the gauntlet:

1. **Three-way `## Signal` dedup-gate.** `best_signal_match` finds the top cosine of the
   candidate's observable against the existing `knowledge_signal` channel, scoped to the
   candidate's topic. `≥ dedup_upper` (0.86) → **merge** (an `append-validation-log` to
   the matched page) — never a duplicate; `[dedup_lower, dedup_upper)` (0.78–0.86) →
   **skip-conservative** (leave the candidate unresolved + log; neither a maybe-dup nor a
   forced merge — revisit next run as the channel populates and the band recalibrates);
   `< dedup_lower` → **novel** → create. This is the literal fix for "we built the same
   solution twice": a paraphrased re-discovery of the same incident merges instead of
   spawning a second page.
2. **Eval-gate — recall-findable-or-quarantine.** A page that cannot be found by its own
   observable is useless, so right after `create-page`/`register-index` the beat indexes
   the new page inline (a one-page mirror of `wiki_sync` — `unified_index` upsert + embed
   its `## Signal`) and then `recall(signal)` must return the new slug in top-N. Miss →
   **quarantine** (a `status: quarantined` frontmatter flag — archive-never-delete, never
   `rm`). This is `synthesize`'s self-validation discipline applied to a page.
3. **Bounded blast radius.** ≤ `ATOMIC_GRADUATE_CAP` (default 3) *created* per run; a
   capped run logs the drop count and leaves the rest for the next pass — no silent
   truncation.
4. **Create-only provenance.** Every new page is stamped `created_by='background_review'`,
   which is exactly the `('agent','background_review')` class the aggressive wall's
   provenance gate considers *mutable* — so a bad auto-atomic is later revertible by the
   self-correction loop, while `human`/`import`/`pinned` units stay untouched.
5. **Per-candidate fail-open.** Any error on one candidate (gateway, embed, eval) leaves
   *that* marker unresolved (retried next run) + one diagnostic line, and the drain
   continues. The whole beat is kill-switchable (`ATOMIC_GRADUATE_DISABLE`) and a no-op on
   an empty queue (it returns before loading fastembed).

A trading/strategy candidate additionally ships with an **unvalidated `[Recent-Regime]`
confidence label** + the entry-trigger disambiguation, so no real-money path treats an
auto-created lesson as established — it is advisory context subject to the normal
maintenance recalibration and the Recall-Reflex safety invariant (recall informs, it never
relaxes a gate).

---

## 10.6 Write-ownership & concurrency

The **single-writer discipline** is the engine's spine. Every write goes through
`_write_txn`, which wraps `_with_immediate_retry`:

- opens its own short-lived `BEGIN IMMEDIATE … COMMIT`;
- retries on `SQLITE_BUSY`/`database is locked` with exponential backoff;
- rolls back defensively only when a transaction is actually active (no double-ROLLBACK
  masking the real error);
- surfaces non-busy errors immediately;
- on retry exhaustion **spools** the operation to `<db_dir>/memory_spool/<hash>.json`
  and raises `WriteSpooled` **loudly** — never a silent drop.

The spool is drained by `maintain.run` (the single serialized production caller of
`replay_spool`, on its own connection), so a busy-casualty write self-heals on the next
maintenance pass. The same bounded retry is reused by the maintenance writes
(`retention.prune_session_events`, the meta-clock stamps), and `record_access` uses an
atomic `access_count = access_count + 1` (no read-modify-write → no lost updates).
**No `claude_cli` call ever happens inside a write transaction.**

**Migration safety:** the runner applies each pending migration's statements *and* its
`user_version` bump inside one explicit transaction (SQLite DDL + `PRAGMA user_version`
are both transactional), so a crash partway rolls back fully — version and schema never
desync. `ADD COLUMN` replay is tolerated. The version is mirrored into
`meta.schema_version`, which (unlike `PRAGMA user_version`) survives `iterdump`, so the
committed dump round-trips the version.

---

## 10.7 The privilege boundary internals

The knowledge MCP is a **privilege boundary**, not just a read tool. The access wall
composes **two orthogonal axes by AND**:

```
visible(fact) ⟺ (topic ∈ agent_topics OR topic IS NULL)
             AND (type ∈ allowed_types_for(caller_class))
```

**The type axis** (`knowledge_mcp.allowed_types_for`). A trusted class
(`orchestrator`/`owner`) sees all types. Anything else — the fail-closed default — is
treated as `subagent` and sees only `SAFE_TYPES = (project, reference)`, **never**
`user`/`feedback` memories. `caller_class_from_env` reads `ULTRA_MEMORY_CALLER_CLASS`,
defaulting to `subagent`. This is the structural answer to "a prose 'do not print
secrets' instruction is ignored when secrets are central to the finding": the boundary
is a **tool constraint**, not a prompt.

**The topic axis** (`unified_query.topic_scope_from_env`). Resolves `agent_topics` from
`ULTRA_MEMORY_CALLER_TOPIC` + the `agent_topic_bindings` table, and **fails closed**: no
binding from either source ⇒ the **empty set**, so a subagent with no topic binding sees
only `topic IS NULL` operational rows of its allowed types — and **zero topiced
knowledge**. The orchestrator / trusted CLI passes `agent_topics=None` (the all-topics
sentinel). The degraded mode is safe by design: a partial binding sees **less, never
more**.

Two extra defenses harden the boundary:

- **Links type-wall** (`filter_links_for_caller`). The type wall extends from the
  primary row to its **edges**: a type-scoped caller's per-row `links` are filtered so
  an edge to a forbidden `user`/`feedback` memory does not leak that endpoint's id+type
  past the wall. Fail-closed (an unresolvable or unknown-kind endpoint is dropped).
- **Read-path redaction.** Every caller-facing title/snippet runs through
  `strip_secrets` again on the read path — defense-in-depth catching a secret that
  entered the DB by a path other than the write chokepoint (the free-form `Edit`
  exception on a wiki page). On a recall exception the client-facing payload is a fixed
  generic string; the raw error (which can embed an internal filesystem path) is logged
  locally and never crosses the boundary.

---

## 10.8 The project-agnostic boundary (the hard NFR)

ultra-memory ships **code only, no content**. The engine must import **nothing** from
any consumer — enforced by `test_no_hardcoded_paths` across both the package and the
Markdown publish surface. So the cross-store fabric is **fed, not coupled**:

- `wiki_sync(conn, wiki_roots, …)` takes consumer-fed root paths; it derives `topic`
  generically (the first path component under the root) and parses front-matter with a
  hand-rolled scanner — no topic-model import, not even PyYAML.
- `mirror_cross_store_links(conn, wiki_edges, …)` takes consumer-read edges; the engine
  never opens the wiki's graph DB.
- `save_memory(genesis_hook=…)` and `topic_router=…` are injectable callables; the
  consumer wires its own topic genesis / keyword map in.
- `maintain.run` reads the wiki roots from the `ULTRA_MEMORY_WIKI_ROOTS` env seam; unset
  ⇒ `wiki_sync` is skipped and a pure-memory deployment is byte-identically unaffected.

This is why `unified_query` **re-implements** the BM25 + cosine + RRF algorithm
engine-side rather than importing a consumer's `wiki_query` module: the agnostic
boundary forbids the import, so the algorithm is reproduced generically and parity is
checked by a consumer-side test that *can* import both.

---

## Where to go next

- The exact public surface — every function signature, every table, every migration:
  **[Chapter 11 — Reference: API & schema](./11-reference-api-schema.md)**.
- The rules for changing any of this — TDD, the invariants, the doc-discipline hook,
  running the suite: **[Chapter 12 — Contributing](./12-contributing.md)**.
- Back to the **[handbook index](./README.md)**.
