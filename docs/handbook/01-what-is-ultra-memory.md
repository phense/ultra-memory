# 1. What is ultra-memory?

> Close a Claude Code session, open a new one tomorrow, and Claude has forgotten
> everything. Not just the conversation — the way you like to work, the decision you
> made together last week, the bug you co-debugged and the fix you agreed on. Every
> session starts from zero. You re-explain. You re-paste. You re-decide. The agent
> never gets to *know* your project, because it has no place to keep what it learns.

**ultra-memory is a Claude Code plugin that gives your agent a memory that lasts —
on your own machine, on your own Claude subscription, with no cloud service and no
API key.** It remembers how you work, what your project decided, and what you've
learned together; it searches all of that the moment Claude needs context; and it
keeps that knowledge tidy on its own, in small, reversible steps you can review.

That's the one-sentence answer. The rest of this chapter unpacks what's behind it,
who it's for, and the principles it's built on. The next chapter,
[The mental model](02-mental-model.md), turns those principles into a picture you can
hold in your head.

---

## The problem: Claude forgets between sessions

A coding agent is brilliant *inside* a session and amnesiac *between* them. The
context window is working memory — vast, but volatile. When the session ends, it's
gone.

The usual workaround is to write things down: a `CLAUDE.md`, a scratch file, a
session log you paste back in. That helps, but it has a low ceiling. A flat file
doesn't *rank* what's relevant to the question at hand; it doesn't separate "this is
how Peter wants commits done" (which changes often) from "here's what we learned
about how this market behaves" (which should outlive a dozen experiments); and it
certainly doesn't notice when two notes contradict each other, or merge three
versions of the same lesson into one. A file is a place to *put* memory. It is not a
*memory*.

Most "memory for Claude" tools improve on the flat file by giving you **one bucket**:
they save a session, compress it, and replay it next time. That's genuinely useful —
but it treats all remembered things as the same kind of thing, aging at the same
speed, retrieved the same way. They don't.

---

## The one-sentence answer, expanded

ultra-memory's bet is that an agent's memory should work less like a **filing
cabinet** and more like an **organism**.

A filing cabinet is passive. You put documents in folders; they sit there exactly as
you left them; nothing improves, nothing connects, nothing notices a duplicate. The
cabinet is only ever as good as the last time *you* tidied it.

An organism is different. It takes in new experience, keeps the parts worth keeping,
lets the rest fade, strengthens the connections it uses, heals small inconsistencies,
and — given enough repetition of the same lesson — forms a new habit. It maintains
itself. You don't reorganise an organism; it reorganises itself, and you check in on
how it's doing.

ultra-memory is built to behave like the second thing. It doesn't just store what you
tell it — it curates what it stores: merging near-duplicate pages, correcting notes it
later finds were wrong, even turning a lesson it keeps re-learning into a brand-new
reusable skill. Always conservatively, always reversibly, and always reporting back so
you stay in the loop. (The how, and the guardrails, are in
[Chapter 2](02-mental-model.md) and the later parts of this handbook.)

---

## Who it's for

ultra-memory is for you if:

- **You work with Claude Code across many sessions** and you're tired of
  re-establishing context every time. You want the agent to *remember* the
  preferences, decisions, and corrections you've already given it.
- **Your project accumulates real knowledge** — not just "what we did," but "what we
  learned": studies, post-mortems, patterns, hard-won rules. You want a place for that
  knowledge that's durable, searchable, and yours.
- **You care where your data lives.** You'd rather your memory, notes, and any secrets
  stay in *your* project on *your* machine than be shipped to someone's cloud — and
  you'd rather not manage a separate, metered API account just to give your agent a
  memory.
- **You use one Claude across several projects** and want a fact learned in one to
  travel to the next, instead of being trapped wherever it was first learned.

It's a single plugin, installed once, that works across all your projects — and it
stays out of the way. It adds a few milliseconds at the start of a session and is
otherwise invisible until you ask it something.

---

## The three pillars

Everything ultra-memory does rests on three ideas that work as one system. Each gets a
fuller treatment later; here's the shape of each.

### Pillar 1 — Two stores, because not everything ages at the same speed

