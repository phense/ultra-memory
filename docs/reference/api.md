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
    UPDATE (an `agent` re-save stays `agent`). Provenance is **never DOWNGRADED**: a
    re-save over an existing `human` row with a non-`human` `created_by` preserves
    `human` (a human-owned row stays human unless a human edits it) — mirrors the
    status/pin preservation already in place on re-save.
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
  replayed. **Redaction (SP-8 bughunt):** title, detail, AND every string element of
  `files`/`refs` pass through `strip_secrets` before persist + spool — honoring the
  module guarantee that *all persisted text* is redacted first (the `event_key` is
  keyed on the RAW pre-redaction text so a rule change can't un-dedupe a replay).
- `event_id_for_key(conn, event_key) -> int | None` — **SP-8 A2.** Resolve the
  content-addressed `event_key` STRING that `record_session_event` returns back to
  its integer `session_events.id` — the value the SP-8 `informed_by` attribution
  edge stores as `src_id` (so the downstream
  `JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)` resolves). `None`
  for an unknown key. Read-only — no write, no audit.
- `record_access(conn, *, target_kind, target_id, ts, context=None, session_id=None, rank=None)`
  — append to `access_log` + atomic `access_count += 1` for memory targets.
  `session_id` (SP-8 substrate) is an optional **generic opaque** string recording
  *which session* recalled the target; `NULL` when unsupplied (= the pre-cutover /
  not-attributable state). `rank` (SP-8 substrate) is the optional 1-based position
  of the unit in the FULL fused recall list (rank=1 = top hit, counting both memory
  and knowledge hits); `NULL` when unsupplied (= a non-recall access). Both are
  logging-only — no ranking effect; the substrate a later usage-outcome / top-k
  attribution policy reads. Spooled + replayed.
- `session_id_from_env(env) -> str | None` — the generic session-id env read, the
  exact mirror of `caller_class_from_env`. Resolution order (SP-8 A3): stripped
  `ULTRA_MEMORY_SESSION_ID` (explicit override) → else stripped `CLAUDE_CODE_SESSION_ID`
  (the ambient session id Claude Code injects natively into tool/CLI subprocesses, so an
  orchestrator recall threads the real session with no hook) → else `None`. Reading the
  platform `CLAUDE_CODE_SESSION_ID` is deployment-env awareness, not a project concept —
  the agnostic boundary (no wiki/Trading import) is intact. A stale value in a long-lived
  process (the MCP server) only orphans rows (under-attributes), never mis-attributes.
  Re-exported from `knowledge_mcp` next to `caller_class_from_env`.
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
- `_with_immediate_retry(conn, work, *, retries=5, base_delay=0.05, sleep=None)` —
  the shared `BEGIN IMMEDIATE`/COMMIT bounded retry-with-backoff loop (§6 discipline).
  `work()` may return a value (returned on success); on exhaustion raises
  `_RetryExhausted` (the last busy error attached) so each caller picks its own
  exhaustion policy. `sleep` is resolved at CALL time (default = live `time.sleep`), so
  a `monkeypatch.setattr(memory_lib.time, "sleep", ...)` reaches the backoff even for
  callers that don't expose a sleep param. Reused by `retention.prune_session_events`
  and `maintain._set_meta` (the maintenance-write busy-retry).
- `_write_txn(conn, work, *, spool=None, retries=5, base_delay=0.05,
  sleep=None)` — the retry/spool transaction wrapper all `memory_lib` writers use:
  `_with_immediate_retry` plus a durable spool + loud `WriteSpooled` on exhaustion.
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
  boost (+0.5), then ×strength, −staleness penalty (sets
  `stale`); attaches 1-hop `links`. (R4 #8: the recall-driven access_count popularity
  boost was REMOVED from this score — feeding a recall-incremented counter back into the
  ranking was a self-reinforcing relevance loop. access_count is retained for the Hot-gist
  ambient ordering, not for relevance ranking.) Returns dicts with `id, title, type, status,
  score, stale, links`. No LLM. `include_types` scopes candidates in SQL **before**
  ranking. `topic` (SP-3 D11), when given, scopes to `topic = ? OR topic IS NULL`
  — a topiced caller still sees cross-topic (`NULL`) operational rows, and an
  un-topiced corpus stays fully visible (no retrieval regression). Topic ⟂
  `include_types` (composed by AND). **R3 bughunt:** the memory backend ranks by
  embedding-cosine ONLY — it has **no BM25-only fallback** — so `embedder` is
  required: `embedder=None` on a non-empty in-scope set raises a clear `ValueError`
  (not the cryptic `'NoneType' object is not callable` it raised before). Only the
  *knowledge* side of `unified_recall` degrades to BM25 when `embedder is None`.
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
  `created_by='import'`. Idempotent. **Round-4 FIX 5:** the mtime is stored as
  tz-aware UTC `%Y-%m-%dT%H:%M:%SZ` (the engine's canonical timestamp format,
  matching maintain/retention/checkpoint/rehydrate) — NOT the former naive-local
  isoformat (19 chars, no offset). The naive-local stamp, sorted as a raw SQLite
  STRING against the CLI/save path's aware-UTC stamps, compared off by the local
  UTC offset and corrupted the rehydrate gist's `ORDER BY updated_at` recency.
- `import_today_file(conn, text, *, day) -> (count, warnings)` — parse `## HH:MM` /
  `## HH:MM[-–—]HH:MM | …` blocks + non-time `## ` headers (captured at midnight
  with a warning) into `legacy-<day>` session events; dedupe within the run so
  `count` reflects rows recorded. Never crashes.

## `memory_export`

- `export_memory(conn, out_dir, *, ts, snapshot=True) -> bool` — read snapshot →
  redacted `memory.dump.sql` (carries `user_version`) → `VACUUM INTO` snapshot →
  `views/<file_slug>.md` + `views/MEMORY.md` (ordered by `sort_order`) → content
  hash last. Atomic (tmp→replace, snapshot-first). **R3 bughunt:** the snapshot
  itself is now atomic too — `VACUUM INTO memory.snapshot.db.tmp` then
  `os.replace` into place (the prior `memory.snapshot.db` is removed only by the
  atomic swap), so a `VACUUM` failure (disk-full / I/O / SIGTERM) after the old
  unconditional `unlink()` no longer destroys the prior good snapshot; any failure
  cleans up the `.tmp`. Returns False if unchanged
  (hash excludes access telemetry — `access_count`/`last_accessed`/`last_verified`
  — so reinforcement churn never drives a commit). **SP-3:** the stable-column
  projection includes `topic` and `created_by`. **SP-8 bughunt:** the hash also
  covers the **semantically-meaningful** outcome fields — `memories.outcome_weight`
  (a `set_outcome_weight` write changes recall ranking) and `session_events.
  outcome_signal` (the attribution evidence) — so an audited weight/signal write
  re-exports rather than going stale in the git-committed rollback dump.
- `export_learnings_projection(conn, path, *, skill_tag, title=None) -> int`
  (SP-3 D14/D15, §7a substrate) — regenerate a Learnings-style markdown
  **projection** from the store: active `memories` whose `index_hook == skill_tag`,
  ordered by `(created_at, id)` → byte-identical on re-run. Both `path` and
  `skill_tag` are **consumer-supplied** (no Trading/skill literal). This is a
  read-only projection capability (the DB is the system of record); the §7a **loop
  is NOT built** — this only materializes the surface the loop will later feed.
  **R3 bughunt:** the git-tracked projection is written **atomically** (`<path>.tmp`
  → `os.replace`), mirroring the dump swap — a SIGKILL/crash mid-write can no longer
  leave a torn file for Stage 3 to commit.

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
  **Redaction chokepoint (SP-8 bughunt):** `wiki_sync` is a write-time redaction
  chokepoint equivalent to the memory write path — `title`/`snippet`/`bm25_text`/
  `frontmatter` pass through `strip_secrets` before the `unified_index` INSERT (and
  the redacted text is what gets embedded). The documented free-form `Edit`
  exception can land an unredacted secret on a wiki page; this keeps it out of the
  queryable mirror that `unified_recall` + the rehydrate gist read. `content_sha256`
  is computed on the RAW page text (idempotency/cache key stays stable, matches the
  on-disk file).

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
> indexes `title + bm25_text` (the **frontmatter is dropped** — SP-6 stage-3 parity
> fix; see below), where `bm25_text` is the full collapsed page body (migration
> `0005`), **not** the ~400-char `snippet`. This matches `wiki_query`'s full-text
> BM25 so a query term in a page's back half ranks — closing the SP-5 parity
> tail-divergence (the SP-5 test loosened θ to tolerate the old snippet-cap). Falls
> back to `snippet` for `NULL`/pre-0005 rows (back-compat). The ranking math is
> unchanged (BM25 `b=0.75` length-norm).
>
> **SP-6 stage-3 parity fix — frontmatter dropped from the BM25 document.** The page
> `frontmatter` was part of the document, so a query term that matched ONLY a
> `tags:`/`type:` value (e.g. `"macro"` in `tags: [..., macro]`) produced a spurious
> near-zero-relevance tail hit, diverging from `wiki_query` (which BM25s the RENDERED
> body, not the frontmatter). The document is now `title + body` only; the high-signal
> `title` stays, the frontmatter noise is gone. Embedding side + ranking math
> unchanged.
>
> **R4 bug-hunt — two PERF-only fixes (results byte-identical, no ranking change).**
> (1) **Chunked embedding fetch (was N+1).** `_knowledge_candidates`'s embed backend
> fetched knowledge vectors with one `SELECT … FROM embeddings WHERE target_id=?` per
> slug — ~1223 per-row round-trips per recall at the designed mirror scale. It now
> issues one batched `… target_id IN (<≤500 placeholders>)` query per chunk
> (`_EMBED_FETCH_CHUNK=500`, safely under SQLite's 999-variable limit with the
> `model_name` param), i.e. `ceil(N/500)` round-trips. The `cached` dict is rebuilt in
> `by_slug` (unified_index row) order so `cosine_search`'s score-tie order is unchanged;
> a slug with no cached vector or a dim-mismatch is skipped exactly as before.
> (2) **Fingerprinted BM25 corpus cache (was: re-tokenize every call).** `_bm25_rank`
> re-tokenized all docs + recomputed `df`/`avgdl` on every call (the consolidation drain
> runs it up to 50× per weekly run → ~50× full-corpus re-tokenizations). The
> query-INDEPENDENT corpus stats (tokenized docs, lengths, avgdl) are now memoized on a
> **stable sha1 content fingerprint** of the docs (`_bm25_corpus_stats`, single-entry
> cache): an unchanged corpus is tokenized once; any doc edit/add/remove changes the
> fingerprint → recompute (never stale). The fingerprint is sha1 over a canonical
> `(doc_id, text)` serialization — **not** `PYTHONHASHSEED`-salted `hash()` — so the
> cache key is process-stable and correctness never depends on the hash seed. The
> query-dependent `df` + scoring still run every call; scores are byte-identical.
> `_bm25_cache_clear()` resets the cache (test/safety hook; no behavioral effect).

- `unified_recall(conn, query, *, caller_class, agent_topics, embedder=None,
  top_k=5, dim=EMBED_DIM, now_ts=None, ts=None, audit=True) -> [dict]` — resolve
  the type wall (`allowed_types_for(caller_class)`) and the topic scope, rank the
  memory backend (`query_memories`) + the two knowledge backends (generic BM25 +
  cached-vector cosine over `unified_index`), fuse with `_best_rank_rrf`, ×
  `outcome_weight`, audit each hit via `record_access`. `agent_topics`: a set ⇒
  topic-scoped; `None` ⇒ all topics (orchestrator / trusted CLI); the **empty set**
  ⇒ fail-closed (only `topic IS NULL` operational memories of allowed types, **zero
  topiced knowledge**). **R3 bughunt:** the scoped set is first normalized — `None`
  and empty-string elements are dropped (`{t for t in agent_topics if t}`) — so a
  `{None}`/`{''}` set collapses to the empty fail-closed set instead of passing
  `topic=None` to `query_memories` (which applies NO filter → WIDENS to every topiced
  row, violating "a partial binding sees LESS, never more"), and a mixed `{None,
  'trading'}` no longer crashes `sorted()` (and scopes to `trading` only). The
  orchestrator all-topics sentinel (`agent_topics is None`) is untouched. **Embedder
  asymmetry (R3 bughunt):** the knowledge backends degrade to BM25 when
  `embedder is None`, but the memory backend requires a real embedder (raises a clear
  `ValueError`) — so the `embedder=None` default is valid only for a
  knowledge-only / empty-memory recall. **Memory-only byte-identity (§5.6 fence):** when no
  knowledge row is in scope, the result is `query_memories`' own dicts verbatim
  (same order/fields/scores). Knowledge hits carry `source_kind='knowledge'`, `slug,
  topic, title, page_type, snippet, path, score`; memory hits carry
  `source_kind='memory'` + the `query_memories` fields. **Read-path redaction (SP-8
  bughunt):** like `knowledge_recall`, every caller-facing title/snippet runs through
  `strip_secrets` — the memory `title`, the knowledge `title`+`snippet`, AND the
  memory-only byte-identity `title` — defense-in-depth catching a secret that entered
  the DB by a path other than the write chokepoint (byte-identity holds for
  secret-free text, which `strip_secrets` leaves unchanged). **Links type-wall (SP-8
  bughunt):** a type-scoped (subagent) caller's per-row `links` are filtered through
  `filter_links_for_caller` so an edge to a forbidden `user`/`feedback` memory does
  not leak that endpoint's id+type past the primary-row type wall (fail-closed; the
  trusted/orchestrator caller keeps all links).
