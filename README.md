# ultra-memory

### Lasting memory for Claude — on your own machine.

**Claude forgets everything when a session ends. ultra-memory gives it a memory that lasts: it remembers how you work, what your project decided, and what you've learned — and keeps that knowledge tidy on its own. One Claude Code plugin, running on your machine and your Claude subscription. No cloud service, no API key, no bill.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![version](https://img.shields.io/badge/version-0.0.3-informational.svg)](.claude-plugin/plugin.json)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-1176%20passing-brightgreen.svg)](tests/)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A63D2.svg)](https://docs.claude.com/en/docs/claude-code)

Most "memory for Claude" tools give you one bucket: they save a session, compress it, and replay it
next time. ultra-memory keeps **two kinds of memory at once**, because not everything you want Claude
to remember ages at the same speed:

- **Session memory** — *how you work*: preferences, your project's current state, corrections you've
  made. Fast-moving, stored in a local SQLite database.
- **A knowledge wiki** — *what you've learned*: concepts, findings, post-mortems — the durable stuff
  worth keeping. Stored as plain Markdown you can read, edit, and track in git.

When Claude needs context, ultra-memory searches **both at once** and returns one ranked list of what's
relevant. A small graph of links ties the two together, so a lesson from a session can "graduate" into a
wiki page and stay connected. And over time the plugin curates itself — merging duplicates, correcting
what it got wrong, even turning repeated lessons into new reusable skills — always in small, reversible
steps you can review, and never touching a rule you've locked down.

> **Your data stays yours.** This repository is **code only** — it ships no content. Your memory
> database, your notes, your paths, and any secrets live in *your* project and are passed in by config;
> nothing personal is ever committed here (a test enforces it). One plugin, many projects.

