# 2. The mental model

> If you remember one thing about ultra-memory, remember this: **there are two stores
> with different half-lives, and you query them as one.** Almost everything else — why
> the wiki is plain Markdown, why session memory is a database, why a lesson can
> "graduate," why the loop is conservative — falls out of that single idea. This
> chapter draws the picture. No commands yet; just the shape of the thing, so the parts
> that follow click into place.

The previous chapter, [What is ultra-memory?](01-what-is-ultra-memory.md), named the
three pillars. This chapter makes them one coherent mental model.

---

## Two stores, different half-lives

Not everything you want an agent to remember ages at the same speed. ultra-memory takes
that seriously and keeps **two stores**, each shaped for the kind of thing it holds.

**Session memory — the volatile store.** This is *how you work*: your preferences, the
project's current state, the corrections you've made, references you've pointed at. It
moves fast — a preference might change next week, the project's "current state" changes
constantly. It lives in a local **SQLite database**, because that's what you want for
fast-moving, queryable, typed facts that get pinned, verified, superseded, and ranked.

**The knowledge wiki — the durable store.** This is *what you've learned*: concepts,
studies, findings, post-mortems — the knowledge meant to outlive any single session or
experiment. It moves slowly, by design. It lives as **plain Markdown pages, tracked in
git**, organised by *topic* (one top-level directory per topic — for example
`trading`, `programming`, `user`). Markdown, because durable knowledge deserves a form
you can read with your own eyes, edit by hand, diff in a pull request, and keep forever.

Here's the contrast at a glance:

| | **Session memory** | **Knowledge wiki** |
|---|---|---|
| **Holds** | How you work · current state · corrections · references | Concepts · studies · post-mortems · matured lessons |
| **Half-life** | Short — changes session to session | Long — meant to outlive any single approach |
| **Form** | Rows in a local SQLite database | Plain Markdown pages, git-tracked |
| **Best for** | Fast, typed, queryable, pinnable facts | Human-readable, hand-browsable, durable knowledge |
| **Rule of thumb** | *How the agent should behave* → here | *What you learned about the domain* → here |

They are **never merged into one store.** That's a deliberate choice, not a missing
feature. Merging them would force a single expiry model and a single way of writing onto
two things that genuinely differ — and would lose the wiki's readable, git-tracked form.
The whole design leans on keeping them apart.

---

## One ranked search across both

Two stores would be a burden if *you* had to remember which one held what. You don't —
because at retrieval time the two behave as **one fabric**. A single search, called
**`unified_recall`**, runs your question against *both* stores and returns **one ranked
list** that interleaves matching session memories and matching wiki pages, ordered by
how well each matches.

Two properties make this trustworthy:

- **It's deterministic.** The same question returns the same order every time. (Under
  the hood it fuses several ranking signals — embedding similarity, keyword/BM25
  overlap, and a graph signal — with a fixed tie-break, so the result never reshuffles
  run to run.)
- **It uses no AI on the read path.** No model call sits between your question and your
  answer, so recall stays fast and reproducible. The "intelligence" is in *how things
  were organised on the way in*, not in a slow inference on the way out.

So you ask one question and get one answer, drawn from both halves of the fabric,
without ever choosing a store.

---

## The typed-link graph that ties them together

Two separate stores, searched as one — but how do they *connect*? Through a small layer
of **typed links**.

A link records a relationship between two stored things — a memory and a memory, or a
memory and a wiki page. Each link has a *type* that says what kind of relationship it
is. The most important type for the mental model is the **graduation** link
(`validated_as`): it points from a session lesson to the durable wiki page that lesson
eventually became.

These links form a small graph that **spans both stores** without copying one into the
other. The wiki pages stay as files; the memories stay as database rows; the link is
just an edge between them. That edge does real work: it's a signal the unified search
can follow (a page that several proven lessons point to ranks more confidently), and
it's what lets a lesson stay *connected* to the knowledge it grew into — so you can
trace a durable wiki page back to the session where it was first learned.

You can also **pin** either a memory *or* a wiki page to keep it hot in recall — across
both stores, one pin space — which is how a hard rule you never want forgotten stays in
view at the start of every session.

---

## How a session lesson "graduates"

