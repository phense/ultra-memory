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

**Contributing:** TDD is mandatory and `docs/` are kept in lockstep with the code.
A warn-only doc-discipline hook ships under `.githooks/`; enable it once per clone
with `git config core.hooksPath .githooks`. See
[`docs/developer/contributing.md`](docs/developer/contributing.md).