ultra-memory keeps **two kinds of memory at once**:

- **Session memory** — *how you work*: your preferences, the project's current state,
  the corrections you've made. Fast-moving, and stored in a local SQLite database.
- **A knowledge wiki** — *what you've learned*: concepts, findings, post-mortems — the
  durable stuff worth keeping. Stored as plain Markdown you can read, edit, and track
  in git.

These are kept deliberately **apart**, never merged into one bucket, because they have
different *half-lives*. A preference might change next week; a hard-won study about how
something behaves should outlive a dozen experiments. Merging them would force a single
expiry rule and a single way of writing onto two things that genuinely differ — and
would throw away the wiki's human-readable, git-tracked form. So they stay two stores.

### Pillar 2 — One ranked search across both

Two stores would be a hassle if you had to remember which one held what. You don't.
When Claude needs context, ultra-memory searches **both at once** and returns a single
ranked list — relevant session memories and relevant wiki pages, interleaved by how
well they match. The ranking is *deterministic* (the same question gives the same order
every time) and uses **no AI call on the read path**, so recall stays fast and
reproducible. A small graph of typed **links** ties the two stores together, so a
lesson from a session can stay connected to the wiki page it eventually grew into.

### Pillar 3 — A self-learning loop

This is the pillar that makes it an organism rather than a filing cabinet. A background
loop runs in four steps:

1. **Capture** what the agent learns while it works.
2. **Consolidate** the lessons that prove their worth — promoting durable ones,
   merging duplicates.
3. **Self-correct** its *own* earlier notes — sharpening, retiring, or setting aside
   what it later finds was wrong (never your locked-down rules).
4. **Synthesize** — turn a cluster of related, proven lessons into a brand-new reusable
   skill, after checking it won't step on one that already exists.

The loop is **on by default** and advances as you use Claude Code. Crucially, it is
*safe by construction*, not by good intentions: the rules are enforced in code, not
merely requested in a prompt. It can **never** delete anything (only archive), can
**never** change a fact you authored or pinned, is capped at a few changes per run,
checkpoints to git before it acts, and writes you a short summary afterward. You read
that summary; you don't babysit the work. Chapter 2 sketches the loop end to end, and a
later part covers every guardrail.

---

## The ethos: local-first, OAuth-only, content-free

Three principles run underneath all three pillars. They're not features bolted on —
they're constraints the whole design honours.

- **Local-first.** Everything lives in local files: a SQLite database for memory, plain
  Markdown for the wiki, both on your machine. There's no cloud service to sign up for
  and no server holding your data. git is your undo button — ultra-memory commits a
  clean, secret-stripped snapshot of your store, so you can always roll back to a
  known-good state.

- **OAuth-only, never the API.** Every AI call the system makes runs through your local
  `claude` command on **your own Claude subscription** — never the Anthropic SDK, never
  a paid API key. This isn't a preference; it's a hard boundary enforced in code: a
  single chokepoint **refuses to run** if a paid API key is present in the environment.
  There is deliberately no metered path. The benefit to you is concrete: your usage
  stays on your subscription, there's no second bill to manage, and there's no API key
  on disk to leak.

- **Content-free.** The plugin's own repository ships **code only** — no content. Your
  memory database, your notes, your file paths, and any secrets live in *your* project
  and are passed in by configuration; nothing personal is ever committed to the plugin
  itself (a test enforces it). And secrets are stripped twice — once on the way *in*,
  when something is saved, and again over the *entire* snapshot before it touches git —
  so nothing leaks even from a corner a single writer never touched.

The throughline: **the system is autonomous in *whether* it acts, but conservative in
*how*.** Full automation lives behind code-level guarantees, so a mistake is rare *and*
cheap to undo.

---

## Where to next

You now have the shape of ultra-memory: the problem it solves, who it's for, the three
pillars, and the principles underneath. The next chapter,
[The mental model](02-mental-model.md), turns this into a single picture — two stores
with different half-lives, one search across both, the links that tie them, and how a
session lesson *graduates* into durable knowledge. Hold that picture in your head and
the rest of the handbook will read like the natural consequences of it.
