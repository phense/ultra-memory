# 6. Configuration reference

Most people install ultra-memory, restart once, and never touch a setting — it is built to run zero-config, deriving one shared store and turning the self-learning loop on by itself. But the moment you want it to behave differently — a custom database path, a memory-only install with no wiki, the self-correcting steps held back, a deterministic schedule instead of the session-driven one — you need to know exactly which knob does it. This chapter is that map: every setting, where it lives, what it defaults to, and a recipe for each common "I want it to do X" wish.

There are three layers, and they always resolve in the same order:

1. **`userConfig`** — the friendly prompts the `/plugin` installer shows you. Each one is a single value; the plugin bridges it into an environment variable for the engine.
2. **`ULTRA_MEMORY_*` environment variables** — the engine's real seams. Anything in `userConfig` ends up here, and you can set them directly (a shell profile, a wrapper script, a scheduler unit) when there's no UI prompt for what you want.
3. **`<project>/.ultra-memory/config.toml`** — a per-project file, only read for the heavier *maintenance* settings (the self-learning beats, the wiki gateway, cadences). Its `[maintenance]` table is where project-specific wiring lives.

**Precedence, top to bottom: an explicit environment variable wins over the `config.toml` file, which wins over the built-in default.** Nothing is ever read from the current working directory — paths are always resolved from config, never from where the process happens to start (the MCP launcher does not preserve `cwd`, so relying on it would silently open the wrong store).

If you have not yet met the two stores or the loop, read [The mental model](02-mental-model.md) first; if you just want it installed, [Quick start](03-quick-start.md) is four lines. This chapter assumes the engine is already running.

---

## Layer 1 — `userConfig` (the install prompts)

These are the values the `/plugin` config UI offers. Every one is optional; leaving it blank gives you the documented default. The four `*_enable` toggles are the opt-out switches for the self-learning loop — **all four ship ON**, so setting any of them to `off` *disables* that step.

