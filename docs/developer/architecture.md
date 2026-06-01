# Architecture

## Canonical model

```
        data/memory.db (SQLite WAL)  =  WORKING TRUTH  (lives in the CONSUMER repo)
          | writes: ONLY via short-lived memory_lib calls (open → BEGIN IMMEDIATE → COMMIT → close)
          | reads : memory_query (+ a future read-only MCP)
          v
   memory_export.export_memory  (deterministic; one read snapshot; atomic swap)
          |
   data/memory_export/   (committed to the consumer's git)
     memory.dump.sql      = redacted text dump, carries user_version  → the ROLLBACK source
     memory.snapshot.db   = VACUUM INTO binary snapshot (gitignored)
     views/*.md + MEMORY.md = regenerated human/harness views
```

git tracks the **consistent text dump**, never the live `.db`. Rollback = restore
the dump → reopen. The markdown views are lossy (no embeddings/audit_log) and are
not a rollback source for those.

## Modules

| Module | Responsibility |
|---|---|
| `db.py` | Connection discipline (WAL, busy_timeout, FK, autocommit) + the forward-only, transactional, idempotent migration runner. |
| `migrations/*.sql` | Ordered `NNNN_name.sql`. `0001` initial schema, `0002` import-fidelity columns, `0003` harness slug + sort order, `0004` cross-store fabric (topic/provenance/outcome columns; `unified_index`/`knowledge_pins`/`agent_topic_bindings` tables; `links` sub-types). |
| `memory_lib.py` | **The only writer.** Every mutation: redact → `BEGIN IMMEDIATE`/`COMMIT` with retry+spool → `audit_log`. SP-3 adds the topic write path + `make_keyword_router`, `record_link`/`mirror_cross_store_links` (the `links` spine's first writer), the generalized `set_pinned(source_kind=…)`, and the gated `backfill_topic`. |
| `wiki_sync.py` | **(SP-3)** Tier-1 wiki→memory mirror: walk consumer-fed `wiki_roots`, upsert pages into `unified_index`, reconcile orphans, embed into the shared cache. Project-agnostic, idempotent (sha-skip), fail-open. Population only. The orphan prune is scoped to the synced **roots' path-prefix** (not to topics that still have files), so a page deleted when its topic goes fully empty is still pruned (its `unified_index` row + knowledge embedding) instead of lingering as a phantom recall hit. |
| `unified_query.py` | **(SP-3)** the warm cross-store retrieval surface: `unified_recall` (memory cosine + generic knowledge BM25/cosine, FU-4 RRF, × outcome_weight) + the fail-closed `topic_scope_from_env`. No LLM. The final fused sort uses a **deterministic total order** `(-weighted_score, (kind, key))` so a tie cannot reorder run-to-run under `PYTHONHASHSEED` (the rrf dict is built from a `set`); the secondary key changes only tie-order, never which score ranks where. |
| `redact_secrets.py` | Pure secret-stripper (the pre-persist + pre-export chokepoint). |
| `retrieval_core.py` | cosine, RRF, vector (de)serialise, content hash, embedding cache (single + batch), lazy fastembed (model cached in a persistent `$HOME` dir, never `$TMPDIR`). |
| `memory_query.py` | Read side: candidates → cosine → title boost → ranking signals → 1-hop links. No LLM. |
| `memory_import.py` | Parse the legacy markdown tree + `.remember/today-*.md` → writes via `memory_lib`. |
| `memory_export.py` | The rollback artifact: redacted dump + snapshot + views, atomic + skip-if-unchanged. |
| `claude_cli.py` | The single OAuth-sanitised LLM chokepoint (engine uses no LLM; this is for future agents). |
| `retention.py` | Bound `session_events`: roll rows older than `keep_days` into `sessions.summary`, then delete (spec §8 D11). |
| `hooks/common.py` | Fail-open, no-LLM hook helpers: role-guard, `db_ready` bootstrap probe, payload parse, session-id. |
| `hooks/checkpoint.py` | Stop hook: replay the raw transcript JSONL → record completed tasks as `task_done` events. Never blocks. |
| `hooks/rehydrate.py` | SessionStart hook: budgeted pure-SQL gist; shadow mode logs it, live mode injects `additionalContext`. |

## Session hooks (spec §9, §10) — the capture/replay edge

Two hooks bracket each interactive session, both **fail-open** (a hook error or a
not-yet-bootstrapped DB never blocks Peter) and **role-scoped** (no-op for
cron/subagent runs via `ULTRA_MEMORY_AGENT_ROLE` or a non-interactive SessionStart
`source`):

- **Stop → `checkpoint.run`**: derives `tasks_done` from the raw transcript JSONL
  on disk (not the compacted in-context view, so mid-session compaction can't
  truncate it) and records each as an idempotent `task_done` session event.
- **SessionStart → `rehydrate.run`**: composes a ≤2k-char gist from the DB
  (pinned rules, where-we-left-off, open follow-ups, hot memories) with no LLM and
  no embedder — `memory_lib` imports in ~15ms and pulls in no fastembed, so the
  hot path stays fast.

Both gate on `common.db_ready` (`meta.import_complete == '1'`): until the one-time
import is done they fail-open to the legacy `remember`/`MEMORY.md` path (spec §7.4).
The **shadow→cutover** rollout (spec §11) runs them against a throwaway
`memory_shadow.db` with injection suppressed until shadow-validated, then flips to
the canonical DB and retires the `remember` plugin.

## Write-ownership & concurrency (spec §6)

- **Single-writer discipline.** Each write opens its own short-lived transaction
  (`BEGIN IMMEDIATE` … `COMMIT`), wrapped by `_write_txn`, which:
  - retries on `SQLITE_BUSY`/`database is locked` with exponential backoff
    (the shared `_with_immediate_retry` loop),
  - rolls back defensively only when a transaction is actually active (no
    double-ROLLBACK masking the real error),
  - surfaces non-busy errors immediately,
  - on retry exhaustion spools the operation to `<db_dir>/memory_spool/<hash>.json`
    and raises `WriteSpooled` **loudly** — never a silent drop.
- The retry loop is extracted as `_with_immediate_retry` so the **maintenance
  writes** — `retention.prune_session_events` and `maintain._set_meta` — share the
  same bounded busy-retry instead of a bare `BEGIN IMMEDIATE` that raised on the first
  transient lock. They use no spool (idempotent maintenance writes; a final exhaustion
  rolls back and is caught by `maintain.run`'s fail-open `try/except`).
- **Spool drain.** `maintain.run` is the single serialized production caller of
  `replay_spool` (top of run, on its own connection), so a busy-casualty write
  self-heals on the next maintenance pass instead of rotting in `memory_spool/`.
- `record_access` uses an atomic `access_count = access_count + 1` (no
  read-modify-write → no lost updates; verified by a 20-thread test).
- `record_session_event` is idempotent via a content-addressed `event_key`.
- No `claude_cli` call ever happens inside a write transaction.

## Migration safety (spec §7.3)

The runner applies each pending migration's statements **and** its `user_version`
bump inside one explicit transaction (SQLite DDL + `PRAGMA user_version` are both
transactional), so a crash partway rolls back fully — version and schema never
desync. `ADD COLUMN` replay is tolerated (duplicate-column → already-applied). The
version is mirrored into `meta.schema_version`, which (unlike `PRAGMA user_version`)
survives `iterdump`, so the committed dump round-trips the version.

## Read path (spec §8, lean per D11)

Phase-1 memory retrieval is **embedding-cosine + title-index only**; BM25/RRF/
reranker are deferred behind a measured eval gate (the wiki side keeps full RRF).
`query_memories` reads candidates from one snapshot, embeds all cache-misses in a
single batched call + one write txn (`get_or_embed_batch`), ranks, **sorts +
truncates to the top_k ids first, THEN attaches 1-hop links** — so the per-row
`_links_for` SELECT runs only for the top_k survivors, not for every candidate
(bounding the per-recall link work to top_k without changing the ranking/scoring or
the returned dict shape). The embedder is always injected.

## Cross-store fabric (SP-3)

SP-3 makes the two stores — Session Memory (`memory.db`) and Expert Knowledge (the
consumer's wiki files) — behave as **one system without merging their canonical
storage**: files stay files, `memory.db` stays the memory store. The fabric is
deliberately on the **non-LLM warm/hot path** (no `claude` CLI on inflow or
retrieval). All of it lands in migration `0004` (additive) plus the engine APIs
above. The §7a self-improvement loop is **not** built — only its substrate (see
below).

```
   memory.db (canonical) ─┬─ memories(+topic,+created_by,+outcome_weight)
                          ├─ session_events(+outcome_signal)
                          ├─ links(+src_type,+dst_type)        ← THE cross-store edge spine
                          ├─ embeddings(target_kind ∈ {memory, knowledge})  ← one warm cache
                          ├─ unified_index(slug, topic, …, outcome_weight)  ← derived wiki mirror
                          ├─ knowledge_pins(slug, topic, pinned)
                          └─ agent_topic_bindings(agent_name, topic)
                                    ▲ wiki_sync (Tier-1, idempotent, no LLM)
   wiki/<topic>/**.md (FILES = canonical) ──┘   wiki/graph/graph.sqlite (wiki-internal; pure wiki↔wiki edges stay here)

   unified_recall(query, caller_class, agent_topics)
     = FU-4 RRF([ memory-cosine ranks, knowledge-BM25 ranks, knowledge-embed ranks ])
       · scoped by (topic ∈ agent_topics OR topic IS NULL) AND (type ∈ allowed_types_for(caller_class))
       · × outcome_weight (inert 1.0 until §7a)
```

### Project-agnostic boundary (the hard NFR)

The engine must import **nothing** from the consumer (enforced by
`test_no_hardcoded_paths` + the no-wiki-import test). So the fabric is fed, not
coupled:

- `wiki_sync(conn, wiki_roots, …)` takes consumer-fed root paths; it derives
  `topic` generically (first path component under the root) and parses front-matter
  with a hand-rolled scanner — no topic-model import, not even PyYAML.
- `mirror_cross_store_links(conn, wiki_edges, …)` takes consumer-read edges; the
  engine never opens `graph.sqlite`.
- `save_memory(genesis_hook=…)` and `topic_router=…` are injectable callables; the
  consumer wires `wiki_topics.ensure_topic` / its keyword map in.
- `maintain.run` reads the wiki roots from the `ULTRA_MEMORY_WIKI_ROOTS` env seam;
  unset ⇒ `wiki_sync` is skipped and a pure-memory deployment is byte-identically
  unaffected.

**Decision D-S6** (the auditable why): the spec said `unified_recall` would *reuse*
`wiki_query`'s backends + FU-4 RRF, but `wiki_query` is a Trading-side module the
agnostic boundary forbids importing. So `unified_query` re-implements the
*algorithm* engine-side — a generic in-module BM25 + cosine over `unified_index`,
fused with a generic re-implementation of best-rank-per-backend RRF (k=60). True
cross-codebase byte-parity with `wiki_query` is **deferred to an SP-5 Trading-side
test** (which can import both). The memory-store byte-identity is enforced here.

### The topic / pin / scope model

- **`topic` is nullable.** `NULL` = "cross-topic / visible to everyone"; a non-NULL
  topic walls the row. Operational `user`/`feedback` rows always stay `NULL` (D11) —
  they apply in every topic, and the *type*-scope (fail-closed) still hides them
  from subagents. Topic ⟂ type. An un-topiced corpus stays fully visible (no
  retrieval regression): `query_memories(topic=…)` filters `topic = ? OR topic IS
  NULL`.
- **One pin space, two stores.** Memory pins use `memories.pinned`; knowledge pins
  (a wiki page has no `memories` row) use `knowledge_pins`. `set_pinned(source_kind
  ∈ {memory, knowledge})` writes the right one; `rehydrate.build_gist` unions both
  into the single `## Pinned rules` gist section. A back-compat `id=` shim keeps
  the SP-1 `/memory-pin` + spooled records working.
- **Fail-closed role + topic scope.** The access wall composes two orthogonal
  axes by AND: `visible(fact) ⟺ (topic ∈ agent_topics OR topic IS NULL) AND (type ∈
  allowed_types_for(caller_class))`. `topic_scope_from_env` resolves
  `agent_topics` from `ULTRA_MEMORY_CALLER_TOPIC` + `agent_topic_bindings`, and
  fails closed: **no binding ⇒ the empty set**, so a subagent with no topic binding
  sees only `topic IS NULL` operational memories of its allowed types — and **zero
  topiced knowledge**. The orchestrator / trusted CLI passes `agent_topics=None`
  (all-topics sentinel). The degraded mode (per-subagent identity unresolved, SP-0
  spike #7) is safe: sees less, never more.

### The `links` spine vs the wiki graph (D6)

`links` is the **cross-store** edge spine (memory↔memory, memory↔knowledge);
`wiki/graph/graph.sqlite` stays the wiki-internal typed graph. A **one-way** mirror
(`mirror_cross_store_links`) lifts only the edges that *cross* stores into `links`;
pure wiki↔wiki edges stay in `graph.sqlite` so the 5k-edge wiki graph is not
duplicated into `memory.db`. `record_link` is idempotent on the edge key
`(src_kind, src_id, predicate, dst_kind, dst_id)` — enforced in code (SELECT-then-
UPDATE-or-INSERT) because the pre-existing table has no UNIQUE on that key. This is
the read path north-star Risk §14.8 flagged as never-exercised; Stage 0 verified
`_links_for` against populated rows before the writer shipped.

### Self-improvement substrate — columns only (the loop is SP-6/SP-7)

SP-3 lands the **columns and relations** the §7a loop will need, all **inert** this
cycle:

- `memories.created_by` (provenance gate input): stamped `human` by the CLI /
  `/memory-*` verbs (the safe-immutable default), `import` by the bootstrap
  importer. **`agent` and `background_review` have no engine write site yet** — they
  are reserved values an agent-initiated save / a future Tier-2 maintenance write
  will set; the SP-7 provenance gate may auto-edit only those non-`human` rows.
  Provenance is **never downgraded on re-save**: an `import`/`agent`/`background_review`
  re-save over a `human` row preserves `human`. The bootstrap **importer is
  edit-safe**: a legacy re-import SKIPS any row whose live `created_by` is `human`
  (it neither reverts the human-edited body nor demotes the provenance) — mirroring
  the deliberate status/pin preservation.
- `session_events.outcome_signal` (the deterministic capture hint) is accepted by
  `record_session_event` but **set by no engine writer** — the Stop-hook capture
  that enqueues `skill_learning_candidate` rows is **Trading-side** (it lives in the
  consumer repo, not this engine), and is paired with this commit, not shipped here.
- `memories.outcome_weight` / `unified_index.outcome_weight` default 1.0 and are
  multiplicatively inert in `unified_recall`; no writer changes them this cycle.
- `export_learnings_projection` materializes the read-only per-skill `Learnings.md`
  **projection** (D14/D15) from the DB system-of-record, the way `export_memory`
  regenerates the views. The loop that *feeds* it (capture-queue drain,
  consolidation judge, auto-edit) is **not built** — that is SP-6 and the gated
  SP-7.

The `procedures` table stays unwired dead weight (Fork A / D12): captured
procedures route through `memories` with `node_type='procedure'` for the full
lifecycle (pinning, links, topic, scope, FSM); dropping the empty table is deferred
to SP-5.

## Secret handling (spec §7.5)

`strip_secrets` runs on every persisted text field at the write chokepoint, and
again over the **entire** export dump (covering columns like `links.evidence` /
`meta.value` / `sessions.summary` that no write-path writer redacts yet). Patterns
cover Anthropic/GitHub/AWS/Google/Slack/Stripe/SendGrid/Twilio keys, JWTs, bearer
tokens, PEM private-key blocks, URI userinfo, and `keyword=value` assignments —
the last only when the value is credential-shaped (quoted or digit-bearing), so
hyphen-joined prose is never mangled.

## OAuth-only (hard rule)

`claude_cli.run_claude` strips Claude-Code env markers, raises `OAuthViolation` if
`ANTHROPIC_API_KEY` is set or `CLAUDE_CODE_OAUTH_TOKEN` is missing, and shells out
to the `claude` CLI. Never the `anthropic` SDK / `api.anthropic.com` /
`messages.create` / `cache_control`. Inject a `runner` for tests.

## What's built vs future

Built + tested: `db`, `migrations` (through `0004`), `memory_lib`,
`redact_secrets`, `retrieval_core`, `memory_query`, `memory_import`,
`memory_export`, `claude_cli`, `retention`, `maintain`, the session `hooks`
(`common`, `checkpoint`, `rehydrate`), the `knowledge_mcp` read tool, and the SP-3
cross-store fabric (`wiki_sync`, `unified_query`, the `links` spine, cross-store
pinning, the topic write path, and the §7a substrate **columns**).

Future:
- **§7a self-improvement loop (SP-6/SP-7).** SP-3 shipped the substrate columns
  (`created_by`, `outcome_signal`, `outcome_weight`) and the `validated_as` link
  relation as **inert**. The loop itself — the capture-queue drain, the
  consolidation judge, the outcome-weight aggregate, and the gated auto-edit /
  self-reversion — is not built. `agent`/`background_review` provenance and a
  non-1.0 outcome weight have no writer yet.
- **The D4 topic backfill** is a gated one-time data step (`backfill_topic`); the
  DDL is live but the row-touch awaits sign-off (spec §10).
- **Cross-codebase wiki_query parity** is deferred to an SP-5 Trading-side test
  (D-S6); **doc consolidation + the generic `using-knowledge` split** are SP-5;
  the consumer-side `wiki/SCHEMA.md` / `CLAUDE.md` updates land with the post-merge
  Trading change, not here.
- The **live** bootstrap import + shadow→cutover wiring behind `meta.import_complete`
  and the per-subagent topic-identity mechanism (SP-0 spike #7; the env-var
  fallback is the locked interim) remain consumer-side / open.
