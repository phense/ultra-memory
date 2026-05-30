# ultra-memory

Project-agnostic agent memory engine + knowledge MCP, delivered as a local Claude plugin.

Design spec: `Trading/docs/superpowers/specs/2026-05-30-internal-memory-and-maintenance-design.md` (v2).

The data (`memory.db`, exports) lives in the *consumer* repo, injected via config.
This repo holds only code. Built with `uv`; tests with `uv run pytest`.
