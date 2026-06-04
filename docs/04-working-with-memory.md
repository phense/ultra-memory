# 4. Working with your memory

A memory is only useful if it surfaces at the right moment — not when you go looking for it, but when Claude needs it and you've forgotten it ever existed. ultra-memory is built around that idea. You spend a few seconds saving a fact; from then on the system decides when it's relevant and puts it in front of Claude for you. This chapter is the working manual for that contract: how to write a memory, how reading happens (mostly without you), and how to keep the store honest over time.

Everything here goes through the same audited write path — one gateway that strips secrets, removes duplicates, files the entry, and logs the change. You never hand-edit the database; you use the verbs.

## The shape of a memory

Every memory has four parts you choose, plus a body:

- a stable **`id`** (lowercase, e.g. `feedback_email_routing`) — your handle for pinning, verifying, or editing it later;
- a **`type`**, one of `user` | `feedback` | `project` | `reference`;
- a short **`title`**;
- the **body** — the actual content.

The `type` is the most consequential field, because it decides who is allowed to read the memory later (the privilege boundary, below). A rough guide:

| Type | Use it for | Example |
|---|---|---|
| `user` | How *you* personally like to work. | "I prefer replies in German." |
| `feedback` | A correction or directive you gave that should stick. | "Only send the daily newsletter through Buttondown; everything else by email." |
| `project` | The current state or decisions of the project. | "The order-execution engine will be written in Rust, not Python." |
| `reference` | A durable lookup fact. | "The IBKR paper account returns delayed quotes (10–20 min)." |

`user` and `feedback` are the *private* tier — a subagent never sees them. `project` and `reference` are shareable. Keep that in mind when you pick a type.

## Saving a fact — `memory-save`

This is the canonical way to create a new durable memory. You don't write a Markdown file and import it; you call the verb and the gateway does the rest (redaction, dedup, the audit line).

```text
/ultra-memory:memory-save Peter prefers replies in German; code and config stay in English
```

Claude chooses a stable id, a type, and a title, writes the body to a temp file (to avoid shell-escaping prose), and saves it through the gateway. Secrets are stripped automatically on the way in — but glance at the body before saving anyway. To make a freshly saved fact *always* in context, pin it next (below).

## Recalling on demand — `memory-recall`

You'll mostly let recall happen for you (the session-start gist does it automatically). When you want to search the store yourself, ask for it:

```text
/ultra-memory:memory-recall how do we route outgoing email?
```

This returns a ranked list of hits — each with a `title`, a `snippet`, a relevance `score`, and the `id` to cite. A hit can be flagged `"stale": true`, meaning it hasn't been reconfirmed in a while and *might* be outdated; that's a prompt to check it and then `memory-verify` it (below). `memory-recall` is the **trusted** read path for you and a top-level Claude session — it returns everything, including the private `user`/`feedback` tier. Subagents read through a separate, narrower door.

## How automatic reading works

Two mechanisms read your memory without you asking:

- **The session-start rehydration gist.** When a session opens (or resumes, or after a compaction), ultra-memory builds a short summary — *every pinned memory*, plus the memories most relevant to the moment — and injects it directly into Claude's context. It's deliberately small (a character budget, default 2000, adjustable in [Configuration](06-configuration-reference.md)) so it costs almost nothing and never crowds out your actual work. This is *why* pinning matters: a pinned memory is guaranteed a seat in that gist on every single session.
- **The end-of-session checkpoint.** When a session stops, a checkpoint of what happened is saved. This is also the raw material the self-learning loop later mines into durable memory (see [The self-learning loop in practice](05-self-learning-in-practice.md)).

Both hooks are **fail-open**: if anything goes wrong they log one line and step aside. They can never wedge or block your session.

## Pinning your hard rules — `memory-pin`

Pinning is the one knob *you* control over what's always in context. A pinned memory is injected into the rehydration gist of every session, so this is where your non-negotiable rules belong — a tax constraint, a "never do X" directive, an architecture decision you don't want re-litigated.

```text
/ultra-memory:memory-pin feedback_email_routing
```

To unpin, add the word `unpin`:

```text
/ultra-memory:memory-pin feedback_email_routing unpin
```

Pin deliberately. The gist has a budget, so pin the handful of rules that must *always* be in view, and let recall surface everything else on demand. (Pinned facts also carry an extra guarantee from the self-learning loop: it can never rewrite, revert, or retire something you've pinned — see [chapter 5](05-self-learning-in-practice.md).)

## Keeping the store honest

Three verbs maintain accuracy over time.

**`memory-verify`** — when a recalled fact shows `"stale": true` and you've checked it still holds, mark it reconfirmed. This stamps "last verified = today" and resets the age-based staleness penalty in the ranking, so it stops being flagged.

```text
/ultra-memory:memory-verify reference_ibkr_paper_quotes
```

**`memory-edit`** — when a stored fact is *wrong* rather than merely old, correct its body. The type, title, and every other field are preserved; only the body changes, and the rewrite is redacted and audited like any other gateway write.

```text
/ultra-memory:memory-edit project_order_execution_engine  the engine is Rust for both paper and live, one code path
```

**`memory-inbox`** — a quiet way to leave instructions *between* sessions. There's a watched inbox file (next to your database) where you can jot directives like `pin <id>`, `unpin <id>`, or `verify <id>`. Running the verb applies the recognized commands and reports an `applied` / `notes` / `errors` summary. Free text that isn't a recognized command is never auto-applied — it's preserved under an "Unprocessed" section for you to handle by hand.

```text
/ultra-memory:memory-inbox
```

**`memory-maintain`** — runs lightweight cleanup right now: it prunes old session events (rolling them into a per-session summary first, so nothing is lost) and refreshes the exported, git-trackable views. It uses **no AI and no token at all**. You rarely need to call it — a throttled session-start hook already runs it about once a day — but it's there when you want a fresh export immediately.

```text
/ultra-memory:memory-maintain
```

## The privilege boundary, from where you sit

You and a top-level Claude session get **full recall** through `/ultra-memory:memory-recall` — every type, including your private `user`/`feedback` memories.

A **subagent** (one Claude spawns to do a scoped sub-task) reads through a different, read-only tool, and it is **fail-closed**: it only ever sees `project` and `reference` facts, *never* your `user`/`feedback` tier, and never another project's facts. So a subagent you dispatch to, say, summarize a file cannot accidentally surface a private preference or a secret in its output. You don't configure this per task — it's the default. (You *can* mark a top-level instance as a trusted `orchestrator` to widen its recall; that's a deliberate setting, covered in [Configuration](06-configuration-reference.md).)

The practical upshot: save personal or sensitive directives as `user`/`feedback` and they stay with you; save shareable project facts as `project`/`reference` and your helper agents can use them.

---

**Next:** [The self-learning loop in practice →](05-self-learning-in-practice.md) — what curates your store automatically, the safety guarantees in plain language, and how to read a digest or turn any of it off.

**See also:** [Configuration](06-configuration-reference.md) for the database path, the gist budget, and the caller-class setting.
