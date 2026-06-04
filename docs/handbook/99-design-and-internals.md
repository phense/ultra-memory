# Appendix — Design Notes & Rationale

Most of this handbook tells you *what* ultra-memory does and *how* to drive it. This
chapter is the other half: *why it is shaped the way it is.* Every system that touches
your private notes, edits its own knowledge, and calls an LLM unattended owes you an
honest account of the trade-offs behind that trust. This appendix is that account —
distilled from the project's internal design log into one readable narrative, so you can
decide whether the guarantees match what you need before you point the engine at your
data.

It is deliberately self-contained: you can read it cold, without the developer reference.
Where you want the module-level mechanics or the exact function contracts, two in-plugin
documents go deeper and stay in lockstep with the code:

- [`../developer/architecture.md`](../developer/architecture.md) — module-by-module
  mechanics and the canonical storage model.
- [`../developer/design-decisions.md`](../developer/design-decisions.md) — the full
  rationale log, including the bug-hunt arc that hardened each boundary.

The whole system rests on a single principle, and every decision below is an application
of it:

> **Separate durable concerns from volatile ones, then unify them through a
> deterministic, auditable, OAuth-only fabric with zero external dependencies.**

The encouraging part — and this is verifiable in the code, not a slogan — is how much the
design handles *by construction*: redaction at chokepoints, reversibility instead of
deletion, bounded blast-radius, fail-open versus fail-closed chosen per-site rather than
by blanket default, and a hard wall against ever touching an API key.

---

## 1. Why two stores, never merged

Knowledge has two half-lives, and a single store cannot honor both.

Your *preferences, current state, and corrections* churn from session to session — "use
tabs," "the build target moved," "I changed my mind about X." That is **Session Memory**:
fast-moving, query-on-demand, with no value in a hand-browsable form. Your *durable
expertise* — patterns, post-mortems, lessons that outlive any one approach — must survive
for years and stay legible to a human. That is **Expert Knowledge**, the wiki: slow-moving,
Markdown, git-tracked, hand-browsable.

Forcing these into one table forces a compromise on every axis that matters:

| Axis | Session Memory | Expert Knowledge | Cost of merging |
|---|---|---|---|
| **Half-life** | Fast (prefs, state, corrections) | Slow (patterns, lessons) | One expiry model can't decay at two rates. |
| **Write authority** | One writer, transactional, row-level redaction | A domain gateway with topic routing + page schema | A merge forces one write discipline on both. |
| **Canonical form** | DB rows, no flat index | Human-readable Markdown, git-tracked | A merge either loses the readable form or re-exports every memory as Markdown. |
| **Coupling** | Engine imports nothing project-specific | Frontmatter, wikilinks, page-type enums are domain logic | If wiki pages were rows, the engine would have to parse Markdown — breaking the project-agnostic boundary. |

There is also a hard performance budget: warm retrieval must return in roughly two to three
seconds **with no LLM call**. One write-ahead-logged SQLite connection, one embedder, and
one BM25 index serving both stores meets that. A merged store would mean either one larger
database (a scaling risk) or several connections and embedders (a warming cost).

So the two stores stay physically separate and are unified *only at retrieval time*. That
choice is covered next.

---

## 2. Unified recall — fusing two stores into one ranked list

If the stores are separate, why does a search feel like one place? Because **unified
recall** runs three independent rank streams and fuses them into a single ordered list:

1. Session-memory cosine similarity (your memories, embedded).
2. Expert-knowledge BM25 (keyword relevance over wiki pages).
3. Expert-knowledge embedding similarity (semantic relevance over the same pages).

These are combined with **Reciprocal-Rank Fusion (RRF)** — a deterministic algorithm that
rewards an item for ranking *near the top of any one stream*, so a strong keyword hit and a
strong semantic hit both surface even though the two streams disagree on absolute scores.
No LLM scores this list; it is pure arithmetic, which is what keeps recall fast and
replayable.

Two design choices keep it trustworthy rather than merely clever:

