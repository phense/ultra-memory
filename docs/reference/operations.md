# Operations reference

This page covers running ultra-memory **as a plugin** (install, the wired MCP +
hooks, the bootstrap gate, self-healing maintenance, the privilege boundary, and
the venv lifecycle), then the lower-level engine operations (export artifacts,
rollback, the write spool, redaction, the embedding cache, migrations).

## Install + bootstrap

Install and configure are documented in the README's "Install as a plugin"
section. In short (**zero-config**): `/plugin install` prompts for nothing required,
then run `/ultra-memory:memory-setup` and restart Claude Code so the `knowledge` MCP registers.
The DB path auto-derives (resolution order below); set `data_db_path` only to override.

**DB-path resolution (single source of truth — `knowledge_mcp.db_path_from_env`).** The
MCP, all three session hooks (via `hooks/common.resolve_db_path`), and `maintain` all
resolve the `memory.db` path the SAME way, NEVER cwd, NEVER project-local: (1) explicit
`ULTRA_MEMORY_DB` (the `data_db_path` override, threaded through
`${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH}`) if set + non-blank; else (2) the fixed global
`~/.ultra-memory/memory.db` — the single store shared by every project. The
local-vs-project fallback (`${CLAUDE_PROJECT_DIR}/data/memory.db` → `~/.claude/memory.db`)
was retired 2026-06-01: the fabric always lives at one fixed user-path. The `.mcp.json`
env carries a bash-default (`${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH:-}`,
`${CLAUDE_PLUGIN_OPTION_CALLER_CLASS:-subagent}`) so the var ALWAYS resolves — Claude
Code rejects an MCP server whose env references an unset `${CLAUDE_PLUGIN_OPTION_*}`; an
empty `data_db_path` ⇒ blank `ULTRA_MEMORY_DB` ⇒ the engine derives the fixed global default.

`userConfig` keys (from `.claude-plugin/plugin.json`):

| Key | Required | Default | Purpose |
|---|---|---|---|
| `data_db_path` | no | `""` (auto-derive) | Optional override. Empty ⇒ the fixed global `~/.ultra-memory/memory.db` (single store shared by every project). Set an absolute path to override that location. |
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
| `ULTRA_MEMORY_BACKFILL_CMD` | A consumer's **cold-start session-cache backfill** runner (e.g. `./scripts/run_backfill.sh`). Set ⇒ `/ultra-memory:memory-setup` prints a one-time *hint* to run it (never auto-runs). Unset ⇒ never offered (greenfield-safe). Independent of the `import_complete` gate — see [`backfill_complete`](#the-import_complete-gate). Backfilled rows are `created_by='backfill_import'`. |
| `ULTRA_MEMORY_PROBE_WORKERS` | Bounded thread-pool size for the SP-10 eval-gate's hijack-direction probes (default `6`). Caps concurrent `claude -p` probe subprocesses so a full eval pass fits the maintenance window (~50 min serial → ~12 min parallel) without swamping the OAuth CLI. |
| `SP7_AGGRESSIVE_DISABLE` / `SP10_SYNTHESIS_DISABLE` | Kill switches for the autonomous self-correct / synthesize beats — any value makes the whole beat a no-op + one log line. **Present-by-default in cron** until a consumer arms the beat; remove the var to run live. `SP7_AGGRESSIVE_DRYRUN` / `SP10_SYNTHESIS_DRYRUN` plan+eval+digest but apply nothing. |
| `SESSION_INGEST_ENABLE` | Additional gate for the `session_ingest` beat (capture + SP-8 attribution); default OFF — the enqueue/fold is a no-op until set. |

`/ultra-memory:memory-setup` builds the runtime venv under `${CLAUDE_PLUGIN_DATA}/venv`,
optionally imports a legacy memory dir **once**, stamps the DB ready (the
`import_complete` gate, below), and sanity-checks. First run downloads the embedder
model (~bge-small, cached afterward).

**Prerequisites (both required, preflighted by `/ultra-memory:memory-setup`):** `uv` and `git`
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
| `/ultra-memory:memory-recall "<query>"` | Trusted full recall (CLI path; all types). |
| `/ultra-memory:memory-save` | Persist a NEW durable fact — the canonical new-fact verb (wraps `memory_lib.save_memory`). |
| `/ultra-memory:memory-pin <id>` | Make a fact always-in-context (auto-injects into the SessionStart gist; for hard rules). |
| `/ultra-memory:memory-verify <id>` | Reconfirm a fact still holds (resets the staleness signal). |
| `/ultra-memory:memory-edit <id>` | Correct a fact's body in place (body only; type/title/fields preserved). |
| `/ultra-memory:memory-inbox` | Apply queued human-correction directives (free text preserved under "Unprocessed"). |
| `/ultra-memory:memory-setup` | One-time bootstrap: venv, optional legacy import, `import_complete` stamp, optional cold-start-backfill hint, sanity-check. |
| `/ultra-memory:memory-maintain` | Force a prune+export now (the same `ultra_memory.maintain.run` the async hook throttles). |

## Wiring at a glance

