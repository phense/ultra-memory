# ultra-memory documentation

A project-agnostic agent-memory engine + knowledge MCP for Claude Code, delivered
as a local plugin. This `docs/` tree is split by reading intent:

- **[user/](user/)** — for *consumers*: what ultra-memory is, how to install it, and
  how to use the engine from a consuming project.
  - [overview.md](user/overview.md) — what/why, the public/private boundary, status
  - [usage.md](user/usage.md) — opening a DB, saving, querying, importing, exporting
- **[developer/](developer/)** — for *contributors*: how the engine is built and how
  to change it safely.
  - [architecture.md](developer/architecture.md) — canonical model, modules, data flow
  - [contributing.md](developer/contributing.md) — TDD, tests, the doc-discipline rule
- **[reference/](reference/)** — *look-up*: schema, per-function API, operations.
  - [schema.md](reference/schema.md) — tables, columns, migrations
  - [api.md](reference/api.md) — every public function + behaviour
  - [operations.md](reference/operations.md) — export/dump format, spool, rollback, redaction

## Status (2026-05-31)

The **memory engine + import/export + session hooks are built and tested**
(Plans 1–4; the test suite is green, 151 tests). The **read-only `knowledge` MCP
core is built and tested** (`ultra_memory/knowledge_mcp.py`: type-scoped recall —
untrusted callers get `project`/`reference` only, never `user`/`feedback` — plus
read-path `strip_secrets`, access-log audit, the `knowledge_query` tool, and a
config-driven stdio `main()`; the embedder needs the `retrieval` extra at launch).
**Plugin packaging scaffolded:** `.claude-plugin/plugin.json` + `marketplace.json`
manifests, `LICENSE` (MIT), `config.example`, and a `test_no_hardcoded_paths` guard
enforcing the project-agnostic invariant (§3.1). Still future: **MCP reachability
wiring + 3-path verification, slash-command verbs, the live one-time bootstrap
import, and the (opt-in, publish-last) GitHub publish** (Plans 5–8). Treat anything
described here as "future" until its plan lands.

A full adversarial audit of the engine ran on 2026-05-30 (verdict
`go-after-fixes`); all findings — 4 critical, 1 high, 7 medium, 11 low + 1 nit —
have been fixed with regression tests.
