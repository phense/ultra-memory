# Encountered Problems — Battle Scars, Proudly Displayed

> A retrospective of the gnarliest bugs, sharpest facepalms, and most dramatic
> near-misses since ultra-memory's first commit (2026-05-30). This is the one
> doc where we let ourselves be funny about it — wry, self-aware, engineer-to-
> engineer — because **every single one of these is now found, fixed, and
> regression-tested.** A bug you can laugh about is a bug that can no longer
> hurt you. Read it as a tour of how the system got tougher, one facepalm at a
> time.

The plugin grew from a bare memory store into an autonomous, multi-root,
self-learning knowledge fabric over ~212 commits. That kind of growth
manufactures dragons. Here are the ones we slew, roughly in order of how loudly
someone said "oh no" when they found it.

---

## 1. The Predicate We Reused Twice, And It Bit Us Twice

**The symptom.** Cold-start backfill dutifully seeded **137** skill-learnings
into the live DB. SP-10 synthesis then looked at all 137 of them and saw…
nothing. Zero. The `Learnings.md` projections rendered empty over a table that
was demonstrably full. Schrödinger's lessons: present in the DB, absent from
every consumer.

**The facepalm.** `select_induction_clusters()` reused the SP-7 **mutability**
gate (`created_by IN ('agent','background_review')`) as if it were an SP-10
**visibility** gate. The backfill rows carry `created_by='backfill_import'`, so
the write-wall predicate — a predicate whose entire job is *"may I rewrite this
row?"* — quietly answered *"no, therefore you may not even SEE it."* And because
we're nothing if not consistent, we made **the exact same mistake a second
time** at the draft source-gate. Two sites, one category error.

**The fix.** Decouple visibility from provenance by their semantic intent, not
their convenient shared code. Cluster selection now filters by
`node_type='learning'` (provenance-agnostic, `98f5c81`); the source-gate uses a
brand-new `assert_synthesis_source()` that reads from any source except pinned
rows (`c97fe48`). Suddenly all 137 lessons were visible and synthesis worked.

**Lesson learned.** *Reusing a write-gate predicate as a read-gate is a category
error — and you will make it twice if you make it once. Decouple gates by what
they mean, not by what's already imported at the top of the file.*

---

## 2. The Knowledge MCP That Died on Hello

**The symptom.** Restart Claude Code, and the knowledge MCP would crash with an
onnxruntime `NoSuchFile` before it could even answer `initialize`. The consumer
just saw a curt `Connection closed`. Knowledge: unavailable. Vibe: ominous.

**The facepalm.** `main()` eagerly built the fastembed bge-small model from
`$TMPDIR/fastembed_cache`. macOS treats `$TMPDIR` as a suggestion, periodically
purging it and *always* nuking it on reboot. So every reboot deleted the one
file the server needed to draw its first breath — and we asked for that file in
the constructor, before saying hello.

**The fix.** Two-pronged (`e88fd9a`): (1) move the cache somewhere macOS won't
vandalize — persistent `~/.cache/ultra-memory/fastembed`; (2) make the embedder
**lazy** so a cache miss degrades a single query instead of strangling the whole
connection in its crib. Result: the server answers `initialize` in 0.36s even
with a cold/stale cache.

**Lesson learned.** *Eager resource init in MCP `main()` is a loaded gun pointed
at your own startup. Lazy + memoized defers the error to query time, where it's
survivable.*

---

## 3. The Four-Headed Leak (SP-8 Bug Hunt, Round 1)

**The symptom.** An adversarial hunt went looking for trouble and found four
separate ways for secrets and forbidden IDs to escape the read path. A bad
afternoon, compressed.

**The facepalm.** (1) `unified_recall` returned text **without** the
`strip_secrets` pass that its sibling `knowledge_recall` faithfully ran. (2)
`wiki_sync` mirrored wiki titles *verbatim* into the queryable index, so a
free-form edit could smuggle an unredacted secret straight into the DB. (3) The
privilege wall filtered row *types* but forgot about *edges* — so the `links`
table cheerfully leaked forbidden `user`/`feedback` memory IDs to subagents. (4)
`record_session_event` redacted JSON *keys* but passed the *values* to
`json.dumps()` raw, persisting secrets into the audit spool.

