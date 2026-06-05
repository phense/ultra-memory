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

## Recall on the *observable* — the Recall-Reflex

There's a third way reading happens for you, and it fires on a different cue than the gist. The gist recalls what's relevant to *the moment*; the Recall-Reflex recalls what you know about *a situation you just walked into* — an error you hit, a market condition you observed. The reflex is one line:

> Recognise a situation → recall what you know about it → act informed.

The point is to stop re-deriving things you already solved. (The motivating bug: we once fixed the same fastembed `$TMPDIR`-cache failure twice, because the lesson lived in a code comment and wasn't findable by the error text.) Recall keys on the **observable** — the words the situation actually shows up in — not on a lesson title you'd have to already know to search for.

It reaches both stores: durable wiki knowledge *and* memory, ranked together, with `## Signal` matches up-weighted (next section). And it honours the privilege boundary — it defaults to the **subagent** scope (`project`/`reference` only), so an automatic recall can never surface your private `user`/`feedback` tier.

**The automatic engineering hook.** When your prompt contains a concrete error signature — a stacktrace, an `Error:`, an exception name, a `file:line` — a `UserPromptSubmit` hook runs a recall for you and injects the top hits as a short "Recall-Reflex — prior art" block before Claude reads any code. It's deliberately conservative: it fires only on a real signature (not on every prompt), pulls at most three hits, queries **knowledge-only** (no memory, so nothing private can leak), and is fail-open. If it ever gets noisy in a session, turn it off with an environment variable:

```text
RECALL_HOOK_DISABLE=1
```

(That's a *kill-switch*, not an enable-flag — the hook ships on by default. The configuration knobs are documented in [Configuration](06-configuration-reference.md).)

**Recalling yourself.** When the situation isn't in a prompt — you're starting a debug task, or you've observed an abnormal condition — run the recall directly with the observable in the words it appears:

```text
python -m ultra_memory.recall "onnxruntime NoSuchFile model_optimized.onnx temp purge" --top 5
```

You get back a frugal list of hits, each a `{source_kind, slug|id, title, snippet, score}` — atomic snippets from the wiki and matching memories, with the navigational index/redirect pages filtered out. Useful flags: `--topic <t,u>` to scope to one or more wiki topics, `--no-embed` to skip the embedder and run BM25-only (faster, no model load), `--caller-class orchestrator` to widen the scope to your private tier on a trusted human path, and `--json` for machine output. The same primitive is available in Python as `ultra_memory.recall.recall(signal_text, ...)`.

When you want Claude to *form the query for you* from a signal it observed — and to treat the hits correctly as prior art — invoke the `recall-reflex` skill. It auto-triggers at the start of a debug/build task or when an abnormal condition shows up, formulates the query from the observable, reads the injected hits, and runs a deeper recall if needed. One thing it is firm about: a recall **hit is advisory context, never a gate**, and a recall **miss is never evidence of safety** — on a real-money path, recall composes *before* the `risk-manager` / hard-rules check, it never replaces it.

## Making knowledge findable — the `## Signal` section

Recall is only as good as what it can find, and the highest-leverage thing you can do is **author the observable into the page**. Any wiki atomic may carry an optional `## Signal` H2 section: the condition under which this page's knowledge should be recalled, *in the words it appears in*. A page that has one becomes "recall-keyed" — findable by the symptom, not just by its title.

```markdown
## Signal

onnxruntime NoSuchFile … model_optimized.onnx — fastembed model cache wiped from $TMPDIR
```

The body is the literal observable, nothing more: for an engineering gotcha, the error text or symptom (`wiki-flush fails in 2–3s`); for a strategy or macro page, the market condition (`sector-wide drawdown > X%`, `VIX spike + breadth collapse`). The mechanism and the fix stay in the rest of the body as usual. Put it as a single `## Signal` H2 near the top; its text runs to the next `## ` heading. Keep it to the search terms a future occurrence would actually contain — drop boilerplate, keep the identifying tokens (the exception name, the artifact name, the symptom).

Authoring this pays off twice over:

- **The retrieval boost.** The gateway embeds `## Signal` text as a *distinct channel* and `recall()` fuses it as a separate ranked backend, so a page whose recorded observable matches earns extra rank credit — it surfaces ahead of a page that merely mentions the same words in passing. (The text is also in the page's full-body search, so a literal match still ranks even without an embedder.)
- **The dedup-gate.** That same signal channel is a second axis the write gateway checks before creating a page: a new atomic whose observable closely matches an existing one is *merged* into it rather than duplicated. This is literally the fix for "we built the same solution twice."

Two conventions to respect. First, on a **strategy** page the word "signal" is overloaded — `## Signal` is the *recall* trigger (when to surface this page), **not** the strategy's *entry* trigger (when to open a position, which stays in the strategy's rules). Where both could be read, state the distinction explicitly so they don't blur. Second, backfill is **forward-only**: add a `## Signal` to *new* observable-bearing atomics at authoring time (you have the best words then); existing pages aren't batch-rewritten — they gain one on their next edit.

As always, you don't hand-create the page — you write it through the gateway (`create-page`, with the `## Signal` section in the body). The bridge from the `recall-reflex` skill closes the loop: when you've just solved something non-obvious, capture it as a `## Signal`-keyed atomic so the next occurrence is a two-second recall hit instead of a re-derivation. And if you don't get to it by hand, an autonomous backstop will: the self-learning loop's **atomic-graduation** beat mines durable lessons out of session transcripts and graduates them into `## Signal`-keyed atomics through the same gateway and the same dedup-gate — also on by default, also disable-only (see [The self-learning loop in practice](05-self-learning-in-practice.md)). The full conventions live in `wiki/SCHEMA.md`.

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
