# Variables & Tunable Constants Reference

This is the **complete reference** for every configuration variable and key tunable
constant in ultra-memory: environment variables, `.ultra-memory/config.toml` keys,
`plugin.json` `userConfig` options, and the code constants worth knowing. Each entry lists
**name · where it lives · default · type · one-line meaning.**

For the *why* behind these knobs, see [`design-decisions.md`](design-decisions.md); for how
they wire into a running install, see [`../reference/operations.md`](../reference/operations.md).

---

## Resolution order (read this first)

For every tunable parameter, precedence is **highest wins**:

1. **Environment variable** (`os.environ.get`) — overrides everything.
2. **`.ultra-memory/config.toml`** (the `[maintenance]` section) — the consumer project's config.
3. **Code default** — the hardcoded `_DEFAULT_*` constants in the Python modules.

A few well-defined special cases:

| Parameter | Resolution chain |
|---|---|
| `db_path_from_env` | explicit `ULTRA_MEMORY_DB` **→** `~/.ultra-memory/memory.db` (global default; **never** cwd, never project-local — a safety property) |
| `session_id_from_env` | `ULTRA_MEMORY_SESSION_ID` **→** `CLAUDE_CODE_SESSION_ID` **→** `None` |
| fastembed cache | `ULTRA_MEMORY_FASTEMBED_CACHE` **→** `FASTEMBED_CACHE_PATH` **→** `~/.cache/ultra-memory/fastembed` |

Conventions that hold everywhere:

- **Blank / whitespace-only env values are treated as unset** (fall through to the next level).
- **Boolean env flags use the `_env_truthy` rule:** `1` / `true` / `yes` = ON; anything else = OFF.
- **Config errors fail-open to defaults.** Malformed TOML or a missing file never crashes — it
  falls back to code defaults.

### Zero-config global defaults

With **no config file and no env vars**, the plugin runs safely out of the box:

- Memory store: **`~/.ultra-memory/memory.db`** (one global DB, shared by all projects).
- Model: **`claude-sonnet-4-6`** (OAuth-only, maintenance beats).
- Rehydration budget: **2000 chars**. Shadow mode: **ON** (gist logged to file, not injected).
- Caller class: **`subagent`** (fail-closed → `SAFE_TYPES` only).
- Attribution: **OFF**. Session-ingest: **OFF**. Wiki: **empty roots** (no wiki without config).
- Beats: all enabled, but aggressive/synthesis gated by kill switches present-by-default in cron.
- Cadences: weekly consolidate; monthly aggressive/synthesize; daily session-ingest/wiki-maintenance.
- Bounds: 3 edits / 3 reversions / 5 quarantines per run; 1 skill induced per run, 2 per period.
- Probes: `RUNS_PER_QUERY=3`, `THETA_DESC=0.6`, `PROBE_MAX_WORKERS=6`.
- Retention: `session_events` pruned after ~90 days (via the maintain beat), excluding events
  referenced by attribution edges.

---

## Core paths + database location

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `ULTRA_MEMORY_DB` | env / `config.py` | `~/.ultra-memory/memory.db` | path | Explicit override for the `memory.db` location; blank = global default; **never the cwd** (safety property). |
| `ULTRA_MEMORY_EXPORT_DIR` | env | `<db_parent>/memory_export` | path | Memory-export output directory (dump + snapshot + views). |
| `CLAUDE_PROJECT_DIR` | env | cwd | path | Project root used to resolve relative paths in `config.toml`. |
| `ULTRA_MEMORY_REBUILD_INDEX` | env | `"0"` | `"0"\|"1"` | `=1` forces a one-pass embedding-cache / `bm25_text` re-population on startup (SP-6 backfill). |
| `ULTRA_MEMORY_MAINTAIN_FORCE` | env | `"0"` | `"0"\|"1"` | `=1` forces a maintenance beat to run despite its cadence throttle. |

## Database connection discipline

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `busy_timeout_ms` | `db.connect` param | `30000` | int | SQLite `PRAGMA busy_timeout` in ms (30 s retry window for contended statements). |
| `retries` | `memory_lib._with_immediate_retry` param | `5` | int | Bounded retry count for `BEGIN IMMEDIATE` on `SQLITE_BUSY`. |
| `base_delay` | `memory_lib._with_immediate_retry` param | `0.05` | float | Initial backoff (s); exponential `base * 2^attempt`. |
| `journal_mode` | `db.connect` check | `WAL` | enum | Enforced journal mode; **fail-loud** if WAL is unavailable (e.g. a network FS). |