- **Retrieval is the right place to unify, not write time.** Unifying at write time would
  put an LLM scorer on the hot path and entangle volatile and durable knowledge at the very
  moment of persisting. Retrieval is where the two stores naturally compose — and, crucially,
  it is *where the privilege wall lives* (see §4). Unifying there means the fused list
  inherently respects the caller's authority: a restricted caller never even sees the rows
  it shouldn't, because the scoping happens inside the same query that ranks them.
- **The fused order is deterministic.** Ties are broken by a fixed secondary key, not left
  to hash order, so the same query against the same corpus returns the same ranking every
  run. (This was a real bug once: a fusion set built in hash order reordered ties between
  runs. The fix — a stable total order — changes only tie-order, never which score ranks
  where.)

---

## 3. The typed-link graph — a cross-store spine, not a second copy

A session learning often *matures into* a wiki page. To represent "this volatile note grew
up into that durable page," ultra-memory keeps a small table of **typed edges**:
`memory → memory`, `memory → wiki`, and the occasional `wiki → memory`. An edge carries a
predicate (`validated_as`, `informed_by`, `superseded_by`, …) so the relationship is
self-describing.

The deliberate restraint here is *what the table does not hold.* The wiki already maintains
its own internal graph of thousands of page-to-page links, read by the wiki's own retrieval.
Mirroring that whole graph into the memory database would bloat it and double the sync cost
for no benefit. So the link table is a **one-way mirror of cross-store edges only** — the
spine that connects the two stores — while pure page-to-page edges stay where they belong.

Idempotency is enforced in code (check-then-insert), because the underlying table predates a
uniqueness constraint on the edge key. And the privilege wall (next section) was extended to
cover *edge endpoints* too — a lesson from a bug-hunt that found restricted memory IDs
leaking through edges even when the row's own type was correctly walled. Visibility data
multiplies across edges and related tables; the wall has to follow it.

---

## 4. The privilege boundary — two axes, fail-closed

Not every caller should see everything. An orchestrator working on your behalf is trusted
with your preferences and feedback; a subagent dispatched into a shared context is not. The
boundary that enforces this composes **two orthogonal axes with a logical AND**:

```
visible(fact)  ⟺  (topic ∈ caller_topics  OR  topic IS NULL)
                  AND
                  (type ∈ allowed_types_for(caller_class))
```

- The **type axis** decides *kinds*. A restricted caller is scoped to project and reference
  facts and **never** sees `user` or `feedback` memories. This directly answers the failure
  mode where a subagent, told in prose "do not print secrets," prints them anyway because the
  secret was central to its answer — prose instructions in a prompt are not a boundary. The
  scope is built into the *query*, not bolted on after.
- The **topic axis** decides *domains*. A caller bound to one topic cannot see another's
  knowledge.

Three properties make this safe rather than merely present:

1. **Fail-closed.** A missing or empty binding resolves to the *empty set* of topics, which
   means "only the cross-topic operational rows," never "everything." The degraded mode —
   the system can't resolve who is asking — sees *less*, never *more*.
2. **Filtered in the query, not after fetch.** The allowlist is pushed into the SQL, so a
   caller asking for the top ten results gets ten *allowed* results — not three, which is
   what happens when you truncate first and filter second. (That was a real bug; the fix
   moved the filter into the query.)
3. **Topic is orthogonal to type.** Operational rows (`user`/`feedback`) carry no topic, so
   they apply everywhere — but the *type* axis still hides them from restricted callers. The
   two axes never collapse into one.

A note on a category error the project learned the hard way (it cost real debugging time):
**provenance gates *mutability*, not *visibility*.** Whether a stored unit may be *rewritten*
is a different question from whether it may be *read from* or *learned from*. Collapsing those
into one predicate once hid a whole batch of valid lessons from the synthesis loop. The rule
that came out of it: *a write-wall predicate must never be reused as a read-scope predicate.*
Decouple gates by what they *mean*.

---