| `userConfig` option | Default | What it controls | Bridges to env |
|---|---|---|---|
| `data_db_path` | `""` (→ `~/.ultra-memory/memory.db`) | Absolute path to the canonical SQLite store. Empty = the fixed global store every project shares. | `ULTRA_MEMORY_DB` |
| `caller_class` | `subagent` | Privilege class for the read-only knowledge MCP. `subagent` = fail-closed (`project`/`reference` facts only); set `orchestrator` only on a trusted top-level instance. | `ULTRA_MEMORY_CALLER_CLASS` |
| `rehydrate_budget` | `2000` | Character budget for the SessionStart rehydration gist. | `ULTRA_MEMORY_REHYDRATE_BUDGET` |
| `oauth_token` | `""` | A Claude **OAuth** token (never an API key), only needed if a consumer runs LLM maintenance and the CLI's own session isn't available. | `CLAUDE_CODE_OAUTH_TOKEN` |
| `session_ingest_enable` | `on` | Session capture — mine each finished session into durable memory. `off` disables. | `SESSION_INGEST_ENABLE` |
| `attribution_enable` | `on` | Outcome attribution — credit which recalled facts helped. `off` disables. | `SP8_ATTRIBUTION_ENABLE` |
| `aggressive_enable` | `on` | Self-correction (rewrite / revert / quarantine of the loop's *own* notes). `off` disables. | `SP7_AGGRESSIVE_DISABLE` (inverted) |
| `synthesize_enable` | `on` | Skill synthesis — induce a new skill from a cluster of matured lessons. `off` disables. | `SP10_SYNTHESIS_DISABLE` (inverted) |

Two of these toggles **invert** when they cross into the engine, and the distinction matters if you ever set the env var by hand:

- `session_ingest_enable` and `attribution_enable` use an **opt-out** convention: the env var is read as ON unless its value is one of `0`, `false`, `no`, `off` (case-insensitive). Unset = ON.
- `aggressive_enable` and `synthesize_enable` map to a **kill switch** (`SP7_AGGRESSIVE_DISABLE` / `SP10_SYNTHESIS_DISABLE`) that uses a **presence** convention: the switch is active whenever the variable *exists at all* — even set to an empty string. So choosing `off` in the UI makes the wrapper *set* `SP7_AGGRESSIVE_DISABLE=1`; choosing anything else leaves it *unset*. Do not set the disable variable to `0` expecting it to mean "enabled" — presence alone disables.

---

## Layer 2 — `ULTRA_MEMORY_*` environment variables

Every seam the engine reads from the environment, in one table. A `userConfig` option always reaches the engine through the matching variable here; the rest of these have no UI prompt and are set directly when you need them.

| Variable | Default | Effect |
|---|---|---|
| `ULTRA_MEMORY_DB` | `~/.ultra-memory/memory.db` | Absolute path to the canonical memory store. Blank = the fixed global path. Resolved, never created from cwd. |
| `ULTRA_MEMORY_CALLER_CLASS` | `subagent` | Knowledge-MCP privilege class. Fail-closed: anything other than `orchestrator`/`owner` is treated as the untrusted `subagent`. |
| `ULTRA_MEMORY_REHYDRATE_BUDGET` | `2000` | Char budget of the no-LLM SessionStart gist. |
| `ULTRA_MEMORY_SHADOW` | `0` in the plugin (engine default `1`) | `1` = shadow mode (log the gist, inject nothing); `0` = live injection. The hook wrapper forces `0` so a plugin consumer actually sees the gist. |
| `ULTRA_MEMORY_SHADOW_OUT` | unset | Optional file path to write the shadow gist to when `ULTRA_MEMORY_SHADOW=1`. |
| `ULTRA_MEMORY_AGENT_ROLE` | unset | A role marker (e.g. `cron`). When set, the interactive session hooks no-op — use for non-interactive/headless runs. Leave unset for an orchestrator session. |
| `ULTRA_MEMORY_EXPORT_DIR` | `<db-parent>/memory_export` | Where the readable, git-trackable export views are written. |
| `ULTRA_MEMORY_WIKI_ROOTS` | unset | The active wiki root(s) maintenance curates (comma- or `os.pathsep`-separated). **Unset = the wiki steps are a no-op** — this is what makes a pure-memory install. |
| `ULTRA_MEMORY_WIKI_GATEWAY` | unset | The audited wiki write gateway (a path like `scripts/wiki_lib.py`, or a `module:Class` spec). Also settable in `config.toml`. |
| `ULTRA_MEMORY_BRIEFINGS_DIR` | unset (→ no audit/digest writes) | Directory for the loop's audit logs + human digests. Relative paths resolve against the project. |
| `ULTRA_MEMORY_PROBE_CORPUS` | unset (→ skill-loop holds) | Path to the skill-trigger probe set the synthesis eval-gate scores against. |
| `ULTRA_MEMORY_MODEL` | `claude-sonnet-4-6` | The model the off-session `claude` CLI uses for any LLM beat. |
| `ULTRA_MEMORY_NOTIFIER` | unset (→ stderr no-op) | A `module:function` notifier called fail-open when a run records beat errors. |
| `ULTRA_MEMORY_WIKI_LINTER` | unset | A `module:function` supplying richer wiki-lint findings; absent = the engine's generic lint. |
| `ULTRA_MEMORY_WIKI_MERGE_DECIDER` | unset | A `module:function` `(cosine, claim, cand_text) -> bool` for grey-zone dedup; absent = auto-merge only. |
| `ULTRA_MEMORY_MAINTAIN_FORCE` | unset | `1` = run the light Tier-1 maintenance now, ignoring the 20-hour throttle. |
| `ULTRA_MEMORY_REBUILD_INDEX` | unset | `1` = force-rebuild the export/index during light maintenance. |
| `ULTRA_MEMORY_HARNESS_DIR` | unset | The legacy harness memory dir the one-time bootstrap import reads. Only used by `/memory-setup`'s import. |
| `ULTRA_MEMORY_BACKFILL_CMD` | unset | A consumer's optional cold-start backfill runner; `/memory-setup` only *offers* it, never auto-runs. |
| `ULTRA_MEMORY_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | The local embedding model for retrieval. |
| `ULTRA_MEMORY_FASTEMBED_CACHE` | platform default | Where the local search model is cached. |

The self-learning beats also read these enable/disable variables directly (see the inversion note above):

| Variable | Convention | Default | Effect |
|---|---|---|---|
| `SESSION_INGEST_ENABLE` | opt-out (`0`/`false`/`no`/`off` = off) | ON | Session capture. |
| `SP8_ATTRIBUTION_ENABLE` | opt-out | ON | Outcome attribution. |
| `SP7_AGGRESSIVE_DISABLE` | presence (set = disabled) | unset = ON | Self-correction beat. |
| `SP7_AGGRESSIVE_DRYRUN` | presence (set = plan-only) | unset | Self-correction plans + writes a digest but applies nothing. |
| `SP10_SYNTHESIS_DISABLE` | presence (set = disabled) | unset = ON | Skill-synthesis beat. |
| `SP10_SYNTHESIS_DRYRUN` | presence (set = plan-only) | unset | Skill synthesis plans + digests but creates no skill. |

LLM-call authentication is **not** a knob you turn here. There is no `ULTRA_MEMORY_API_KEY`. Every LLM beat runs through your local `claude` CLI on your own subscription; an `ANTHROPIC_API_KEY` on the process is a hard error that refuses to run. See [Privacy, cost & control](07-privacy-cost-control.md) for the full chokepoint.

---

## Layer 3 — `<project>/.ultra-memory/config.toml`

The heavier maintenance settings live in a per-project TOML file, read from `<project>/.ultra-memory/config.toml` under the `[maintenance]` table. With no file at all, every field falls back to a safe project-agnostic default — a fresh install runs the light beats and skips anything that needs project content. A malformed file never crashes the load; it degrades to defaults (fail-open).

A complete example with every field:

```toml
[maintenance]
briefings_dir = "briefings"                          # audit/digest dir, relative to the project
probe_corpus  = "tests/fixtures/skill_trigger_probes.json"
wiki_gateway  = "scripts/wiki_lib.py"                # the audited wiki write gateway (None → no wiki)
topics        = ["trading"]                          # wiki topics this project owns
model         = "claude-sonnet-4-6"                  # the OAuth CLI model for LLM beats
notifier      = "mymod:notify"                       # module:function, called fail-open on beat errors

[maintenance.beats]                                   # the autonomous posture: default ON, wall-governed
session_ingest   = true
consolidate      = true
aggressive       = true
synthesize       = true
learnings        = true
wiki_maintenance = true

[maintenance.cadence_hours]                           # per-beat throttle (the session-driven clock)
session_ingest = 24                                   # ~daily
consolidate    = 168                                  # weekly
aggressive     = 720                                  # monthly
synthesize     = 720                                  # monthly
learnings      = 168                                  # weekly
wiki_maintenance = 24                                 # ~daily

# Optional — only for a consumer wiki that does NOT follow the reference conventions:
[maintenance.wiki]                                    # override any WikiSchemaConfig field
# (omit the whole table if your wiki follows the reference Karpathy schema — every default already matches)
```

### `[maintenance]` field reference

| Field | Default | Meaning |
|---|---|---|
| `briefings_dir` | unset → no audit/digest writes | Directory for the loop's audit JSONL + human digests. (Env override: `ULTRA_MEMORY_BRIEFINGS_DIR`.) |
| `probe_corpus` | unset → skill-loop holds | Probe set the synthesis eval-gate scores against. (Env: `ULTRA_MEMORY_PROBE_CORPUS`.) |
| `wiki_gateway` | unset → no wiki | The audited wiki write gateway; a path is `uv-run`, a `module:Class` string becomes a `--gateway-class`. (Env: `ULTRA_MEMORY_WIKI_GATEWAY`.) |
| `topics` | `[]` | The wiki topics this project owns. |
| `model` | `claude-sonnet-4-6` | The model for any LLM beat. (Env: `ULTRA_MEMORY_MODEL`.) |
| `notifier` | `""` → stderr no-op | `module:function` called fail-open with a `NotifyEvent` when a run records beat errors. (Env: `ULTRA_MEMORY_NOTIFIER`.) |
| `wiki_linter` | `""` → generic lint | `module:function` supplying Stage-1 lint findings. (Env: `ULTRA_MEMORY_WIKI_LINTER`.) |
| `wiki_merge_decider` | `""` → auto-merge only | `(cosine, claim, cand_text) -> bool` for grey-zone dedup. (Env: `ULTRA_MEMORY_WIKI_MERGE_DECIDER`.) |
| `wiki_graph_extractor` | `[]` → no graph rebuild | A command template that builds the graph DB the graph detector queries; `{wiki_root}` / `{graph_dir}` are substituted. |
| `self_learning_files` | `[]` | `[path, tag]` pairs naming each `Learnings.md` the projection-regen beat rebuilds. Generated `gen-*` skills are added on top automatically. |
| `[maintenance.beats]` | all `true` | Per-beat on/off. A beat set `false` here never runs, regardless of the env toggles. |
| `[maintenance.cadence_hours]` | see below | Per-beat throttle in hours. |
| `[maintenance.wiki]` | `{}` → reference schema | Override any `WikiSchemaConfig` field. Omit entirely if your wiki follows the reference conventions. |

### The beats and their default cadence

The heavy steps run on a **session-lifecycle clock**: an async `SessionStart` hook fires the dispatcher every time you open Claude Code, but each beat is throttled by its own per-beat `meta` clock, so opening ten sessions in a day still only advances a weekly beat once. The order is fixed — `session_ingest` runs first (it is the ingestion source), `learnings` runs last (it projects what the earlier beats graduated).

| Beat | Default cadence (hours) | What it is | Needs a git checkpoint? |
|---|---|---|---|
| `session_ingest` | 24 (~daily) | Mines finished sessions into the store. | No |
| `consolidate` | 168 (weekly) | Promotes proven lessons into the store / wiki (additive only). | No |
| `aggressive` | 720 (monthly) | Self-correction: rewrite / revert / quarantine the loop's own notes. | **Yes** — self-skips on a dirty/no-git tree |
| `synthesize` | 720 (monthly) | Induces a new skill from a cluster of matured lessons. | **Yes** — self-skips on a dirty/no-git tree |
| `learnings` | 168 (weekly) | No-LLM projection-regen of the per-skill `Learnings.md` views. | No |
| `wiki_maintenance` | 24 (~daily) | Wiki curation; a no-op unless `ULTRA_MEMORY_WIKI_ROOTS` is set. | No |

The light, no-LLM maintenance slice (prune old session events + refresh the export views) is separate from the beats. It runs from its own async SessionStart arm, throttled to once every 20 hours, and keeps session events for 90 days (rolled into a session summary before deletion, so nothing is lost). Force it now with `/ultra-memory:memory-maintain` or `ULTRA_MEMORY_MAINTAIN_FORCE=1`.

---

## "If you want X → set Y" recipes

### Run memory-only, with no wiki

Leave `ULTRA_MEMORY_WIKI_ROOTS` unset (the default) and set no `wiki_gateway`. The `wiki_maintenance` beat becomes a no-op and every wiki-related step is byte-identically skipped. Nothing else to do — this is the out-of-the-box behavior for any project that hasn't wired a wiki.

### Point at a custom database path

Set the path once — either in the install prompt or directly:

```bash
export ULTRA_MEMORY_DB="/abs/path/to/my/memory.db"
```

The path is only resolved (never created from `cwd`); `/memory-setup` will create and migrate the file. After setup, confirm the printed **resolved DB path** is the one you intend.

### Disable self-correction (keep capture + consolidate)

Pick `off` for **Self-correction** in the `/plugin` config, or set the kill switch directly. Because it is a presence switch, its mere existence disables the beat:

```bash
export SP7_AGGRESSIVE_DISABLE=1
```

To disable skill synthesis too:

```bash
export SP10_SYNTHESIS_DISABLE=1
```

### Watch self-correction without letting it act (dry run)

Set the dry-run presence switch — the beat plans, scores its eval-gate, and writes a digest, but applies nothing:

```bash
export SP7_AGGRESSIVE_DRYRUN=1     # or SP10_SYNTHESIS_DRYRUN=1 for synthesis
```

### Turn off session capture (no transcript mining)

Use the opt-out value (it is *not* a presence switch — set it to a disable word):

```bash
export SESSION_INGEST_ENABLE=off   # 0 / false / no also work
```

### Use a deterministic schedule instead of the session clock

The loop normally advances whenever you open Claude Code. On a headless box that rarely opens a session, install the OS scheduler `/memory-setup` *offers* (it prints the snippet — it never installs one for you). Both run the dispatcher daily:

```bash
# macOS (launchd) — save as ~/Library/LaunchAgents/ng.ultra-memory.maintenance.plist
#   with a daily StartCalendarInterval running:
#   <venv-python> -m ultra_memory.maintenance
# then: launchctl load ~/Library/LaunchAgents/ng.ultra-memory.maintenance.plist
```

```bash
# Linux (systemd --user) — create ~/.config/systemd/user/ultra-memory.service with
#   ExecStart=<venv-python> -m ultra_memory.maintenance
# and a matching .timer (OnCalendar=daily), then:
systemctl --user enable --now ultra-memory.timer
```

The per-beat cadence throttle still applies, so the scheduler firing daily does not re-run a weekly or monthly beat early.

### Give subagents full recall (lift the privilege boundary)

Only on a trusted top-level instance, and understanding the consequence — recall stops being type-scoped, so `user`/`feedback` memories become readable through the MCP:

```bash
export ULTRA_MEMORY_CALLER_CLASS=orchestrator
```

Leave it at the `subagent` default for anything spawned or untrusted. See [Privacy, cost & control](07-privacy-cost-control.md) for what the boundary protects.

### Make the gist longer or shorter

```bash
export ULTRA_MEMORY_REHYDRATE_BUDGET=3500   # default 2000
```

A larger budget injects more context at every session start; a smaller one keeps it terse.

### Get alerted when a maintenance run records errors

Wire your own notifier (the plugin ships no mail/notify transport). It is a `module:function` resolved with `<project>/scripts` on `sys.path`, called fail-open with a `NotifyEvent`:

```toml
[maintenance]
notifier = "mymod:notify"
```

```bash
# or, equivalently:
export ULTRA_MEMORY_NOTIFIER="mymod:notify"
```

A starter you can copy lives at `ultra_memory/maintenance/notify.py::example_notifier` (SMTP / CLI shell-out / webhook templates). Headless cron means no direct Gmail/M365 MCP, and it is OAuth-only — no API key.

---

## Kill switches at a glance

When you want a step to stop *now*, regardless of the install prompts:

| To stop… | Set | Convention |
|---|---|---|
| Session capture | `SESSION_INGEST_ENABLE=off` | opt-out value |
| Outcome attribution | `SP8_ATTRIBUTION_ENABLE=off` | opt-out value |
| Self-correction | `SP7_AGGRESSIVE_DISABLE=1` | presence (any value, incl. empty) |
| Skill synthesis | `SP10_SYNTHESIS_DISABLE=1` | presence |
| A specific beat entirely | `[maintenance.beats]` `<beat> = false` | per-beat config flag |
| The interactive session hooks (headless run) | `ULTRA_MEMORY_AGENT_ROLE=cron` | presence of a role marker |

Even with every beat on, the self-correcting steps are bounded by design — a few changes per run, archive-never-delete, a git checkpoint before they act, never touching a pinned or human-authored unit. [Privacy, cost & control](07-privacy-cost-control.md) covers that safety wall and how to verify what ran.

---

**Next:** [Privacy, cost & control →](07-privacy-cost-control.md) — what reads your data, why there is no API key, what stays on your machine, and how to disable every step.
