# Third-Party Licenses

ultra-memory itself is licensed under the **MIT License** (see [`LICENSE`](LICENSE)),
Copyright (c) 2026 Peter Hense.

The **core engine has no required runtime dependencies** — it is pure Python 3.13 +
the standard library + SQLite. The third-party packages below are pulled in only by
**optional extras** (and the dev/build toolchain). Each is installed from PyPI at the
user's machine — **none is vendored into this repository**, so this repo ships no
third-party source.

## Optional runtime dependencies

| Package | Pulled in by | License | Project |
|---|---|---|---|
| `fastembed` | `pip install "ultra-memory[retrieval]"` | Apache-2.0 | https://github.com/qdrant/fastembed |
| `mcp` (Model Context Protocol SDK) | `…[mcp]` | MIT | https://github.com/modelcontextprotocol/python-sdk |
| `PyYAML` | `…[maintenance]` | MIT | https://github.com/yaml/pyyaml |

With no extras installed, ultra-memory runs on the standard library alone (the
`retrieval` embedder, the `mcp` server, and the `maintenance` safety-wall YAML parse
degrade gracefully when their extra is absent).

## Build / development tooling (not shipped to users)

| Package | Role | License |
|---|---|---|
| `hatchling` | build backend (`[build-system]`) | MIT |
| `pytest` | test runner (`dev` group) | MIT |
| `uv` | runtime + venv provisioner (external tool) | Apache-2.0 / MIT |

## Apache-2.0 notice (fastembed)

`fastembed` is © its respective authors and licensed under the Apache License,
Version 2.0. ultra-memory does **not** modify or redistribute fastembed's source; it
is installed from PyPI only by the optional `retrieval` extra. The full Apache-2.0
license text is at <https://www.apache.org/licenses/LICENSE-2.0>.

## Acknowledged prior work (ideas / approach — no bundled code)

These shaped ultra-memory but are **not** redistributed here (no source copied into
this repository). They are also credited in the README *Acknowledgments*:

- **llm-wiki plugin** — Praney Behl (MIT). The wiki engine's retrieval / lint / graph
  *approach* draws on it; no llm-wiki source is copied into this repo.
- **LLM-Wiki** — Andrej Karpathy. The knowledge-wiki concept.
- **superpowers** — obra. The skill-framework conventions.
- **Hermes** — the template for the capture → consolidate → self-correct → synthesize
  self-learning loop.
- **Anthropic** — Claude Code, the skills framework, and bundled skills referenced in
  docs (e.g. `simplify`, `skill-creator`, `code-review`).
