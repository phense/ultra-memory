# 11. Reference — API & schema

> This is the contract. Everything in [Chapter 10 — Architecture](./10-architecture.md)
> is *why*; this chapter is *exactly what*. If you are wiring ultra-memory into a new
> consumer, writing a test, or reviewing a change, this is the page you keep open: every
> public function with its real signature, every table with its real columns, and every
> migration in order.

Two conventions hold across the whole engine and are worth stating once:

- **The caller owns the connection and supplies the time.** Functions take a `conn` and
  an explicit `ts` / `now_ts`. There are no module-level globals, no ambient clock
  reads, no hidden I/O. That is what makes the suite deterministic and offline.
- **The embedder is injected.** Anything that ranks takes an `embedder`
  (`list[str] -> list[list[float]]`). In tests it's a fake; in production it's the lazy
  fastembed loader from `retrieval_core`.

The canonical model is `BAAI/bge-small-en-v1.5`, 384 dimensions
(`retrieval_core.EMBED_MODEL` / `EMBED_DIM`).

---

## 11.1 The public engine API

### `db`

- `connect(db_path, *, busy_timeout_ms=30000) -> Connection` — WAL, `busy_timeout`,
  `foreign_keys=ON`, `isolation_level=None` (autocommit), `row_factory=Row`.
- `migrate(conn, migrations_dir) -> int` — apply each pending `NNNN_*.sql` (file
  version > `user_version`) in **one transaction**: statements + the `user_version` bump
  + the `meta.schema_version` mirror. Tolerates `ADD COLUMN` replay (idempotent).
  Returns the new version.

### `memory_lib` — the only writer

Every function here goes through `_write_txn` (retry + spool + `audit_log`).

- `open_memory_db(path, migrations_dir=_MIGRATIONS) -> Connection` — connect + migrate.
  The caller closes.
- `save_memory(conn, *, id, type, title, body, ts, …, topic=None, topic_router=None, genesis_hook=None, caller_class=None, created_by="human") -> id`
  — redact → upsert → audit. The verb most consumers actually call.
  - `topic` is the topic to persist. If omitted and a `topic_router` is supplied, the
    **deterministic, no-LLM** router assigns one; abstention → `NULL`. `user`/`feedback`
    rows **always** stay `NULL`, even if a `topic=` is passed.
  - `topic_router` is a caller-supplied callable; build a generic one with
    `make_keyword_router({topic: (kw, …)})` (whole-word, insertion-order priority).
  - `genesis_hook(topic)` is an optional, injectable, best-effort callback fired only on
    a resolved non-NULL topic, **after** the durable write. A raising hook never aborts
    the write (fail-open). The engine has no wiki dependency.
  - `created_by` is the write's provenance: `human` (the safe-immutable default) /
    `agent` / `background_review` / `import` / `backfill_import`. **Never downgraded:** a
    re-save over an existing `human` row with a non-`human` value preserves `human`.
    **It gates MUTABILITY, not synthesis visibility** — the self-correct beat may rewrite
    only `('agent','background_review')` rows, but synthesis selects induction clusters
    by `node_type='learning'` (provenance-agnostic).
- `make_keyword_router(keyword_map) -> router` — a deterministic, content-free fallback
  topic router from `{topic: (kw, …)}`. Whole-word match, insertion-order priority;
  abstains to `None`; `user`/`feedback` always abstain. No LLM / wiki / network.
- `record_link(conn, *, src_kind, src_id, predicate, dst_kind, dst_id, src_type=None, dst_type=None, evidence=None, confidence=None, ts)`
  — upsert a cross-store edge, **idempotent on the edge key**
  `(src_kind, src_id, predicate, dst_kind, dst_id)` (enforced in code; the table has no
  UNIQUE on it). The first and only writer of the `links` spine.