## 5. The single audited gateway and twice-applied secret stripping

Every mutation — to memory, to the wiki, to the edge graph — goes through **one audited
gateway.** There is no path to a raw `INSERT` or a hand-edited page that bypasses it. That
single chokepoint buys five guarantees at once:

- **Redaction.** Secrets are stripped at the write boundary by a pure pattern-matcher
  (Anthropic / GitHub / AWS / Google / Slack / Stripe keys, JWTs, bearer tokens, PEM private
  keys, URI userinfo, credential-shaped `key=value` assignments). Prose with hyphens is left
  alone — only credential-shaped values are mangled.
- **Audit.** Every write lands an entry in an audit log (when, who, what target, what action),
  so "who changed fact X, and when?" is answerable without grepping git diffs.
- **A deterministic spool.** If the database is locked by a concurrent job, the write is
  spooled to a content-addressed file and replayed when the lock clears — so a busy database
  never silently drops a write, and the same write never fires twice. The operator is told
  *loudly* that a write was spooled, never left to discover a silent gap.
- **Transactional discipline.** Each write acquires its lock *up-front* (an immediate
  transaction wrapped in a bounded, backing-off retry) rather than lazily — the difference
  between surviving concurrent jobs and failing on the first contended write.
- **Single point of enforcement.** Every writer — you at the CLI, an agent, a cron job, the
  maintenance loop — inherits the same redaction, audit, spool, and transactional rules,
  because they all flow through the same door.

### Why redaction runs *twice*

Redacting once at write time is not enough, because a secret can hide in a column no single
writer touches — a token in an edge's evidence field, a key in a metadata value, a password
copied into a summary. So redaction runs **again over the entire export dump** — every column,
every row — before anything is committed to git. This is defense-in-depth, and it was hardened
by a bug-hunt that found four read-path leaks at once. The rule that emerged: *redact at the
boundary, not per return site; redact before serializing, not after.* Privacy data multiplies
across edges, related tables, and audit trails, and only a chokepoint discipline catches all of
it.

### Never delete — soft-delete instead

Nothing is ever physically removed. A superseded learning becomes a redirect-stub pointing at
its successor; a retired unit gets a `status` flag (`deleted` / `redirect` / `quarantined` /
`reverted`); the original row stays in the database and stays in git history. This is what
makes even the most aggressive self-correction (next section) *non-destructive by
construction*: the loop cannot `rm` anything — it can only redirect and archive, and every
action is git-checkpointed and reversible.

---

## 6. The self-learning loop and its code-enforced safety wall

This is the part that earns the most scrutiny, so it gets the most structure. The system
improves itself through a four-beat loop, each beat separated from the next by **time and
locus**, every beat **fail-open** (an error means a skipped no-op plus one diagnostic line —
never a wedged session), and every beat governed by one posture:

> **Full autonomy in *whether* it runs; conservatism in *how* it acts.**

| Beat | When | LLM? | Verb posture |
|---|---|---|---|
| **1 · Capture** | Session end | No | Append-only; never blocks. |
| **2 · Consolidate** | Throttled (≈weekly) | One batched call | *Adds only* — graduate / merge / skip. |
| **3 · Self-correct** | Throttled (≈weekly) | One call, strict bounds | Edit / revert (proposed) / quarantine. |
| **4 · Synthesize** | Throttled (≈weekly) | One call + eval-gate | Mint one new skill, eval-gated. |

