# Schema reference

`data/memory.db` — SQLite, WAL journal mode, `isolation_level=None` (autocommit)
with explicit `BEGIN IMMEDIATE`/`COMMIT` around writes, `foreign_keys=ON`,
`busy_timeout=30000`.

Version is tracked by `PRAGMA user_version` and mirrored in `meta.schema_version`
(the latter survives `iterdump`, so the committed dump round-trips the version).

## Migrations

| File | user_version | Adds |
|---|---|---|
| `0001_initial.sql` | 1 | all base tables + 3 indexes |
| `0002_import_fidelity.sql` | 2 | `memories.description`, `memories.index_hook`, `memories.node_type` |
| `0003_harness_slug.sql` | 3 | `memories.file_slug`, `memories.sort_order` |
| `0004_cross_store_fabric.sql` | 4 | SP-3 cross-store fabric: `memories.topic`/`created_by`/`outcome_weight`, `session_events.outcome_signal`, `links.src_type`/`dst_type`; new tables `unified_index`, `knowledge_pins`, `agent_topic_bindings`; index `idx_unified_topic`, `idx_links_dst` |
| `0005_unified_index_bm25_text.sql` | 5 | SP-6 BM25 full-body fix (D11): `unified_index.bm25_text` — the FULL collapsed page body for the knowledge-side BM25 document (`snippet` stays the ~400-char display preview) |
| `0006_access_log_session_id.sql` | 6 | SP-8 substrate (§5.1): `access_log.session_id` — a generic opaque string recording *which session* recalled a unit (the harmless logging substrate the usage-outcome attribution joins on; `NULL` = not attributable) |
| `0007_access_log_rank.sql` | 7 | SP-8 substrate: `access_log.rank` — the 1-based position of the unit in the FULL fused recall list at recall time (rank=1 = top hit, counting both memory and knowledge hits); `NULL` on pre-0007 rows and any non-recall access; harmless (logging only, no ranking effect), feeds a later top-k attribution policy |

The runner (`db.migrate`) applies each pending file's statements + the version bump
in one transaction; `ADD COLUMN` replay is tolerated (idempotent).

