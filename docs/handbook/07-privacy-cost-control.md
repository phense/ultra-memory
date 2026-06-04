# 7. Privacy, cost & control

A memory that watches your sessions and rewrites itself is exactly the kind of thing you should be suspicious of. So this chapter answers the suspicious questions directly: What reads my data? Where does it go? What does it cost? And how do I stop any part of it? The short version is that ultra-memory is built to be boring on all three fronts — it runs on the Claude login you already have, it keeps everything on your own machine, and every step that does anything has an off switch. The long version is below, with the exact mechanism for each claim, because "trust us" is not a privacy model.

If you have not yet seen what runs automatically, [Quick start](03-quick-start.md) names the steps; this chapter is the one that explains *why each one is safe to leave on* — and how to turn it off if you'd rather it didn't.

---

## No API key — and it refuses to start with one

Every LLM call in ultra-memory goes through exactly one chokepoint, and that chokepoint runs your local `claude` command on **your own Claude subscription via OAuth**. There is deliberately no metered API path:

- If `ANTHROPIC_API_KEY` is set in the process environment, the chokepoint **raises an error and refuses to run** rather than quietly billing an API. This is enforced in code, not documented as a convention.
- If no OAuth session token is available, it refuses too — it will not silently fall back to anything.
- An empty `ANTHROPIC_API_KEY` is dropped from the child environment entirely, so it cannot be reintroduced downstream.

The practical consequence: the self-learning loop costs you **nothing beyond the Claude subscription you already pay for**. There is no separate bill to watch, no key to leak, and no way for a misconfiguration to route a call through the metered API. The light steps cost even less than that — they use no LLM at all (see the next section).

This is not a setting you tune. There is no `ULTRA_MEMORY_API_KEY` and no toggle to "use the API instead." OAuth-only is a structural property of the engine.

---

## What actually uses an LLM (and what doesn't)

Most of what ultra-memory does is plain Python and SQLite — no model call at all. Only the heavy beats spend an LLM call, and each spends at most **one batched call per run**, on your subscription.

| Step | Uses an LLM? | What it reads |
|---|---|---|
| SessionStart rehydration gist | No | Your store (pinned rules + most-relevant memories). Pure ranking, no model. |
| Light maintenance (prune + export) | No | Your store. Bounds session-event history, refreshes the readable export. |
| `Learnings.md` projection-regen | No | Your store. Rebuilds the per-skill views. |
| Session capture | Yes (one call per session) | Your local session transcript (prose + tool *names* only). |
| Outcome attribution | No model of its own | The links between recalled facts and outcomes already in your store. |
| Consolidate | Yes (one batched call) | Un-resolved learning candidates + your store, to promote proven lessons. |
| Self-correction | Yes (one batched call per track) | The loop's own earlier agent-authored notes. |
| Skill synthesis | Yes (one batched call) | A cluster of matured lessons, to draft a new skill. |

So on a quiet day — you open a session, the gist injects, you work, the session ends — **no LLM call happens at all** beyond the model you were already talking to. The capture call happens once, throttled to about daily; consolidate is weekly; self-correction and synthesis are monthly.

---

## What reads your data, and what stays local

Nothing leaves your machine. This repository ships **code only — never content** (a test enforces that the entire published surface contains no personal paths or data). Your memory database, your wiki, your transcripts, and your config all live in your own project and home directory.

Concretely:

- **Your store** is a local SQLite file (`~/.ultra-memory/memory.db` by default, or wherever you pointed `ULTRA_MEMORY_DB`). The rehydration gist, recall, and maintenance all read it locally.
- **Session capture reads only your local session transcript** — the file Claude Code already wrote to disk on your machine. It is read once, mined, and the raw transcript is **never persisted**. Only the extracted, redacted knowledge is saved.
- **The transcript digest the capture step builds excludes raw tool-output bodies** — the large, secret-bearing surface. Only the user/assistant prose and the tool *names* are kept. A tool that returned a credential never has that body fed to the capture call.
- **The only outbound traffic is the LLM call itself**, to Claude, over your existing authenticated session — the same connection your interactive Claude Code session already uses. There is no telemetry, no analytics, and no upload of your store.