**The fix.** Treat the read path as a **chokepoint** (`1a9d1f4`): redact all
returned text; make `wiki_sync` a redaction chokepoint over title/snippet/bm25;
add `filter_links_for_caller()` that extends the type-wall to edges and
fail-closes on unknown kinds; redact string values *before* the dump so replay
carries only redacted text.

**Lesson learned.** *Redaction belongs at the chokepoint, not sprinkled across
every return site. Privacy-critical data multiplies across edges and sibling
tables — wherever you forgot to look is exactly where it leaks.*

---

## 4. The Migration That Half-Happened

**The symptom.** A crash mid-migration could leave the schema partway updated
while `PRAGMA user_version` had *already* ticked forward — a DB that believes
it's at version N while physically sitting at N-minus-a-bit. Replays then
crashed on `ADD COLUMN … column already exists` and wedged the runner.

**The facepalm.** `migrate()` used `executescript()`, which auto-commits after
every statement. So "schema change" and "version bump" weren't one atomic act —
they were a sequence of independently-committed steps, perfectly poised to
desync at the worst moment. SQLite DDL *is* transactional, but only if you
actually wrap it in a transaction. We hadn't.

**The fix.** Each pending migration's statements **and** its version bump now run
inside one explicit `BEGIN/COMMIT`; `ADD COLUMN` got `IF NOT EXISTS` so replays
are tolerant; and the dump now carries `PRAGMA user_version` so restore→reopen
round-trips the version without re-running anything (`6da197b`). A mid-migration
crash now rolls back cleanly and stays at the old version.

**Lesson learned.** *`executescript()`'s auto-commit silently breaks DDL
atomicity. Wrap migration + version-bump in one explicit transaction, and assume
every migration will be replayed.*

---

## 5. The Import That Ate Your First Memory

**The symptom.** Import two memories with the same frontmatter `name`, and the
first one silently vanished — overwritten by the second. The import counter,
meanwhile, confidently reported success, masking the data loss it had just
caused.

**The facepalm.** `memory_import` had no collision check; it just upserted, and
duplicates won by being last. The over-reported count was the perfect alibi:
everything *looked* fine.

**The fix.** `memory_import` now fails **loud** on a duplicate frontmatter name;
`import_memory_dir` validates uniqueness up front (`62f7739`).

**Lesson learned.** *Silent overwrites in an import flow are debugging hell. Fail
early, fail loud — a crash with a clear message beats a missing memory you won't
notice for a week.*

---

## 6. The Privilege Wall That Starved Recall

**The symptom.** A type-scoped subagent (allowed only `project`/`reference`)
asks for `top_k=10` and gets… 3 results. Not because there were only 3 — because
7 of the top candidates were forbidden types, filtered out *after* truncation.
The allowlist quietly shrank every result set.

**The facepalm.** `knowledge_recall` had no `include_types` SQL filter. It
over-fetched, truncated to top_k, *then* applied the allowlist — so the wall ate
the results the user was actually entitled to.

**The fix.** Push the type allowlist into the SQL itself (`include_types`) so the
query only ever returns allowed rows, making top_k and the allowlist compatible;
clamp and cap per type (`62f7739`).

**Lesson learned.** *Privilege filters live in the query layer, not post-fetch.
Filter-after-truncate silently breaks the top_k contract.*

---

## 7. Green Tests, Hollow Veneer (The Cron That Broke On Day One)

**The symptom.** The wiki-gateway Phase-1 build passed **everything** — 1115
green plugin tests, 1193 green Trading tests, 9 skipped, not a red dot in sight.
We shipped it. The live cron broke **immediately.**

**The facepalm.** The subagent had built a beautiful `WikiGateway` class and a
suite of pure-scaffold tests that mocked the gateway or exercised only the golden
path — and never once asked the one question that mattered: *is the resolver
actually wired into the Stage-2 beat?* It wasn't (M1 was unfinished). The tests
were a veneer painted over broken wiring. All green, all hollow.

**The fix.** Orchestrator **structural** verification plus an Opus review caught
the unwired resolver before it could do more damage, then truly wired it
(`4af5828`). The same discipline later caught a `create_page` contract creep
before *it* shipped.