`0004` is **additive / forward-only** (version 3 → 4): every statement is `ADD
COLUMN` or `CREATE … IF NOT EXISTS`, no `DROP`/`RENAME`, so a restore or replay
against an already-shaped DB is a no-op. The one data step — the topic backfill
(`memory_lib.backfill_topic`, D4) — is a **separate, gated** code path, NOT in the
`.sql`, so the row-touch is guarded + idempotent + audited (see
[operations.md](operations.md#topic-backfill-gated)).

## Tables

### `memories`
The durable typed notes.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | stable identity = the `name:` slug (hyphenated); used for cross-links |
| `type` | TEXT | `feedback` / `project` / `reference` / `user` … |
| `title`, `body` | TEXT | redacted at write time |
| `description` | TEXT | one-line summary (from frontmatter) |
| `index_hook` | TEXT | the `MEMORY.md` "— hook" (distinct from `description`) |
| `node_type` | TEXT | default `'memory'` |
| `file_slug` | TEXT | the harness FILENAME stem (underscored); drives export filename + MEMORY.md links — **not** derivable from `id` |
| `sort_order` | INTEGER | the `MEMORY.md` line index → preserves curated order on export |
| `created_at`, `updated_at` | TEXT | canonical tz-aware UTC `%Y-%m-%dT%H:%M:%SZ` (r4 FIX 5 — import sets these from file mtime in this format, matching the CLI/save + maintain/retention paths, so raw-string `ORDER BY` is chronological; drives staleness) |
| `last_verified`, `valid_until` | TEXT | reserved |
| `strength` | REAL | default 1.0; multiplies relevance |
| `access_count` | INTEGER | derived; atomically incremented by `record_access` |
| `last_accessed` | TEXT | |
| `status` | TEXT | `active` / `deleted` / `redirect` / `quarantined` / `reverted` (default `active`). The last two are **(SP-7)** demotions written by `set_status`; only `active` is recalled by default. |
| `supersedes` | TEXT | canonical id when `status='redirect'` |
| `pinned` | INTEGER | default 0 |
| `topic` | TEXT | **(0004)** nullable. `NULL` = cross-topic / visible-to-all (composes with the §5 access wall as `topic IS NULL`); a non-NULL topic walls the row. `user`/`feedback` operational rows stay `NULL` (D11). |
| `created_by` | TEXT | **(0004)** provenance; `NOT NULL DEFAULT 'human'`. `human` (CLI / `/memory-*`) / `agent` / `background_review` / `import`. The §7a provenance gate (SP-7) may auto-edit only non-`human` rows; the safe default treats un-marked rows as immutable. |
| `outcome_weight` | REAL | **(0004)** `NOT NULL DEFAULT 1.0`. Multiplied into the unified-recall score (`1.0` is multiplicatively neutral). **First writer (SP-7):** `set_outcome_weight` (the EWMA regression signal); inert until written. |

### `sessions`
`id` (UUID) PK, `started_at`, `ended_at`, `status`, `branch`, `cwd`,
`first_prompt`, `summary`, `commit_shas`.

### `session_events`
Append-only episodic log. `id` PK, `session_id` FK→sessions, `ts`, `kind`,
`title`, `detail`, `files`, `refs`, `resolved`, and `event_key TEXT UNIQUE` =
`sha256(session_id|ts|kind|title|detail)` for idempotent `INSERT OR IGNORE`.
`title`, `detail`, and every string element of `files`/`refs` are **redacted at
write time** (`strip_secrets`, SP-8 bughunt) — the `event_key` is keyed on the RAW
pre-redaction text so a redaction-rule change can't shift the key and un-dedupe a
replay.
**(0004)** `outcome_signal TEXT` — an optional per-event deterministic outcome
hint (e.g. `tests_passed` / `trade_win` / `commit_landed`); it is **payload, not
part of `event_key`**, so two otherwise-identical events differing only in
`outcome_signal` still dedupe to one row (first write wins). The §7a substrate;
**inert this cycle** — no engine writer sets it (the consumer-side capture hook
will).
**Retention preservation (SP-8 bughunt):** `retention.prune_session_events`
**excludes** any event still referenced by an SP-8 attribution edge (a `links` row
with `src_kind='session_event'` and predicate in `('validated_as',
'superseded_by','informed_by')`) from both the roll-into-summary and the DELETE —
such an event is the EWMA fold's evidence and its `outcome_signal` would otherwise
be lost and the link left dangling.

### `embeddings`
`PRIMARY KEY (target_kind, target_id, model_name)`, `dim`, `vector` (float32 BLOB
via `struct`), `content_sha256` (cache invalidation). The `(model_name, dim)`
invariant is enforced loudly.

### `audit_log`
`id` PK, `ts`, `op` (`save`/`redirect`/`soft_delete`/`pin`/`verify`/`link`/
`outcome_weight`/`set_status`/…), `target_kind`, `target_id`, `reason`,
`prior_state` (JSON snapshot before the change).

### `access_log`
Append-only reinforcement source of truth: `id`, `target_kind`, `target_id`,
`ts`, `context`, plus **(0006)** `session_id` — a generic opaque string recording
*which session* recalled the target (SP-8 substrate). `NULL` on pre-0006 rows and
whenever the recall caller supplied no session id; harmless (logging only, no
ranking effect) — it is the substrate a later usage-outcome attribution joins on.
Plus **(0007)** `rank` — the unit's 1-based position in the FULL fused recall list
at recall time (rank=1 = top hit, counting both memory and knowledge hits). `NULL`
on pre-0007 rows and any non-recall access; harmless (logging only, no ranking
effect) — the signal a later top-k attribution policy reads.

### `links`
The **cross-store edge spine** (SP-3 D5/D6): `src_kind`, `src_id`, `predicate`,
`dst_kind`, `dst_id`, `evidence`, `confidence`, `created_at`, plus **(0004)**
`src_type` / `dst_type` — the within-kind sub-type (e.g. `feedback`, `mechanism`)
so a reader can filter without a join. `*_kind` is the store side
(`memory`/`knowledge`); e.g. `memory → grounded_in → knowledge`. The edge **key**
is `(src_kind, src_id, predicate, dst_kind, dst_id)`; there is **no UNIQUE on it**
(adding one would need a destructive rebuild of the pre-existing table), so
idempotency is enforced in code by `record_link` (SELECT-then-UPDATE-or-INSERT in
one txn). Until SP-3 this table was **defined and read but never written**
(north-star Risk §14.8); `record_link` (Stage 3) is its first writer. **SP-8 A2**
adds the usage-outcome edge: an `informed_by` row with `src_kind='session_event'`,
`src_id = str(<session_events.id>)`, `dst_kind='memory'` — written by
`attribution.attribute_usage` to join a session's outcome event to the memories it
recalled. The consumer reads it with
`JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)`, so `src_id` is the
integer id **as a string** (resolve via `memory_lib.event_id_for_key`).

### `unified_index` *(0004)*
A **derived, rebuildable mirror** of the Expert-Knowledge (wiki) pages, kept beside
the memory tables so unified recall spans both stores on one warm connection (D8).
The wiki files stay canonical; this table is regenerated by `wiki_sync`, which is a
**write-time redaction chokepoint** (SP-8 bughunt): `title`/`snippet`/`bm25_text`/
`frontmatter` pass through `strip_secrets` before insert, so the free-form-`Edit`
exception can't copy an unredacted secret from a wiki page into this queryable
mirror (`content_sha256` stays computed on the raw page text).
`slug TEXT PK`, `topic`, `page_type` (frontmatter `type:`), `title`, `snippet`,
`frontmatter` (JSON), `path`, `content_sha256` (sha-skip idempotency),
`outcome_weight REAL NOT NULL DEFAULT 1.0` (reserved §7a; inert), `updated_at`,
**(0005)** `bm25_text TEXT`. `snippet` is the ~400-char **display preview**;
`bm25_text` is the **FULL collapsed body** used as the knowledge-side BM25
document (`unified_query._knowledge_doc_text`) so a query term in a page's back
half ranks — matching `wiki_query`'s full-text BM25 (closes the SP-5 parity tail
divergence, D11). `NULL` on pre-0005 / un-resynced rows, where `_knowledge_doc_text`
falls back to `snippet`; a `wiki_sync(rebuild=True)` / `maintain --rebuild` pass
backfills it in one sweep.
Knowledge embeddings live in the shared `embeddings` table with
`target_kind='knowledge', target_id=slug`.

### `knowledge_pins` *(0004)*
The **knowledge side of the one pin space** (D7). A wiki page has no row in
`memories`, so its pin lives here: `slug TEXT PK`, `topic`, `pinned INTEGER NOT
NULL DEFAULT 1`, `reason`, `pinned_at`. Memory pins still use `memories.pinned`;
`rehydrate.build_gist` unions both into the single `## Pinned rules` gist section.

### `agent_topic_bindings` *(0004)*
The persistent many-to-many **topic access binding** (D10): `agent_name TEXT`,
`topic TEXT`, `created_at`, `PRIMARY KEY (agent_name, topic)`. The per-request
identity mechanism (how a shared-instance MCP learns the agent's topic) is the
unresolved SP-0 spike #7, so the runtime topic source is the
`ULTRA_MEMORY_CALLER_TOPIC` env-var fallback; this table is the binding store the
engine is forward-compatible with.

### `procedures`
Reserved for learned procedures: `id`, `name`, `steps`, `trigger`,
`source_sessions`, `times_seen`, `created_at`, `updated_at`. **Still unwired dead
weight** (no reader, no writer) — SP-3 routes captured procedures through
`memories` with `node_type='procedure'` (Fork A / D12) rather than wiring this
table; dropping it is deferred to the SP-5 doc/schema overhaul.

### `meta`
`key` PK, `value`. Holds `schema_version` (and, in future, `import_complete`).

## Indexes

`idx_memories_status(status)`, `idx_session_events_session(session_id)`,
`idx_links_src(src_kind, src_id)`, **(0004)** `idx_unified_topic(topic)`,
`idx_links_dst(dst_kind, dst_id)` (the reverse-edge lookup `memory ← knowledge`).
