---
name: using-memory
description: Use whenever you READ FROM or WRITE TO the agent-memory layer — recall a remembered fact, persist a durable fact about how the user wants to work, pin a hard rule, or correct a stored memory. READ via /memory-recall (trusted CLI) or the type-scoped knowledge MCP; WRITE only through the memory verbs (/memory-save, /memory-pin, /memory-verify, /memory-edit, /memory-inbox) — NEVER raw SQLite. Trigger before any memory read or write.
---

# Using memory

This project's **volatile** knowledge — how the user wants to work, feedback, current project state, references — lives in a SQLite-canonical memory store (`ultra-memory`). The verbs enforce the rules; this skill teaches you which path to use and what never to do.

## What memory is / is not
- **Memory** = volatile, fast-moving facts: user preferences, feedback directives, current project state, references. Rule of thumb: *how the user wants to work* → memory.
- **Not** the durable domain knowledge base. If a wiki skill is co-installed (e.g. `using-trading-knowledge`), *what we learn about the domain* → there, not here.

## READ paths
1. **Ambient (already injected — do not re-fetch):** on every SessionStart the rehydration gist (pinned rules + where-we-left-off + open follow-ups + hot memories) is injected into your context. It is already there; do not call recall just to get it.
2. **Trusted / full recall:** `/memory-recall "<query>"` → JSON `{title, snippet, score, id, stale, links}`. This is the human/orchestrator path (full type access). Field semantics:
   - `stale: true` ⇒ `last_verified` is older than the staleness window ⇒ consider `/memory-verify` after reconfirming.
   - `links` ⇒ 1-hop outbound references; follow them for related context.
   - `score` is title-boosted (NOT raw cosine) — treat as a relative rank, not a probability.
3. **Scoped recall (subagents / crons):** the `knowledge` MCP `knowledge_query` tool. **Privilege boundary (fail-closed):** an untrusted caller (subagent/cron) recalls only `project` / `reference` facts — **never** `user` / `feedback` memories. Trusted full recall is the CLI, not the MCP. An agent opts into scoped recall by adding `mcp__knowledge__knowledge_query` to its `tools:` allowlist.

## WRITE paths
**Gateway rule (hard):** every write goes through a memory verb / `memory_lib` — never raw SQL, never an `INSERT`/`UPDATE` against the DB file. Each gateway write is secret-redacted, transactional, and audited.

| To… | Use |
|---|---|
| Persist a NEW durable fact | `/memory-save` (the canonical new-fact verb — wraps `memory_lib.save_memory`) |
| Make a fact always-in-context | `/memory-pin <id>` (auto-injects into the SessionStart gist — for hard rules; do not pin reflexively) |
| Reconfirm a fact still holds | `/memory-verify <id>` (resets the staleness signal) |
| Correct a fact's body in place | `/memory-edit <id>` (body only — NOT for adding new facts; type/title/fields preserved) |
| Apply queued human-correction directives | `/memory-inbox` (free text is preserved under "Unprocessed", never auto-applied) |

**Consolidate vs delete:** to supersede a fact, save its replacement and turn the old one into a redirect/tombstone (status change) — do not delete history. Audit + export are the rollback.

**Things you'd otherwise guess wrong:**
- Timestamps are naive-UTC ISO (`...Z`), the engine's convention.
- Secrets are auto-stripped on write, but still review a body for secrets before saving.
- `WriteSpooled` in a result ⇒ the DB was locked and the write is queued for replay — do NOT blindly retry; it will replay.

## The "never" list
- Never open the SQLite file directly, and never `INSERT`/`UPDATE`/`DELETE` outside the verbs / `memory_lib`.
- Never hand-clean `-wal` / `-shm` files.
- Never run `retention.prune_session_events`, `memory_export`, or `memory_import` reflexively — those are bootstrap-only (`/memory-setup`) or throttled-maintenance-only (`/memory-maintain`).
