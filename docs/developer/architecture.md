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
| `migrations/*.sql` | Ordered `NNNN_name.sql`. `0001` initial schema, `0002` import-fidelity columns, `0003` harness slug + sort order. |
| `memory_lib.py` | **The only writer.** Every mutation: redact → `BEGIN IMMEDIATE`/`COMMIT` with retry+spool → `audit_log`. |
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
  - retries on `SQLITE_BUSY`/`database is locked` with exponential backoff,
  - rolls back defensively only when a transaction is actually active (no
    double-ROLLBACK masking the real error),
  - surfaces non-busy errors immediately,
  - on retry exhaustion spools the operation to `<db_dir>/memory_spool/<hash>.json`
    and raises `WriteSpooled` **loudly** — never a silent drop.
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
single batched call + one write txn (`get_or_embed_batch`), ranks, and attaches
1-hop links. The embedder is always injected.

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

Built + tested: `db`, `migrations`, `memory_lib`, `redact_secrets`,
`retrieval_core`, `memory_query`, `memory_import`, `memory_export`, `claude_cli`,
`retention`, and the session `hooks` (`common`, `checkpoint`, `rehydrate`).
Future (Plans 5 cutover onward): the **live** bootstrap import + shadow→cutover
wiring behind `meta.import_complete` (consumer-side; the hook logic is built and
unit-tested here), the `knowledge` MCP server (read-only, writes proxied to a
short-lived `memory_lib`), the wiki write-gateway, and plugin packaging/publish.