- `mirror_cross_store_links(conn, wiki_edges, *, ts) -> {mirrored, skipped_wiki_internal}`
  — lift **cross-store** wiki-graph edges into `links` via `record_link`. `wiki_edges`
  is consumer-fed; only edges crossing stores are mirrored; pure wiki↔wiki edges are
  skipped. Idempotent.
- `set_pinned(conn, *, source_kind=None, source_id=None, id=None, pinned, ts, reason="manual pin")`
  — set/clear a pin in the one cross-store pin space. `source_kind='memory'` flips
  `memories.pinned`; `source_kind='knowledge'` upserts `knowledge_pins`. A legacy
  `set_pinned(id=…)` shim still works (treated as `source_kind='memory'`).
- `set_status(conn, *, id, status, ts, reason)` — set `memories.status`, validated
  against `('active','redirect','deleted','quarantined','reverted')`. Any non-`active`
  status drops the row out of recall with **no recall-query change**. Enforces no
  "protected row" policy — the safety wall is the caller's job.
- `set_outcome_weight(conn, *, id, weight, ts, reason="outcome aggregate")` — the first
  writer of `memories.outcome_weight` (`1.0` neutral; `<1.0` demotes, `>1.0` promotes
  recall rank). The self-correct beat's EWMA regression signal lands here.
