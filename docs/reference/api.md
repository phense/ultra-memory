# API reference

Every public function. The caller owns connections and supplies timestamps.

## `db`

- `connect(db_path, *, busy_timeout_ms=30000) -> Connection` — WAL, busy_timeout,
  `foreign_keys=ON`, `isolation_level=None`, `row_factory=Row`.
- `migrate(conn, migrations_dir) -> int` — apply pending `NNNN_*.sql` (version >
  `user_version`) each in one transaction; bump `user_version` + mirror
  `meta.schema_version`; tolerate `ADD COLUMN` replay. Returns the new version.

## `memory_lib`

- `open_memory_db(path, migrations_dir=_MIGRATIONS) -> Connection` — connect +
  migrate. Caller closes.
- `save_memory(conn, *, id, type, title, body, ts, origin_session_id=None,
  description=None, index_hook=None, node_type="memory", file_slug=None,
  sort_order=None, created_at=None, updated_at=None, topic=None, topic_router=None,
  genesis_hook=None, caller_class=None, created_by="human") -> id` — redact →
  upsert → audit. Update preserves a `deleted`/`redirect` status.
  `created_at`/`updated_at` default to `ts`.
  - `topic` (SP-3) is the topic to persist (D1/D3). If omitted and a `topic_router`
    is supplied, the **deterministic, no-LLM** router assigns one; abstention → `NULL`.
    `user`/`feedback` rows **always** stay `NULL` (D11) even if `topic=` is passed.
  - `topic_router` is a caller-supplied callable
    `(*, type, title, body, origin_session_id, caller_class) -> str | None`. Build a
    generic one with `make_keyword_router({topic: (kw, …)})` (whole-word, ordered).
  - `genesis_hook(topic)` is an **optional, injectable, best-effort** callback
    (default no-op) the *consumer* wires its topic genesis into (e.g.
    `wiki_topics.ensure_topic`); fired only on a resolved non-NULL topic, **after**
    the durable write; a raising hook never aborts the write (fail-open). The engine
    has no wiki dependency.
  - `created_by` (SP-3 D16) is the write's provenance: `human` (the safe-immutable
    default) / `agent` / `background_review` / `import`. Stamped on both INSERT and
    UPDATE (an `agent` re-save stays `agent`).
- `make_keyword_router(keyword_map) -> router` — build a deterministic, **generic**,
  content-free fallback topic router from `{topic: (kw, …)}`. Whole-word match,
  map-insertion-order priority; abstains to `None` on no hit; `user`/`feedback`
  always abstain. No LLM / wiki / network.
- `record_link(conn, *, src_kind, src_id, predicate, dst_kind, dst_id,
  src_type=None, dst_type=None, evidence=None, confidence=None, ts)` — **the first
  writer the `links` table ever gets** (SP-3 D5). Upsert a cross-store edge,
  idempotent on the edge key `(src_kind, src_id, predicate, dst_kind, dst_id)` (a
  re-record refreshes sub-types/evidence/confidence/`created_at`, never duplicates).
  Through redact + `_audit` + `_write_txn`; registered in `replay_spool`.
- `mirror_cross_store_links(conn, wiki_edges, *, ts) -> {mirrored,
  skipped_wiki_internal}` — lift **cross-store** wiki-graph edges into `links` via
  `record_link` (D6). `wiki_edges` is a **consumer-fed** iterable (the engine never
  opens `graph.sqlite`); only edges crossing stores (one side `memory`, one
  `knowledge`) are mirrored; pure wiki↔wiki edges are skipped. Idempotent.
- `set_pinned(conn, *, source_kind=None, source_id=None, id=None, pinned, ts,
  reason="manual pin")` — set/clear a pin in the one cross-store pin space (SP-3
  D7). `source_kind='memory'` flips `memories.pinned`; `source_kind='knowledge'`
  upserts `knowledge_pins(slug=source_id, …)`. **Back-compat shim:** the legacy
  `set_pinned(id=…)` signature still works (treated as `source_kind='memory'`), so
  `/memory-pin` + pre-SP-3 spool records keep replaying. Audited; registered in
  `replay_spool` under the new arg shape.
