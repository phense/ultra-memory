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
  sort_order=None, created_at=None, updated_at=None) -> id` — redact → upsert →
  audit. Update preserves a `deleted`/`redirect` status. `created_at`/`updated_at`
  default to `ts`.
- `record_session_event(conn, *, session_id, kind, title, ts, detail=None,
  files=None, refs=None, session_fields=None) -> event_key` — ensure session row,
  append event idempotently (UNIQUE `event_key`).
- `record_access(conn, *, target_kind, target_id, ts, context=None)` — append to
  `access_log` + atomic `access_count += 1` for memory targets.
- `consolidate(conn, *, loser_id, canonical_id, reason, ts)` — redirect-stub
  (`status='redirect'`, `supersedes=canonical`). Raises `KeyError` if absent.
- `delete(conn, *, id, reason, tier, ts)` — soft tombstone (`status='deleted'`).
  `tier` ∈ {`durable`, `volatile`}; `ValueError` otherwise, `KeyError` if absent.
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
  clear `RuntimeError` if the `[retrieval]` extra is absent.

## `memory_query`

- `query_memories(conn, query, *, embedder, top_k=5, dim=EMBED_DIM,
  include_statuses=("active",), now_ts=None, staleness_days=90) -> [dict]` — cosine
  rank + word-bounded title boost (+0.5), then ×strength, +bounded access boost,
  −staleness penalty (sets `stale`); attaches 1-hop `links`. Returns dicts with
  `id, title, type, status, score, stale, links`. No LLM.

## `memory_import`

- `split_frontmatter(text) -> (dict, body)` — no-YAML parser for the known memory
  frontmatter (flat keys + nested `metadata`); tolerant of the `metadata: ` trailing
  space and body `---` lines.
- `parse_memory_index(text) -> {slug: {title, hook}}` — parse `MEMORY.md` lines.
- `import_memory_dir(conn, memory_dir, *, index_path=None, ts) -> count` — glob
  `*.md` (excluding `MEMORY.md`), upsert each; set `file_slug`=stem,
  `sort_order`=index position, `created_at`/`updated_at`=file mtime. Idempotent.
- `import_today_file(conn, text, *, day) -> (count, warnings)` — parse `## HH:MM` /
  `## HH:MM[-–—]HH:MM | …` blocks + non-time `## ` headers (captured at midnight
  with a warning) into `legacy-<day>` session events; dedupe within the run so
  `count` reflects rows recorded. Never crashes.

## `memory_export`

- `export_memory(conn, out_dir, *, ts, snapshot=True) -> bool` — read snapshot →
  redacted `memory.dump.sql` (carries `user_version`) → `VACUUM INTO` snapshot →
  `views/<file_slug>.md` + `views/MEMORY.md` (ordered by `sort_order`) → content
  hash last. Atomic (tmp→replace, snapshot-first). Returns False if unchanged
  (hash excludes access telemetry).

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
