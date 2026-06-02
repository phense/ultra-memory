# ultra-memory

### The One-Stop Memory Solution for Claude.

**Session memory + a durable expert-knowledge wiki + a self-improving skill loop — fused into one ranked recall, in a single Claude Code plugin. Local-first, OAuth-only, zero cloud, zero API keys.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![version](https://img.shields.io/badge/version-0.0.2-informational.svg)](.claude-plugin/plugin.json)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-1044%20passing-brightgreen.svg)](tests/)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A63D2.svg)](https://docs.claude.com/en/docs/claude-code)

Most "memory for Claude" tools give you one bucket: they capture a session, compress it,
and squirt it back next time. ultra-memory gives you a **knowledge fabric** — two stores
with *different half-lives* (fast-moving session memory **and** a curated, git-canonical
expert-knowledge wiki), a **typed-edge graph** that spans both, **one ranked recall** that
fuses them, a **single audited write gateway** that redacts secrets on the way in *and* out,
and an opt-in **self-learning loop** that consolidates, self-corrects, and even synthesizes
new skills from what it learns. All of it runs on your machine, on your Claude subscription —
no vector cloud, no metered API key, ever.

> **The boundary (this repo is published; your data is not):** this repository holds **only code**
> and is **content-free**. Your `memory.db`, exports, paths, the knowledge base it indexes, and any
> secrets live in *your* project and are injected via config — never committed here. No hardcoded user
> paths (enforced by a test). One plugin, many consumers.

**[Why ultra-memory](#why-ultra-memory)** · **[Quick Start](#quick-start)** · **[What makes it different](#what-makes-it-different)** · **[How it works](#how-it-works)** · **[Comparison](#comparison)** · **[Configuration](#configuration)** · **[Status](#status--honest-roadmap)** · **[License](#license)**

---

## Why ultra-memory

🧠 **It's a fabric, not a bucket.** A session-memory store for *how you work* (preferences, project
state, corrections) and a durable, topic-partitioned **LLM-Wiki** for *what you've learned* (concepts,
studies, post-mortems) — kept separate because they decay at different speeds, ranked **together** at
recall time.

🔗 **It has a real graph.** A typed-edge `links` table spans memory ↔ wiki, so a session learning can
be *graduated* into a wiki page and the edge is recorded — recall traverses it.

🔒 **It's private by construction.** Local SQLite, **OAuth-only** (an `ANTHROPIC_API_KEY` on the
process raises a hard `OAuthViolation` — the engine never touches the metered API or the SDK), and a
single audited write gateway that strips secrets at both persist and export.

♻️ **It improves itself — on your terms.** An opt-in, wall-governed self-learning loop
(consolidate → attribute → self-correct → synthesize) that can dedup, reinforce, revert, and even
induce new Claude skills from matured lessons. Built, tested, and shipped behind explicit arming
gates so *you* stay in control.

⚡ **It installs in one step and stays out of your way.** A zero-config Claude Code plugin: a
~15 ms SessionStart rehydration gist, a Stop-checkpoint, throttled background maintenance — fail-open
everywhere, so it can never wedge a session.

---

## Quick Start