- `backfill_topic(conn, *, default_topic, ts, reason=…) -> {stamped,
  skipped_already_complete}` — **gated, one-time, idempotent** data step (D4):
  stamp `topic = default_topic` on every `memories` row that is `topic IS NULL AND
  type NOT IN ('user','feedback')`. A `meta.topic_backfill_complete` flag
  short-circuits re-runs (mirrors `import_complete`); audited per row; reversible.
  `default_topic` is consumer-supplied (content-free). **Touches the live canonical
  store — gated on sign-off (spec §10); never run on a live DB ungated.**
- `record_session_event(conn, *, session_id, kind, title, ts, detail=None,
  files=None, refs=None, session_fields=None, outcome_signal=None) -> event_key` —
  ensure session row, append event idempotently (UNIQUE `event_key`).
  `outcome_signal` (SP-3 D13, §7a substrate) is an optional deterministic per-event
  hint; it is **payload, excluded from `event_key`**, so events differing only in
  the signal still dedupe (first write wins). Inert by default (`NULL`); spooled +
  replayed.
- `record_access(conn, *, target_kind, target_id, ts, context=None)` — append to
  `access_log` + atomic `access_count += 1` for memory targets.
- `consolidate(conn, *, loser_id, canonical_id, reason, ts)` — redirect-stub
  (`status='redirect'`, `supersedes=canonical`). Raises `KeyError` if absent.
- `delete(conn, *, id, reason, tier, ts)` — soft tombstone (`status='deleted'`).
  `tier` ∈ {`durable`, `volatile`}; `ValueError` otherwise, `KeyError` if absent.
- `set_outcome_weight(conn, *, id, weight, ts, reason="outcome aggregate")` —
  **SP-7 generic support.** Set `memories.outcome_weight = weight` (the **first
  writer** of the migration-0004 column; until now read-only/inert at its `1.0`
  default in unified ranking). SP-7's deterministic EWMA aggregate writes its
  regression signal through here (sub-`1.0` demotes a unit's recall rank, `>1.0`
  promotes it); the engine neither computes nor bounds the weight. `KeyError` if
  absent. Audited; spooled + replayed.
- `set_status(conn, *, id, status, ts, reason)` — **SP-7 generic support.** Set
  `memories.status = status`, validated against `_KNOWN_STATUSES` =
  `('active','redirect','deleted','quarantined','reverted')` — SP-7 **adds**
  `'quarantined'` (a contradictory pair demoted pending adjudication) and
  `'reverted'` (a regressed auto-edited unit rolled back). A row in **any**
  non-`'active'` status drops out of recall by default (`query_memories`
  `include_statuses=('active',)`, `unified_recall`, the rehydrate gist's
  `status='active'` filters) — so a flip removes the unit from recall with **no
  recall-query change**. `ValueError` on an unknown status, `KeyError` if absent.
  **Generic:** enforces NO "protected row" policy — the SP-7 safety wall
  ("never auto-touch a human/pinned unit") is the **consumer's** job. Audited;
  spooled + replayed.
- `WriteSpooled` — raised when a write is spooled after retry exhaustion.
- `_write_txn(conn, work, *, spool=None, retries=5, base_delay=0.05,
  sleep=time.sleep)` — the retry/spool transaction wrapper all writers use.
  (Internal, but documented because it is the §6 discipline.)

## `retrieval_core`

- `EMBED_MODEL` = `"BAAI/bge-small-en-v1.5"`, `EMBED_DIM` = 384 (one canonical id
  for both the cache key and the fastembed model).
- `cosine(a, b) -> float` — 0.0 if either vector is zero.
- `cosine_search(query_vec, items, *, top_k=None) -> [(id, score)]` desc.
- `rrf_fuse(rankings, *, k=60) -> [(id, score)]` — reciprocal-rank fusion (wiki
  side; memory stays cosine-only per D11).
- `pack_vector(vec) -> bytes` / `unpack_vector(blob, dim=EMBED_DIM) -> [float]` —
  float32 (de)serialise.