If you wire a failure **notifier** to be alerted when a run hits errors, that is *your* code and *your* transport — the plugin ships no mail or webhook integration. You decide if and where a notification goes.

---

## Secret stripping — the mandatory pre-persist chokepoint

Before anything is written to your store, it passes through a pure secret-stripper. This is a mandatory chokepoint on the write path, not an optional pass — every saved memory goes through it. It is conservative by design: it redacts obvious credentials and leaves normal prose intact.

What it catches and replaces with `[REDACTED]`:

- API keys with recognizable shapes — `sk-ant-…`, generic `sk-…`, GitHub tokens (`ghp_`/`gho_`/… and `github_pat_…`), AWS access keys (`AKIA…`), Google API keys (`AIza…`), Slack tokens (`xox…`), GitLab PATs (`glpat-…`), npm tokens, DigitalOcean tokens, Stripe (`sk_live_`/`rk_test_`…), SendGrid (`SG.…`), Twilio SIDs, and Slack webhook URLs.
- JWTs and `Bearer …` tokens.
- PEM private-key blocks (the whole block collapses to one `[REDACTED]`).
- URI userinfo — `scheme://user:password@host` keeps the host and path, scrubs the credential.
- `keyword=value` / `keyword: value` pairs where the keyword is credential-shaped (`api_key`, `secret`, `token`, `password`, `username`, …) **and** the value looks like a real credential (quoted, or carrying entropy/digits) — so hyphen-joined prose like `institutional-grade-discipline` is never mangled.
- Anything you wrap in `<private>…</private>` tags, redacted wholesale.

It is deliberately *not* a guarantee that every conceivable secret is caught — a bare, prose-shaped password with no delimiter and no entropy can slip a regex. Treat it as a strong safety net, not a license to paste credentials into a memory. The stronger guarantee is upstream: the capture digest never includes tool-output bodies in the first place.

---

## The privilege boundary — subagents can't read everything

Memory is split by type, and not every caller can read every type. The read-only `knowledge` MCP — the tool a spawned subagent uses to recall — is a **privilege boundary that fails closed**:

- A **trusted** caller (`orchestrator` / `owner`) gets full recall: `project`, `reference`, `user`, and `feedback` memories.
- **Everything else** — a subagent, a cron run, an unknown or unset caller class — is limited to `project` and `reference` facts only. It **never** sees your `user` or `feedback` memories (your preferences, directives, the personal layer).

This is fail-closed: anything that is not explicitly the trusted class is treated as the untrusted `subagent`. The default caller class *is* `subagent`, so a fresh install is locked down until you deliberately set `ULTRA_MEMORY_CALLER_CLASS=orchestrator` on a top-level instance you trust.

The boundary is enforced in the query itself (the type filter is applied in SQL, then re-checked as defense-in-depth), and it extends to the *links* hanging off an allowed memory — a subagent recalling an allowed `project` fact that links to a `user` memory does not receive that forbidden memory's id or type; the edge is dropped, fail-closed. And **every recall writes an access-log audit row**, so any attempt to read is auditable after the fact.

