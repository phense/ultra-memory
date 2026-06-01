# ultra-memory

Project-agnostic agent memory engine + knowledge MCP for Claude Code, delivered as a local plugin.

**Boundary (this repo is meant to be published, the data is not):** this repo holds only
**code** and is **content-free**. The data (`memory.db`, exports) and any consumer-specific
configuration (paths, the knowledge base it indexes, secrets) live in the *consumer* repo and
are injected via config — never committed here. No hardcoded user paths. One plugin, many
possible consumers.

Built with `uv`; run tests with `uv run pytest`.

**Documentation:** [`docs/`](docs/) — split by reading intent into
[`user/`](docs/user/) (overview + usage), [`developer/`](docs/developer/)
(architecture + contributing), and [`reference/`](docs/reference/) (schema, API,
operations). Start at [`docs/README.md`](docs/README.md).

## Install as a plugin

ultra-memory is a drop-in Claude Code plugin. Into any consumer project:

1. **Install** (local, this cycle — no public marketplace):
   ```
   /plugin marketplace add /Users/<you>/Agents/ultra-memory
   /plugin install ultra-memory@ultra-memory
   ```
2. **Configure — nothing required (zero-config install).** The installer prompts
   for nothing mandatory. The DB path auto-derives: `<project>/data/memory.db` for a
   project/local install (`${CLAUDE_PROJECT_DIR}`), else `~/.claude/memory.db`
   at user scope. Optional overrides: `data_db_path` (set an absolute path to point at
   a `memory.db` elsewhere), `caller_class` (default `subagent`), `rehydrate_budget`
   (default `2000`), `oauth_token` (only if you run LLM maintenance — never an API key).
3. **Bootstrap:** run `/memory-setup` (builds the runtime venv under
   `${CLAUDE_PLUGIN_DATA}/venv`, optionally imports a legacy memory dir once,
   stamps the DB ready, sanity-checks). Then restart Claude Code so the
   `knowledge` MCP registers.

That is the whole install: no hand-editing `.mcp.json`, `settings.json`, or any
wrapper. On each SessionStart the rehydration gist is injected (sync) and
maintenance runs (async, throttled ≤ ~1×/day); on Stop a checkpoint is written.
The `using-memory` skill teaches every agent the read/write interface.

The full command surface (`/memory-recall`, `/memory-pin`, `/memory-verify`,
`/memory-edit`, `/memory-inbox`, `/memory-save`, `/memory-setup`,
`/memory-maintain`) plus the MCP, the hooks, the `import_complete` gate, the
self-healing maintenance, and the fail-open behavior are documented in
[`docs/reference/operations.md`](docs/reference/operations.md).

**Requirements (both required to function):** `uv` and `git` on PATH.
- `uv` provisions the Python 3.13 runtime (the engine is pure Python 3.13 +
  SQLite — no other binary is shelled).
- `git` is the rollback/safety model — the deterministic export
  (`memory.dump.sql` + snapshot + views) is *the sole git-committed rollback
  artifact* and the wiki/maintenance lifecycle is archive-never-delete *via
  git*; without git there is no restore net. `/memory-setup` preflights both
  (`setup.REQUIRED_TOOLS`) and aborts if either is missing.

First `/memory-setup` downloads the embedder model (~bge-small, cached afterward).

**Contributing:** TDD is mandatory and `docs/` are kept in lockstep with the code.
A warn-only doc-discipline hook ships under `.githooks/`; enable it once per clone
with `git config core.hooksPath .githooks`. See
[`docs/developer/contributing.md`](docs/developer/contributing.md).