- `content_sha256(text) -> str` — None-safe cache-invalidation hash.
- `get_or_embed(conn, *, target_kind, target_id, text, embedder,
  model_name=EMBED_MODEL, dim=EMBED_DIM) -> [float]` — cached embed; recompute on
  content change; enforce the `(model_name, dim)` invariant; miss = one write txn.
- `get_or_embed_batch(conn, items, *, embedder, model_name=EMBED_MODEL,
  dim=EMBED_DIM) -> {target_id: vector}` — batched: all misses in one embedder call
  + one write txn. `items` = iterable of `(target_kind, target_id, text)`.
- `default_embedder(model_name=EMBED_MODEL) -> callable` — lazy fastembed; raises a
  clear `RuntimeError` if the `[retrieval]` extra is absent. The model is cached in
  `persistent_cache_dir()`, never the OS temp dir.
- `persistent_cache_dir() -> str` — the fastembed model-cache dir, anchored under
  `$HOME` (never `tempfile.gettempdir()`, which macOS purges → onnxruntime
  `NoSuchFile` at load). Resolution: `ULTRA_MEMORY_FASTEMBED_CACHE` →
  `FASTEMBED_CACHE_PATH` → `~/.cache/ultra-memory/fastembed`; created on demand.

## `memory_query`

- `query_memories(conn, query, *, embedder, top_k=5, dim=EMBED_DIM,
  include_statuses=("active",), include_types=None, now_ts=None,
  staleness_days=90, topic=None) -> [dict]` — cosine rank + word-bounded title
  boost (+0.5), then ×strength, +bounded access boost, −staleness penalty (sets
  `stale`); attaches 1-hop `links`. Returns dicts with `id, title, type, status,
  score, stale, links`. No LLM. `include_types` scopes candidates in SQL **before**
  ranking. `topic` (SP-3 D11), when given, scopes to `topic = ? OR topic IS NULL`
  — a topiced caller still sees cross-topic (`NULL`) operational rows, and an
  un-topiced corpus stays fully visible (no retrieval regression). Topic ⟂
  `include_types` (composed by AND).
- `_links_for(conn, mid)` (internal, read path) now surfaces the SP-3
  `src_type`/`dst_type` alongside `predicate, dst_kind, dst_id`. This is the reader
  that, per north-star Risk §14.8, had never run against populated rows until
  `record_link` landed (verified at Stage 0).

## `memory_import`

- `split_frontmatter(text) -> (dict, body)` — no-YAML parser for the known memory
  frontmatter (flat keys + nested `metadata`); tolerant of the `metadata: ` trailing
  space and body `---` lines.
- `parse_memory_index(text) -> {slug: {title, hook}}` — parse `MEMORY.md` lines.
- `import_memory_dir(conn, memory_dir, *, index_path=None, ts) -> count` — glob
  `*.md` (excluding `MEMORY.md`), upsert each; set `file_slug`=stem,
  `sort_order`=index position, `created_at`/`updated_at`=file mtime, and (SP-3 D16)
  `created_by='import'`. Idempotent.
- `import_today_file(conn, text, *, day) -> (count, warnings)` — parse `## HH:MM` /
  `## HH:MM[-–—]HH:MM | …` blocks + non-time `## ` headers (captured at midnight
  with a warning) into `legacy-<day>` session events; dedupe within the run so
  `count` reflects rows recorded. Never crashes.

## `memory_export`

- `export_memory(conn, out_dir, *, ts, snapshot=True) -> bool` — read snapshot →
  redacted `memory.dump.sql` (carries `user_version`) → `VACUUM INTO` snapshot →
  `views/<file_slug>.md` + `views/MEMORY.md` (ordered by `sort_order`) → content
  hash last. Atomic (tmp→replace, snapshot-first). Returns False if unchanged
  (hash excludes access telemetry). **SP-3:** the stable-column projection now
  includes `topic` and `created_by`, so the new columns round-trip through the
  git-tracked dump + drive the content hash.