**Beat 1 — Capture (no LLM).** At session end, the engine records a *deterministic* outcome
signal — an observable fact the session already knows ("did the test pass?", "did the commit
land?") — and enqueues a candidate for each tracked skill that was used without leaving a
lesson behind. No LLM is asked "did this work?" at exit time, because the context is gone and
we want to exit fast. Capture is pure Python, sub-millisecond, append-only: *capture fast,
never lose a learning, never wedge a session.*

**Beat 2 — Consolidate (one call, conservative).** Later, a throttled pass reads a bounded
batch of captured candidates, asks the LLM **once**, and applies a per-candidate plan:
*graduate* a durable lesson into the store, *merge* it into an existing page, or *skip* it as
a transient false-positive. This beat **only adds** — it never rewrites. It refuses to touch a
human-authored or pinned unit. It is bounded by a per-run cap. A parse error or a failed call
simply leaves the candidate for next time.

**Beat 3 — Self-correct (aggressive verbs, six-mechanism wall).** This is the beat that can
*rewrite, revert, or quarantine* the system's own **agent-authored** knowledge when the
downstream evidence has gone net-negative. Because it is the highest-blast-radius autonomous
verb, it lives behind a wall enforced **in the apply path (code), not the prompt** — the LLM
*proposes*, the code *enforces*:

1. **Provenance gate.** Before any change, the code re-reads the live row and refuses any
   action on a human-authored, imported, or pinned unit. Only `agent` / `background_review`
   units are mutable. A single attempt at a forbidden target *halts the whole run*.
2. **Archive-never-delete.** Every verb is a reversible state transition (active → redirect,
   active → quarantined). There is no `rm` anywhere.
3. **Bounded blast radius.** A hard cap per run (a handful of edits / reversions /
   quarantines) — and it *halts on exceed* rather than silently truncating.
4. **Pre-run git checkpoint.** A tag and a store snapshot before any change; it refuses to run
   on a dirty or untracked tree, so there is always a clean point to roll back to.
5. **Audit + human digest.** Every action is written to a human-readable digest. The operator
   is in the *audit* loop, never the *write* loop.
6. **Kill switch.** A single env toggle disables the beat, plus a dry-run mode that produces a
   plan without applying it.

One nuance worth surfacing because it reflects the conservative posture: a *reversion* of a
past graduation is **proposed, not auto-applied** — the loop flags it in the digest for the
operator to confirm. Reversion is the one verb where the system asks first.

**Beat 4 — Synthesize (mint a skill, plus a seventh mechanism).** When a cluster of matured,
positively-scored lessons accumulates around a genuinely new domain, this beat induces a
*native skill* from them. Because a generated skill auto-loads and shapes every future
session's tool-routing, it reuses the full six-mechanism wall (bounded to **one skill per
run**, with per-domain uniqueness so a re-qualifying domain *supersedes* its predecessor
rather than piling up duplicates) **plus a seventh: a load-bearing eval-gate.**

