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

## The store recalls itself: recognise → recall → act

A store you have to *remember to consult* is a store you will forget to consult. The most
expensive failure ultra-memory can have isn't a missing fact — it's a fact that's *there*,
findable, and never looked at, so the same problem gets solved a second time from scratch.
(That has happened: the same bug, fixed twice, because the lesson sat in the store unread.)

So the mental model adds one more move: knowledge isn't only *searched* on demand, it's
**reflexively recalled by the situation itself**. The shape is a three-beat loop:

> **Recognise a situation → recall what you know about it → act informed.**

The trigger is the **observable** — the thing you can *see* before you've decided to look
anything up. For an engineer that's an error signature in the prompt: a stacktrace, an
exception name, a `No such file`, a `path:line`. For a real-money trading agent it's an
abnormal condition *in the data*: a sector-wide drawdown, a volatility spike, a regime that
doesn't fit. The point is that the recall fires at the *start* — when the situation is first
recognised — not after you've already thought "I should search the wiki." By then the
re-derivation has often already begun.

Two pieces make this work, and both are just refinements of the fabric you already have:

- **Lessons become findable by the words a future occurrence will use.** An atomic can
  carry an optional **`## Signal`** section: the observable condition *in its own words* —
  the literal error text, or the market condition — separate from the lesson's prose. A
  page titled by its *insight* ("persistent model cache") is invisible to a search for its
  *symptom* ("`NoSuchFile … model_optimized.onnx`"); the `## Signal` section closes that gap
  by indexing the symptom you'd actually type. The unified search treats it as its own
  ranking signal, so a match on the observable counts for extra.
- **The recall is reflexive, not requested.** The same one ranked search you already met
  becomes a primitive any consumer can fire on a signal. On the engineering side a
  lightweight hook watches each prompt and, only when it spots a concrete error signature,
  runs that recall *for* you and hands back a short block of prior art — so the relevant
  lesson is already in front of you with zero reliance on remembering to ask. On the trading
  side the same primitive is fired from the agent's own observation loop when it sees an
  abnormal condition.

Hold one boundary firmly: recalled hits are **advisory context, never a verdict.** A recall
*miss* is never evidence that something is safe, and a recall *hit* never relaxes a gate —
on a real-money path the reflex runs *before* the risk check, never in place of it. It tells
you what you already know; it doesn't decide.

This is the read-side mirror of graduation. Graduation makes a proven lesson *durable*; the
recall reflex makes it *reach back out* to the next occurrence of the situation that taught
it. The whole point of storing knowledge — getting more competent the more you've learned,
instead of re-deriving — only pays off if the store recalls itself.

---

## Capture so it can be found, not just stored

The reflex above has an obvious dependency: it can only recall what was captured *with* its
observable. A lesson written down as pure insight, with no record of the symptom that
provoked it, is durable but unreachable — exactly the trap that let the same bug get fixed
twice. So capture gains a matching discipline: **write the lesson down keyed to the words a
future occurrence will use.** When you solve something non-obvious you (or the autonomous
loop) record not just *what you learned* but *what you'd see when it recurs* — that's the
`## Signal` again, set at capture time, where the observable words are freshest and most
accurate.

There's also an **autonomous backstop**, so this doesn't depend on anyone remembering. The
same once-per-session pass that already harvests lessons into memory now also notices the
durable, reusable ones — an engineering gotcha with its literal error text, a strategy
lesson with its market condition — and, on the next maintenance cycle, **graduates each into
a `## Signal`-keyed wiki page on its own**, so it becomes recall-findable without a human in
the loop. It is the same graduation move you already know, aimed at *findability*: a captured
lesson earns a durable, searchable home keyed to its observable.

Because this beat creates wiki pages unattended, its safety is built **into the mechanism**,
not asked for in a prompt — the same posture as the rest of the loop:

- before it writes, it **checks the new observable against the ones already stored**: a clear
  duplicate is *merged* into the existing page instead of creating a second one (this is
  literally the fix for "we built the same solution twice"), an uncertain near-match is left
  alone rather than guessed, and only a genuinely new observable becomes a new page;
- after it writes, it **proves the page recalls itself** — it runs the very recall the page
  is meant to answer, and a page that can't be found by its own observable is *set aside*
  (never deleted), because an unfindable recall page is useless;
- it's **bounded per run** and **never deletes** — every page it makes is reversible and
  carries the loop's own provenance, so the self-correcting beat can later revise it;
- and an auto-captured *trading* lesson always ships with an **unvalidated, [Recent-Regime]
  confidence label**, so a real-money path never mistakes a freshly auto-graduated lesson for
  an established one.

So the recall reflex and findable capture are two arms of one idea: **recognise the
situation, recall what you know, act informed — and capture each new lesson by the very
words that will summon it next time.** The first half makes the store reach out; the second
makes sure there's always something keyed to reach with.

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