**Lesson learned.** *Green tests without structural verification are a veneer.
"Does the test pass?" is not the same question as "is the resolver threaded into
beat X?" — ask the second one too.*

---

## 8. The Session Event That Outlived Its Edges

**The symptom.** `retention.prune_session_events` hard-`DELETE`d old session
events. But `validated_as` / `informed_by` outcome edges still pointed at those
rows. Query an orphaned edge later and the endpoint resolved to `None` — a
dangling pointer in a graph store.

**The facepalm.** We hard-deleted nodes in a graph without checking whether
anything still pointed at them. Classic.

**The fix.** `prune_session_events` now **soft-tombstones** instead of deleting,
and edge traversals check liveness, so attribution edges stay safe (`6c546f4`).

**Lesson learned.** *In a graph store, hard-delete is a foot-gun. Soft tombstones
plus liveness checks keep your edges honest.*

---

## 9. Heisenberg's Ranking (Results That Shuffled Themselves)

**The symptom.** Run the *same* query twice, get results in a *different* order.
Reproducibility's favorite nightmare.

**The facepalm.** The RRF reranker built its dict from a `set` of `(kind, key)`
tuples — iteration order salted by `PYTHONHASHSEED` — and the final sort keyed on
score *alone*, with no tie-break. Equal-score hits flipped top_k membership
depending on the phase of the moon (well, the hash seed).

**The fix.** Deterministic total order: sort on `(-weighted_score, (kind, key))`.
Same scores, same ranks — now also the same *order*, every time (`4e81b39`).

**Lesson learned.** *Sorting on a single key in the presence of ties is a latent
bug. Always add a secondary tie-break, or your "deterministic" engine isn't.*

---

## 10. The Re-Import That Demoted A Human

**The symptom.** A human edits a memory directly in the DB, then re-imports the
matching markdown file. The engine cheerfully reverts the human's edit back to
the stale markdown body **and** downgrades `created_by` from `'human'` to
`'agent'`. The machine overruled the person and erased the evidence.

**The facepalm.** `save_memory`'s UPDATE didn't guard provenance, and
`import_memory_dir` had no concept of human-authored rows being sacred. A
re-import was a loaded clobber.

**The fix.** UPDATE now **preserves** `'human'` provenance (a non-human re-save
over a human row keeps it human), and `import_memory_dir` **skips** any live row
that's `created_by='human'` — re-import is now edit-safe and provenance-safe
(`4e81b39`).

**Lesson learned.** *Human provenance is sacred. Import flows must respect it —
the machine never gets to quietly overrule the person.*

---

## 11. The Stale Phantom That Pointed At Nothing

**The symptom.** Delete a knowledge page from disk, and its `unified_index` row
(plus embedding) lived on as a **phantom hit** in `unified_recall` — a search
result confidently pointing at a file that no longer existed.

**The facepalm.** The orphan-prune scoped itself by `topic IN (…)`, recomputed
from current on-disk pages. If a topic went fully empty, it dropped out of the
`IN` list entirely, so the prune **skipped its own orphans.** The logic deleted
exactly the references it should have cleaned up — by losing track of them.

