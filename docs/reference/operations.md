# Operations reference

This page covers running ultra-memory **as a plugin** (install, the wired MCP +
hooks, the bootstrap gate, self-healing maintenance, the privilege boundary, and
the venv lifecycle), then the lower-level engine operations (export artifacts,
rollback, the write spool, redaction, the embedding cache, migrations).

## Install + bootstrap

Install and configure are documented in the README's "Install as a plugin"
section. In short: `/plugin install`, set the one required `userConfig` value
`data_db_path` (absolute path to the consumer's canonical `memory.db`), then run
`/memory-setup` and restart Claude Code so the `knowledge` MCP registers.

`userConfig` keys (from `.claude-plugin/plugin.json`):

| Key | Required | Default | Purpose |
|---|---|---|---|
| `data_db_path` | yes | — | Absolute path to the consumer's canonical `memory.db`; the MCP + hooks read this. |
| `caller_class` | no | `subagent` | MCP recall privilege class. Fail-closed: `subagent` ⇒ `project`/`reference` only. |
| `rehydrate_budget` | no | `2000` | Character budget for the SessionStart rehydration gist. |
| `oauth_token` | no | — | OAuth token, NEVER an `ANTHROPIC_API_KEY`. Only for LLM maintenance; the prune+export slice does not use it. |

`/memory-setup` builds the runtime venv under `${CLAUDE_PLUGIN_DATA}/venv`,
optionally imports a legacy memory dir **once**, stamps the DB ready (the
`import_complete` gate, below), and sanity-checks. First run downloads the embedder
model (~bge-small, cached afterward) — `uv` on PATH and Python 3.13 are
prerequisites.

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
  bridge. `knowledge_mcp.caller_class_from_env` fail-closes to `subagent`, so the
  MCP is safe even if `${...}` substitution does not occur on a given Claude Code
  version (the SP-0 P1-D1 uncertainty).
- `hooks/hooks.json` → `hooks/um-hook.cmd {rehydrate|maintain|checkpoint}`,
  wired as SessionStart (rehydrate sync + maintain async) and Stop (checkpoint).
  The wrapper resolves all env explicitly (P1-D1, deterministic — no reliance on
  `${user_config.*}` substitution) and is **fail-open**: a missing venv or any
  error exits 0 so a session is never blocked. It exports `ULTRA_MEMORY_SHADOW=0`
  for live gist injection (the engine default is shadow=1).

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

## Privilege boundary

`caller_class=subagent` (the default) ⇒ MCP recall is type-scoped to
`project`/`reference` only, never `user`/`feedback`. Trusted full recall is the
`/memory-recall` CLI, not the MCP — there is no `orchestrator` MCP instance this
cycle (`[[feedback_subagents_can_leak_secrets]]`).

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
only — it **excludes** `access_count`/`last_accessed`/`access_log`, so reinforcement
telemetry never drives a commit.

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