## Caller + privilege boundary

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `ULTRA_MEMORY_CALLER_CLASS` | env / `plugin.json` userConfig | `"subagent"` | string | Knowledge-MCP privilege class. **Fail-closed:** only an explicit `orchestrator`/`owner` unlocks `ALL_TYPES`; anything else → `SAFE_TYPES` (`project`/`reference` only). |
| `SAFE_TYPES` | `knowledge_mcp.py:16` | `("project", "reference")` | tuple[str] | Memory types an untrusted caller (subagent/cron) may recall. |
| `ALL_TYPES` | `knowledge_mcp.py:17` | `("project","reference","user","feedback")` | tuple[str] | Full set; unlocked only for trusted callers. |
| `_TRUSTED` | `knowledge_mcp.py:18` | `{"orchestrator","owner"}` | frozenset | The privileged caller classes. |
| `ULTRA_MEMORY_AGENT_ROLE` | env | (unset) | string | Runtime role hint; stripped, **not** a privilege gate (`caller_class` wins). |
| `ULTRA_MEMORY_AGENT_NAME` | env | (unset) | string | Session's agent identifier; used for topic-binding lookup. |

## Session + rehydration

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `ULTRA_MEMORY_SESSION_ID` | env | (unset) | string | Explicit session-id override; resolution: this **→** `CLAUDE_CODE_SESSION_ID` **→** `None`. |
| `CLAUDE_CODE_SESSION_ID` | env | (unset) | string | Native Claude Code session id (ambient; anti-recursion-stripped on outbound CLI calls). |
| `ULTRA_MEMORY_REHYDRATE_BUDGET` | env / `plugin.json` userConfig | `2000` | int | Char budget for the SessionStart rehydration gist (over budget = tail-cut). |
| `ULTRA_MEMORY_SHADOW` | env | `"1"` | `"0"\|"1"` | Shadow mode: `1` = log gist to file, no injection (**safe default**); `0` = live inject. |
| `ULTRA_MEMORY_SHADOW_OUT` | env | (unset) | path | Shadow-mode gist output file (used only when `ULTRA_MEMORY_SHADOW=="1"`). |
| `_FIELD_MAX` | `rehydrate.py:18` | `200` | int | Max chars for any single gist field (title/summary/label) — prevents structure injection. |
| `_PIN_MEM_CAP` | `rehydrate.py:21` | `12` | int | Max pinned-memory lines in the gist before a `(N more omitted)` marker. |
| `_SUMMARY_MAX_LINES` | `retention.py:10` | `200` | int | Max lines in a `sessions.summary` digest (bounds growth). |

---

## Self-learning loop — SP-7 aggressive (self-correction)

> The highest-blast-radius autonomous verbs. The kill switch is **present-by-default in
> cron** (disabled); the bounds **halt-on-exceed** rather than truncate. See
> [`design-decisions.md`](design-decisions.md) §5–§6 for the six-mechanism wall.

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `SP7_AGGRESSIVE_DISABLE` | env | **present** in cron until opt-in | presence | Kill switch: any value = whole pass is a no-op + one log line (fail-soft). |
| `SP7_AGGRESSIVE_ENABLE` | env | (unset) | presence | The explicit arm flag (must be set to run the beat live). |
| `SP7_AGGRESSIVE_DRYRUN` | env | (unset) | presence | Dry-run: plans + evals + digests, applies **nothing**. Safe validation. |
| `MAX_EDITS_PER_RUN` | `aggressive_bounds.py:53` | `3` | int | Per-run cap on memory edits (halt-on-exceed). |
| `MAX_REVERSIONS_PER_RUN` | `aggressive_bounds.py:54` | `3` | int | Per-run cap on reversions. |
| `MAX_QUARANTINES_PER_RUN` | `aggressive_bounds.py:55` | `5` | int | Per-run cap on quarantine pairs (for manual adjudication). |
| `_PERIOD_META_PREFIX` | `aggressive_bounds.py:66` | `"sp7_aggressive_period"` | string | Namespace prefix for the per-period (`YYYY-MM`) aggregate counter in `meta`. |
| `MUTABLE_PROVENANCES` | `aggressive_wall.py:60` | `("agent","background_review")` | tuple[str] | The **only** `created_by` values SP-7 may touch; `human`/`import`/`backfill_import` are immutable. |
| `_GEN_PREFIX` | `aggressive_wall.py:214` | `"gen-"` | string | Generated-skill path prefix (under `.claude/skills/`); only `gen-` skills may be rewritten. |

