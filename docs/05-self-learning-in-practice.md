# 5. The self-learning loop in practice

Most memory tools are filing cabinets: they store what you tell them and hand it back unchanged. ultra-memory is closer to an organism. Left running, it notices which of its memories actually helped, promotes the lessons that keep proving their worth, quietly fixes the notes it got wrong, and — when it sees the same lesson land again and again — turns that cluster into a brand-new reusable skill. It does all of this on its own, on your machine, on your Claude login.

That sounds alarming until you see how it's fenced. The whole loop is **safe by construction, not by good intentions**: the rules are enforced in code, not merely asked for in a prompt. This chapter explains what runs, when, the guarantees that make it safe to leave on, how to read the summary it writes you, and how to trust it — or switch any of it off.

It is **on by default** in v0.0.4. Nothing below requires you to turn anything *on*.

## The beats, and when each runs

The loop advances automatically whenever you open Claude Code — an async session-start hook checks which steps are *due* and runs only those. Each step (a "beat") is throttled on its own clock, so opening ten sessions in a day doesn't re-run a weekly job. There's also a daily, AI-free cleanup that prunes old session events and refreshes your exports.

There are **four AI beats** — capture, consolidate, self-correct, synthesize — each throttled on its own clock and run in a fixed order: capture first (it's the input the others feed on), the heavier reasoning beats in the middle, the projection rebuild last. **Outcome attribution** is the one no-AI step woven through them — it isn't scheduled on its own clock; it credits recalled facts as part of the loop and is toggled with the same `_enable` switch as the rest:

| Step | What it does | Uses AI? | Default cadence |
|---|---|---|---|
| **Session capture** | Mines each finished session's transcript into durable memory candidates. | yes (your login) | ~daily |
| **Outcome attribution** | Credits which recalled facts actually helped, so good memories rise and dead ones fade. *(No-AI; runs as part of the loop, not on its own clock — toggled via `SP8_ATTRIBUTION_ENABLE`.)* | no | with the loop |
| **Consolidate** | Promotes lessons that have proven their worth into the store / wiki; merges near-duplicates conservatively. | yes (your login) | ~weekly |
| **Self-correct** | Sharpens, retires, or sets aside the loop's *own* earlier agent-authored notes — never yours. | yes (your login) | ~monthly |
| **Synthesize** | Turns a cluster of repeated, positively-scored lessons into a new reusable skill. | yes (your login) | ~monthly |

The cadences are defaults you can change (see [Configuration](06-configuration-reference.md)). The two boldest beats — self-correct and synthesize — are deliberately the rarest.

If you run a headless or always-on box where sessions don't open often, `/ultra-memory:memory-setup` offers an OS-scheduler snippet you can install yourself for a deterministic cadence. It prints it; it never installs it for you.

## The safety guarantees, in plain language

Five properties, all enforced in the apply path (the code that makes the change), make the loop safe to leave unattended:

1. **Archive-never-delete.** No beat ever runs `rm`. When a page or memory is superseded, it's *archived and redirected*, not erased. Every change is a reversible step. Your store only ever grows a recoverable history.
2. **It can never touch what's yours.** Before any change, the loop re-reads the live record and refuses outright if the target is something *you* authored or *pinned*. A single attempt to touch a forbidden target halts the whole run — zero tolerance. So your hard rules, your pinned facts, your hand-written memories are physically immutable to the loop. It only ever edits its *own* earlier output.
3. **git is the undo button, and the loop self-gates on it.** The two boldest beats (self-correct, synthesize) take a git checkpoint *before* they act and refuse to run on a dirty or untracked tree. No checkpoint, no action — they simply skip and try next time. That means every autonomous change has a tagged commit you can revert to, and the loop will not act in a state where it couldn't be cleanly undone.
4. **Bounded per run.** Each run is capped to a small number of changes: at most a few edits and a few reversions, at most a handful of quarantines, and **at most one new skill** per run. A run that would exceed a cap halts rather than blasting through it. Mistakes, if any, are small *and* cheap to undo.
5. **A new skill must pass a check before it exists.** Synthesize won't create a skill that would hijack one you already have — there's a trigger-probe eval-gate that proves the generated skill doesn't steal an existing skill's job. If it can't prove that, the skill isn't created.

On top of all that, the loop is **fail-open everywhere**: any error in any beat becomes one log line and a no-op — it never wedges your session, and it never half-applies a change.

And the privacy floor from [chapter 4](04-working-with-memory.md) still holds throughout: every AI call runs on **your Claude login (no API key, no metered bill)**, the loop reads only your **local** session transcripts, and the single audited write path strips secrets on the way in *and* on the way out.

## How to read a digest

You stay in the *review* loop, not the *work* loop — so when the bold beats act, they write you a short, human-readable summary instead of making you watch. If your project has a reports directory configured, you'll find them as dated Markdown:

- **Self-correct** writes `…/<YEAR>/sp7-self-improvement-<DATE>.md`
- **Synthesize** writes `…/<YEAR>/sp10-synthesize-<DATE>.md`

A digest tells you, in plain terms: what the beat changed, what it deliberately *didn't* change and why, and — most usefully — the **rollback handle**, the git checkpoint tag it took before acting. Reading one takes a minute. The intended rhythm is: skim the digest, and if something looks wrong, `git revert` to the tag named in it. (On a pure-memory install with no reports directory configured, the beats still run and stay fully bounded; they just don't write a digest file.)

There's also a machine-readable audit trail in JSON-Lines alongside the digests, if you want to track the loop's behavior over time rather than read prose.

## How to trust it — or turn it off

The honest way to trust an autonomous system is to verify it's reversible, then watch it for a while. ultra-memory makes both easy:

- **Verify reversibility once.** Pick a recent digest, find its checkpoint tag, and confirm `git revert <tag>` cleanly undoes the run. Now you know the floor.
- **Watch the cadence.** The defaults are tight on purpose (a few edits, one new skill per run). Read a digest or two; if you're comfortable, you can loosen the caps in [Configuration](06-configuration-reference.md) and watch the effect in the next summary.

Prefer to start narrow, or pause a step entirely? Every beat has an individual off switch in the `/plugin` config, no code required:

| Toggle (in `/plugin` config) | Turns off |
|---|---|
| **Session capture** | Mining finished sessions into memory. |
| **Outcome attribution** | Crediting which facts helped. |
| **Self-correction** | The rewrite / retire / set-aside beat. |
| **Skill synthesis** | Creating new skills from clustered lessons. |

Setting a toggle to `off` disables exactly that beat and nothing else. There's no all-or-nothing switch you're forced into: you can keep the gentle capture-and-credit beats on while pausing the bold ones, or run memory-only with no wiki at all (the wiki-touching steps then simply do nothing). And because the self-correcting beats self-gate on a git checkpoint, even with everything on, every autonomous change remains something you can undo.

---

**Back to:** [Working with your memory](04-working-with-memory.md) · **Continue to:** [Configuration](06-configuration-reference.md) for the cadences, caps, and per-beat toggles.
