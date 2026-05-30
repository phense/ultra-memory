# Contributing

## TDD is mandatory

No production code without a failing test first (red → green → refactor). Every
function and bug-fix has a test; bugs get a regression test that reproduces them
before the fix. The suite must stay green and the output pristine.

```bash
uv run pytest -q          # full suite
uv run pytest tests/test_memory_lib.py -q
```

Tests are deterministic and offline: inject a fake embedder
(`list[str] -> list[list[float]]`), pass `ts`/`now_ts` explicitly, never hit the
network, and never load the real fastembed model (the `[retrieval]` extra is opt-in
and exercised only via an ImportError-branch test).

## Documentation discipline (keep docs in lockstep with code)

**Rule:** any change under `ultra_memory/` should be reflected in `docs/`. When you
touch a module, ask:

- New/changed public function or signature? → update `docs/reference/api.md`.
- Schema or migration change? → update `docs/reference/schema.md`.
- New behaviour, flow, or invariant? → update `docs/developer/architecture.md`.
- User-visible capability or command? → update `docs/user/`.
- Operational change (export/dump/spool/rollback)? → update `docs/reference/operations.md`.

A **warn-only pre-commit hook** (`.githooks/pre-commit`) enforces the *habit*: if a
commit stages changes under `ultra_memory/` but nothing under `docs/`, it prints a
reminder listing the changed modules — then lets the commit through. It never
blocks (no escape-hatch to abuse); the discipline is on us. Enable it once per
clone:

```bash
git config core.hooksPath .githooks
```

(The repo ships the hook tracked under `.githooks/` so it travels with clones; git
does not auto-enable `core.hooksPath`, hence the one-time setup.)

If a code change genuinely needs no doc update (e.g. an internal-only refactor with
no behavioural or interface change), it's fine to commit through the warning —
just be honest about it.

## Conventions

- Pure functions where possible; pass `conn`, `ts`, and the embedder in (no globals,
  no clock reads, no hidden I/O).
- One writer: route every mutation through `memory_lib` and `_write_txn`.
- One LLM chokepoint: `claude_cli` only. Never the Anthropic SDK or an API key.
- Keep the published repo content-free and path-free: no `memory.db`, no exports,
  no secrets, no hardcoded user paths.
- Migrations are forward-only and idempotent; add a test per migration.
- Match the surrounding style; keep files focused and small.