## Self-learning loop — SP-8 attribution (usage tracking)

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `SESSION_INGEST_ENABLE` | env | OFF (ships disabled) | presence | Gated switch for the `session_ingest` beat; controls whether the beat **runs at all** (a throttle, not a feature gate — if it runs, learnings are always produced). |
| `SP8_ATTRIBUTION_ENABLE` | env / userConfig (proposed) | `"0"` (OFF) | `"0"\|"1"\|"true"\|"yes"` | Master flag: enables outcome-signal recording at session end (via the Stop hook). |
| `SP8_ATTRIBUTION_K` | env | `1` | int | Top-k for the attribution policy (how many top-ranked hits get credit). |
| `SP8_ATTRIBUTION_POLICY` | env | `"top_k"` | string | `top_k` = topmost K hits; `all` = every hit equally. |
| `_attribution_predicates` | `retention.py:19` | `("validated_as","superseded_by","informed_by")` | tuple[str] | Link predicates that anchor attribution edges; events with these edges are excluded from prune. |

## Self-learning loop — SP-10 synthesis (skill generation)

> Reuses the SP-7 wall **plus** the eval-gate (below). Bounded to **one** skill per run.

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `SP10_SYNTHESIS_DISABLE` | env | **present** in cron until opt-in | presence | Kill switch: any value = whole beat is a no-op (mirrors SP-7). |
| `SP10_SYNTHESIS_ENABLE` | env | (unset) | presence | Explicit arm flag for the synthesize beat. |
| `SP10_SYNTHESIS_DRYRUN` | env | (unset) | presence | Dry-run: plans + evals + digests, applies no skills. |
| `DEFAULT_N` | `skill_synthesize.py:41` | `3` | int | Min lesson count per domain to trigger synthesis (`N >= threshold`). |
| `DEFAULT_THETA_W` | `skill_synthesize.py:42` | `1.0` | float | Min mean `outcome_weight` for a cluster to qualify (higher = stricter). |
| `DEFAULT_LESSON_CAP` | `skill_synthesize.py:43` | `40` | int | Max lesson bodies pulled into one synthesis draft prompt. |
| `MAX_SKILLS_INDUCED_PER_RUN` | `synthesize_bounds.py:23` | `1` | int | Per-run cap on skill generation (tightest possible). |
| `MAX_SKILLS_INDUCED_PER_PERIOD` | `synthesize_bounds.py:26` | `2` | int | Global per-period (`YYYY-MM`) cap on inductions. |
| `_PERIOD_META_PREFIX` | `synthesize_bounds.py:29` | `"sp10_synthesis_period"` | string | Namespace prefix for the per-period counter in `meta`. |

## Eval-gate tuning (skill-hijack protection)

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `THETA_DESC` | `skill_eval.py:45` | `0.6` | float | **Tier-A:** reject a candidate if its token-cosine to any static skill description exceeds this. |
| `RUNS_PER_QUERY` | `skill_eval.py:46` | `3` | int | **Tier-B:** concurrent probe count per corpus query (zero-tolerance — fire if **any** sample hijacks). |
| `PROBE_MAX_WORKERS` | `skill_eval.py:50` (env `ULTRA_MEMORY_PROBE_WORKERS`) | `6` | int | Thread-pool size for hijack-direction probes (bounded to avoid swamping the OAuth CLI). |
| `ULTRA_MEMORY_PROBE_WORKERS` | env | `6` | int | Env override for `PROBE_MAX_WORKERS` (parallelization of the eval-gate probes). |

---

## Maintenance beats — enable flags (`config.toml [maintenance.beats]`)

