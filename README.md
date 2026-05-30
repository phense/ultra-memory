# ultra-memory

Project-agnostic agent memory engine + knowledge MCP for Claude Code, delivered as a local plugin.

**Boundary (this repo is meant to be published, the data is not):** this repo holds only
**code** and is **content-free**. The data (`memory.db`, exports) and any consumer-specific
configuration (paths, the knowledge base it indexes, secrets) live in the *consumer* repo and
are injected via config — never committed here. No hardcoded user paths. One plugin, many
possible consumers.

Built with `uv`; run tests with `uv run pytest`. A genericized design doc is added under `docs/`
during packaging.
