# 12. Contributing

> Most of what makes ultra-memory trustworthy is not in any one clever function — it's
> in a handful of invariants that *every* change has to respect. A memory engine that
> sometimes loses a write, occasionally bills an API, or once-in-a-while leaks a secret
> into a public dump is worse than no memory engine at all, because you'd stop trusting
> it. So the contribution rules here are not bureaucracy: they are the things that, if
> you keep them, mean the next person can build on your change without re-auditing the
> whole store. Read them once, internalize the four invariants, and the rest is just
> good taste.

This chapter is for someone about to send a change. It assumes you've read
[Chapter 10 — Architecture](./10-architecture.md) (for the shape) and keep
[Chapter 11 — Reference: API & schema](./11-reference-api-schema.md) open (for the
surface).

`uv` and `git` are the only prerequisites. `uv` provisions Python 3.13 and the deps.

---

## 12.1 Quick start

```bash
git clone https://github.com/phense/ultra-memory
cd ultra-memory
uv run pytest -q                       # the full suite (~1,200 tests)
git config core.hooksPath .githooks    # enable the warn-only doc-reminder hook (once)
```

If those two commands pass and the hook is enabled, your clone is ready.

---

## 12.2 TDD is mandatory

**No production code without a failing test first** (red → green → refactor). Every
function and every bug-fix has a test; a bug gets a regression test that *reproduces it*
before the fix. The suite must stay green and the output pristine.

```bash
uv run pytest -q                          # everything
uv run pytest tests/test_memory_lib.py    # one module
uv run pytest -k "redact or oauth"        # a slice by name
uv run pytest tests/test_migration_0008.py   # a single migration's test
```

Tests are **deterministic and offline**, and that is enforced by how you write them:

- **Inject a fake embedder** (`list[str] -> list[list[float]]`). Never load the real
  fastembed model — the `[retrieval]` extra is opt-in and exercised only via an
  `ImportError`-branch test.
- **Pass `ts` / `now_ts` explicitly.** No ambient clock reads. A time-dependent function
  (e.g. `render_union_blend_block`) takes an explicit `now` so it's deterministic for a
  fixed input.
- **Never hit the network.** No real `claude` CLI call — inject a `runner` into
  `run_claude`.
- **Use a temp DB.** Open a fresh `memory.db` per test; the engine creates + migrates it.

If you find yourself reaching for a real model, a real clock, or a real network call to
make a test pass, the *code* needs an injection seam, not the test an exception.

---

## 12.3 The four invariants

These are the load-bearing rules. A change that violates one is wrong even if the test
passes — and most of them have a guard test that will fail anyway.

### 1. OAuth-only — one LLM chokepoint

Every model call goes through `claude_cli.run_claude`, which shells out to the local
`claude` CLI on **your own Claude subscription**. It strips Claude-Code env markers and
**raises `OAuthViolation` if `ANTHROPIC_API_KEY` is set or the OAuth token is missing**.

**Never** the Anthropic SDK, `api.anthropic.com`, `client.messages.create`, or
`cache_control`. There is deliberately no API key on disk. If your change needs an LLM,
route it through `claude_cli` and inject the `runner` for tests. Treat any SDK/API usage
as a bug to flag in review.

### 2. One writer — single-writer discipline

Route **every** mutation through `memory_lib` and `_write_txn`. No raw `INSERT`/`UPDATE`
in feature code. `_write_txn` is what gives you the `BEGIN IMMEDIATE` retry, the durable
spool + loud `WriteSpooled` on exhaustion, and the `audit_log` row — for free, and
consistently. If you need a new mutation, add a `memory_lib` verb that goes through it;
don't open your own transaction.

A corollary: **no `claude_cli` call ever happens inside a write transaction**. LLM
latency must never hold the write lock.

### 3. Content-free / path-free — the publish surface stays clean

This repository ships **code only, no content**. No `memory.db`, no exports, no secrets,
and **no hardcoded user/home paths** — not in the package, and not in the Markdown you
write (including this handbook). Consumer data and paths are passed in by config and
resolved at runtime (`db_path_from_env`, the `ULTRA_MEMORY_*` env seams).