| Name | Default | Type | Meaning |
|---|---|---|---|
| `consolidate` | `true` | bool | Conservative graduate/merge drain (weekly default). |
| `aggressive` | `true` | bool | Self-correction loop (monthly default; gated by `SP7_AGGRESSIVE_*`). |
| `synthesize` | `true` | bool | Skill generation (monthly default; gated by `SP10_SYNTHESIS_*`). |
| `session_ingest` | `true` | bool | Session-event ingestion + attribution (gated by `SESSION_INGEST_ENABLE`). |
| `learnings` | `true` | bool | Per-skill `Learnings.md` projection rebuild (Tier-1, no LLM, weekly default). |
| `wiki_maintenance` | `true` | bool | Wiki Stage-1+2 (detection + adjudication; daily default; gated by cron stages). |

## Maintenance beats — cadence throttles (`config.toml [maintenance.cadence_hours]`)

| Name | Default | Type | Meaning |
|---|---|---|---|
| `consolidate` | `168` | int | Weekly throttle for the consolidate beat. |
| `aggressive` | `720` | int | Monthly throttle (a consumer may override to `168` = weekly). |
| `synthesize` | `720` | int | Monthly throttle (overridable per consumer). |
| `session_ingest` | `24` | int | Daily throttle. |
| `learnings` | `168` | int | Weekly throttle (synced to consolidate). |
| `wiki_maintenance` | `24` | int | Daily throttle (synced to the cron cadence). |
| `_DEFAULT_CADENCE` | `config.py:41` | dict | Hard defaults when `config.toml` is absent: `{session_ingest:24, consolidate:168, aggressive:720, synthesize:720, learnings:168, wiki_maintenance:24}`. |

## Maintenance config (`config.toml [maintenance]` + env overrides + code defaults)

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `briefings_dir` | config.toml / env `ULTRA_MEMORY_BRIEFINGS_DIR` | (unset) | path | Audit/digest output dir; `None` = no audit writes. |
| `probe_corpus` | config.toml / env `ULTRA_MEMORY_PROBE_CORPUS` | auto-build from skills | path | Hijack-test corpus file; `None` = auto-discover skill descriptions (complete coverage by construction). |
| `wiki_gateway` | config.toml / env `ULTRA_MEMORY_WIKI_GATEWAY` | `None` | string\|path | Consumer's wiki write gateway (`"module:Class"` or a filesystem path); `None` = no wiki. |
| `wiki_linter` | config.toml / env `ULTRA_MEMORY_WIKI_LINTER` | `""` | string | Consumer lint hook (`"module:function"`) for Stage-1 findings; empty = generic lint. |
| `wiki_merge_decider` | config.toml / env `ULTRA_MEMORY_WIKI_MERGE_DECIDER` | `""` | string | Consumer grey-zone dedup judge (`"module:function"`); empty = auto-merge only. |
| `wiki_graph_extractor` | config.toml | `[]` | list[str] | Graph-builder command template with `{wiki_root}` placeholders. |
| `topics` | config.toml | `[]` | list[str] | Known wiki topics (context + fallback). |
| `model` | config.toml / env `ULTRA_MEMORY_MODEL` | `"claude-sonnet-4-6"` | string | LLM model for maintenance (OAuth-only). |
| `self_learning_files` | config.toml | `[]` | list[[path, tag]] | Enforced `(path, skill_tag)` pairs for the `Learnings.md` rebuild (`gen-*` auto-discovered on top). |
| `wiki_schema` | config.toml `[maintenance.wiki]` | `{}` | dict | Wiki schema overrides (atomics dir, dedup band, etc.). |
| `_DEFAULT_MODEL` | `config.py:49` | `"claude-sonnet-4-6"` | string | Hard default when no model in config/env. |

---

## Wiki / knowledge configuration

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `ULTRA_MEMORY_WIKI_ROOTS` | env | `[]` | path list (`os.pathsep` or comma-sep) | Wiki root directories to maintain (parsed by `_resolve_wiki_roots`). |
| `ULTRA_MEMORY_CALLER_TOPIC` | env | (unset) | string list (`os.pathsep` or comma-sep) | Topic-scoped recall list; empty = all topics; `None` = caller defaults. |
| `_WIKI_ROOTS_ENV` | `config.py:51` | `"ULTRA_MEMORY_WIKI_ROOTS"` | string | The env-var name constant (for maintainability). |

