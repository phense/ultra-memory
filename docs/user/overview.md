# Overview

## What it is

`ultra-memory` is a **DB-canonical agent-memory engine**. It replaces a pile of
loose markdown memory files + ad-hoc session logs with a single SQLite database
(`memory.db`) that supports:

- **Typed memories** (`feedback` / `project` / `reference` / `user` …) with an
  audit trail, soft-delete tombstones, and redirect-stub consolidation.
- **Session episodes** — a typed, idempotent event log per session.
- **Retrieval** — embedding-cosine ranking with a title-index boost and
  staleness/strength/access ranking signals (no LLM on the read path).
- **Import** of an existing markdown memory tree + `.remember/today-*.md` logs.
- **Export** of a consistent, redacted text dump — the git rollback artifact.
- **Cross-store fabric (SP-3)** — memory becomes one system with an external
  Expert-Knowledge (wiki) store *without merging their storage*: a `topic` on each
  memory, one `links` edge spine spanning both stores, one pin space surfaced in the
  SessionStart gist, and a single warm `unified_recall` that ranks across both —
  scoped by an orthogonal (type × topic) fail-closed access wall. Still no LLM on
  the warm path. The fabric is fed consumer-side (root paths / edges injected), so
  the engine stays project-agnostic. *(The §7a self-improvement loop is not built —
  SP-3 lands only its inert substrate columns.)*

## Why a database

Markdown files give you no retrieval, no typed episodic record, and no safe
concurrent writes. The database is the **working truth**; git tracks a consistent
*text dump* of it (not the live `.db`), so version history and rollback still work
without git being the source of truth. See
[developer/architecture.md](../developer/architecture.md).

## The public / private boundary

This repository is meant to be **published** and is therefore **code-only and
content-free**:

- No memory content, no `memory.db`, no exports, no secrets, and **no hardcoded
  user paths** live here.
- The data and any consumer-specific configuration live in the *consumer* repo and
  are injected via config. One plugin, many possible consumers.

The live `*.db` (+ `-wal`/`-shm`) is gitignored. Secrets are stripped at the write
chokepoint **and** again at export time, so the committed dump stays clean.

## Install

Built with [`uv`](https://github.com/astral-sh/uv):

```bash
uv sync                      # core (stdlib-only runtime deps)
uv run pytest                # run the test suite
```

Retrieval against the real embedding model is an **optional extra** (keeps the core
install light and test runs offline):

```bash
uv pip install -e '.[retrieval]'   # pulls fastembed (BAAI/bge-small-en-v1.5, 384d)
```

Without the extra you inject your own embedder (any `list[str] -> list[list[float]]`
callable) — which is exactly what the tests do.

## LLM calls

Any LLM call this project makes goes through the local `claude` CLI on an OAuth
subscription — never the Anthropic SDK or an API key. The single chokepoint
(`ultra_memory/claude_cli.py`) refuses to run if `ANTHROPIC_API_KEY` is set or the
OAuth token is missing. The memory engine itself makes **no** LLM calls.