**[Why](#why-ultra-memory)** · **[Quick start](#quick-start)** · **[What's different](#what-makes-it-different)** · **[How it works](#how-it-works)** · **[Comparison](#comparison)** · **[Configuration](#configuration)** · **[Status](#status--honest-roadmap)** · **[Acknowledgments](#acknowledgments)** · **[License](#license)**

---

## Why ultra-memory

🧠 **Two memories, searched as one.** Quick "how you work" facts and a durable, growing knowledge base —
kept apart because they age differently, ranked together when Claude needs them.

🔒 **Private by design.** Everything lives in local files. It runs on your Claude login and **refuses to
start if a paid API key is present** — there is deliberately no metered-API path. Secrets are stripped
on the way in *and* on the way out.

♻️ **It tidies itself, safely.** A background loop merges duplicates, improves what it stored, and can
even create new skills from lessons it keeps seeing — in small, bounded, reversible steps. It can
**never** delete anything (only archive it) and can **never** change a rule you've pinned. You read a
short summary of what it did; you don't babysit it.

⚡ **One-step install, then invisible.** A drop-in Claude Code plugin. It adds a few milliseconds at
session start and otherwise stays out of the way — and if anything ever goes wrong, it logs a line and
steps aside rather than blocking your work.

---

## Quick start

ultra-memory is a drop-in [Claude Code](https://docs.claude.com/en/docs/claude-code) plugin.

```bash
# 1. Add the marketplace
/plugin marketplace add phense/ultra-memory

# 2. Install
/plugin install ultra-memory@ultra-memory

# 3. Set up (builds the runtime, prepares the database, runs a quick check), then restart Claude Code
/ultra-memory:memory-setup
```

That's the whole install — no editing `.mcp.json`, `settings.json`, or any wrapper by hand. By default
your memory lives in `~/.ultra-memory/memory.db`, one store shared across all your projects. A few
optional settings exist (see [Configuration](#configuration)), but nothing is required.

**Then just use it** (Claude Code namespaces a plugin's commands with the plugin name):

```text
/ultra-memory:memory-save      save a durable fact (how you work, a decision, a reference)
/ultra-memory:memory-recall    search your memory on demand
/ultra-memory:memory-pin       keep a rule in view at the start of every session
/ultra-memory:memory-verify    reconfirm a fact is still true (resets its "stale" clock)
/ultra-memory:memory-edit      correct a stored memory
/ultra-memory:memory-inbox     apply pin/verify notes you jotted between sessions
/ultra-memory:memory-maintain  run cleanup now (no AI calls)
```

At the start of each session, ultra-memory injects a short summary of your pinned rules and most
relevant memories. When a session ends, it saves a checkpoint. Subagents can read your memory through a
read-only tool, behind a privilege boundary so they only ever see the facts they're allowed to.

**Requirements:** `uv` and `git` on your `PATH` (both checked by setup). `uv` provides the Python 3.13
runtime; `git` is how you roll back — ultra-memory commits a readable, secret-stripped snapshot of your
store, and nothing else. The first setup downloads a small local search model (about the size of
bge-small), cached afterward. **No API key, no cloud account, ever.**

---

## What makes it different

Three things that, together, no other Claude-memory tool ships:

### 1. A real knowledge base — not just session memory
Session memory is volatile: preferences, state, corrections. But real expertise — concepts, studies,
lessons learned — deserves a lasting, organized home. ultra-memory treats a **Markdown knowledge wiki**
(plain text, versioned in git) as a first-class store *alongside* session memory. It ships the whole
curation pipeline: it flags stale and duplicate pages, keeps links healthy, and merges near-duplicates
conservatively. Every structured write goes through **one gateway** that files the page in the right
place, removes duplicates, strips secrets, and logs the change. Want your own wiki layout? Subclass the
gateway and scaffold a starter in one command:

```
python -m ultra_memory.wiki_gateway scaffold --out scripts/my_wiki.py --class-name MyWikiGateway --topic mytopic
```

Then override only the parts that differ and point `wiki_gateway = "my_wiki:MyWikiGateway"` at it in
`.ultra-memory/config.toml`. Or skip the wiki entirely and run memory-only — with no wiki configured,
the wiki steps simply do nothing.

### 2. One ranked search across both stores
A small **links table** records typed connections — for example, the link from a session lesson to the
wiki page it grew into. A single search then blends memory and wiki results into one ranked list, scoped
by a **privilege boundary**: a subagent can't read another project's facts or a more-trusted caller's
private ones. The ranking is deterministic, so the same query gives the same order every time.

### 3. A self-learning loop
This is what makes it feel like an organism rather than a filing cabinet. A background loop runs in four
steps: **consolidate** (promote lessons that have proven their worth), **attribute** (notice which
remembered facts actually helped), **self-correct** (fix, retire, or set aside its *own* earlier notes —
never your pinned rules), and **synthesize** (turn a cluster of related lessons into a brand-new
reusable skill, after a check that it won't step on an existing one).

It's fully built and tested, and **safe by construction** rather than by good intentions. The rules are
enforced in code, not just asked for in a prompt: it cannot touch a fact you authored or pinned, cannot
delete (only archive), is capped per run (at most a few edits, a few reversions, one new skill),
checkpoints to git before it acts, and writes you a summary afterward. Because every step is small and
reversible, mistakes are rare *and* cheap to undo. You stay in the review loop, not the work loop. (See
[Status](#status) for exactly what's on by default.)

---

## How it works

```
        ┌───────────────────────── one knowledge base ─────────────────────────┐
        │                                                                       │
   Session memory (SQLite)                      Knowledge wiki (Markdown, in git)
   how you work · state · fixes                 concepts · studies · lessons
        │                                                                       │
        └──────────────┬──────────────  links between them  ──────────┬─────────┘
                       │                                               │
                 one ranked search  ──  blends both  ──  scoped by a privilege boundary
                       │
        one audited write path  ·  strips secrets in and out  ·  your Claude login only
                       │
   session-start summary (fast, never blocks) · end-of-session checkpoint · background cleanup
```

- **Your Claude login only.** Every AI call goes through the local `claude` command on your own
  subscription. A paid API key on the process is a hard error — there's deliberately no metered path.
- **One audited write path.** Every change funnels through a single gateway that strips secrets on save
  *and* on export, and retries safely under load.
- **git is your undo button.** ultra-memory commits a readable, secret-stripped snapshot of your store.
  Nothing is ever hard-deleted — it's archived and redirected instead.
- **It never blocks you.** If a background step or a hook hits an error, it logs one line and steps
  aside — it can't wedge your session.

The full architecture is in [`docs/`](docs/): [`user/`](docs/user/) (overview + usage),
[`developer/`](docs/developer/) (architecture + contributing), [`reference/`](docs/reference/)
(schema, API, operations).

---

## Comparison

How ultra-memory compares to the most popular Claude/AI memory **and knowledge** projects — including
[STORM](https://github.com/stanford-oval/storm), Stanford's ~28k★ "LLM-writes-a-wiki" system, to test
our knowledge-wiki claim against a *real* one. We lead on features today and say plainly where we don't:
the field's real edge over us is **adoption** — we're not public yet.

Legend: ✅ shipped & live · ⚠️ partial / opt-in / caveated (see notes) · ❌ absent

| Capability | **ultra-memory** | [claude-mem](https://github.com/thedotmack/claude-mem) (~80k★) | [mem0](https://github.com/mem0ai/mem0) (~58k★) | [Basic Memory](https://github.com/basicmachines-co/basic-memory) (~3.1k★) | [STORM](https://github.com/stanford-oval/storm) (~28k★) |
|---|:--:|:--:|:--:|:--:|:--:|
| **Durable knowledge wiki** ¹ (separate from session memory) | ✅ | ❌ | ❌ | ⚠️ | ⚠️ |
| **One ranked search across memory + wiki** | ✅ | ❌ | ⚠️ | ⚠️ | ❌ |
| **Knowledge graph / typed links** | ✅ | ❌ | ✅ | ✅ | ⚠️ |
| **Self-learning** ² (dedup · consolidate · self-correct · synthesize) | ✅ | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **Audited writes + secret stripping** (one gateway) | ✅ | ⚠️ | ❌ | ⚠️ | ❌ |
| **Privilege boundary on recall** | ✅ | ❌ | ⚠️ | ❌ | ❌ |
| **Local-first, no paid API key** ⁵ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ |
| **Plain-text, git-trackable storage** | ✅ | ⚠️ | ❌ | ✅ | ⚠️ |
| **Claude-Code-native, one-command install** ³ | ⚠️ | ✅ | ⚠️ | ✅ | ❌ |
| **Adoption / community** ⁴ | ❌ | ✅ | ✅ | ✅ | ✅ |

<sub>
¹ ultra-memory ships the wiki as an <em>engine</em> (sync, the cross-store search, the full curation
pipeline) with a subclassable write path — git-canonical and bring-your-own-layout, not a point-and-click
authoring UI. (Basic Memory has durable notes but one flat store; STORM writes standalone articles, not an
accumulating, re-queryable base.)
² Runs automatically but conservatively: in code it can't touch facts you authored or pinned, can't delete
(only archive), is capped per run, and checks a new skill won't collide before creating it — broader than
the dedup-only tools.
³ Native zero-config plugin, but <strong>not public yet</strong>. Marked ⚠️ until published.
⁴ Pre-public, zero stars — the field's clearest advantage over us today; claude-mem (~80k★) and mem0
(~58k★, funded, hosted, millions of downloads) have distribution we have yet to earn.
⁵ mem0 and STORM can run fully local with no paid key (mem0 self-hosted with an Ollama LLM; STORM with
Ollama + a keyless search backend such as SearXNG / DuckDuckGo) — but their <em>default, documented</em>
path uses a paid LLM and/or search API, so ⚠️ not ✅. ultra-memory has no metered path at all.
</sub>

**Bottom line:** ultra-memory is the only Claude-memory layer that ships the *whole thing in one box* —
a session store **and** a git-tracked knowledge wiki, blended into one ranked search over a graph of
links, behind a single secret-stripping write path, on your Claude login only. No competitor combines all
of these. It out-features the field on architecture today; it has yet to earn the field's reach.

---

## Configuration

Zero-config by default. Everything below is optional — set it at install, as an `ULTRA_MEMORY_*`
environment variable, or in a project's `.ultra-memory/config.toml`:

| Setting | Default | What it does |
|---|---|---|
| `data_db_path` | `~/.ultra-memory/memory.db` | Where your memory is stored. |
| `caller_class` | `subagent` | Who's asking, for the recall privilege boundary. `subagent` sees project/reference facts only; set `orchestrator` only on a trusted top-level session. |
| `rehydrate_budget` | `2000` | Size (in characters) of the session-start summary. |
| `oauth_token` | — | Your Claude login token (**never** a paid API key); only needed if you turn on the AI-powered cleanup. |

The background cleanup finds its project specifics — wiki gateway, where to write its reports, which
model to use — through the same config, and **degrades gracefully**: with no wiki configured, the wiki
steps simply do nothing, so a memory-only install just works.

---

## Status — honest roadmap

ultra-memory is **early and pre-public** — strong engine, reach not yet earned. In the spirit of "the
docs match reality":

- ✅ **Live today:** the two-store memory, one ranked search across both, the graph of links, the single
  audited write path with secret stripping, the "your-login-only" rule, the session-start/-end hooks, the
  read-only recall tool with its privilege boundary, the wiki-curation pipeline, zero-config install,
  **1176 passing tests**, and a content-free repository.
- ✅ **Self-learning loop — automatic and conservative:** all four steps (consolidate, attribute,
  self-correct, and create-new-skill) run behind the in-code safety rules
  (can't touch your facts, archive-not-delete, capped per run, git checkpoint, a written summary, a
  kill switch, and a collision check before any new skill). The defaults are deliberately tight (a few
  edits / a few reversions / one new skill per run); you can loosen them and watch the effect in the next
  summary. The whole loop is **on by default** and advances automatically as you use Claude Code
  (or on an optional installed schedule). It reads only your **local** session transcripts
  and runs on **your Claude login — no API key, no metered bill**. Turn any beat off from
  the `/plugin` config (Session capture / Outcome attribution / Self-correction / Skill
  synthesis). The self-correcting beats act only where a git checkpoint exists and
  otherwise self-skip — so they can always be undone.
- ✅ **Release hygiene — shipped:** continuous integration, contributor files (CONTRIBUTING / CHANGELOG),
  a third-party-license notice, and a content-free / path-free guard over the whole markdown publish surface.
- ⬜ **At publish time:** a one-time git-history scrub (the working tree is clean, but older commits still
  carry internal notes). Today everything is per-machine; sharing one store across projects is designed but
  not yet on.

---

## Acknowledgments

ultra-memory builds on other people's ideas and code:

- **Andrej Karpathy** — the [LLM-Wiki idea](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) behind the knowledge-wiki tier.
- **Praney Behl** — the [llm-wiki plugin](https://github.com/praneybehl/llm-wiki-plugin) (MIT), whose retrieval / lint / graph approach the wiki engine draws from.
- **obra** — the [superpowers](https://github.com/obra/superpowers) skill framework that shaped how this project is built (and how its skills work).
- **Anthropic** — Claude Code, the skills framework, and bundled skills (e.g. `simplify`, `skill-creator`, `code-review`).
- **The Hermes agent** — the template for the self-learning loop (capture → consolidate → self-correct → synthesize).

---

## Contributing

Tests come first (TDD), and the `docs/` are kept in step with the code. A warn-only doc-reminder hook
ships under `.githooks/`; enable it once per clone with `git config core.hooksPath .githooks`. Run the
suite with `uv run pytest`. See [`docs/developer/contributing.md`](docs/developer/contributing.md).

## License

[MIT](LICENSE). Code-only and content-free — your data and config stay private in your own project.
Third-party dependency licenses (all permissive — MIT / Apache-2.0) are listed in
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md).