The eval-gate proves a generated skill does **not hijack** an existing skill's auto-trigger.
It runs a deterministic description-similarity pre-filter (reject if the new skill reads too
much like an existing one) and a behavioral trigger-probe (does a realistic prompt route to
the *intended* skill, with zero false-positive captures of an existing skill's territory?).
Its probe coverage is *complete by construction* — if no curated probe set is configured, one
is auto-derived per discovered skill, so the gate never goes stale or fails open as the skill
set changes.

This is also why synthesis **augments rather than competes**: lessons learned *while using*
an existing skill are tagged to that skill and render into its own learnings file (live,
working augmentation); the eval-gate (correctly) rejects any attempt to mint a competitor for
a skill that already exists. Synthesis is reserved for genuinely new domains.

---

## 7. The autonomous-by-default model

As of v0.0.4 the four heavy beats ship **on by default** behind the safety wall above. This
is opt-*out*, not opt-*in* — a deliberate posture, and worth explaining honestly.

Shipping a brand-new self-editing capability disabled (dry-run-first, never-fires) is the
right call *while it is new*. But once the mechanisms are proven, tested, and observed in a
real run, the disabled state stops protecting you and starts costing you: it becomes a
*hidden decision* — "do I trust this?" — that you must re-confirm every cycle, delaying the
benefit for no marginal safety. So once a beat is proven, it is armed, and the *defaults stay
conservative* so it takes the safe path even unattended.

Three things make on-by-default the responsible choice rather than a reckless one:

- **A session-lifecycle driver, not an external scheduler.** The heavy beats are driven from
  a throttled clock on session start — each beat fires at most once per its interval — so the
  loop needs no cron daemon and degrades gracefully on a fresh, no-git, or no-OAuth store
  (it simply no-ops).
- **Reversibility is the real safety.** Because nothing is ever deleted, a revert is a pointer
  flip, not a recovery operation. Eval-gates and bounded blast-radius make mistakes *rare*;
  archive-never-delete plus the git checkpoint make them *cheap to undo*. The system is built
  to make small mistakes and correct them fast, which is a stronger guarantee than a gate that
  waits on a human's schedule.
- **Code bounds never miss.** "If this run has already made three edits, stop" is guaranteed
  by a counter; human attention is not. The provenance check is a database query before any
  mutation, not a subprocess that might crash. Conservatism is *structural*, baked into the
  defaults — even with the operator asleep, the system takes the gentlest verb first.

Turning any beat off is one toggle (a userConfig switch at install, or the matching opt-out
environment variable). And the privacy disclosure is plain: the loop runs on *your* Claude
login (no API key — see §8), reads only your *local* session transcript, and persists only
the extracted, redacted knowledge.

---

## 8. OAuth-only — a hard boundary, not a policy

Every LLM call in the system — every maintenance beat, every future agent — runs through the
local `claude` CLI on *your* OAuth login. The engine never imports an LLM SDK, never calls a
message-create endpoint, and never has an API key on the process. An API key found in the
environment is treated as a hard violation that *crashes loudly* — because it is better to
fail with a clear message than to silently fall through to a separately-metered API account.

The reasons compound:

- **Metering control.** Cost stays on your subscription, not an unpredictable API bill.
- **Session isolation.** The CLI inherits your login — no key rotation, no key-per-project
  fragmentation, no key shared with a subagent.
- **Auditability.** Every LLM call is a visible, grep-able subprocess invocation. An SDK
  import would be caught immediately by review and static guards.
- **Zero keys on disk.** Git never commits a key; the harness stores none. A break is always a
  *visible* expired session or a code bug — never a leaked or misconfigured key.

There is one chokepoint function for this. It validates the environment, strips the ambient
session markers (anti-recursion), and shells out. Tests inject a fake runner. That single
door *is* the rule.

---

## 9. The extensible wiki gateway

The wiki write path is a **subclassable base class**, not a monolith you have to fork. A
project that wants its own routing, dedup, frontmatter, anchors, or confidence labels
overrides only the **six hooks** it cares about:

| Hook | Decides |
|---|---|
| `route` | Which topic / page a new claim belongs to. |
| `theme_for` | The theme-index a page is filed under. |
| `render_frontmatter` | The YAML header written onto a page. |
| `dedup_check` | Whether a claim already exists (and where). |
| `derive_anchor` | The in-page anchor a claim attaches to. |
| `confidence_label` | The confidence tag applied to a claim. |

Everything else — the verb materializers that actually write pages, the embedding and cosine
machinery, the write-lock, secret redaction, and the audit row — is **inherited**. You wire a
subclass with one line in a project config file; leave it unset and you get a turnkey built-in
gateway; provide no wiki config at all and you get a pure-memory install where every wiki beat
quietly no-ops. A scaffold command emits a ready-to-edit starter subclass with all six hook
stubs and the config snippet.

This is the same principle as the rest of the design at a different scale: a single audited
door, with the project-specific judgment calls factored into a small, named set of seams — so a
new consumer extends the engine without forking it, and inherits every safety guarantee for
free.

---

## See also

- [`../developer/architecture.md`](../developer/architecture.md) — the canonical storage
  model and a module-by-module map of the engine.
- [`../developer/design-decisions.md`](../developer/design-decisions.md) — the complete
  rationale log, including the hardening bug-hunt arc summarized here.
- [`../user/overview.md`](../user/overview.md) — the user-level tour of the two stores and the
  loop.
- [`../reference/operations.md`](../reference/operations.md) — install, wiring, rollback, and
  the write spool.
