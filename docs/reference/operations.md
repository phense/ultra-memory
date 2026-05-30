# Operations reference

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
A future replay step drains the spool; until then, the loud failure ensures the
loss is visible, not silent. (Hands-off operation should wire `WriteSpooled` to an
alert — see the spec's observability section.)

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
