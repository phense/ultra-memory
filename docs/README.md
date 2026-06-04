# ultra-memory documentation

A project-agnostic agent-memory engine + topic-partitioned knowledge wiki + an
autonomous self-learning skill loop for Claude Code, delivered as a local plugin.
This `docs/` tree is split by reading intent:

- **[user/](user/)** — for *consumers*: what ultra-memory is and how to use it.
  - [overview.md](user/overview.md) — the mental model (two stores, one fabric), headline capabilities, and what we've already considered (privacy, OAuth-only, reversibility)
  - [usage.md](user/usage.md) — install (`/ultra-memory:memory-setup`), the everyday verbs, recall + rehydration, the wiki surface, the cold-start backfill, the loop day-to-day
- **[developer/](developer/)** — for *contributors*: how it's built and **why**.
  - [design-decisions.md](developer/design-decisions.md) — the **WHY** behind every major choice (one global DB + topic-partitioned wiki, two-stores-one-fabric, OAuth-only, gateway-only writes, the self-learning loop, autonomy-via-code-wall) with the trade-offs considered and rejected
  - [variables.md](developer/variables.md) — the **complete** reference of every config variable + tunable constant (env / `config.toml` / `userConfig` / code), grouped, with defaults
  - [architecture.md](developer/architecture.md) — the canonical model, modules, data flow
  - [contributing.md](developer/contributing.md) — TDD, tests, the doc-discipline rule
- **[reference/](reference/)** — *look-up* (complete but concise, technical only):
  - [schema.md](reference/schema.md) — tables, columns, provenance values, migrations
  - [api.md](reference/api.md) — the engine API surface, verbs, the MCP
  - [operations.md](reference/operations.md) — install/bootstrap, env vars, the gates, maintenance beats, the autonomous apply path, export/rollback/redaction
- **[ENCOUNTERED_PROBLEMS.md](ENCOUNTERED_PROBLEMS.md)** — a wry, technically-accurate
  retrospective of the gnarliest bugs since the first commit. Every one is now found,
  fixed, and regression-tested — read it as a tour of how the system got tougher.

## Status (2026-06-04)

The fabric is **live and tested** — **1160 green tests**: the two-store knowledge fabric
(Session Memory in SQLite + the topic-partitioned LLM-Wiki), `unified_recall` deterministic
RRF fusion, the typed-edge `links` graph, the single audited write gateway with redaction at
persist *and* export, OAuth-only enforcement, SessionStart/Stop hooks, the read-only
`knowledge` MCP behind the fail-closed (type × topic) privilege wall, the wiki-curation
maintenance pipeline, and zero-config install.

The **self-learning loop is built and autonomous** (posture set 2026-06-03): capture →
consolidate (SP-6) → attribute (SP-8) → self-correct (SP-7) → synthesize (SP-10), running on
a weekly cadence behind a seven-mechanism **code** safety wall (provenance gate ·
archive-never-delete · bounded blast radius · git checkpoint · audit digest · kill switch ·
synthesis eval-gate). Full autonomy in *whether* it acts; conservative, reversible defaults in
*how*. See [design-decisions.md](developer/design-decisions.md) §5–6 for the rationale.

**Single-root today.** The engine is parameterized over a `(global, project)` root pair; the
global cross-project root is designed and built, activation pending.

The repo is **content-free**: only code ships here; your `memory.db`, exports, and any secrets
stay in your own project, injected via config. A `test_no_hardcoded_paths` guard enforces it.