ultra-memory is a drop-in [Claude Code](https://docs.claude.com/en/docs/claude-code) plugin.

```bash
# 1. Add the marketplace (replace <owner> with the GitHub owner of this repo)
/plugin marketplace add <owner>/ultra-memory

# 2. Install
/plugin install ultra-memory@ultra-memory

# 3. Bootstrap (builds the runtime venv, stamps the DB, sanity-checks), then restart Claude Code
/memory-setup
```

That's the whole install — no hand-editing `.mcp.json`, `settings.json`, or any wrapper.
**Zero-config:** the store auto-derives to `~/.ultra-knowledge/memory.db` (one fabric shared by all
your projects). Optional `userConfig` overrides exist (`data_db_path`, `caller_class`,
`rehydrate_budget`) but nothing is required.

**Then just use it:**

```text
/memory-save     persist a durable fact (how you want to work, a decision, a reference)
/memory-recall   query the store on demand
/memory-pin      keep a hard rule hot in every session's rehydration gist
/memory-verify   reconfirm a fact is still true (resets its staleness clock)
/memory-edit     correct a stored memory in place
/memory-inbox    apply pin/verify directives you typed between sessions
/memory-maintain run maintenance now (prune + refresh views; no LLM)
```

On each **SessionStart** the rehydration gist (pinned rules + hot memories) is injected; on **Stop** a
checkpoint is written; a read-only, type-scoped `knowledge` MCP tool exposes recall to subagents
behind a fail-closed privilege wall.

**Requirements:** `uv` and `git` on `PATH` (both preflighted by `/memory-setup`). `uv` provisions the
Python 3.13 runtime; `git` is the rollback model — the deterministic redacted SQL dump is the sole
git-committed restore artifact. First `/memory-setup` downloads a small local embedder (~bge-small),
cached afterward. **No API key, no cloud account, ever.**

---

## What makes it different

Three things that, *together*, no other Claude-memory tool ships:

### 1. A durable expert-knowledge wiki — not just session memory
Session memory is volatile (preferences, state, corrections). Real expertise — concepts, indicator
studies, post-mortems — has a longer half-life and deserves a **curated, canonical home**.
ultra-memory treats a **topic-partitioned LLM-Wiki** (plaintext Markdown, canonical in git) as a
first-class store *beside* session memory: it ships the **wiki sync**, the **cross-store fusion**, and
the full **curation/maintenance engine** — a schema-driven 5-detector framework (stale · dedup · scope ·
lint · graph), a grey-zone dedup judge, and a conservative consolidation drain. Structured writes go
through a single **audited gateway** (routed, deduped, secret-redacted, audited) wired via a thin
consumer config seam — scaffold your own gateway in one command, or run **pure-memory with no wiki
at all**.

### Extending the wiki gateway

The wiki write path is a subclassable plugin API — `ultra_memory.wiki_gateway.WikiGateway`. To give a
project its own wiki (custom routing, dedup, frontmatter, anchors, labels), scaffold an extension:

```
python -m ultra_memory.wiki_gateway scaffold --out scripts/my_wiki.py --class-name MyWikiGateway --topic mytopic
```

then override only the hooks that differ and wire `wiki_gateway = "my_wiki:MyWikiGateway"` in
`.ultra-memory/config.toml` (unset → a turnkey built-in). The `using-wiki-gateway` skill teaches the
6-hook contract; the inherited engine (materialization, secret redaction, write-lock, audit) is never
re-implemented. (No consumer config at all → a pure-memory install with no wiki.)

### 2. A typed-edge graph + one ranked recall across both stores
A `links` table records typed edges (e.g. a `validated_as` edge from a session learning to the wiki
page it matured into). `unified_recall` then fuses **memory** and **wiki** hits into a single ranked
list via deterministic Reciprocal-Rank-Fusion (stable under `PYTHONHASHSEED`), scoped by a
fail-closed **role × topic** privilege wall — a subagent literally cannot recall another topic's or a
higher-privilege caller's rows.

### 3. A Hermes-style self-learning skill loop (opt-in)
The fourth beat is what makes it an *organism*: a four-stage loop —
**consolidate** (graduate matured lessons) → **attribute** (credit outcomes back to recalled memories)
→ **self-correct** (auto-edit / revert / quarantine *agent-authored* knowledge, never your pinned
rules) → **synthesize** (induce a new `gen-*/SKILL.md` from a cluster of graduated lessons). It's
**built, tested (part of the 1044-test suite), and wall-governed** — provenance-gated (human/pinned
rows are physically immutable), bounded-blast-radius, archive-never-delete, git-checkpointed,
OAuth-only — and it **ships disabled by default** so you arm it deliberately after reviewing dry-run
digests. (See [Status](#status--honest-roadmap) for exactly what's live vs. armed-by-you.)

---

## How it works

```
            ┌──────────────────────── one knowledge fabric ────────────────────────┐
            │                                                                       │
   Session Memory (SQLite, volatile)            Expert Knowledge — LLM-Wiki (Markdown, durable)
   how you work · state · corrections           concepts · studies · post-mortems · canonical in git
            │                                                                       │
            └──────────────┬───────────  typed-edge graph (links)  ────────┬────────┘
                           │                                               │
                    unified_recall  ── deterministic RRF fusion ──  one ranked list
                           │            (role × topic privilege wall, fail-closed)
                           │
     single audited write gateway (memory_lib) · strip_secrets on persist + export · OAuth-only
                           │
   SessionStart gist (~15 ms, fail-open) · Stop checkpoint · throttled background maintenance beats
                           (consolidate · attribute · self-correct · synthesize · projection-regen)
```

- **OAuth-only chokepoint:** every LLM call goes through `claude_cli` on your Claude subscription. An
  `ANTHROPIC_API_KEY` on the process is a hard error — there is deliberately no metered-API path.
- **Single audited write gateway:** all mutations funnel through `memory_lib`, with secret redaction
  at persist *and* export, retryable transactions, and a spool for busy-casualties.
- **git is the rollback model:** a deterministic, redacted SQL dump + a VACUUM snapshot are the
  committed restore artifacts; nothing destructive, soft-delete + redirect-stub only.
- **Fail-open hooks:** a maintenance or hook error degrades to one log line — it never blocks or
  wedges a session.

Full architecture lives in [`docs/`](docs/) — [`user/`](docs/user/) (overview + usage),
[`developer/`](docs/developer/) (architecture + contributing), [`reference/`](docs/reference/)
(schema, API, operations).

---

## Comparison

How ultra-memory stacks up against the most popular Claude/AI memory **and knowledge** projects —
including [STORM](https://github.com/stanford-oval/storm), the ~28k★ "LLM-writes-a-wiki" system from
Stanford, to test our knowledge-wiki claim against a *real* LLM-wiki. **Honest:** we lead on
architecture today and say plainly where we don't — the field's real advantage over us is **adoption**
(we're pre-public), and our self-learning loop, though built and test-covered, **ships opt-in**.

Legend: ✅ shipped & live · ⚠️ partial / opt-in / caveated (see notes) · ❌ absent

| Capability | **ultra-memory** | [claude-mem](https://github.com/thedotmack/claude-mem) (~80k★) | [mem0](https://github.com/mem0ai/mem0) (~56k★) | [Basic Memory](https://github.com/basicmachines-co/basic-memory) (~2.8k★) | [STORM](https://github.com/stanford-oval/storm) (~28k★) |
|---|:--:|:--:|:--:|:--:|:--:|
| **Durable expert-knowledge wiki** (separate half-life from session memory) | ✅ ⁷ | ❌ | ❌ | ⚠️ ¹ | ⚠️ ⁸ |
| **Cross-store unified recall** (memory + wiki, one ranked list) | ✅ | ❌ | ⚠️ ² | ⚠️ ² | ❌ |
| **Knowledge graph / typed links** | ✅ | ❌ | ✅ | ✅ | ⚠️ ⁹ |
| **Autonomous self-learning** (dedup · consolidate · self-correct · synthesize) | ⚠️ ³ | ⚠️ | ⚠️ | ⚠️ | ❌ |
| **Audited write + secret redaction** (single gateway) | ✅ | ⚠️ | ❌ | ⚠️ | ❌ |
| **Role × topic privilege wall** (fail-closed) | ✅ | ❌ | ⚠️ | ❌ | ❌ |
| **Local-first, no paid API key** | ✅ | ✅ | ❌ | ✅ | ❌ |
| **Git-trackable plaintext storage** | ✅ | ⚠️ ⁴ | ❌ | ✅ | ⚠️ ¹⁰ |
| **Claude-Code-native, one-command install** | ⚠️ ⁵ | ✅ | ❌ | ✅ | ❌ |
| **Adoption / community** | ❌ ⁶ | ✅ | ✅ | ⚠️ | ✅ |

<sub>
¹ Basic Memory's Markdown notes are durable, but it's one flat store — it doesn't split fast-moving
session memory from durable expert knowledge.
² mem0 and Basic Memory do hybrid retrieval *within one store*; neither fuses a separate durable-wiki
store and a session store into one ranked list the way <code>unified_recall</code> does.
³ ultra-memory's four-beat loop (consolidate → attribute → self-correct → synthesize) is fully built
and test-covered but <strong>ships disabled by default</strong> — you arm it after reviewing dry-run
digests. (Honesty: some niche memory tools, e.g. <a href="https://github.com/doobidoo/mcp-memory-service">doobidoo/mcp-memory-service</a>,
run autonomous consolidation live today; ours is broader in scope but opt-in.)
⁴ claude-mem keeps a local but <em>binary</em> Chroma vector store — not human-readable/diffable like
Markdown or a redacted SQL dump.
⁵ Native zero-config plugin, but <strong>not yet public</strong> — a 2026-06-02 audit lists a few
release blockers (see <a href="BACKLOG.md">BACKLOG §5.2</a>). Marked ⚠️ until published.
⁶ Pre-public, zero stars. This is the field's clearest advantage over us today — claude-mem (~80k★)
and mem0 (~56k★, funded, hosted dashboard, 14M+ downloads) have distribution we have yet to earn.
⁷ ultra-memory ships the wiki tier as an <em>engine</em> — sync, cross-store fusion, and the full
curation/maintenance pipeline (5 detectors + grey-zone judge + consolidation); the structured write
gateway is a subclassable base (<code>WikiGateway</code>) — scaffold a starter extension with one
command (<code>python -m ultra_memory.wiki_gateway scaffold</code>), then wire it via a thin consumer
config seam (or leave unset for the built-in turnkey). Genuinely first-class and git-canonical, but
bring-your-own-wiki rather than a turnkey authoring UI.
⁸ STORM autonomously <em>writes</em> citation-grounded, Wikipedia-style articles (genuine LLM
curation) — but each run emits a <strong>standalone report</strong>: no <code>[[wikilinks]]</code>, no
cross-article knowledge base, no topic-partitioned store that accumulates and is re-queried over time.
⁹ Only Co-STORM builds a hierarchical "mind map," and it is <strong>per-session and ephemeral</strong>
— not a persistent typed-edge graph that retrieval traverses.
¹⁰ STORM emits Markdown/JSON you can commit, but these are generated <strong>report artifacts</strong>
read/edited elsewhere — not a git-tracked store agents continuously read from and write back to.
</sub>

**Bottom line:** ultra-memory is the only Claude-memory layer that ships the *full fabric in one box* —
a session store **and** a git-canonical expert-knowledge wiki, fused into one ranked recall over a
typed-edge graph, behind an audited redaction gateway, gated by a role × topic wall, on an OAuth-only
path. No competitor combines all of these. It out-features the field on architecture today; it has yet
to earn the field's distribution.

---

## Configuration

Zero-config by default. Everything below is optional (`userConfig` at install, or `ULTRA_MEMORY_*`
env, or a consumer `.ultra-memory/config.toml`):

| Setting | Default | Purpose |
|---|---|---|
| `data_db_path` | `~/.ultra-knowledge/memory.db` | Override the fixed global store location. |
| `caller_class` | `subagent` | Privilege class for the `knowledge` MCP. `subagent` ⇒ project/reference facts only (fail-closed); set `orchestrator` only on a trusted top-level instance. |
| `rehydrate_budget` | `2000` | Character budget for the SessionStart rehydration gist. |
| `oauth_token` | — | Claude OAuth token (**never** an `ANTHROPIC_API_KEY`); only needed if you arm LLM maintenance. |

The maintenance pipeline (consolidate / self-correct / synthesize / projection-regen) resolves its
project specifics — wiki gateway, audit dir, probe corpus, model — through the same config seam, and
**degrades gracefully**: with no wiki configured the wiki beats are no-ops, so a pure-memory install
just works.

---

## Status — honest roadmap

ultra-memory is **early and pre-public** — strong engine, distribution not yet earned. In the spirit
of "the docs match reality":

- ✅ **Live today:** the two-store fabric, `unified_recall` RRF fusion, the typed-edge graph, the
  single audited write gateway + redaction, OAuth-only enforcement, SessionStart/Stop hooks, the
  read-only `knowledge` MCP with the role × topic wall, the wiki-curation maintenance pipeline,
  zero-config install, **1044 green tests**, content-free repo.
- 🔵 **Built + tested, ships disabled (you arm it):** the self-learning loop —
  consolidate (SP-6), usage-attribution (SP-8 substrate), aggressive self-correct (SP-7), skill
  synthesis (SP-10), session-ingest. Each is behind an explicit arming gate; `outcome_weight` stays
  inert at `1.0` until you enable attribution. This is deliberate: the highest-blast-radius autonomy
  is opt-in, dry-run-first, human-in-the-audit-loop.
- ⬜ **Open before a clean public release** (tracked in [`BACKLOG.md`](BACKLOG.md) §5.2): a fresh-install
  MCP fix, scrubbing an internal audit dir, the public-marketplace install path above, and a version
  bump. Single-root today; global cross-project root activation is designed, not yet enabled.

---

## Contributing

TDD is mandatory and `docs/` are kept in lockstep with the code. A warn-only doc-discipline hook ships
under `.githooks/`; enable it once per clone with `git config core.hooksPath .githooks`. Run the suite
with `uv run pytest`. See [`docs/developer/contributing.md`](docs/developer/contributing.md).

## License

[MIT](LICENSE). Code-only and content-free — your data and config stay private in your own project.
