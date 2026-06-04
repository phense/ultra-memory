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
| `wiki_sync.py` | Tier-1 wiki→`unified_index` mirror: walk consumer-fed roots, upsert pages, reconcile orphans, embed into the shared cache. Project-agnostic, idempotent (sha-skip), fail-open, **a write-time redaction chokepoint**. |
| `unified_query.py` | The warm cross-store retrieval surface: `unified_recall` (memory cosine + knowledge BM25/cosine, fused with best-rank-per-backend RRF, × `outcome_weight`) + the fail-closed `topic_scope_from_env`. No LLM. |
| `knowledge_mcp.py` | The read-only MCP tool + the type axis of the access wall (`allowed_types_for`, `caller_class_from_env`) + the single DB-path resolver `db_path_from_env`. |
| `attribution.py` | The deterministic, no-LLM usage→outcome join: links the memories a session recalled to that session's outcome signal via `informed_by` edges. |
| `retention.py` | Bounds `session_events`: roll rows older than `keep_days` into `sessions.summary`, then delete (preserving any event an attribution edge still references). |
| `claude_cli.py` | The single OAuth-sanitised LLM chokepoint (the engine's read/recall path uses no LLM; this is for the maintenance beats and future agents). |
| `maintain.py` | The light Tier-1 maintenance slice: spool drain → prune → export → `wiki_sync`, all behind one ~20h throttle. No LLM. |
| `wiki_gateway.py` | The subclassable wiki **write**-gateway base class (the 6 override hooks + inherited verb materializers). See [Chapter 11 §`wiki_gateway`](./11-reference-api-schema.md#wiki_gateway). |
| `hooks/` | The three session hooks (`common`, `rehydrate`, `checkpoint`) — §10.4. |
| `maintenance/` | The heavy Tier-2 self-learning beats + their config + the session-lifecycle driver — §10.5. |
| `wiki_maintenance/` | The project-agnostic LLM-wiki curation engine the `wiki_maintenance` beat drives. |

---

## 10.4 The session hooks — the capture/replay edge

Two hooks bracket every interactive session, plus two more fire the maintenance beats.
**All are fail-open** (a hook error, or a not-yet-bootstrapped DB, never blocks the
operator) and **role-scoped** (a no-op for cron/subagent runs, gated on
`ULTRA_MEMORY_AGENT_ROLE` or a non-interactive SessionStart `source`).

The plugin registers them in `hooks/hooks.json`, dispatched through the path-free,
fail-open `hooks/um-hook.cmd` wrapper:

```
SessionStart (startup|resume|clear|compact):
  ├─ rehydrate   (sync)   → inject the ≤2k-char gist as additionalContext
  ├─ maintain    (async)  → the light Tier-1 slice (drain/prune/export/wiki_sync)
  └─ beats       (async)  → the throttled heavy Tier-2 self-learning beats
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

All hooks gate on `common.db_ready` (`meta.import_complete == '1'`): until the one-time
bootstrap import is done, they fail-open to the legacy path rather than touching an
un-migrated store.

---

## 10.5 The maintenance beats + the session-lifecycle driver

The self-learning loop is **four beats** plus two no-LLM housekeeping beats. The whole
thing is **autonomous by default, conservative in how it acts** — autonomy in *whether*
a beat fires, conservatism in *how* it changes the store (gentlest verb first, bounded,
archived-never-deleted, eval-gated). The earlier "ships-disabled / dry-run-first"
posture is superseded.

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
BEAT_ORDER = (session_ingest, consolidate, aggressive, synthesize, learnings, wiki_maintenance)
```

| Beat | Cadence (default) | LLM? | What it does |
|---|---|---|---|
| `session_ingest` | 24 h | OAuth | Mines each finished session's redacted transcript digest in ONE call → durable memories + `feedback` corrections + skill-tagged learnings. Runs **first** so its knowledge is present for the downstream beats. Opt-out via `SESSION_INGEST_ENABLE`. |
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