- `.mcp.json` (plugin root) → the read-only `knowledge` MCP, launched from
  `${CLAUDE_PLUGIN_DATA}/venv/bin/python`. Env (`ULTRA_MEMORY_DB`,
  `ULTRA_MEMORY_CALLER_CLASS`) comes from the userConfig→`CLAUDE_PLUGIN_OPTION_*`
  bridge **with bash-default fallbacks** so both ALWAYS resolve (zero-config —
  Claude Code rejects a server whose env references an unset `${CLAUDE_PLUGIN_OPTION_*}`,
  and it does not inject manifest defaults): `ULTRA_MEMORY_DB` falls back to an
  **empty string** (`${CLAUDE_PLUGIN_OPTION_DATA_DB_PATH:-}`) and `ULTRA_MEMORY_CALLER_CLASS`
  to `subagent` (`…:-subagent`). An empty `ULTRA_MEMORY_DB` ⇒
  `knowledge_mcp.db_path_from_env` DERIVES the fixed global `~/.ultra-memory/memory.db`
  (the single store shared by every project; the old project-local / `~/.claude` fallback
  was retired 2026-06-01), and `knowledge_mcp.caller_class_from_env` fail-closes to
  `subagent`, so the MCP is safe even if `${...}` substitution does not occur on a given
  Claude Code version (the SP-0 P1-D1 uncertainty).
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
`/ultra-memory:memory-setup` stamps it (via `setup.mark_import_complete`) after the optional
legacy import — production code, not just tests, sets it now. A migrated-but-
unstamped DB intentionally fails open to the legacy path.

A separate `meta.backfill_complete` flag tracks the **optional cold-start
session-cache backfill** (a consumer-side runner declared via
`ULTRA_MEMORY_BACKFILL_CMD`). It is **deliberately independent** of
`import_complete` and **not** read by `db_ready` — `/ultra-memory:memory-setup` only uses it
to decide whether to print the one-time backfill hint
(`setup.should_offer_backfill` / `setup.backfill_hint`), and stamping it
(`setup.mark_backfill_complete`) merely silences that hint. Declining or never
running the backfill therefore never disables the session hooks.

## Maintenance (self-healing, throttled)

`/ultra-memory:memory-maintain` forces a prune+export; the async SessionStart hook runs the
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

## Self-learning maintenance beats (autonomous, OAuth-gated)

Beyond the pure-Python prune+export+`wiki_sync` above, the maintenance pipeline runs
the four-beat self-learning loop. The heavy beats make ONE OAuth call each (via
`claude_cli`, never the SDK/API), are **autonomous but conservative**, and are
governed by the code safety wall rather than a human approval step. The earlier
"ships-disabled / dry-run-first" posture is **superseded** (2026-06-03).

