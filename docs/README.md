# ultra-memory documentation

A project-agnostic agent-memory engine + topic-partitioned knowledge wiki + an
autonomous self-learning skill loop for Claude Code, delivered as a local plugin.

## 📖 Start here — the handbook

The documentation now lives as a single, progressively-ordered read:

### **[➡ The ultra-memory Handbook](handbook/README.md)**

It takes you from the mental model (Part I — Understand), through everyday use (Part II
— Use) and configuration (Part III — Configure), to building your own knowledge domain
(Part IV — Extend) and developing on the engine itself (Part V — Develop), plus a
design-rationale appendix. The [handbook index](handbook/README.md) maps every chapter
with a one-line "what you'll learn".

> The old split-by-audience pages — `user/`, `developer/`, `reference/` — have been
> **consolidated into the handbook**; each now contains a short redirect stub pointing
> at the chapter that superseded it.

## Also here

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
*how*. See the handbook's [Design notes & rationale](handbook/99-design-and-internals.md)
appendix for the rationale.

**Single-root today.** The engine is parameterized over a `(global, project)` root pair; the
global cross-project root is designed and built, activation pending.

The repo is **content-free**: only code ships here; your `memory.db`, exports, and any secrets
stay in your own project, injected via config. A `test_no_hardcoded_paths` guard enforces it.