The graduation link points at a *process*, and that process is the heart of the mental
model. Knowledge doesn't start out durable. It starts out as a fresh, unproven
observation in the fast store, and **earns its way** into the slow one.

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                        ONE KNOWLEDGE FABRIC                                │
  │                                                                            │
  │   SESSION MEMORY (SQLite)                  KNOWLEDGE WIKI (Markdown, git)  │
  │   volatile · fast-moving                   durable · slow-moving           │
  │   how you work · state · fixes             concepts · studies · lessons    │
  │        │                                              ▲                     │
  │        │   a lesson proves its worth over time        │                     │
  │        │   ───────── GRADUATES ──────────────────────►│                     │
  │        │   (a typed `validated_as` link is recorded)  │                     │
  │        │                                              │                     │
  │        └──────────────┬──────────────  links  ────────┴──────────┐         │
  │                       │                                          │         │
  └───────────────────────┼──────────────────────────────────────────┼─────────┘
                          │                                          │
                  ┌───────▼──────────────────────────────────────────▼───────┐
                  │            unified_recall  —  ONE ranked search           │
                  │   blends both stores · deterministic · no AI on read      │
                  │   scoped by a privilege boundary (who's asking)           │
                  └──────────────────────────────────────────────────────────┘
```

The path a lesson walks:

1. **It's captured** in session memory while the agent works — a fresh, unproven note.
2. **It's used, or it isn't.** Facts that keep proving relevant gain strength; ones that
   don't fade.
3. **A proven, durable lesson graduates** into the wiki — promoted into a real Markdown
   page in the right topic — and a `validated_as` link is recorded from the original
   memory to its new home, so the two stay connected.

This is the organism behaving like an organism: experience comes in fast and cheap, and
only what proves its worth is promoted into long-term, durable knowledge — while the
trail back to where it was learned is never lost.

---

## The loop, in one view

Graduation is one beat of a larger rhythm. The **self-learning loop** runs in four
beats, slowest-blast-radius last, each more cautious than the one before:

1. **Capture** — *hot, no AI.* While the agent works, candidate lessons are noticed and
   queued. Cheap, and never blocking your session.
2. **Consolidate** — *batched.* The proven lessons are promoted (graduation), and
   near-duplicates are merged, so the store gets *better organised*, not just bigger.
3. **Self-correct** — *conservative.* The loop revisits its *own* earlier notes and can
   sharpen, retire, or set aside ones it later finds were wrong — never the facts *you*
   authored or pinned.
4. **Synthesize** — *the most cautious beat.* When a cluster of related, proven lessons
   keeps recurring, the loop can draft a **brand-new reusable skill** from them — after
   a check that the new skill won't step on one that already exists.

The loop is **on by default** and advances as you use Claude Code. What makes that safe
is that the guardrails are enforced **in code**, not asked for in a prompt:

- it can **never delete** — only *archive* (a superseded note becomes a redirect; a
  contradiction is set aside, not erased), so every change is reversible;
- it can **never touch** a fact you authored or pinned — those are immutable to it;
- it's **bounded per run** (at most a few edits, a few reversions, one new skill) and
  *halts* rather than exceed the cap;
- it **checkpoints to git** before it acts on anything aggressive, and refuses to run on
  a messy tree;
- it writes you a **short summary** of what it did.

So you sit in the **audit loop** — you read the summary — not the **write loop** — you
don't approve each action. Because every step is small and reversible, mistakes are rare
*and* cheap to undo. (Each beat, and each guardrail, gets a full chapter later in the
handbook.)

---

## The privilege boundary (a one-paragraph preview)

One last piece belongs in the mental model, because it shapes *what any given search
returns*. Not every caller is equally trusted. The unified search is scoped by a
**privilege boundary**: a subagent or a background job sees only the facts it's allowed
to — never your private preferences, never another topic's knowledge — while a trusted
top-level session sees everything. The boundary lives *inside* the search itself and
**fails closed**: an unknown or unbound caller sees the *least*, not the most. You'll
meet this in detail later; for now, just hold that *who is asking* is part of *what
comes back*.

---

## Holding it all at once

Put the picture together and it's small enough to keep in your head:

> **Two stores** — a fast, volatile session memory and a slow, durable knowledge wiki —
> **searched as one** by a deterministic, AI-free unified recall, **tied together** by a
> small graph of typed links, with proven lessons **graduating** from the fast store
> into the slow one, and a four-beat **self-learning loop** keeping the whole thing tidy
> in small, reversible, audited steps — all on your machine, on your Claude login.

That's the model. The rest of the handbook is the natural consequence of it: how to
read from and write to each store, how the loop's beats work in detail, what every
guardrail does, and how to configure it for your project. Next up is Part II, where the
abstractions above become commands you can actually run.