## Embedding + retrieval core

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `EMBED_MODEL` | `retrieval_core.py:15` | `"BAAI/bge-small-en-v1.5"` | string | fastembed model id; also the embedding-cache key (prevents re-embedding). |
| `EMBED_DIM` | `retrieval_core.py:16` | `384` | int | Embedding vector dimension (bge-small produces 384-d vectors). |
| `ULTRA_MEMORY_FASTEMBED_CACHE` | env | `~/.cache/ultra-memory/fastembed` | path | Persistent fastembed model cache dir; resolution: this **→** `FASTEMBED_CACHE_PATH` **→** default. |
| `FASTEMBED_CACHE_PATH` | env | (unset) | path | fastembed's own convention (fallback if `ULTRA_MEMORY_FASTEMBED_CACHE` is unset). |

> The cache **must** be a persistent `$HOME` dir, never `$TMPDIR` — macOS purges `$TMPDIR` on
> reboot, which previously killed the knowledge MCP on startup. See
> [`design-decisions.md`](design-decisions.md) §7.

## OAuth + authentication

| Name | Where | Default | Type | Meaning |
|---|---|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | env / `plugin.json` userConfig | (unset) | string (sensitive) | OAuth token for the LLM maintenance beats. **NEVER** an `ANTHROPIC_API_KEY`; stripped for outbound CLI calls (anti-recursion). |
| `ULTRA_MEMORY_MODEL` | env | config.toml / default | string | Override the model for the current session (read by OAuth-gated beats). |

> An `ANTHROPIC_API_KEY` on the process is a hard `OAuthViolation` — the engine never touches
> the SDK or an API key. See [`design-decisions.md`](design-decisions.md) §3.

---

## `plugin.json` userConfig keys (consumer-facing)

These surface in the plugin install UI; they map onto the engine env names above.

| userConfig key | Maps to | Default | Meaning |
|---|---|---|---|
| `data_db_path` | `ULTRA_MEMORY_DB` | `~/.ultra-memory/memory.db` | Where the global memory DB lives (the **one** value a consumer typically sets). |
| `caller_class` | `ULTRA_MEMORY_CALLER_CLASS` | `subagent` | The privilege class for this install's knowledge MCP. |
| `rehydrate_budget` | `ULTRA_MEMORY_REHYDRATE_BUDGET` | `2000` | Char budget for the SessionStart gist. |
| `oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` | (unset) | OAuth token for armed maintenance beats. |

> **Asymmetry by design:** benign signal-only gates (e.g. a proposed `attribution_enable`) may
> surface a `userConfig` option; the destructive aggressive verbs (SP-7 auto-edit/revert,
> SP-10 synthesis) deliberately stay **env-only and disabled by default**.

---

## Worked consumer example — the reference consumer's `<project>/.ultra-memory/config.toml`

A concrete `[maintenance]` config from the reference consumer's `<project>/.ultra-memory/config.toml`,
showing real overrides:

| Key | Value | Note |
|---|---|---|
| `briefings_dir` | `"briefings"` | Audit/digest directory. |
| `wiki_gateway` | `"scripts/wiki_lib.py"` | `uv`-run wiki write gateway. |
| `topics` | `["trading", "programming"]` | Known topics. |
| `model` | `"claude-sonnet-4-6"` | Explicit model override. |
| `beats.*` | all `true` | consolidate / aggressive / synthesize / learnings / wiki_maintenance. |
| `cadence_hours.consolidate` | `168` | Weekly. |
| `cadence_hours.aggressive` | `168` | **Weekly, not the monthly default.** |
| `cadence_hours.synthesize` | `168` | **Weekly, not the monthly default.** |
| `wiki_linter` | `"wiki_lint_findings:lint_findings"` | Trading's area-stripped lint hook. |
| `wiki_merge_decider` | `"wiki_merge_decider:merge_decider"` | Calibrated grey-zone dedup judge. |
| `wiki_graph_extractor` | `["/opt/homebrew/bin/python3.13", "scripts/wiki_graph_extract.py", …]` | Custom graph builder. |
| `self_learning_files` | 8 entries | 7 project skills + the markov-regime plugin. |
| `probe_corpus` | *unset (on purpose)* | Auto-discovery enabled for complete coverage. |

---

## See also

- [`design-decisions.md`](design-decisions.md) — the rationale behind these knobs.
- [`architecture.md`](architecture.md) — where each variable is consumed.
- [`../reference/operations.md`](../reference/operations.md) — install, wiring, the env bridge,
  and the DB-path resolution single-source-of-truth.
- [`../reference/schema.md`](../reference/schema.md) — the tables these settings drive.
