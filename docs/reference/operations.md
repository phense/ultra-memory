# Operations reference

This page covers running ultra-memory **as a plugin** (install, the wired MCP +
hooks, the bootstrap gate, self-healing maintenance, the privilege boundary, and
the venv lifecycle), then the lower-level engine operations (export artifacts,
rollback, the write spool, redaction, the embedding cache, migrations).

## Install + bootstrap

Install and configure are documented in the README's "Install as a plugin"
section. In short (**zero-config**): `/plugin install` prompts for nothing required,
then run `/memory-setup` and restart Claude Code so the `knowledge` MCP registers.
The DB path auto-derives (resolution order below); set `data_db_path` only to override.

**DB-path resolution (single source of truth — `knowledge_mcp.db_path_from_env`).** The
MCP, all three session hooks (via `hooks/common.resolve_db_path`), and `maintain` all
resolve the `memory.db` path the SAME way, NEVER cwd, NEVER project-local: (1) explicit
`ULTRA_MEMORY_DB` (the `data_db_path` override, threaded through
`${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH}`) if set + non-blank; else (2) the fixed global
`~/.ultra-knowledge/memory.db` — the single store shared by every project. The
local-vs-project fallback (`${CLAUDE_PROJECT_DIR}/data/memory.db` → `~/.claude/memory.db`)
was retired 2026-06-01: the fabric always lives at one fixed user-path. The `.mcp.json`
env carries a bash-default (`${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH:-}`,
`${CLAUDE_PLUGIN_OPTION_CALLER_CLASS:-subagent}`) so the var ALWAYS resolves — Claude
Code rejects an MCP server whose env references an unset `${CLAUDE_PLUGIN_OPTION_*}`; an
empty `data_db_path` ⇒ blank `ULTRA_MEMORY_DB` ⇒ the engine derives the fixed global default.

`userConfig` keys (from `.claude-plugin/plugin.json`):

| Key | Required | Default | Purpose |
|---|---|---|---|
| `data_db_path` | no | `""` (auto-derive) | Optional override. Empty ⇒ the fixed global `~/.ultra-knowledge/memory.db` (single store shared by every project). Set an absolute path to override that location. |
| `caller_class` | no | `subagent` | MCP recall privilege class (the **type** axis). Fail-closed: `subagent` ⇒ `project`/`reference` only. |
| `rehydrate_budget` | no | `2000` | Character budget for the SessionStart rehydration gist. |
| `oauth_token` | no | — | OAuth token, NEVER an `ANTHROPIC_API_KEY`. Only for LLM maintenance; the prune+export slice does not use it. |

Engine env seams the wrapper / consumer can also set (SP-3):

