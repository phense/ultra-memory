# Usage

All examples assume you opened a database with `open_memory_db`, which connects
(WAL, busy-timeout, foreign keys) and runs any pending migrations. **The caller
owns the connection and must close it.**

```python
from ultra_memory import memory_lib, memory_query, memory_import, memory_export

conn = memory_lib.open_memory_db("data/memory.db")
try:
    ...
finally:
    conn.close()
```

`ts` arguments are ISO-8601 strings the caller supplies (the engine never reads the
clock itself — that keeps it deterministic and testable).

## Save a memory

```python
memory_lib.save_memory(
    conn,
    id="feedback-oauth-only",        # stable identity (used for cross-links)
    type="feedback",
    title="OAuth only",
    body="Every LLM call uses the claude CLI, never the API.",
    ts="2026-05-30T10:00:00",
)
```

`save_memory` is an upsert: a new `id` inserts, an existing one updates content
while **preserving** a `deleted`/`redirect` tombstone. All text passes through the
secret-redactor first, and every mutation writes an `audit_log` row. Optional
fields: `description`, `index_hook`, `node_type`, `file_slug`, `sort_order`,
`origin_session_id`, and `created_at`/`updated_at` overrides (default to `ts`).

## Query memories

```python
results = memory_query.query_memories(
    conn, "how do we authenticate LLM calls?",
    embedder=my_embedder,            # list[str] -> list[list[float]]
    top_k=5,
    now_ts="2026-05-30T12:00:00",    # for the staleness signal
)
# -> [{"id", "title", "type", "status", "score", "stale", "links"}, ...]
```

Ranking = embedding cosine, **plus** a fixed boost when the title appears as a whole
token in the query, **then** ×strength, +bounded access boost, −staleness penalty.
Deleted/redirect memories are excluded by default. No LLM is involved.

For production, get the real embedder lazily:

```python
from ultra_memory import retrieval_core
embedder = retrieval_core.default_embedder()   # needs the [retrieval] extra
```

## Record a session event

```python
memory_lib.record_session_event(
    conn, session_id="<uuid>", kind="task_done",
    title="Shipped the export module", ts="2026-05-30T12:30:00",
    detail="...", files=[...], refs=[...],
)
```

Idempotent: a deterministic `event_key` (over session/ts/kind/title/detail) means
re-recording the same event is a no-op.

## Consolidate / delete

```python
memory_lib.consolidate(conn, loser_id="dup", canonical_id="canon",
                       reason="duplicate", ts="...")   # redirect-stub, never removes
memory_lib.delete(conn, id="garbage", reason="...", tier="volatile", ts="...")  # soft tombstone
```

There is no automatic fuzzy-batch deletion. `consolidate` redirects; `delete`
tombstones a single id (tier `durable` or `volatile`). Hard purge is a separate,
later step.

## Import a legacy markdown tree

```python
n = memory_import.import_memory_dir(
    conn, "/path/to/memory", index_path="/path/to/memory/MEMORY.md", ts="...")
count, warnings = memory_import.import_today_file(conn, today_text, day="2026-05-29")
```

`import_memory_dir` globs `*.md` (excluding `MEMORY.md`), upserts each, and records
the file's mtime as the memory's age (so staleness is correct after a bulk import)
plus its `MEMORY.md` position (so the index order survives a round-trip).
`import_today_file` parses `## HH:MM | …` / `## HH:MM-HH:MM | …` blocks (ASCII or
en/em-dash ranges) into session events; any non-time `## ` header is captured as a
midnight block **with a warning** — never silently folded.

## Export (the rollback artifact)

```python
written = memory_export.export_memory(conn, "data/memory_export", ts="...")
```

Writes `memory.dump.sql` (redacted, carries `user_version`), a `VACUUM INTO`
binary snapshot, and regenerated markdown views (`views/<file_slug>.md` +
`views/MEMORY.md`). Skips if a content hash (excluding access telemetry) is
unchanged. The whole export is all-or-nothing; a failure leaves the prior dump
intact. See [reference/operations.md](../reference/operations.md).