- `topic_scope_from_env(env, conn=None, *, agent_name=None) -> set` — **fail-closed**
  topic-scope resolver (mirrors `caller_class_from_env`): union of
  `ULTRA_MEMORY_CALLER_TOPIC` (comma/`:`/`;`-separated) and any
  `agent_topic_bindings` rows for the agent name. No binding from either source ⇒
  the **empty set** (degraded mode is safe — sees less, never more). A
  binding-lookup error never widens scope.
- Generic IR helpers (internal, no Trading specifics): `_bm25_rank` (fingerprint-cached
  corpus tokenization — R4 perf), `_bm25_corpus_stats`/`_bm25_corpus_fingerprint`/
  `_bm25_cache_clear`, `_rrf_score`, `_best_rank_rrf`, `_knowledge_candidates` (chunked
  embedding fetch — R4 perf). `allowed_types_for_caller` delegates to
  `knowledge_mcp.allowed_types_for` (single source of truth for the type wall).

## `attribution` *(SP-8 stage A2)*

The usage-outcome **attribution join** — deterministic, **NO LLM**, project-agnostic
(imports only stdlib + `memory_lib`; no policy config, no Trading/wiki concept). At
session-end it JOINs the memories a session actually recalled (logged in `access_log`
with the session id + a 1-based fused `rank` — stage A1) to that session's outcome
`session_event` (its `outcome_signal`) by writing `informed_by` graph edges; a
downstream consumer (Trading-side, not the engine) folds those edges into an EWMA.

