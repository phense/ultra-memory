# 3. Quick start

Claude forgets everything the moment a session ends. The fastest way to feel what ultra-memory changes is to install it, restart once, and watch the next session open with your project's context already in front of Claude — pinned rules, recent decisions, the way you like to work. No cloud account, no API key, no migration. Four lines and a restart.

This chapter takes you from nothing to a working install, then names exactly what starts happening on its own — so there are no surprises about what is running on your machine.

## Before you start

ultra-memory is a drop-in [Claude Code](https://docs.claude.com/en/docs/claude-code) plugin. It needs two tools already on your `PATH`, and the setup command checks both before doing anything:

| Requirement | Why it's needed |
|---|---|
| **`uv`** | Provisions the Python 3.13 runtime the engine runs on. The engine is pure Python 3.13 + SQLite — no other binary is ever shelled. |
| **`git`** | Your undo button. ultra-memory commits a readable, secret-stripped snapshot of your store; the self-correcting steps only act where a git checkpoint exists. Without git there is no restore net. |

If either is missing, setup stops with a clear message and changes nothing. There is **no API key and no cloud account** — the plugin runs entirely on your local machine and your existing Claude login.

The first time you run setup it downloads one small local search model (about the size of `bge-small`) and caches it. Every run after that is offline.

## Install

Run these four steps inside Claude Code. The first three are slash commands; the fourth is a restart.

```text
# 1. Add the marketplace
/plugin marketplace add phense/ultra-memory

# 2. Install the plugin
/plugin install ultra-memory@ultra-memory

# 3. Build the runtime, prepare the database, run a quick self-check
/ultra-memory:memory-setup

# 4. Restart Claude Code  (so the read-only knowledge MCP registers)
```

That is the whole install. You do **not** hand-edit `.mcp.json`, `settings.json`, or any wrapper script — `/ultra-memory:memory-setup` builds the runtime virtualenv, resolves where your memory lives, stamps the database as ready, and runs a sanity check (the MCP module imports, the search model loads, a trial recall returns). It is idempotent: re-running it only repairs whatever is missing, so it is always safe to run again.

When it finishes it prints the **resolved database path** — confirm that path is the store you intend before moving on. By default that path is:

```text
~/.ultra-memory/memory.db
```

— one store shared across every project on your machine. You can point it elsewhere, but you don't have to; see [Configuration](06-configuration-reference.md) for the one optional override and the full table.

## Use it

Claude Code namespaces a plugin's commands with the plugin name, so every verb starts with `/ultra-memory:`. These are the seven you'll reach for (each is covered with a worked example in [Working with your memory](04-working-with-memory.md)):

```text
/ultra-memory:memory-save      save a durable fact (how you work, a decision, a reference)
/ultra-memory:memory-recall    search your memory on demand
/ultra-memory:memory-pin       keep a rule in view at the start of every session
/ultra-memory:memory-verify    reconfirm a fact is still true (resets its "stale" clock)
/ultra-memory:memory-edit      correct a stored memory
/ultra-memory:memory-inbox     apply pin/verify notes you jotted between sessions
/ultra-memory:memory-maintain  run lightweight cleanup now (no AI calls)
```

You rarely have to *ask* for memory, though. Most of the value arrives without a command:

- **At the start of every session,** ultra-memory injects a short summary — your pinned rules plus the memories most relevant right now — straight into Claude's context. (This is the "rehydration gist"; budget it in [Configuration](06-configuration-reference.md).)
- **When a session ends,** it saves a checkpoint of what happened.
- **Subagents** can read your memory through a read-only tool, but only the facts they're allowed to see — there's a privilege boundary between a trusted top-level session and a spawned subagent.

## What's running out of the box (and it's all yours)

This is the part people most want stated plainly. As of **v0.0.4 the self-learning loop is on by default** — it is *opt-out*, not opt-in. After install, with no further configuration, the following advance on their own whenever you open Claude Code (each step is throttled on its own clock, so opening many sessions a day is cheap):

| What runs automatically | What it does, in one line | Cadence |
|---|---|---|
| **Session capture** | Mines each finished session's transcript into durable memory. | ~daily |
| **Outcome attribution** | Notices which recalled facts actually helped. | with capture |
| **Consolidate** | Promotes lessons that have proven their worth into the store / wiki. | ~weekly |
| **Self-correct** | Fixes, retires, or sets aside the loop's *own* earlier notes — never yours. | ~monthly |
| **Synthesize** | Turns a cluster of repeated lessons into a brand-new reusable skill. | ~monthly |

Two guarantees make this comfortable to leave on:

1. **It runs on *your* Claude login — no API key, no metered bill.** Every AI call goes through your local `claude` command on your own subscription. A paid API key on the process is a hard error; there is deliberately no metered path. The light steps (session capture, the daily cleanup) use no AI at all.
2. **It reads only your *local* session transcripts.** Nothing is uploaded. Your memory database, notes, and paths stay on your machine and in your own project — this repository ships code only, never content.

And it is built to be conservative: the self-correcting steps can never delete (only archive), can never touch a rule you pinned or a fact you authored, are capped to a few changes per run, checkpoint to git before they act, and write you a short summary afterward. You read the summary; you don't babysit the work. [The self-learning loop in practice](05-self-learning-in-practice.md) walks through reading a digest and turning any step off if you'd rather it sit quiet.

If you'd prefer to start narrow: every step can be switched off individually from the `/plugin` config (Session capture / Outcome attribution / Self-correction / Skill synthesis), and a memory-only install — no wiki configured — simply skips the wiki-related steps with no setup at all.

---

**Next:** [Working with your memory →](04-working-with-memory.md) — every verb with a real example, how recall and the session-start gist work, and how to pin your hard rules.