| Env var | Purpose |
|---|---|
| `ULTRA_MEMORY_WIKI_ROOTS` | `os.pathsep`- or comma-separated Expert-Knowledge root paths. Read by `maintain.run` → `wiki_sync`. **Unset ⇒ `wiki_sync` is skipped** and a pure-memory deployment is byte-identically unaffected. |
| `ULTRA_MEMORY_CALLER_TOPIC` | Comma/`:`/`;`-separated topic list — the **topic** axis of the access wall (the interim source until SP-0 spike #7 resolves per-subagent identity). Fail-closed: unset + no `agent_topic_bindings` row ⇒ the empty topic set. |
| `ULTRA_MEMORY_AGENT_NAME` | Agent name for the `agent_topic_bindings` lookup (`topic_scope_from_env`). |
| `ULTRA_MEMORY_REBUILD_INDEX` | `=1` ⇒ `maintain` forces a one-pass re-population of every `unified_index` row regardless of `content_sha256` (the SP-6 `bm25_text` backfill, equivalent to `maintain --rebuild`). Implies force (bypasses the throttle). |

`/memory-setup` builds the runtime venv under `${CLAUDE_PLUGIN_DATA}/venv`,
optionally imports a legacy memory dir **once**, stamps the DB ready (the
`import_complete` gate, below), and sanity-checks. First run downloads the embedder
model (~bge-small, cached afterward).

**Prerequisites (both required, preflighted by `/memory-setup`):** `uv` and `git`
on PATH — declared as `setup.REQUIRED_TOOLS`; `setup.missing_prerequisites()` is
the testable mirror and the command's step-0 shell preflight aborts if either is
missing.
- `uv` provisions the Python 3.13 runtime + extras. The engine is pure
  Python 3.13 + SQLite — **no other binary is shelled**.
- `git` is the **rollback/safety model**, not a runtime call: the deterministic
  export (`memory.dump.sql` + VACUUM snapshot + views) is *the sole git-committed
  rollback artifact* (see [Rollback](#rollback) / [Export artifacts](#export-artifacts-datamemory_export)),
  and the wiki/maintenance lifecycle is archive-never-delete *via git*. Without
  git the engine still runs, but there is no restore net — so it is a hard
  prerequisite.

## Command surface

All write paths go through verbs — never raw SQL. The `using-memory` skill teaches
agents which to use; this is the operator-facing list.

| Command | What it does |
|---|---|
| `/memory-recall "<query>"` | Trusted full recall (CLI path; all types). |
| `/memory-save` | Persist a NEW durable fact — the canonical new-fact verb (wraps `memory_lib.save_memory`). |
| `/memory-pin <id>` | Make a fact always-in-context (auto-injects into the SessionStart gist; for hard rules). |
| `/memory-verify <id>` | Reconfirm a fact still holds (resets the staleness signal). |
| `/memory-edit <id>` | Correct a fact's body in place (body only; type/title/fields preserved). |
| `/memory-inbox` | Apply queued human-correction directives (free text preserved under "Unprocessed"). |
| `/memory-setup` | One-time bootstrap: venv, optional legacy import, `import_complete` stamp, sanity-check. |
| `/memory-maintain` | Force a prune+export now (the same `ultra_memory.maintain.run` the async hook throttles). |

## Wiring at a glance

- `.mcp.json` (plugin root) → the read-only `knowledge` MCP, launched from
  `${CLAUDE_PLUGIN_DATA}/venv/bin/python`. Env (`ULTRA_MEMORY_DB`,
  `ULTRA_MEMORY_CALLER_CLASS`) comes from the userConfig→`CLAUDE_PLUGIN_OPTION_*`
  bridge **with bash-default fallbacks** so both ALWAYS resolve (zero-config —
  Claude Code rejects a server whose env references an unset `${CLAUDE_PLUGIN_OPTION_*}`,
  and it does not inject manifest defaults): `ULTRA_MEMORY_DB` falls back to
  `${CLAUDE_PROJECT_DIR}/data/memory.db` and `ULTRA_MEMORY_CALLER_CLASS` to `subagent`.
  `knowledge_mcp.db_path_from_env` then DERIVES the same default (belt-and-suspenders for
  the user-scope case where `CLAUDE_PROJECT_DIR` is empty), and
  `knowledge_mcp.caller_class_from_env` fail-closes to `subagent`, so the MCP is safe even
  if `${...}` substitution does not occur on a given Claude Code version (the SP-0 P1-D1
  uncertainty).
- `hooks/hooks.json` → `hooks/um-hook.cmd {rehydrate|maintain|checkpoint}`,
  wired as SessionStart (rehydrate sync + maintain async) and Stop (checkpoint).
  The wrapper resolves all env explicitly (P1-D1, deterministic — no reliance on
  `${user_config.*}` substitution) and is **fail-open**: a missing venv or any
  error exits 0 so a session is never blocked. It exports `ULTRA_MEMORY_SHADOW=0`
  for live gist injection (the engine default is shadow=1). The hooks share the MCP's
  DB-path derivation (via `hooks/common.resolve_db_path` → `knowledge_mcp.db_path_from_env`),
  so a zero-config install resolves the same `memory.db` for the MCP and all three hooks.

## The `import_complete` gate

The session hooks no-op until `meta.import_complete='1'` (`hooks/common.db_ready`).
`/memory-setup` stamps it (via `setup.mark_import_complete`) after the optional
legacy import — production code, not just tests, sets it now. A migrated-but-
unstamped DB intentionally fails open to the legacy path.

## Maintenance (self-healing, throttled)

`/memory-maintain` forces a prune+export; the async SessionStart hook runs the
same `ultra_memory.maintain.run` throttled (≤ once per ~20h via
`meta.last_maintenance`). Pure Python — no LLM, no OAuth token. Retention rolls
old `session_events` into per-session summaries before deleting, so the digest
survives while raw rows stay bounded. Retention window: `maintain._KEEP_DAYS`
(90d default); export dir defaults to `<db-dir>/memory_export/views`
(override with `ULTRA_MEMORY_EXPORT_DIR`).

**`maintain.run` is the write-spool drainer.** Before pruning/exporting, `maintain.run`
calls `memory_lib.replay_spool(conn)` on its own connection — the single, serialized
nightly entry is a safe single drainer (no concurrent double-apply of the
non-idempotent `record_access` increment). This re-applies any busy-casualty write
left in `<db-dir>/memory_spool/` (e.g. a Stop-hook `record_session_event` lost to a
transient `SQLITE_BUSY`), so a spooled write self-heals on the next maintenance pass
instead of rotting. The drain is the **only** production caller of `replay_spool`; the
return carries a `spool_replay` summary `{replayed, failed, errors}`. Fail-open: a
replay error is logged and maintenance continues into prune/export (it never aborts).

**Bounded busy-retry for maintenance writes.** `retention.prune_session_events` and
`maintain._set_meta` route their `BEGIN IMMEDIATE` through the same shared bounded
retry-with-backoff discipline as the `memory_lib` write path
(`memory_lib._with_immediate_retry`, the loop extracted from `_write_txn`). A transient
`SQLITE_BUSY` from a writer holding the lock past the `busy_timeout` window is retried,
not raised immediately. No spool is used for these (idempotent maintenance writes — a
retry is enough); a final exhaustion still rolls back and is caught by `maintain.run`'s
broad fail-open `try/except`.

**SP-3 — `wiki_sync` inside the same throttle.** When `ULTRA_MEMORY_WIKI_ROOTS`
is set, `maintain.run` also mirrors the Expert-Knowledge pages into `unified_index`
(and embeds their `title+snippet` into the shared `embeddings` cache as
`target_kind='knowledge'`). It is idempotent (a `content_sha256` match skips a page
+ skips re-embed), reconciling (orphan rows whose file vanished are pruned, scoped
to the topics synced this call — the Risk §14.4 drift guard), and fail-open (a sync
error lands as `result["wiki_sync"]["error"]` and never blocks). The return then
carries a `wiki_sync` summary `{upserted, skipped, pruned, embedded, errors}`
beside `{pruned, exported, skipped}`. With no roots configured the sync is skipped
entirely.

**SP-6 — `--rebuild` one-pass backfill.** `python -m ultra_memory.maintain --rebuild`
(or `ULTRA_MEMORY_REBUILD_INDEX=1`) forces every `unified_index` row to re-populate
regardless of `content_sha256` — the backfill for `unified_index.bm25_text` (the
full-body BM25 column added in migration `0005`) on rows written by the pre-SP-6
`wiki_sync`. A rebuild implies force (else the ~20h throttle would skip the run it
was invoked to perform). A normal nightly sync repopulates `bm25_text` lazily on the
next content change of each page.

## Topic backfill (gated) {#topic-backfill-gated}

`memory_lib.backfill_topic(conn, *, default_topic, ts)` stamps `topic =
default_topic` on every `memories` row that is `topic IS NULL AND type NOT IN
('user','feedback')` (operational rows stay cross-topic `NULL`). It is the **one
data step** SP-3's `0004` migration deliberately keeps out of the `.sql`:

- **Idempotent + guarded:** a `meta.topic_backfill_complete` flag short-circuits a
  re-run (mirrors `import_complete`).
- **Audited per row** + **git-reversible** (the export dump + `audit_log` + clearing
  the flag undo it).
- `default_topic` is **consumer-supplied** (content-free in the engine; Trading →
  `trading`).
- **Gated on sign-off (spec §10):** it touches the live canonical store, so it runs
  only behind Peter's explicit go, in the same paused-cron + git-checkpoint
  discipline SP-2's wiki re-root used. The `0004` DDL itself is non-destructive and
  can land first; only this data step gates.

## Privilege boundary

The access wall has **two orthogonal axes** (composed by AND):

- **Type** (SP-0/SP-1): `caller_class=subagent` (the default) ⇒ MCP recall is
  type-scoped to `project`/`reference` only, never `user`/`feedback`.
- **Topic** (SP-3): a caller sees a fact only if `topic ∈ agent_topics OR topic IS
  NULL`. `agent_topics` comes from `ULTRA_MEMORY_CALLER_TOPIC` /
  `agent_topic_bindings` (`topic_scope_from_env`), **fail-closed** — no binding ⇒
  the empty set ⇒ only `topic IS NULL` operational memories of allowed types, and
  **zero topiced knowledge**. The cross-store `unified_recall` (the MCP routes to it
  when topic bindings are present) enforces both axes.

`visible(fact) ⟺ (topic ∈ agent_topics OR topic IS NULL) AND (type ∈
allowed_types_for(caller_class))`. Trusted full recall (all topics + all types) is
the `/memory-recall` CLI, not the MCP — there is no `orchestrator` MCP instance
this cycle (`[[feedback_subagents_can_leak_secrets]]`). The per-subagent
topic-identity mechanism is the unresolved SP-0 spike #7; the env-var fallback is
the locked interim, and the fail-closed default means the degraded mode is safe
(sees less, never more).

## venv lifecycle

The venv lives under `${CLAUDE_PLUGIN_DATA}` (survives plugin updates, SP-0
P1-D4). If a plugin update or a manual cleanup removes it, the wrapper fails open
(SessionStart prints "venv missing → run /memory-setup") and `/memory-setup`
idempotently re-bootstraps it.

## Export artifacts (`data/memory_export/`)

`export_memory` produces, in this order (all-or-nothing):

1. **read snapshot** — `wal_checkpoint(TRUNCATE)`, then one `BEGIN` read txn covers
   the content hash, the dump, and the view rows (consistent point-in-time).
2. **`memory.snapshot.db`** — `VACUUM INTO` binary snapshot. Gitignored. Preserves
   `user_version`. Runs first, so a failure here touches nothing else.
3. **`views/<file_slug>.md`** + **`views/MEMORY.md`** — regenerated markdown,
   filenames + index links from `file_slug`, ordered by `sort_order` (NULLs last).
4. **`memory.dump.sql`** — written to `…​.tmp` then `os.replace`d into place
   (atomic). Redacted; ends with `PRAGMA user_version=N;`. **This is the committed
   rollback artifact.**
5. **`content.hash`** — written LAST. If the run is interrupted before this, the
   next run re-exports (rather than skipping on a stale hash).

The content hash covers the stable projection of `memories` + `session_events`
only — it **excludes** `access_count`/`last_accessed`/`last_verified`/`access_log`,
so reinforcement telemetry never drives a commit. It **includes** the
semantically-meaningful outcome fields (`memories.outcome_weight`,
`session_events.outcome_signal`), so an audited `set_outcome_weight` / outcome-signal
write — which changes recall ranking / carries attribution evidence — re-exports
rather than leaving the committed rollback dump stale (SP-8 bughunt).

## Rollback

The live `.db` is gitignored; the committed `memory.dump.sql` is the rollback
source. To roll back:

```bash
sqlite3 data/memory.db < data/memory_export/memory.dump.sql   # into a fresh/empty db
```

The dump carries `user_version`, so reopening via `open_memory_db` does **not**
re-run migrations against an already-shaped schema (which would otherwise fail with
`duplicate column name`). Restoring also recovers the `audit_log` and embedding
BLOBs (the markdown views do not).

## Write spool (`<db_dir>/memory_spool/`)

If a write cannot get the lock after the bounded retries, `_write_txn` writes the
operation's intent to `memory_spool/<hash>.json` and raises `WriteSpooled`. The
file is keyed by content hash (a retried-then-spooled op writes one stable file).
`memory_lib.replay_spool(conn)` drains the spool: it re-applies each spooled write
via its op (deleting the file on success), leaves a still-failing op spooled (it
re-writes the same content-hash file — no duplicate) and records it, and keeps any
unknown/corrupt record rather than dropping it. Returns `{replayed, failed, errors}`.
The loud `WriteSpooled` failure still ensures the original loss is visible, not
silent. (Hands-off operation should also wire `WriteSpooled` to an alert — see the
spec's observability section.)

**The drainer is `maintain.run`** (see *Maintenance* above): it calls `replay_spool`
on its own connection at the top of every (non-throttled) run. Because maintenance is
the single serialized nightly entry, it is a safe single drainer — no other writer
double-applies a non-idempotent op concurrently. This is the only production caller of
`replay_spool`, so a spooled busy-casualty write self-heals on the next maintenance
pass rather than persisting indefinitely in `memory_spool/`.

## Redaction policy

Secrets are stripped twice: at the write chokepoint (every persisted text field)
and over the entire export dump (catching columns no write-path writer redacts
yet). Keeping the live `.db` gitignored + redacting at export means secrets stay
out of git history without needing a history rewrite. If one ever slips into
history, the documented break-glass is `git filter-repo --invert-paths` + force
push (and a follow-up migration to remap stored `commit_shas`).

## Embedding cache

Keyed by `(target_kind, target_id, model_name)`; invalidated when
`content_sha256(text)` changes. The `(model_name, dim)` invariant is enforced — a
drift raises `ValueError` loudly rather than silently mixing dimensions. Vectors
are float32 BLOBs. Use the canonical `EMBED_MODEL` string everywhere.

## Migrations

Forward-only, ordered by the numeric filename prefix, each applied + version-bumped
in one transaction. To add one: drop `NNNN_name.sql` in `ultra_memory/migrations/`,
make it idempotent where possible, and add a test. An automatic export should run
before any migration in production so a failed migration has a non-lossy fallback
(wiring is part of the bootstrap plan).