- `record_session_event(conn, *, session_id, kind, title, ts, detail=None, files=None, refs=None, session_fields=None, outcome_signal=None) -> event_key`
  — ensure the session row, append the event idempotently (UNIQUE `event_key`).
  `outcome_signal` is an optional deterministic per-event hint; it is **payload,
  excluded from `event_key`**, so events differing only in the signal still dedupe.
  `title`/`detail` and every string in `files`/`refs` are redacted before persist (the
  `event_key` is keyed on the RAW text so a rule change can't un-dedupe a replay).
- `event_id_for_key(conn, event_key) -> int | None` — resolve the content-addressed
  string key back to the integer `session_events.id` the attribution edge stores.
  Read-only.
- `record_access(conn, *, target_kind, target_id, ts, context=None, session_id=None, rank=None)`
  — append to `access_log` + atomic `access_count += 1` for memory targets. `session_id`
  and `rank` are the attribution substrate (which session recalled this, at what fused
  rank); both logging-only, no ranking effect.
- `session_id_from_env(env) -> str | None` — resolve the session id: explicit
  `ULTRA_MEMORY_SESSION_ID` → else the ambient `CLAUDE_CODE_SESSION_ID` → else `None`.
- `consolidate(conn, *, loser_id, canonical_id, reason, ts)` — redirect-stub
  (`status='redirect'`, `supersedes=canonical`).
- `delete(conn, *, id, reason, tier, ts)` — soft tombstone (`status='deleted'`).
- `WriteSpooled` — raised when a write is spooled after retry exhaustion.
- `_write_txn` / `_with_immediate_retry` — the §6 retry/spool discipline every writer
  uses (internal, but documented because it *is* the discipline).

### `retrieval_core`

- `EMBED_MODEL` = `"BAAI/bge-small-en-v1.5"`, `EMBED_DIM` = 384.
- `cosine(a, b) -> float` — 0.0 if either vector is zero.
- `cosine_search(query_vec, items, *, top_k=None) -> [(id, score)]`.
- `rrf_fuse(rankings, *, k=60) -> [(id, score)]` — reciprocal-rank fusion.
- `pack_vector(vec) -> bytes` / `unpack_vector(blob, dim=EMBED_DIM) -> [float]`.
- `content_sha256(text) -> str` — None-safe cache-invalidation hash.
- `get_or_embed(conn, …)` / `get_or_embed_batch(conn, items, …)` — cached embedding
  (recompute on content change; a miss is one write txn; the batch form does all misses
  in one embedder call).
- `default_embedder(model_name=EMBED_MODEL) -> callable` — the lazy fastembed loader;
  raises a clear `RuntimeError` if the `[retrieval]` extra is absent.
- `persistent_cache_dir() -> str` — the fastembed model-cache dir, anchored under
  `$HOME` (never the OS temp dir, which macOS purges).

### `memory_query`

- `query_memories(conn, query, *, embedder, top_k=5, dim=EMBED_DIM, include_statuses=("active",), include_types=None, now_ts=None, staleness_days=90, topic=None) -> [dict]`
  — cosine rank + word-bounded title boost (+0.5), ×`strength`, −staleness penalty;
  attaches 1-hop `links`. Sorts + truncates to top-k *first*, then attaches links.
  Returns `{id, title, type, status, score, stale, links}`. `topic`, when given, scopes
  to `topic = ? OR topic IS NULL` (a topiced caller still sees cross-topic operational
  rows). **The memory backend ranks on embedding-cosine only** — `embedder=None` on a
  non-empty in-scope set raises a clear `ValueError`.

### `memory_import`

- `import_memory_dir(conn, memory_dir, *, index_path=None, ts) -> count` — glob `*.md`
  (excluding `MEMORY.md`), upsert each, set `file_slug`/`sort_order`/mtimes and
  `created_by='import'`. Idempotent and **edit-safe** (skips live `human` rows).
- `import_today_file(conn, text, *, day) -> (count, warnings)` — parse `## HH:MM` blocks
  into `legacy-<day>` session events; never crashes.
- `split_frontmatter` / `parse_memory_index` — the no-YAML parsers for the legacy
  format.

### `memory_export`

- `export_memory(conn, out_dir, *, ts, snapshot=True) -> bool` — read snapshot →
  redacted `memory.dump.sql` (carries `user_version`) → atomic `VACUUM INTO` snapshot →
  `views/<file_slug>.md` + `views/MEMORY.md`. Atomic throughout (tmp → replace).
  Returns `False` if unchanged (the change-hash excludes access telemetry so
  reinforcement churn never drives a commit, but **includes** `outcome_weight` /
  `outcome_signal` so an audited weight/signal write re-exports).
- `export_learnings_projection(conn, path, *, skill_tag, title=None) -> int` —
  regenerate a `Learnings.md`-style projection from the store (active `memories` whose
  `index_hook == skill_tag`, ordered deterministically). Both args consumer-supplied;
  the DB is the system of record. Written atomically.
- `render_union_blend_block(conn, *, hooks, now, cap=20, halflife_days=45) -> str` —
  the recency-decayed, outcome-weighted markdown block a generated skill's managed
  region holds. Time-dependent by design (takes an explicit `now`).

### `wiki_sync`

- `wiki_sync(conn, wiki_roots, *, embedder=None, rebuild=False, ts) -> {upserted, skipped, pruned, embedded, embedded_signal, errors}`
  — walk each `<root>/<topic>/**/*.md`, upsert each page into `unified_index`,
  reconcile orphans (scoped to the topics synced this call), embed changed pages into
  the shared cache. **Project-agnostic** (roots are consumer-fed; no topic-model
  import, not even PyYAML), **idempotent** (sha-skip), **fail-open** (a missing
  root/unreadable page increments `errors` and continues), and a **write-time redaction
  chokepoint** (`title`/`snippet`/`bm25_text`/`frontmatter` pass through `strip_secrets`
  before insert). `rebuild=True` forces a full re-populate (the one-pass `bm25_text`
  backfill).
- `extract_signal_text(body) -> str | None` — the optional `## Signal` H2 body (the
  Recall-Reflex observable); embedded as a distinct `knowledge_signal` channel
  (`embedded_signal` count; pruned on orphan AND on signal-removed-on-edit). See the
  `recall` section + `wiki/SCHEMA.md`.
- **CLI:** `python -m ultra_memory.wiki_sync [--roots R] [--db DB] [--rebuild] [--no-embed]`
  — populate/refresh the mirror from a consumer cron (no roots → rc 0 no-op; fail-soft to
  BM25-only if no embedder).

### `unified_query`

The cross-store **warm** retrieval surface — one ranked list spanning `memories` + the
`unified_index` knowledge mirror, scoped by `(type × topic × caller_class)`, fused with
best-rank-per-backend RRF (k=60), weighted by `outcome_weight`. **No LLM.**

- `unified_recall(conn, query, *, caller_class, agent_topics, embedder=None, top_k=5, dim=EMBED_DIM, now_ts=None, ts=None, audit=True, include_memory=True) -> [dict]`
  — resolve the type wall + topic scope, rank the memory backend (`query_memories`) +
  the knowledge backends (generic BM25 + cached-vector cosine + the optional **`## Signal`**
  cosine backend, `target_kind='knowledge_signal'` — the Recall-Reflex boost), fuse,
  × `outcome_weight`, audit each hit via `record_access`. `include_memory=False` skips the
  memory backend entirely (no `query_memories` call → no embedder requirement, and **no**
  `user`/`feedback`/memory can surface — the knowledge-only path the engineering hook uses).
  `agent_topics`: a set ⇒ topic-scoped; `None` ⇒
  all topics (orchestrator); the **empty set** ⇒ fail-closed (only `topic IS NULL`
  operational memories of allowed types, **zero topiced knowledge**). Knowledge hits
  carry `source_kind='knowledge'` + `slug, topic, title, page_type, snippet, path,
  score`; memory hits carry `source_kind='memory'` + the `query_memories` fields.
  Read-path `strip_secrets` and the links type-wall both apply.
- `topic_scope_from_env(env, conn=None, *, agent_name=None) -> set` — the **fail-closed**
  topic-scope resolver: union of `ULTRA_MEMORY_CALLER_TOPIC` + `agent_topic_bindings`
  rows; no binding ⇒ the empty set.

### `recall` — the Recall-Reflex primitive

> Recognise a situation → recall what you know about it → act informed.

The single public entry point that turns the warm retrieval surface into a reflex used by
every consumer (the engineering hook + the trading observation surfaces).

- `recall(signal_text, *, top_k=5, caller_class="subagent", agent_topics=None, db_path=None, embedder=None, build_embedder=True, knowledge_only=False, exclude_page_types=("theme-index","master-index","index","redirect"), conn=None, now_ts=None) -> [dict]`
  — a thin, **fail-open** (`[]` on any error) wrapper over `unified_recall`. Defaults to
  `caller_class="subagent"` (SAFE_TYPES) so a main-session caller cannot leak `user`/`feedback`.
  `knowledge_only=True` → `include_memory=False` (wiki-only; privacy-safe + no embedder needed).
  `build_embedder` lazily builds a fastembed embedder unless one is passed (fail-soft to BM25-only).
  Navigational page-types are filtered out (over-fetch then trim). Hits are the uniform
  `{source_kind, slug|id, title, snippet, path?, page_type?, topic?, score}`.
- **CLI:** `python -m ultra_memory.recall "<signal>" [--top N] [--topic t,u] [--caller-class C] [--no-embed] [--json]` — always rc 0.

**The `## Signal` channel.** `wiki_sync.extract_signal_text(body)` extracts an atomic's optional
`## Signal` H2 (the observable for recall, see `wiki/SCHEMA.md`). `wiki_sync` embeds it as a distinct
`knowledge_signal` cache row; `unified_recall` fuses it as a separate RRF backend (the boost); and
`detect_dedup` compares it as a second dedup axis (`signal_vecs`, max(mechanism, signal) cosine — the
"same observable, different prose" merge the mechanism axis misses).

**The `UserPromptSubmit` hook** (`ultra_memory.hooks.recall_prompt`, verb `recall` in `um-hook.cmd`):
on a concrete error signature (`detect_signature`), it calls `recall(knowledge_only=True,
build_embedder=False)` and injects the hits as `additionalContext`. Tier-2 only, conservative matcher,
fail-open, `<=3` hits, kill-switch `RECALL_HOOK_DISABLE`. The generic method is the `recall-reflex` skill.

### `attribution`

The deterministic, no-LLM usage→outcome join (imports only stdlib + `memory_lib`).

- `recalled_units_for_session(conn, *, session_id) -> [{'id','rank'}]` — the session's
  recalled memory units (non-NULL `rank`, ordered by `(rank, id)`). Fail-closed-to-empty.
- `apply_attribution_policy(rows, *, policy='top_k', k=1) -> [id]` — **pure** (no DB).
  `'all'` = every distinct id by best rank; `'top_k'` = the `k` lowest-rank distinct ids.
  Unknown policy raises `ValueError`.
- `attribute_usage(conn, *, session_id, outcome_event_id, ts, policy='top_k', k=1) -> int`
  — write an `informed_by` edge from the outcome `session_event` to each selected
  recalled memory; returns the edge count. Idempotent, **fail-open** (any error ⇒ 0;
  it runs in a Stop hook and must never wedge a session).

### `redact_secrets`

- `strip_secrets(text) -> text` — redact `<private>…</private>`, PEM blocks, URI
  userinfo, provider keys (Anthropic / GitHub / AWS / Google / Slack / Stripe /
  SendGrid / Twilio), JWTs, bearer tokens, and credential-shaped `keyword=value`.
  Conservative: hyphen-joined prose survives; `None`/`""` pass through.

### `claude_cli`

- `run_claude(prompt, *, model, system=None, claude_bin="claude", timeout=120, runner=subprocess.run, env=None) -> str`
  — the OAuth-sanitised CLI call. Raises `OAuthViolation` (API key present / OAuth token
  missing) or `ClaudeCliError` (nonzero exit). Inject `runner` in tests.

### `retention` & `maintain`

- `prune_session_events(conn, *, keep_days, ts) -> int` — roll old `session_events` into
  `sessions.summary`, then delete, in one transaction. **Excludes** any event still
  referenced by an attribution edge (it is the EWMA fold's evidence).
- `maintain.run(conn, *, out_dir, ts=None, keep_days=90, force=False, wiki_roots=None, embedder=None, env=None) -> dict`
  — the throttled (~20h) Tier-1 slice: drain the spool → prune → export → `wiki_sync`
  (when roots are configured). Fail-open. With no roots, byte-identical to a pure-memory
  deployment.

### `knowledge_mcp` — the read-only MCP

- `allowed_types_for(caller_class)` / `caller_class_from_env(env)` — the **type axis** of
  the access wall. Trusted (`orchestrator`/`owner`) → all types; else `SAFE_TYPES =
  (project, reference)`, fail-closed.
- `db_path_from_env(env) -> Path` — the single source of truth for the `memory.db` path
  (the MCP, all hooks via `resolve_db_path`, and `maintain` route through it).
  Resolution, **never cwd, never project-local**: explicit `ULTRA_MEMORY_DB` → else the
  fixed global `~/.ultra-memory/memory.db`.
- `filter_links_for_caller(conn, links, *, caller_class)` — the links type-wall:
  fail-closed, drops any edge whose memory endpoint resolves to a forbidden type.
- `run_query_tool(arguments, *, conn, embedder, caller_class, …, agent_topics=…)` — the
  MCP tool handler. Routes to `unified_recall` when `agent_topics` is supplied, else the
  legacy pure-memory `knowledge_recall`. Never raises (returns a structured `{"error":
  …}`); on an exception the client-facing payload is a fixed generic string.

### `hooks`

- `hooks.common`: `agent_role_optout`, `resolve_db_path`, `db_ready`, `read_payload`,
  `session_id_of` — the shared, fail-open, no-LLM, no-write helpers.
- `hooks.checkpoint`: `completed_tasks`, `has_material_work`, `run`, `main` — the Stop
  hook (records `task_done` events; always returns `{}`).
- `hooks.rehydrate`: `build_gist(conn, *, budget_chars=2000)`, `run`, `main` — the
  SessionStart hook (the pure-SQL, no-LLM gist; pinned rules + where-we-left-off + open
  follow-ups + hot memories, structure-injection-sanitized, pins exempt from the budget
  cut).

### `wiki_gateway`

The subclassable wiki **write**-gateway base class. A consumer subclasses `WikiGateway`
and overrides only the 6 project-specific hooks; the verb materializers, embedding/cosine
machinery, the fcntl write-lock, `strip_secrets`, and the audit row are inherited. Wire a
subclass via `wiki_gateway = "<module>:<Class>"` in `<project>/.ultra-memory/config.toml`
(unset → the built-in turnkey gateway).

**The 6 override hooks** (each defaults to `super()`):

| Hook | Default |
|---|---|
| `route(claim) -> Path` | `<topic>/concepts/<slug(title)>.md` |
| `theme_for(claim) -> str` | `claim["theme"]` or `"general"` |
| `render_frontmatter(claim) -> dict` | `{"type": "mechanism", "title": claim["title"]}` |
| `dedup_check(text, topic) -> match\|None` | OFF (always create) |
| `derive_anchor(claim, existing=None) -> str\|None` | `None` (standalone atomic) |
| `confidence_label(claim) -> str` | `"Standard"` |

**Inherited verbs** (do not re-implement): `create_page`,
`append_validation_log_entry`, `register_in_theme_index`, `log`.

**CLI verbs** (`python -m ultra_memory.wiki_gateway <verb>`): `create-page`,
`append-validation-log`, `register-index`, `log`, and
`scaffold --out <path> --class-name <Name> --topic <topic>` (emits a ready-to-edit
subclass stub — deterministic, no LLM).

---

## 11.2 The database schema

`data/memory.db` — SQLite, **WAL** journal mode, `isolation_level=None` (autocommit)
with explicit `BEGIN IMMEDIATE`/`COMMIT` around writes, `foreign_keys=ON`,
`busy_timeout=30000`.

Version is tracked by `PRAGMA user_version` and mirrored in `meta.schema_version` (the
mirror survives `iterdump`, so the committed dump round-trips the version).

### Migrations (`0001` … `0008`)

Every migration is **additive / forward-only**: every statement is `ADD COLUMN` or
`CREATE … IF NOT EXISTS`, no `DROP`/`RENAME`. A restore or replay against an
already-shaped DB is a no-op. The one *data* step (the topic backfill) is a separate,
gated code path, never in a `.sql`.

| File | `user_version` | Adds |
|---|---|---|
| `0001_initial.sql` | 1 | all base tables + 3 indexes |
| `0002_import_fidelity.sql` | 2 | `memories.description`, `memories.index_hook`, `memories.node_type` |
| `0003_harness_slug.sql` | 3 | `memories.file_slug`, `memories.sort_order` |
| `0004_cross_store_fabric.sql` | 4 | the cross-store fabric: `memories.topic`/`created_by`/`outcome_weight`, `session_events.outcome_signal`, `links.src_type`/`dst_type`; new tables `unified_index`, `knowledge_pins`, `agent_topic_bindings`; indexes `idx_unified_topic`, `idx_links_dst` |
| `0005_unified_index_bm25_text.sql` | 5 | `unified_index.bm25_text` — the FULL collapsed page body for the knowledge-side BM25 document (`snippet` stays the ~400-char display preview) |
| `0006_access_log_session_id.sql` | 6 | `access_log.session_id` — which session recalled a unit (the attribution substrate; `NULL` = not attributable) |
| `0007_access_log_rank.sql` | 7 | `access_log.rank` — the unit's 1-based position in the fused recall list (logging only; feeds the top-k attribution policy) |
| `0008_access_log_session_index.sql` | 8 | the composite index `idx_access_log_session(session_id, target_kind)` covering the session-end attribution lookup over the never-pruned `access_log` |

### Tables

#### `memories` — the durable typed notes

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | stable identity = the slug (hyphenated); used for cross-links |
| `type` | TEXT | `feedback` / `project` / `reference` / `user` … |
| `title`, `body` | TEXT | redacted at write time |
| `description` | TEXT | one-line summary |
| `index_hook` | TEXT | the index "— hook"; also the skill tag for `node_type='learning'` rows |
| `node_type` | TEXT | `'memory'` (default), `'procedure'`, `'learning'` (the synthesis induction unit, selected provenance-agnostically), `'generated_skill'` (excluded from induction to prevent self-seeding) |
| `file_slug` | TEXT | the harness filename stem; drives the export filename — not derivable from `id` |
| `sort_order` | INTEGER | preserves curated order on export |
| `created_at`, `updated_at` | TEXT | canonical tz-aware UTC `%Y-%m-%dT%H:%M:%SZ`; drives staleness |
| `last_verified`, `valid_until` | TEXT | reserved |
| `strength` | REAL | default 1.0; multiplies relevance |
| `access_count` | INTEGER | derived; atomically incremented by `record_access` |
| `last_accessed` | TEXT | |
| `status` | TEXT | `active` / `deleted` / `redirect` / `quarantined` / `reverted` (default `active`); only `active` is recalled by default |
| `supersedes` | TEXT | canonical id when `status='redirect'` |
| `pinned` | INTEGER | default 0 |
| `topic` | TEXT | **(0004)** nullable. `NULL` = cross-topic / visible-to-all; a non-NULL topic walls the row. `user`/`feedback` rows stay `NULL`. |
| `created_by` | TEXT | **(0004)** provenance; `NOT NULL DEFAULT 'human'`. Values: `human` / `agent` / `background_review` / `import` / `backfill_import`. **Gates mutability** (the self-correct beat may auto-edit only `agent`/`background_review`), **not synthesis visibility**. |
| `outcome_weight` | REAL | **(0004)** `NOT NULL DEFAULT 1.0`. Multiplied into the unified-recall score; written by `set_outcome_weight`. |

#### `sessions`

`id` (UUID) PK, `started_at`, `ended_at`, `status`, `branch`, `cwd`, `first_prompt`,
`summary`, `commit_shas`.

#### `session_events` — the append-only episodic log

`id` PK, `session_id` FK→`sessions`, `ts`, `kind`, `title`, `detail`, `files`, `refs`,
`resolved`, and `event_key TEXT UNIQUE` = `sha256(session_id|ts|kind|title|detail)` for
idempotent `INSERT OR IGNORE`. `title`/`detail` and every string in `files`/`refs` are
redacted at write time (the `event_key` is keyed on the RAW text). **(0004)**
`outcome_signal TEXT` — an optional deterministic outcome hint (`tests_passed` /
`trade_win` / `commit_landed` …); payload, not part of `event_key`. Written by the
**consumer-side** Stop-hook capture; the attribution join reads it as the EWMA fold's
evidence. An event referenced by an attribution edge is excluded from retention pruning.

#### `embeddings`

`PRIMARY KEY (target_kind, target_id, model_name)`, `dim`, `vector` (float32 BLOB),
`content_sha256` (cache invalidation). The `(model_name, dim)` invariant is enforced
loudly. `target_kind ∈ {memory, knowledge}` — one warm cache for both stores.

#### `audit_log`

`id` PK, `ts`, `op` (`save`/`redirect`/`soft_delete`/`pin`/`verify`/`link`/
`outcome_weight`/`set_status`/…), `target_kind`, `target_id`, `reason`, `prior_state`
(a JSON snapshot before the change).

#### `access_log` — the append-only reinforcement source of truth

`id`, `target_kind`, `target_id`, `ts`, `context`, plus **(0006)** `session_id` and
**(0007)** `rank` — the attribution substrate (which session recalled this, at what
fused rank). Both logging-only; no ranking effect. **(0008)** the composite
`idx_access_log_session(session_id, target_kind)` indexes the session-end attribution
lookup so it stops full-scanning this never-pruned, fastest-growing table.

#### `links` — the cross-store edge spine

`src_kind`, `src_id`, `predicate`, `dst_kind`, `dst_id`, `evidence`, `confidence`,
`created_at`, plus **(0004)** `src_type` / `dst_type` (the within-kind sub-type so a
reader can filter without a join). `*_kind` is the store side (`memory`/`knowledge`).
The edge **key** is `(src_kind, src_id, predicate, dst_kind, dst_id)`; there is **no
UNIQUE on it**, so idempotency is enforced in code by `record_link`. The autonomous
beats write two predicate families: **`validated_as`** (every graduation links the
source lesson to the durable unit it matured into — the reversion provenance trail) and
**`informed_by`** (the usage-outcome edge from a session's outcome event to the memories
it recalled; `src_id` is the integer `session_events.id` as a string).

#### `unified_index` *(0004)* — the derived wiki mirror

A **rebuildable** mirror of the wiki pages, kept beside the memory tables so unified
recall spans both stores on one warm connection. The files stay canonical; `wiki_sync`
regenerates this table (a redaction chokepoint). `slug TEXT PK`, `topic`, `page_type`,
`title`, `snippet` (~400-char display preview), `frontmatter` (JSON), `path`,
`content_sha256`, `outcome_weight REAL NOT NULL DEFAULT 1.0` (reserved knowledge-side
weight), `updated_at`, **(0005)** `bm25_text TEXT` (the FULL collapsed body used as the
BM25 document). Knowledge embeddings live in the shared `embeddings` table
(`target_kind='knowledge', target_id=slug`).

#### `knowledge_pins` *(0004)* — the knowledge side of the one pin space

A wiki page has no `memories` row, so its pin lives here: `slug TEXT PK`, `topic`,
`pinned INTEGER NOT NULL DEFAULT 1`, `reason`, `pinned_at`. `rehydrate.build_gist`
unions these with `memories.pinned` into the single `## Pinned rules` gist section.

#### `agent_topic_bindings` *(0004)* — the persistent topic access binding

`agent_name TEXT`, `topic TEXT`, `created_at`, `PRIMARY KEY (agent_name, topic)`. The
binding store the topic axis of the access wall reads (the runtime per-request identity
mechanism uses the `ULTRA_MEMORY_CALLER_TOPIC` env fallback).

#### `procedures`

Reserved for learned procedures (`id`, `name`, `steps`, `trigger`, `source_sessions`,
`times_seen`, …). **Unwired** — captured procedures route through `memories` with
`node_type='procedure'`. Dropping the table is a deferred follow-up.

#### `meta`

`key` PK, `value`. Holds `schema_version` (mirrors `PRAGMA user_version`),
`import_complete` (the one-time legacy-import gate the hooks read), `backfill_complete`
(the cold-start session-cache backfill flag), `topic_backfill_complete` (the gated
topic-stamp step), `last_maintenance` (the ~20h Tier-1 throttle), the per-beat clocks
`last_maintenance_beat:<beat>`, and the per-period blast-radius counters
`sp7_aggressive_period:<YYYY-MM>` / `sp10_synthesis_period:<YYYY-MM>`.

### Indexes

`idx_memories_status(status)`, `idx_session_events_session(session_id)`,
`idx_links_src(src_kind, src_id)`, **(0004)** `idx_unified_topic(topic)`,
`idx_links_dst(dst_kind, dst_id)`, **(0008)**
`idx_access_log_session(session_id, target_kind)`.

---

## Where to go next

- The *why* behind every chokepoint and beat: **[Chapter 10 —
  Architecture](./10-architecture.md)**.
- The rules for changing this surface — TDD, the invariants, the doc-discipline hook:
  **[Chapter 12 — Contributing](./12-contributing.md)**.
- Back to the **[handbook index](./README.md)**.