- `recalled_units_for_session(conn, *, session_id) -> [{'id', 'rank'}]` — the
  session's recalled MEMORY units: one row per `access_log` entry with
  `target_kind='memory'`, this `session_id`, and a **non-NULL `rank`** (a knowledge
  recall, another session's recall, and a NULL-rank access are excluded). Ordered by
  `(rank, id)`. Read-only; **fail-closed-to-empty** (a read error returns `[]`,
  never raises).
- `apply_attribution_policy(rows, *, policy='top_k', k=1) -> [id]` — **PURE** (no
  DB). Selects the DISTINCT memory ids: `'all'` = every distinct id, ordered by best
  (lowest) rank, ties by id; `'top_k'` = the `k` distinct ids with the lowest rank
  (dedup keeping each id's best rank; ties by id; `k>=1`). An unknown policy raises
  `ValueError` (never silently attribute-all). Rank-weighted / `scope='recall'` /
  `'applied'` are deliberately NOT implemented (substrate doesn't exist yet).
- `attribute_usage(conn, *, session_id, outcome_event_id, ts, policy='top_k', k=1)
  -> int` — write an `informed_by` edge from the outcome `session_event` to each
  policy-selected recalled memory; returns the edge count. **THE INTEGRATION
  CONTRACT:** each edge is `record_link(src_kind='session_event',
  src_id=str(outcome_event_id), predicate='informed_by', dst_kind='memory',
  dst_id=<id>)`, so the consumer's
  `JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)` resolves.
  `outcome_event_id` is the INTEGER `session_events.id` (resolve via
  `memory_lib.event_id_for_key` upstream); `None` ⇒ no-op (0). Idempotent
  (`record_link` upserts on the edge key — a re-run writes no duplicate),
  **fail-open** (any error ⇒ 0, never raises out — it runs in a session-end Stop
  hook and must never wedge a session). The conservative default `policy='top_k',
  k=1` attributes only the single most-relevant recall.

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
  transaction routed through `memory_lib._with_immediate_retry` (the same bounded
  busy-retry as the `memory_lib` write path — a transient `SQLITE_BUSY` is retried,
  not raised immediately; no spool, idempotent maintenance write). Returns the count
  deleted; 0 (no-op) when nothing is old enough.
  Bounds the table where the real growth is (spec §8 D11). **SP-8 bughunt:** an
  event still referenced by an SP-8 attribution edge (`links` row with
  `src_kind='session_event'`, predicate in `_ATTRIBUTION_PREDICATES` =
  `('validated_as','superseded_by','informed_by')`) is **excluded from both the
  roll-into-summary SELECT and the DELETE** — it is evidence the downstream EWMA
  fold reads (`JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER) … WHERE
  se.outcome_signal IS NOT NULL`); pruning it would dangle the link and silently
  drop `outcome_signal`. Unreferenced old events prune as before.

## `maintain`

- `run(conn, *, out_dir, ts=None, keep_days=90, force=False, wiki_roots=None,
  embedder=None, env=None) -> dict` — throttled (≤ once / ~20h via
  `meta.last_maintenance`) prune + export. **SP-3:** also runs `wiki_sync` **inside
  the same throttle** (no second throttle) when wiki roots are configured — from
  `wiki_roots=` (explicit) or the `ULTRA_MEMORY_WIKI_ROOTS` env seam
  (`os.pathsep`- or comma-separated). With **no roots** (a pure-memory deployment)
  the sync is skipped entirely and the return is byte-identical to pre-SP-3
  (`{pruned, exported, skipped, spool_replay}`); with roots, the dict also carries a
  `wiki_sync` summary (or `{"error": …}` — fail-open, never blocks). The embedder
  defaults to the lazy fastembed one, degrading to `None` (rows still upsert) if the
  extra is absent. **r2 bughunt — write-spool drainer:** before prune/export, `run`
  calls `memory_lib.replay_spool(conn)` on its own connection (the single serialized
  drainer — no concurrent double-apply of the non-idempotent `record_access`), so a
  busy-casualty write self-heals; the result carries a `spool_replay` summary
  `{replayed, failed, errors}`. Fail-open: a replay error is logged and maintenance
  continues. `_set_meta` (the `last_maintenance` stamp) routes through
  `memory_lib._with_immediate_retry` (the same bounded busy-retry as the write path).

## `knowledge_mcp` (read-only MCP)

- `allowed_types_for(caller_class)` / `caller_class_from_env(env)` — the **type**
  axis of the access wall (unchanged): trusted (`orchestrator`/`owner`) → all types;
  else `SAFE_TYPES` = `(project, reference)`, fail-closed.
- `db_path_from_env(env) -> Path` — the single source of truth for resolving the
  `memory.db` path (the MCP, all three session hooks via `hooks/common.resolve_db_path`,
  and `maintain` route through it, so a zero-config install opens the same DB everywhere).
  Resolution order, **NEVER cwd**: (1) explicit `ULTRA_MEMORY_DB` if set + non-blank →
  `Path(it)`; else (2) `${CLAUDE_PROJECT_DIR}/data/memory.db` if `CLAUDE_PROJECT_DIR` is
  set + non-blank; else (3) `~/.claude/memory.db` (user-global). Blank values are
  treated as unset (fall through). It only RESOLVES — `open_memory_db` downstream does the
  create+migrate, and an empty store recalls nothing gracefully. **Zero-config change
  (2026-06-01):** it no longer raises — `ConfigError` is kept for callers that reference
  it but this resolver derives a default instead.
- `filter_links_for_caller(conn, links, *, caller_class)` — **SP-8 bughunt.** Extends
  the type wall from the PRIMARY row to its **edges**: for a type-scoped caller it
  drops any `links` edge whose `memory` endpoint resolves (via `SELECT type FROM
  memories WHERE id=?`, trusting the live row over the edge's stored `*_type`) to a
  type outside `allowed_types_for(caller_class)` — so an allowed project/reference
  row can't leak a forbidden `user`/`feedback` endpoint's id+type. **Fail-closed**
  (unresolvable endpoint → drop). **R3 bughunt:** only an EXPLICIT `dst_kind ==
  'knowledge'` bypasses the type wall; a `None`/missing/unknown kind is treated as a
  `memory` endpoint (re-read, fail-closed) rather than blindly kept — closing a leak
  where a `dst_kind=None` edge pointing at a `user`/`feedback` memory was passed
  through as a "safe non-memory endpoint". A trusted caller keeps all links
  unchanged. Wired into both `knowledge_recall` and `unified_recall`. `knowledge_recall` also still
  runs read-path `strip_secrets` on title/snippet (defense-in-depth, unchanged).
- `session_id_from_env(env)` — re-export of `memory_lib.session_id_from_env` (SP-8
  substrate), sitting next to `caller_class_from_env` so the recall path's two
  env-read dimensions (caller-class + session-id) are side by side. Both recall
  sites (`knowledge_recall`, `unified_query._audit_hits`) thread the result into
  `record_access(session_id=…)`; unset env → `NULL` → no attribution, never errors.
  **SP-8 bughunt:** the per-hit `record_access` audit-write in `knowledge_recall` is
  wrapped `try/except` (best-effort, matching `_audit_hits`) — `record_access` goes
  through `_write_txn` which can raise (e.g. `WriteSpooled` under write contention),
  and a SUCCEEDED read must survive an audit-write failure on the read-only MCP.
- `run_query_tool(arguments, *, conn, embedder, caller_class, dim=None, now_ts=None,
  ts=None, agent_topics=_NO_TOPIC_ARG)` — the MCP tool handler. **Additive SP-3
  routing:** when `agent_topics` is supplied (a set, or the orchestrator's `None`
  all-topics sentinel) it routes to `unified_query.unified_recall` so recall spans
  both stores, fail-closed on the (type × topic) wall; when `agent_topics` is **not**
  supplied (the `_NO_TOPIC_ARG` sentinel — the legacy SP-1 invocation) behavior is
  unchanged (pure memory-store `knowledge_recall`), so every existing MCP test keeps
  passing. Never raises (returns a structured `{"error": …}` payload). On a recall
  exception the client-facing payload is a **fixed generic string** (`"recall failed
  (internal error)"`) — the raw `str(exc)` (which can embed an internal filesystem /
  DB path that `strip_secrets` does NOT redact) is logged LOCALLY (stderr) and never
  crosses the privilege boundary.

## `hooks.common`

Shared, fail-open, no-LLM, no-write helpers for the session hooks.

- `agent_role_optout(payload=None) -> bool` — True when the hook must no-op:
  env `ULTRA_MEMORY_AGENT_ROLE` is non-empty (cron/subagent wrappers set it), or
  a SessionStart `payload["source"]` is not in `INTERACTIVE_SOURCES`
  (`{startup, resume, clear, compact}`).
- `resolve_db_path(env=None) -> str` — resolve the `memory.db` path the SAME way the
  knowledge MCP does (delegates to `knowledge_mcp.db_path_from_env`), so the whole plugin
  is zero-config-consistent: explicit `ULTRA_MEMORY_DB`, else
  `${CLAUDE_PROJECT_DIR}/data/memory.db`, else `~/.claude/memory.db` — never cwd.
  Returns a `str` (hooks feed it to `db_ready` / `open_memory_db`). Used by all three hook
  `main()` shells.
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
- `main(stdin, stdout) -> int` — CLI shell: read payload, resolve the DB path via
  `common.resolve_db_path()` (zero-config: explicit `ULTRA_MEMORY_DB`, else
  `${CLAUDE_PROJECT_DIR}/data/memory.db`, else `~/.claude/memory.db` — never cwd),
  stamp `ts`, run, write any output. Exit 0.

## `hooks.rehydrate` (SessionStart hook)

- `build_gist(conn, *, budget_chars=2000) -> str` — pure-SQL, no-LLM gist:
  pinned rules + "where we left off" (last `sessions.summary`, else recent
  events) + open follow-ups + hot memories + a pull-on-demand pointer; truncated
  to `budget_chars` (spec §9.2). **SP-3 (D7):** the `## Pinned rules` section
  unions **memory pins** (`memories.pinned`, capped at 12) with **knowledge pins**
  (`knowledge_pins WHERE pinned=1`, display title from `unified_index`). With zero
  surviving `knowledge_pins` rows (Trading's current state) the gist is unchanged.
  **Round-4 hardening:**
  - **FIX 1 — structure-injection sanitize.** Every field rendered into the gist
    (pin title + body head, hot title, follow-up title, the session summary, the
    knowledge-pin label) is passed through `_one_line(s)` — `" ".join(s.split())`
    collapses every whitespace run (newlines, tabs, control whitespace) to one
    line and caps length (`_FIELD_MAX=200`; summary 500). So no stored field can
    forge a counterfeit `## …` header or `- …` list LINE inside the trusted
    SessionStart context — the injected text survives only as inline prose.
    (`save_memory` does not strip newlines from titles; only the body's first
    line was previously defended.)
  - **FIX 2 — pinned rules survive budget pressure.** `## Pinned rules` is
    rendered FIRST and is EXEMPT from the budget tail-cut: under budget pressure
    a *later* section is trimmed before any pinned rule, and if the 12-line cap
    forces a pinned rule out it is named with an explicit `(…N more pinned rules
    omitted)` marker — never silently lost. If the pinned section alone meets/
    exceeds `budget_chars`, the later sections are dropped entirely.
  - **FIX 3 — no pinned/hot duplication.** The hot-memory query carries
    `AND pinned=0`, so a pinned unit is not re-listed under `## Hot memories`.
  - **FIX 4 — stale knowledge-pin skip.** `_knowledge_pin_lines` INNER-joins
    `unified_index` (via `WHERE EXISTS`): a pin whose page was deleted (no mirror
    row) is SKIPPED rather than emitting a bare-slug "rule". The slug-fallback
    title is KEPT only for a page that EXISTS but has an empty title.
  - **FIX 5 — deterministic ordering.** The pins / hot ORDER BYs carry a stable
    secondary `id` tie-break (`ORDER BY updated_at DESC, id` and `ORDER BY
    access_count DESC, updated_at DESC, id`), so equal-timestamp rows (e.g. a
    bootstrap import stamping same-mtime files identically) sort deterministically.
- `run(payload, *, db_path, shadow, ts, shadow_out=None, budget_chars=2000) -> dict`
  — shadow mode writes the gist to `shadow_out` and returns `{}` (no injection);
  live mode returns `{"hookSpecificOutput": {"hookEventName": "SessionStart",
  "additionalContext": gist}}`. No-ops on role opt-out / DB not ready / empty
  gist. Fail-open: any error → `{}`.
- `_budget_from_env() -> int` — resolve the gist char budget from
  `ULTRA_MEMORY_REHYDRATE_BUDGET` (consumer-tunable); default `2000`. Empty,
  non-numeric, or non-positive values fail-soft back to `2000`.
- `main(stdin, stdout) -> int` — CLI shell; DB path via `common.resolve_db_path()`
  (zero-config derivation — see `db_path_from_env`), `ULTRA_MEMORY_SHADOW` (default `"1"`),
  `ULTRA_MEMORY_SHADOW_OUT`, and `ULTRA_MEMORY_REHYDRATE_BUDGET` (default `2000`) from env.