- `export_learnings_projection(conn, path, *, skill_tag, title=None) -> int`
  (SP-3 D14/D15, §7a substrate) — regenerate a Learnings-style markdown
  **projection** from the store: active `memories` whose `index_hook == skill_tag`,
  ordered by `(created_at, id)` → byte-identical on re-run. Both `path` and
  `skill_tag` are **consumer-supplied** (no Trading/skill literal). This is a
  read-only projection capability (the DB is the system of record); the §7a **loop
  is NOT built** — this only materializes the surface the loop will later feed.

## `wiki_sync` *(SP-3 Stage 5)*

The Tier-1 wiki→memory mirror (population only; retrieval is `unified_query`).
**Project-agnostic:** roots are consumer-fed; the engine imports nothing from the
consumer's wiki (no topic-model module, not even PyYAML).

- `wiki_sync(conn, wiki_roots, *, embedder=None, rebuild=False, ts) -> {upserted,
  skipped, pruned, embedded, errors}` — walk each `<root>/<topic>/**/*.md`, upsert
  each page into `unified_index` (PK `slug`; `topic` = first path component under the
  root; `page_type`/`title`/`snippet`/`bm25_text` parsed with a tiny hand-rolled
  front-matter scanner). `snippet` is the ~400-char display preview; **(SP-6, D11)**
  `bm25_text` is the FULL collapsed body — the knowledge-side BM25 document
  (`unified_query._knowledge_doc_text`) so a back-half query term ranks. `rebuild=True`
  forces every page to re-populate regardless of `content_sha256` — the one-pass
  `bm25_text` backfill for rows written by the pre-SP-6 `wiki_sync` (CLI:
  `maintain --rebuild` / `ULTRA_MEMORY_REBUILD_INDEX=1`). **Idempotent**
  (`content_sha256` match → skip + no re-embed, unless `rebuild`),
  **reconciling** (orphan-prune rows whose slug no longer maps to a file, scoped to
  the topics synced this call — mirrors the `memory_export` orphan prune),
  **fail-open** (a missing root / unreadable page / embed failure increments
  `errors` and continues). Changed/new pages are embedded into the shared
  `embeddings` table with `target_kind='knowledge'` (reuses `get_or_embed_batch` +
  its sha invalidation; `embedder=None` → upsert rows, skip embedding). No LLM, no
  model download beyond the one shared embedder.

## `unified_query` *(SP-3 Stage 6)*

The cross-store **warm** retrieval surface; one ranked list spanning `memories` +
the `unified_index` knowledge mirror, scoped by (type × topic), fused with FU-4
best-rank-per-backend RRF, weighted by `outcome_weight` (inert 1.0). No LLM.

> **Decision D-S6 (auditable).** Spec §5.6 says "reuse the wiki_query backends".
> But `wiki_query` is a *Trading-side* module and the project-agnostic NFR forbids
> the engine importing it. So `unified_query` does **not** import `wiki_query`; it
> re-implements the *algorithm* engine-side — a generic in-module BM25 + cosine
> over `unified_index`, fused with a generic re-implementation of FU-4
> best-rank-per-backend RRF (k=60). Cross-codebase byte-parity with `wiki_query`
> is **deferred to an SP-5 Trading-side test** (which can import both). The
> memory-store byte-identity (below) *is* enforced here.
>
> **SP-6 (D11) — BM25 document is the FULL body.** `_knowledge_doc_text(row)`
> indexes `title + bm25_text + frontmatter`, where `bm25_text` is the full
> collapsed page body (migration `0005`), **not** the ~400-char `snippet`. This
> matches `wiki_query`'s full-text BM25 so a query term in a page's back half
> ranks — closing the SP-5 parity tail-divergence (the SP-5 test loosened θ to
> tolerate the old snippet-cap). Falls back to `snippet` for `NULL`/pre-0005 rows
> (back-compat). The ranking math is unchanged (BM25 `b=0.75` length-norm).

