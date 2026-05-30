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

The runner (`db.migrate`) applies each pending file's statements + the version bump
in one transaction; `ADD COLUMN` replay is tolerated (idempotent).

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
| `created_at`, `updated_at` | TEXT | ISO; import sets these from file mtime (drives staleness) |
| `last_verified`, `valid_until` | TEXT | reserved |
| `strength` | REAL | default 1.0; multiplies relevance |
| `access_count` | INTEGER | derived; atomically incremented by `record_access` |
| `last_accessed` | TEXT | |
| `status` | TEXT | `active` / `deleted` / `redirect` (default `active`) |
| `supersedes` | TEXT | canonical id when `status='redirect'` |
| `pinned` | INTEGER | default 0 |

### `sessions`
`id` (UUID) PK, `started_at`, `ended_at`, `status`, `branch`, `cwd`,
`first_prompt`, `summary`, `commit_shas`.

### `session_events`
Append-only episodic log. `id` PK, `session_id` FK→sessions, `ts`, `kind`,
`title`, `detail`, `files`, `refs`, `resolved`, and `event_key TEXT UNIQUE` =
`sha256(session_id|ts|kind|title|detail)` for idempotent `INSERT OR IGNORE`.

### `embeddings`
`PRIMARY KEY (target_kind, target_id, model_name)`, `dim`, `vector` (float32 BLOB
via `struct`), `content_sha256` (cache invalidation). The `(model_name, dim)`
invariant is enforced loudly.

### `audit_log`
`id` PK, `ts`, `op` (`save`/`redirect`/`soft_delete`), `target_kind`,
`target_id`, `reason`, `prior_state` (JSON snapshot before the change).

### `access_log`
Append-only reinforcement source of truth: `id`, `target_kind`, `target_id`,
`ts`, `context`.

### `links`
Cross-layer graph: `src_kind`, `src_id`, `predicate`, `dst_kind`, `dst_id`,
`evidence`, `confidence`, `created_at`. e.g. `memory → grounded_in → wiki`.

### `procedures`
Reserved for learned procedures: `id`, `name`, `steps`, `trigger`,
`source_sessions`, `times_seen`, `created_at`, `updated_at`.

### `meta`
`key` PK, `value`. Holds `schema_version` (and, in future, `import_complete`).

## Indexes

`idx_memories_status(status)`, `idx_session_events_session(session_id)`,
`idx_links_src(src_kind, src_id)`.