To lift the boundary (only on a trusted top-level session), see the recipe in [Configuration](06-configuration-reference.md#give-subagents-full-recall-lift-the-privilege-boundary).

---

## The safety wall on the self-correcting steps

Self-correction and skill synthesis are the only steps that can *change* the system's own knowledge rather than just add to it. They are the highest-blast-radius capabilities in the plugin, so they run behind a wall that lives **in the apply path (code), never only in a prompt**:

- **Provenance gate.** The apply path re-reads the live row before acting and **refuses any action targeting a unit you pinned or authored** (`created_by='human'` / `'import'`). A single forbidden target halts the whole run. Your pinned hard rules and your own facts are physically immutable to the loop.
- **Archive-never-delete.** Every change is a reversible transition or a redirect stub. There is no `rm` anywhere on these paths — a retired learning is archived, not destroyed; a superseded skill moves to an archive directory with a lineage pointer.
- **Bounded blast radius.** Per run: at most **3 edits**, **3 reversions**, **5 quarantines**, and **1 new skill**. A plan exceeding a cap applies *none* of that class (halt-on-exceed, not truncate-and-continue), and the cap is also enforced as a per-period aggregate so stacked re-runs can't accumulate past the budget.
- **Pre-run git checkpoint.** Self-correction and synthesis act **only where a clean git checkpoint exists**. On a dirty or no-git tree they self-skip and apply nothing — git is the restore net, so they refuse to act without one.
- **Audit + human digest.** Each run writes you a short digest (under your configured `briefings_dir`, e.g. `briefings/YYYY/sp7-self-improvement-YYYY-MM-DD.md`) naming what it changed and the exact one-command rollback. You read the summary; you don't babysit the work.
- **Reversion is propose-only.** The riskiest verb — reverting a past graduation whose outcome later regressed — is never applied autonomously. The loop *flags* it in the digest for you to confirm.

The posture is **full autonomy in *whether* it runs, conservatism in *how* it acts**: it runs unattended, but with the gentlest verb first, bounded, reversible, and never touching your authored or pinned knowledge. [The self-learning loop in practice](05-self-learning-in-practice.md) walks through reading a digest and rolling back a change.

---

## Every opt-out, and how to disable each step

You are never locked in. Each step can be switched off individually — from the `/plugin` config UI, or by setting the matching variable directly. The four loop toggles ship **ON** (the loop is opt-out, not opt-in), so disabling means choosing `off` / setting the switch.

| To disable… | `/plugin` config option | Or set directly | Convention |
|---|---|---|---|
| **Session capture** (transcript mining) | Session capture → `off` | `SESSION_INGEST_ENABLE=off` | opt-out value (`0`/`false`/`no`/`off`) |
| **Outcome attribution** | Outcome attribution → `off` | `SP8_ATTRIBUTION_ENABLE=off` | opt-out value |
| **Self-correction** (rewrite/revert/quarantine) | Self-correction → `off` | `SP7_AGGRESSIVE_DISABLE=1` | presence (any value disables) |
| **Skill synthesis** | Skill synthesis → `off` | `SP10_SYNTHESIS_DISABLE=1` | presence |
| **A specific beat entirely** | — | `[maintenance.beats]` `<beat> = false` in `config.toml` | per-beat flag |
| **The wiki steps** (memory-only install) | — | leave `ULTRA_MEMORY_WIKI_ROOTS` unset | default — no-op without it |
| **Interactive session hooks** (headless run) | — | `ULTRA_MEMORY_AGENT_ROLE=cron` | presence of a role marker |

Two conventions to keep straight (they differ on purpose):

- **Opt-out value** switches (`SESSION_INGEST_ENABLE`, `SP8_ATTRIBUTION_ENABLE`) are ON unless set to `0`/`false`/`no`/`off`. Unset = ON.
- **Presence** switches (`SP7_AGGRESSIVE_DISABLE`, `SP10_SYNTHESIS_DISABLE`) are active whenever the variable *exists at all* — even set to an empty string. So `SP7_AGGRESSIVE_DISABLE=0` still **disables** (the value is irrelevant; presence is the signal). To re-enable, *remove* the variable, don't set it to a falsy value.

Want to *watch* self-correction or synthesis without letting them act? Set the dry-run presence switch — they plan, run the eval-gate, and write the digest, but apply nothing:

```bash
export SP7_AGGRESSIVE_DRYRUN=1     # self-correction: plan + digest, apply nothing
export SP10_SYNTHESIS_DRYRUN=1     # synthesis: plan + digest, create nothing
```

Prefer to start narrow and add capability later? Turn the heavy beats off, run the memory layer alone (capture, recall, the gist), read a few weeks of digests in dry-run, then enable each step as you trust it. Nothing about the design assumes you run everything at once.

---

## Your readable, recoverable copy

ultra-memory keeps a human-readable, git-trackable export of your store under `<db-parent>/memory_export` (override with `ULTRA_MEMORY_EXPORT_DIR`), refreshed by the light maintenance step. That export — plus the per-run git checkpoints the self-correcting steps make — is your audit trail and your undo button. You can read what the engine knows in plain text, diff it over time, and roll back any change. The store is yours, in a format you can inspect without the engine running.

---

**Next:** [The self-learning loop in practice →](05-self-learning-in-practice.md) — reading a digest, confirming a proposed reversion, and rolling back a change. Or jump back to the [Configuration reference](06-configuration-reference.md) for the full settings table.
