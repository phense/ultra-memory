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
(Plans 1–4). The **read-only `knowledge` MCP core is built and tested**
(`ultra_memory/knowledge_mcp.py`: type-scoped recall — untrusted callers get
`project`/`reference` only, never `user`/`feedback` — plus read-path
`strip_secrets`, access-log audit, the `knowledge_query` tool, and a config-driven
stdio `main()`; the embedder needs the `retrieval` extra at launch). The
**human-correction path is built**: `memory_lib.set_pinned`/`set_verified`, the
`memory_inbox` importer, the `memory_cli` (recall/pin/verify/edit/inbox), and the
five `commands/memory-*.md` slash commands. **Plugin packaging scaffolded:**
`.claude-plugin/plugin.json` + `marketplace.json` manifests, `LICENSE` (MIT),
`config.example`, and a `test_no_hardcoded_paths` guard enforcing the
project-agnostic invariant (§3.1).

**SP-3 cross-store fabric is built and tested** (migration `0004`; suite green at
412 passed / 1 skipped): the `topic` write path + generic keyword router,
`record_link`/`mirror_cross_store_links` (the `links` edge spine's first writer),
cross-store `set_pinned(source_kind=…)` + the rehydrate-gist pin union, `wiki_sync`
→ `unified_index`, `unified_recall` with the fail-closed (type × topic) access
wall, and the inert §7a substrate columns (`created_by`, `outcome_signal`,
`outcome_weight`) + `export_learnings_projection`. The **§7a self-improvement loop
itself is NOT built** — only its substrate; `agent`/`background_review` provenance
and a non-1.0 outcome weight have no writer. The **D4 topic backfill** is a gated
one-time data step (the DDL is live; the row-touch awaits sign-off).

Still future: cross-codebase `wiki_query` parity (deferred to an SP-5 Trading-side
test, **D-S6**); the consolidated doc rewrite + the generic `using-knowledge` split
+ the consumer-side `wiki/SCHEMA.md` / `CLAUDE.md` updates (**SP-5** / the
post-merge Trading change); the §7a loop (**SP-6/SP-7**); the live one-time
DB-canonical write-path cutover; and the (opt-in, publish-last) GitHub publish.
Treat anything described as "future" until its plan lands.

A full adversarial audit of the engine ran on 2026-05-30 (verdict
`go-after-fixes`); all findings — 4 critical, 1 high, 7 medium, 11 low + 1 nit —
have been fixed with regression tests.
