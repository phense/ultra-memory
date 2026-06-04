# The ultra-memory Handbook

Lasting memory for Claude — on your own machine. This handbook is the single front
door to ultra-memory: it takes you from *what it is* and *why it is shaped that way*,
through *using it day to day* and *configuring it*, to *building your own knowledge
domain* and *developing on the engine itself*. Read it front to back the first time;
afterwards it doubles as a reference.

The chapters are arranged as a progression — **Understand → Use → Configure → Extend
→ Develop** — followed by an appendix of design rationale. Each entry below says, in
one line, what you will learn there.

---

## Part I — Understand

The two ideas the whole system falls out of. Read these even if you only ever want to
*use* ultra-memory.

1. [What is ultra-memory?](01-what-is-ultra-memory.md) — the problem (Claude forgets
   everything at session end) and the one-paragraph promise: two memories, searched as
   one, on your own machine.
2. [The mental model](02-mental-model.md) — the single idea everything else falls out
   of: two stores with different half-lives, queried as one, tied by a graph of links.

## Part II — Use

Get it running and live with it.

3. [Quick start](03-quick-start.md) — nothing to a working install in four lines and a
   restart, and exactly what starts running on its own afterwards.
4. [Working with your memory](04-working-with-memory.md) — the everyday verbs: how to
   save a fact, how reading happens (mostly without you), and how to keep the store
   honest over time.
5. [The self-learning loop in practice](05-self-learning-in-practice.md) — what the
   background loop does, when it runs, the in-code guarantees that make it safe to leave
   on, and how to read the summary it writes you.

## Part III — Configure

Make it behave the way you want.

6. [Configuration reference](06-configuration-reference.md) — every setting, where it
   lives, what it defaults to, and a recipe for each common "I want it to do X" wish.
7. [Privacy, cost & control](07-privacy-cost-control.md) — what reads your data, where
   it goes, what it costs (your Claude login, no API key), and the off switch for every
   step that does anything.

## Part IV — Extend

Teach it your own subject matter.

8. [Build your own domain](08-build-your-own-domain.md) — stand up a brand-new
   knowledge domain in the two small places the engine leaves to you (a content-free
   engine plus your topic).
9. [Curating a domain](09-curating-a-domain.md) — keep a domain healthy: the
   deterministic maintenance pipeline that lints, dedups, cross-links, and re-indexes,
   and the audited verbs for the writes that *are* yours.

## Part V — Develop

Work on the engine itself.

10. [Architecture](10-architecture.md) — the modules, the data flow, and the discipline
    invariants (never lose a write, never leak a secret, never bill an API).
11. [Reference — API & schema](11-reference-api-schema.md) — the contract: every public
    function with its real signature, every table with its real columns, the verbs and
    the MCP surface.
12. [Contributing](12-contributing.md) — the invariants every change must respect, the
    TDD workflow, the tests, and the doc-discipline rule.

## Appendix

99. [Design notes & rationale](99-design-and-internals.md) — *why* the system is shaped
    the way it is: the trade-offs behind every choice that touches your private notes,
    its self-edits, and its unattended LLM calls.

---

*New here?* Read [1](01-what-is-ultra-memory.md) and [2](02-mental-model.md), then jump
to [3 — Quick start](03-quick-start.md). *Just want it configured?* Go straight to
[6](06-configuration-reference.md) and [7](07-privacy-cost-control.md). *Building on
it?* Start at [10](10-architecture.md).