- `unified_recall(conn, query, *, caller_class, agent_topics, embedder=None,
  top_k=5, dim=EMBED_DIM, now_ts=None, ts=None, audit=True) -> [dict]` — resolve
  the type wall (`allowed_types_for(caller_class)`) and the topic scope, rank the
  memory backend (`query_memories`) + the two knowledge backends (generic BM25 +
  cached-vector cosine over `unified_index`), fuse with `_best_rank_rrf`, ×
  `outcome_weight`, audit each hit via `record_access`. `agent_topics`: a set ⇒
  topic-scoped; `None` ⇒ all topics (orchestrator / trusted CLI); the **empty set**
  ⇒ fail-closed (only `topic IS NULL` operational memories of allowed types, **zero
  topiced knowledge**). **Memory-only byte-identity (§5.6 fence):** when no
  knowledge row is in scope, the result is `query_memories`' own dicts verbatim
  (same order/fields/scores). Knowledge hits carry `source_kind='knowledge'`, `slug,
  topic, title, page_type, snippet, path, score`; memory hits carry
  `source_kind='memory'` + the `query_memories` fields.
- `topic_scope_from_env(env, conn=None, *, agent_name=None) -> set` — **fail-closed**
  topic-scope resolver (mirrors `caller_class_from_env`): union of
  `ULTRA_MEMORY_CALLER_TOPIC` (comma/`:`/`;`-separated) and any
  `agent_topic_bindings` rows for the agent name. No binding from either source ⇒
  the **empty set** (degraded mode is safe — sees less, never more). A
  binding-lookup error never widens scope.
- Generic IR helpers (internal, no Trading specifics): `_bm25_rank`, `_rrf_score`,
  `_best_rank_rrf`, `_knowledge_candidates`. `allowed_types_for_caller` delegates to
  `knowledge_mcp.allowed_types_for` (single source of truth for the type wall).

## `redact_secrets`

- `strip_secrets(text) -> text` — redact `<private>…</private>`, PEM blocks, URI
  userinfo, provider keys (Anthropic/GitHub/AWS/Google/Slack/Stripe/SendGrid/
  Twilio), JWT, bearer, and credential-shaped `keyword=value`. Conservative:
  hyphen-joined prose survives. None/"" pass through.

## `claude_cli`

- `run_claude(prompt, *, model, system=None, claude_bin="claude", timeout=120,
  runner=subprocess.run, env=None) -> str` — OAuth-sanitised CLI call. Raises
  `OAuthViolation` (API key present / OAuth token missing) or `ClaudeCliError`
  (nonzero exit). Inject `runner` in tests.

## `retention`

- `prune_session_events(conn, *, keep_days, ts) -> int` — roll `session_events`
  older than `keep_days` (relative to `ts`) into the owning `sessions.summary`
  (one digest line per event), then delete the rows in one `BEGIN IMMEDIATE`
  transaction. Returns the count deleted; 0 (no-op) when nothing is old enough.
  Bounds the table where the real growth is (spec §8 D11).

## `maintain`

- `run(conn, *, out_dir, ts=None, keep_days=90, force=False, wiki_roots=None,
  embedder=None, env=None) -> dict` — throttled (≤ once / ~20h via
  `meta.last_maintenance`) prune + export. **SP-3:** also runs `wiki_sync` **inside
  the same throttle** (no second throttle) when wiki roots are configured — from
  `wiki_roots=` (explicit) or the `ULTRA_MEMORY_WIKI_ROOTS` env seam
  (`os.pathsep`- or comma-separated). With **no roots** (a pure-memory deployment)
  the sync is skipped entirely and the return is byte-identical to pre-SP-3
  (`{pruned, exported, skipped}`); with roots, the dict also carries a `wiki_sync`
  summary (or `{"error": …}` — fail-open, never blocks). The embedder defaults to
  the lazy fastembed one, degrading to `None` (rows still upsert) if the extra is
  absent.

## `knowledge_mcp` (read-only MCP)

- `allowed_types_for(caller_class)` / `caller_class_from_env(env)` — the **type**
  axis of the access wall (unchanged): trusted (`orchestrator`/`owner`) → all types;
  else `SAFE_TYPES` = `(project, reference)`, fail-closed.