This is guarded: `tests/test_no_hardcoded_paths.py` scans both the package and the
tracked Markdown publish surface for `Users`/`home`-rooted absolute path literals (the
`/(?:Users|home)/<name>` pattern). If you need to *show* a path in docs, use a
placeholder (`<project>/…`, `$HOME/.ultra-memory/…`), never a real one.

### 4. Forward-only migrations

Migrations are `migrations/NNNN_name.sql`, applied in order, **additive and idempotent**:
every statement is `ADD COLUMN` or `CREATE … IF NOT EXISTS` — no `DROP`, no `RENAME`. The
runner applies the statements + the `user_version` bump in one transaction, so a crash
rolls back fully and the schema never desyncs from the version. A restore or replay
against an already-shaped DB must be a no-op.

**Add a test per migration** (see `tests/test_migration_0004.py`,
`…_0005.py`, `…_0008.py` for the pattern). A one-time *data* step (like the topic
backfill) is **never** in a `.sql` — it's a separate, gated, audited code path in
`memory_lib`.

---

## 12.4 The doc-discipline hook

Docs are kept in **lockstep** with code. The mechanism is a **warn-only pre-commit
hook** (`.githooks/pre-commit`): if a commit stages changes under `ultra_memory/` but
nothing under `docs/`, it prints a reminder listing the changed modules — then **lets the
commit through**. It never blocks, and there is deliberately **no escape-hatch env var to
abuse**; the discipline is on you.

Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

(The hook is tracked under `.githooks/` so it travels with clones, but git does not
auto-enable `core.hooksPath` — hence the one-time setup.)

When you touch a module, ask which doc owns the change:

| You changed… | …update |
|---|---|
| A public function or signature | `docs/11-reference-api-schema.md` |
| A table, column, or migration | `docs/11-reference-api-schema.md` |
| A behaviour, flow, or invariant | `docs/10-architecture.md` |
| A user-visible capability or command | `docs/04-working-with-memory.md` |
| An operational flow (export / dump / spool / rollback) | `docs/10-architecture.md` |

If a change genuinely needs no doc update (an internal-only refactor with no behavioural
or interface change), it's fine to commit through the warning — just be honest about it.

> **Note:** the handbook *is* the documentation — its chapters live directly under `docs/`
> and stay in lockstep with the code. Update the chapter that owns the change; the
> doc-discipline hook nudges you whenever a code commit touches no `docs/` file at all.

---

## 12.5 Conventions

Beyond the four invariants, match these so a reviewer reads your change as "obviously
fine":

- **Pure functions where possible.** Pass `conn`, `ts`, and the embedder in — no
  globals, no clock reads, no hidden I/O. (This is also what keeps the suite offline.)
- **Fail-open on the session/maintenance path.** A hook or a maintenance beat that
  raises must degrade to a recorded error + one log line — never wedge a session. The
  autonomous beats additionally enforce their safety wall **in the apply path (code),
  not the prompt**.
- **Redact at the write chokepoint.** Any new persisted text field passes through
  `strip_secrets`. If you add a write path, it redacts; if you add a read path that
  surfaces stored text across the privilege boundary, redact again (defense-in-depth).
- **Keep the privilege wall fail-closed.** A new recall path scopes by
  `(type × topic × caller_class)` and defaults to *less* visibility, never more. An
  unresolved binding sees the empty set.
- **Keep files focused and small,** and match the surrounding style.

---

## 12.6 Running the suite & opening a PR

```bash
uv run pytest -q          # must be green, output pristine, before you push
```

Then keep the change focused and small, match the surrounding style, and open the PR
with the doc updates the hook reminded you about already included. The short version of
this whole chapter:

- a failing test came first;
- the four invariants hold (OAuth-only, one-writer, content-free/path-free,
  forward-only migrations);
- the docs moved with the code;
- `uv run pytest -q` is green.

If all four are true, you're done.

---

## Where to go next

- The shape of the system you're changing: **[Chapter 10 —
  Architecture](./10-architecture.md)**.
- The exact surface — every function, table, and migration:
  **[Chapter 11 — Reference: API & schema](./11-reference-api-schema.md)**.
- Back to the **[handbook index](./README.md)**.