**The fix.** Scope the prune to the synced **roots' path-prefix**, independent of
topic membership (mirroring `memory_export`'s path-based prune); also prune the
knowledge embedding (`4e81b39`).

**Lesson learned.** *Orphan-prune must be path-based, not topic-based. Topic
membership disappears with the last page — and takes your dangling references
with it.*

---

## 12. The Eval-Gate That Ran For 50 Minutes (Inside A 60-Minute Window)

**The symptom.** The SP-10 hijack eval-gate fired ~265 **serial** `claude -p`
probes (5 samples × ~53 skills, ~14-18s each), blew past the 60-minute
`MAINT_TIMEOUT_STAGE2`, got killed with exit 124, and applied exactly zero
skills. A safety gate that times itself to death is not, strictly, safe.

**The facepalm.** A naive serial probe loop. Also, every candidate reused the
*same* probe filename — a delightful race hazard quietly waiting for the day
someone parallelized it.

**The fix.** First, the panic button: `RUNS_PER_QUERY 5→2` to fit the budget
(`a3a8125`). Then the real one (§1.4.7): a bounded `ThreadPoolExecutor`
(`PROBE_MAX_WORKERS=6`), each probe with a **unique** per-probe temp file
(`<slug>-probe-<nonce>.md`). ~50 min → ~12 min (`c87389b`), and we could afford
`RUNS_PER_QUERY` back up to 3.

**Lesson learned.** *Tight-deadline evaluation loops need parallelism, and every
concurrent probe needs its own scratch file. A serial gate that can't beat the
clock never runs at all.*

---

## 13. The Ghost Command File That Committed Itself

**The symptom.** A killed eval-gate run left behind an ephemeral
`.claude/commands/<slug>-probe.md`. The session's auto-commit hook swept it into
git, and it loaded as a stray slash-command in the *next* session. The dead
probe came back as a poltergeist.

**The facepalm.** The probe generator wrote a temp command file but had no
cleanup and no gitignore. A timeout/kill left the corpse in a user-facing
directory, where the auto-committer found it and gave it a permanent home.

**The fix.** Remove the artifact + add `.claude/commands/*-probe.md` to
`.gitignore` (Trading `f15e064`). Verified the live store was clean — every
killed run had died in draft/eval *before* apply (0 `generated_skill` rows), so
no bad state ever reached production.

**Lesson learned.** *Temp artifacts in user-facing directories need both
defensive cleanup AND a gitignore pattern. Assume the kill signal will arrive at
the worst moment, because it will.*

---

## 14. The Classifier That Rejected Everything (And Was Right To)

**The symptom.** SP-10's anti-hijack gate rejected **every** generated skill from
the cold-start backfill. `gen-superpowers-subagent-driven-development` fired on 3
static-skill probes and got bounced. Looked like a total blocker.

**The plot twist (not a bug).** All 137 backfill lessons were learned *while
using existing skills*, so each backfill domain is **named after a skill that
already exists** (`index_hook=<skill>`). A `gen-<skill>` competes head-to-head
with the static `<skill>`, so the anti-hijack gate rejecting it is exactly,
precisely correct. The deeper realization: the backfill **cannot mint new
skills by design** — it can only *augment* existing ones via `Learnings.md`
projections (which work great). Genuinely new generated skills come from the
**forward** loop's novel domains.

**Lesson learned (design, not a bug).** *The eval-gate was working perfectly. The
category error was ours — expecting a cache seeded from existing skills to
produce* new *skills, rather than to enhance the ones it was seeded from.*

---

## 15. The Embedder That Said "NoneType Is Not Callable" Instead Of The Truth

**The symptom.** Call `query_memories` with `embedder=None` on a non-empty store
and get a cryptic `'NoneType' object is not callable` from somewhere deep in the
function. The docstring, meanwhile, cheerfully promised "embedder=None → BM25-only
fallback."

**The facepalm.** That fallback only existed on the *knowledge* side of
`unified_recall`. The *memory* side requires a real embedder — but nobody checked
at the door, and the docstring lied about it.

**The fix.** `query_memories` now raises a clear `ValueError` at entry if
`embedder is None` on a non-empty store, surfacing the real constraint instead of
a mid-function mystery (`6bfd4b9`).

**Lesson learned.** *Check type invariants at the front door. A false docstring is
worse than no docstring — it hides the real error behind a confident lie.*

---

## 16. The LLM JSON That Broke On A Newline

**The symptom.** Skill synthesis would occasionally just… stop. No skill, no
error anyone wanted to read — the generated draft's JSON failed to parse
(unescaped newline or special char in a long markdown body) and the whole beat
silently halted.

**The facepalm.** A single parse failure killed the entire skill draft, with no
retry. Long bodies — exactly the valuable ones — were the most fragile.

**The fix.** Wrap draft generation in a `retry_on_parse` loop — one parse failure
triggers one retry, which handles occasional LLM fragility gracefully (`956bbce`).

**Lesson learned.** *LLM-generated structured output is fragile by nature. Design
for a single-pass retry on parse failure; one bad newline shouldn't sink the
whole beat.*

---

## 17. SQLITE_BUSY On The Doorstep (WAL + Deferred Transactions)

**The symptom.** Under concurrent write pressure, writes would raise `database is
locked` **immediately** during `BEGIN DEFERRED` — even with `busy_timeout=30000`
politely set. Migrations and retention were especially exposed.

**The facepalm.** `BEGIN DEFERRED` (the default) only grabs its lock on the first
*write*, and in WAL mode under contention that grab can fail before the timeout
window even applies. The busy_timeout covers contended *statements*, not the lazy
lock the deferred transaction was saving for later.

**The fix.** Acquire the lock up front with `BEGIN IMMEDIATE`, wrapped in a
bounded retry-with-exponential-backoff (`bounded_busy_retry`) so a transient busy
backs off and retries instead of exploding (`0c704e8`, `42c1eaf`). The spool
catches anything that still loses, and `maintain.run` drains it.

**Lesson learned.** *WAL mode with concurrent writers wants upfront lock
acquisition (`BEGIN IMMEDIATE`) plus bounded retry. A lazy lock is a lock you'll
fail to get at the worst time.*

---

## 18. The Em-Dash That Swallowed A Work Session

**The symptom.** Import a `.remember` today-file with a header like
`## 14:00–15:30` (en-dash range) or any non-`HH:MM` header, and the parser
silently folded it into the *previous* block — quietly merging two distinct work
sessions into one and losing the timestamps.

**The facepalm.** The `_TODAY_HEADER` regex only recognized the ASCII hyphen
(`-`) as a range separator. En-dash (U+2013) and em-dash (U+2014) — which any
civilized editor inserts automatically — went unrecognized, so the header looked
like prose and got absorbed. Silent data loss, courtesy of Unicode.

**The fix.** Accept all dash variants in the range separator, and capture
non-time headers as their *own* block at day-midnight **with a warning** instead
of folding them silently. Verified against the real corpus: 2 en-dash blocks now
land as distinct rows (`5816e25`).

**Lesson learned.** *"Never silently lose data" is the mandate. A loud warning
always beats a silent fold — and your regex must speak Unicode, because your
editor already does.*

---

## 19. The Session-Event JSON Leak (Keys Redacted, Values Not)

**The symptom.** `record_session_event` promised "all persisted text is redacted
first." It redacted the `title` and `detail` *keys* — and then handed the string
*values* straight to `json.dumps()`, persisting secrets verbatim into the audit
spool. The audit trail, of all places.

**The facepalm.** We redacted the labels and forgot the contents. `json.dumps()`
does not, it turns out, redact for you.

**The fix.** Redact each string element **before** `json.dumps()`, so the spool
carries only redacted text. (`event_key` still keys on the raw text so it
survives redaction-rule changes — content-addressing where it's safe, redaction
where it's persisted) (`1a9d1f4`).

**Lesson learned.** *Serialization is not redaction. Redact before the dump, never
after — and the audit trail is the* last *place you want a leak.*

---

## 20. The Fetch-Then-Filter N+1 (Links For Rows You Threw Away)

**The symptom.** `query_memories` built the full result dict — including a
link-`SELECT` per row — for the **entire** candidate set, *then* truncated to
top_k. So it ran N link subqueries to keep K results. On a growing store, that's
a lot of wasted SQL.

**The facepalm.** Fetch-then-truncate: the textbook N+1, hiding in plain sight.

**The fix.** Score all candidates, **sort + truncate to top_k first**, *then*
fetch links only for the survivors. Per-recall link work is now bounded to top_k,
and the return shape is byte-identical (`4e81b39`). (A composite index
`idx_access_log_session` in migration 0008 cleaned up a sibling full-scan on the
attribution query, too.)

**Lesson learned.** *Fetch-then-filter is the N+1 in disguise. Filter on the key
before the join, every time.*

---

## What They All Taught Us

Every one of these traces back to a single discipline: **separate concerns by
what they MEAN, not by what's convenient** — write-gates from read-gates,
redaction-at-the-chokepoint from redaction-everywhere, soft-delete from
hard-delete, lazy-init from eager-init, a real test from a green veneer — and
when in doubt, **fail loud, never silent.**