- `run_query_tool(arguments, *, conn, embedder, caller_class, dim=None, now_ts=None,
  ts=None, agent_topics=_NO_TOPIC_ARG)` — the MCP tool handler. **Additive SP-3
  routing:** when `agent_topics` is supplied (a set, or the orchestrator's `None`
  all-topics sentinel) it routes to `unified_query.unified_recall` so recall spans
  both stores, fail-closed on the (type × topic) wall; when `agent_topics` is **not**
  supplied (the `_NO_TOPIC_ARG` sentinel — the legacy SP-1 invocation) behavior is
  unchanged (pure memory-store `knowledge_recall`), so every existing MCP test keeps
  passing. Never raises (returns a structured `{"error": …}` payload).

## `hooks.common`

Shared, fail-open, no-LLM, no-write helpers for the session hooks.

- `agent_role_optout(payload=None) -> bool` — True when the hook must no-op:
  env `ULTRA_MEMORY_AGENT_ROLE` is non-empty (cron/subagent wrappers set it), or
  a SessionStart `payload["source"]` is not in `INTERACTIVE_SOURCES`
  (`{startup, resume, clear, compact}`).
- `db_ready(db_path) -> bool` — True only when the schema is present AND
  `meta.import_complete == '1'`. Any error / missing file → False (fail-open to
  the legacy path, spec §7.4).
- `read_payload(stream) -> dict` — parse a hook stdin payload; `{}` on any error.
- `session_id_of(payload, transcript_path=None) -> str` — prefer
  `payload["session_id"]`, else the transcript filename stem, else
  `"unknown-session"`.

## `hooks.checkpoint` (Stop hook)

- `completed_tasks(transcript_path) -> [(task_id, subject)]` — replay
  `TaskCreate`/`TaskUpdate` blocks from the raw transcript JSONL; emit subjects
  whose final folded status is `completed`, in first-seen order (spec §9.1).
- `has_material_work(transcript_path) -> bool` — True if the session ran an
  `Edit`/`Write`/`NotebookEdit` or a `git commit` Bash call.
- `run(payload, *, db_path, ts) -> dict` — record each completed task as a
  `kind='task_done'` session event via `memory_lib.record_session_event`
  (idempotent on `event_key`). Always returns `{}` (NEVER blocks). No-ops on:
  `stop_hook_active`, role opt-out, DB not ready, missing transcript, or no
  material work.
- `main(stdin, stdout) -> int` — CLI shell: read payload, `ULTRA_MEMORY_DB` from
  env, stamp `ts`, run, write any output. Exit 0.

## `hooks.rehydrate` (SessionStart hook)

- `build_gist(conn, *, budget_chars=2000) -> str` — pure-SQL, no-LLM gist:
  pinned rules + "where we left off" (last `sessions.summary`, else recent
  events) + open follow-ups + hot memories + a pull-on-demand pointer; truncated
  to `budget_chars` (spec §9.2). **SP-3 (D7):** the `## Pinned rules` section now
  unions **memory pins** (`memories.pinned`, capped at 12) with **knowledge pins**
  (`knowledge_pins WHERE pinned=1`, display title from `unified_index`, slug
  fallback). Byte-identity guarantee: with zero `knowledge_pins` rows (Trading's
  current state) the gist is unchanged — the knowledge block is appended only when
  at least one knowledge pin exists, and the `unified_index` title lookup is
  fail-open (a pre-Stage-5 DB still renders the slug).
- `run(payload, *, db_path, shadow, ts, shadow_out=None, budget_chars=2000) -> dict`
  — shadow mode writes the gist to `shadow_out` and returns `{}` (no injection);
  live mode returns `{"hookSpecificOutput": {"hookEventName": "SessionStart",
  "additionalContext": gist}}`. No-ops on role opt-out / DB not ready / empty
  gist. Fail-open: any error → `{}`.
- `_budget_from_env() -> int` — resolve the gist char budget from
  `ULTRA_MEMORY_REHYDRATE_BUDGET` (consumer-tunable); default `2000`. Empty,
  non-numeric, or non-positive values fail-soft back to `2000`.
- `main(stdin, stdout) -> int` — CLI shell; `ULTRA_MEMORY_DB`,
  `ULTRA_MEMORY_SHADOW` (default `"1"`), `ULTRA_MEMORY_SHADOW_OUT`, and
  `ULTRA_MEMORY_REHYDRATE_BUDGET` (default `2000`) from env.