Each beat is enabled in `config.toml [maintenance.beats]` (every beat defaults
**ON**) and throttled by `[maintenance.cadence_hours]`. The two aggressive beats
carry an additional **present-by-default kill switch** — remove the env var (the
consumer's wrapper does this when arming) to run live; a `*_DRYRUN` variant
plans+evals+digests but applies nothing.

| Beat | Default cadence | LLM | What it does | Extra gate (beyond `beats`) |
|---|---|---|---|---|
| consolidate | 168h (weekly) | one batched call | graduate / merge / skip un-resolved `skill_learning_candidate` rows; writes `validated_as` edges; ADD-only (never rewrites) | — |
| learnings | 168h (weekly) | none (Tier-1) | regenerate each `Learnings.md` projection from `memories` rows | — |
| session_ingest | 24h (daily) | one call | capture + SP-8 attribution fold; writes `informed_by` edges | `SESSION_INGEST_ENABLE` env |
| aggressive (self-correct) | 720h (monthly) | one call | auto-edit / propose-revert / quarantine net-negative agent lessons | `SP7_AGGRESSIVE_DISABLE` kill switch (present by default) |
| synthesize | 720h (monthly) | one call | induce a `gen-<slug>` skill from a matured lesson cluster | `SP10_SYNTHESIS_DISABLE` kill switch (present by default) |

A consumer may tighten the aggressive/synthesize cadence (e.g. to 168h) in its own
`config.toml`. After the consolidate drain graduates lessons, the `learnings` beat
re-projects the augmented `Learnings.md` files on its own weekly schedule.

**The apply path enforces the wall in CODE, not the prompt** (the LLM proposes, the
code disposes):

1. **Provenance gate** — re-reads the live row; refuses any action on
   `created_by='human'`/`import`/`backfill_import` or `pinned` (a single forbidden-
   target attempt halts the whole run).
2. **Archive-never-delete** — every verb is a reversible FSM transition
   (`active → redirect`/`quarantined`/`reverted`); no `rm` anywhere.
3. **Bounded blast radius** — ≤3 edits / ≤3 reversions / ≤5 quarantines per run, max
   1 synthesized skill/run; per-period (`YYYY-MM`) caps live in `meta`; halt-on-exceed.
4. **Pre-run git checkpoint** — tags the attempt + snapshots the DB; refuses to apply
   on a dirty/untracked tree.
5. **Audit + human digest** — every action lands in `briefings_dir` (the operator is in the
   audit loop, never the write loop).
6. **Kill switch** — the present-by-default `*_DISABLE` env vars above.
7. **(synthesize only) eval-gate** — a behavioral trigger-probe proving the generated
   skill does not hijack a static skill, parallelized via `ULTRA_MEMORY_PROBE_WORKERS`
   (see [api.md](api.md#skill-eval-gate-the-7th-sp-10-mechanism)).

**Net-new domains only (synthesize).** A domain that IS an existing skill is skipped —
the cold-start backfill seeds lessons tagged with the skills they were learned *while
using*, so every backfill cluster is a same-domain competitor the eval-gate rejects.
Those lessons augment the static skill's `Learnings.md` (live); synthesis mints new
skills only for novel forward-loop domains.

## Notification / alerting {#notification-alerting}

A maintenance run is **fail-open per beat** — a beat that errors is caught, recorded
in `result.errors`, and never wedges the session. To make those caught errors
*visible*, `python -m ultra_memory.maintenance` exposes two complementary signals:

1. **Exit code** — the process returns **1** iff a beat recorded a (caught) error,
   else 0. This is the pure SIGNAL a consumer's cron wrapper can watch even for the
   paths the in-process notifier cannot cover (a `timeout`/SIGKILL of the whole run,
   an OOM, a crash before `main` returns). Keep a thin catch-all on it (e.g. mail on a
   non-zero wrapper exit) as the catastrophic net.
2. **In-process notifier hook** — on `result.errors`, `__main__` fires a pluggable
   notifier **FAIL-OPEN** (a notifier error degrades to one log line; alerting MUST
   NOT fail a maintenance run) with a structured `NotifyEvent`.

**ultra-memory ships NO transport.** The notifier is a consumer seam:

- **Config key:** `[maintenance] notifier = "module:function"` in
  `<project>/.ultra-memory/config.toml` (or `ULTRA_MEMORY_NOTIFIER` to override). It is
  resolved exactly like `wiki_linter` — `<project>/scripts` and `<project>` are put on
  `sys.path` so an in-tree module imports.
- **No-op default:** unset / unresolvable → a one-line stderr no-op (the plugin sends
  nothing). A bad hook spec logs one line and degrades to the no-op — never wedges.
- **`NotifyEvent` fields:** `kind` (`"maintenance_failure"`), `project`, `run_ts`,
  `errors` (beat → `repr(exc)`), `ran`, `skipped`, plus pre-built `subject` and `body`
  the hook can use verbatim or ignore in favor of the structured fields.
- **Transports** (the hook wires exactly one): SMTP (stdlib, headless), shelling out to
  any CLI / existing mailer, an own mail server / webhook (stdlib `urllib`), or MCP
  (Gmail / M365). **Headless reality:** the maintenance pipeline runs as a cron with no
  interactive Claude session, so a Gmail/M365 **MCP tool cannot be called directly** —
  route it through a `claude -p` bridge with the MCP servers loaded (heavier; costs
  OAuth tokens).
- **OAuth-only invariant** (project hard rule): a notifier MUST NOT import the
  `anthropic` SDK or use an API key. The `claude -p` path is OAuth via the CLI;
  SMTP/webhook touch no Anthropic surface at all.

Copy `ultra_memory/maintenance/notify.py::example_notifier` — it is a `COPY ME`
template carrying all four transport snippets. The same seam is the future
generalization of the `WriteSpooled` alert (see [Write spool](#write-spool)):
today `WriteSpooled` surfaces loudly via the exit-code signal; a later change can route
it through this same notifier.

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
  only behind the operator's explicit go, in the same paused-cron + git-checkpoint
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
the `/ultra-memory:memory-recall` CLI, not the MCP — there is no `orchestrator` MCP instance
this cycle (`[[feedback_subagents_can_leak_secrets]]`). The per-subagent
topic-identity mechanism is the unresolved SP-0 spike #7; the env-var fallback is
the locked interim, and the fail-closed default means the degraded mode is safe
(sees less, never more).

## venv lifecycle

The venv lives under `${CLAUDE_PLUGIN_DATA}` (survives plugin updates, SP-0
P1-D4). If a plugin update or a manual cleanup removes it, the wrapper fails open
(SessionStart prints "venv missing → run /ultra-memory:memory-setup") and `/ultra-memory:memory-setup`
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

## Write spool (`<db_dir>/memory_spool/`) {#write-spool}

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
in **one explicit `BEGIN/COMMIT`** transaction — so a mid-migration crash rolls back
the whole step rather than leaving the schema and `user_version` desynchronized.
`ADD COLUMN` statements use `IF NOT EXISTS` (replay-tolerant), and the dump appends
`PRAGMA user_version` so a restore→reopen round-trips the version without re-running
migrations. The full list (`0001`–`0008`) with per-version DDL scope lives in
[schema.md](schema.md#migrations). To add one: drop `NNNN_name.sql` in
`ultra_memory/migrations/`, make it idempotent, and add a test (including a mid-
migration-crash rollback regression). An automatic export should run before any
migration in production so a failed migration has a non-lossy fallback.
