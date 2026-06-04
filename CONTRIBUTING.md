# Contributing to ultra-memory

Thanks for your interest! This is the quick version; the full developer guide —
conventions, the doc-discipline rule, the engine invariants — lives in
[`docs/developer/contributing.md`](docs/developer/contributing.md).

## Quick start

```bash
git clone https://github.com/phense/ultra-memory
cd ultra-memory
uv run pytest -q          # full suite (uv provisions Python 3.13 + deps)
git config core.hooksPath .githooks   # enable the warn-only doc-reminder hook (once per clone)
```

`uv` and `git` are the only prerequisites.

## Ground rules

- **TDD is mandatory.** No production code without a failing test first
  (red → green → refactor). Bugs get a regression test that reproduces them before
  the fix. The suite stays green and the output pristine.
- **Tests are deterministic and offline.** Inject a fake embedder, pass `ts`/`now_ts`
  explicitly, never hit the network, never load the real fastembed model.
- **One LLM chokepoint — OAuth only.** Every model call goes through `claude_cli`
  (the local `claude` CLI on your own subscription). **Never** the Anthropic SDK or an
  `ANTHROPIC_API_KEY` — the engine refuses to run if one is present.
- **One writer.** Route every mutation through `memory_lib` / `_write_txn`. No raw SQL
  in feature code.
- **Keep the repo content-free and path-free.** No `memory.db`, no exports, no secrets,
  no hardcoded user/home paths — a `test_no_hardcoded_paths` guard enforces it across
  both the package and the markdown publish surface.
- **Docs in lockstep.** A change under `ultra_memory/` should be reflected in `docs/`
  (the `.githooks/pre-commit` reminder lists what to update — it warns, never blocks).
- **Migrations** are forward-only and idempotent; add a test per migration.

## Pull requests

Keep changes focused and small; match the surrounding style. Run `uv run pytest -q`
and ensure it's green before opening a PR. See the full guide for the doc-update
checklist and the engine conventions.
