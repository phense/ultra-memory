# 8. Build your own domain

A wiki that only knows about trading is a wiki someone hardcoded for trading. ultra-memory refuses to do that. The engine ships **content-free**: it knows *how* to route, dedup, frontmatter, anchor, and audit a page, but it knows **nothing** about what your pages are *about*. The subject matter — markets, recipes, case law, your home-lab runbook — is supplied by *you*, in two small, well-defined places. This chapter is the map of those two places.

By the end you will have stood up a brand-new knowledge domain — pick whatever you like; we will build a "cooking" domain in parallel with the real "trading" reference so you can see the abstract contract and a concrete instance side by side.

> **Prerequisite.** You have ultra-memory installed and bootstrapped (`/ultra-memory:memory-setup`). If not, start at the [Quick start](03-quick-start.md). For *reading* and *writing* an existing domain's wiki, see [Curating a domain](09-curating-a-domain.md) — this chapter is about *creating* one.

---

## The mental model: topics, the wiki, and the gateway

Three nouns carry the whole design. Learn these and the rest is mechanics.

**A topic** is a walled-off subject area — one top-level directory under the wiki root. `trading` is a topic. `programming` is a topic. `cooking` will be a topic. Topics never bleed into each other: a query scoped to `cooking` never surfaces a `trading` page. A topic is *cheap* — creating one is a directory, an `index.md`, and a registry row, all generated with **no LLM call**.

**The wiki** is the on-disk tree of those topics. It follows Andrej Karpathy's "LLM Wiki" pattern: small **atomic** pages (one idea each), gathered under **theme-indexes**, gathered under a per-topic **master index**, all gathered under one **master-over-masters**. The shape is uniform across every topic — `cooking` and `trading` have byte-identical *structure*, only different *content*:

```
wiki/
  index.md                         ← master-over-masters: links one [[<topic>/index]] per topic
  SCHEMA.md                        ← the convention spec
  topics-registry.yaml             ← the list of known topics
  trading/
    index.md                       ← topic master: links this topic's theme-indexes
    concepts/
      <slug>-index.md              ← a theme-index
      <slug>.md                    ← an atomic page
    synthesis/                     ← longer cross-cutting write-ups
    sources/                       ← provenance pages
  cooking/                         ← your new topic — same shape
    index.md
    concepts/
      ...
```

**The gateway** is the single audited write path *into* that tree. Nothing — no agent, no script, no human-with-an-editor for structured content — creates a page by hand. Every write goes through `ultra_memory.wiki_gateway.WikiGateway`, which routes the page to the right path, deduplicates it against what is already there, redacts secrets, stamps frontmatter, wires up the index links, and appends an audit row. The gateway is *why* the wiki stays consistent without a human policing it.

You build a domain by teaching the gateway your domain's conventions. That is the whole job.

---

## You may not need any code at all

Here is the progressive-disclosure payoff: **the base `WikiGateway` is turnkey.** Out of the box it gives every domain correct, no-LLM defaults:

| Hook | Built-in default |
|---|---|
| where a page lands | `<topic>/concepts/<slug(title)>.md` |
| which theme-index it joins | `claim["theme"]`, else `"general"` |
| frontmatter | `{"type": "mechanism", "title": …}` |
| dedup-on-write | **off** (always create) |
| in-page anchor | none (standalone atomic) |
| confidence label | `"Standard"` |

If those defaults fit your domain, you write **zero Python**. You leave `wiki_gateway` unset in config, point the engine at a wiki root, and start writing pages through the built-in CLI verbs (covered in [Curating a domain](09-curating-a-domain.md)). A "cooking" domain that just wants `cooking/concepts/<recipe>.md` pages tagged by cuisine could ship exactly like this.

You only reach for a subclass when your domain has an *opinion* the defaults don't encode — a routing table, a richer frontmatter schema, dedup-on-write, stable anchors. The next sections are for that case.

---

## Step 1 — Scaffold a gateway subclass

When you do want to customize, never start from a blank file. Generate a stub:

```bash
python -m ultra_memory.wiki_gateway scaffold \
  --out scripts/cooking_wiki.py \
  --class-name CookingWikiGateway \
  --topic cooking
```

or, inside Claude Code, the slash command:

```
/ultra-memory:wiki-gateway-scaffold CookingWikiGateway cooking scripts/cooking_wiki.py
```

This is deterministic — no LLM. It writes a ready-to-edit `class CookingWikiGateway(WikiGateway)` with **all six override hooks present**, each defaulting to `super()`, each documented inline, plus the config snippet. The contract the stub embeds is blunt: **override ONLY the hooks that differ from the defaults; delete the rest** (a deleted override simply falls through to the inherited base). The scaffold ends with a CLI shim so your subclass is runnable as its own command:

```python
if __name__ == "__main__":
    from ultra_memory.wiki_gateway import cli
    raise SystemExit(cli(CookingWikiGateway))
```

---

## Step 2 — Override only what differs (the six hooks)

These six methods are the entire surface a domain customizes. Each takes a *claim* (a `dict` describing the thing being written) and returns a small value. Use the **"if you want X → override Y"** table to find the one you need:

| If you want… | Override | It returns | Default if you don't |
|---|---|---|---|
| pages to land somewhere other than `concepts/<slug>.md` (e.g. route by tag) | `route(claim) -> Path` | a path **relative to the wiki root** | `<topic>/concepts/<slug(title)>.md` |
| atomics grouped under a theme you derive from content/tags | `theme_for(claim) -> str` | the theme name | `claim["theme"]` or `"general"` |
| richer YAML frontmatter (tags, sources, dates, custom type) | `render_frontmatter(claim) -> dict` | the frontmatter dict | `{"type":"mechanism","title":…}` |
| duplicate pages merged on write instead of created | `dedup_check(text, topic) -> match \| None` | a match (→ merge) or `None` (→ create) | `None` — dedup **off** |
| stable in-page section anchors (for multi-section concept pages) | `derive_anchor(claim, existing) -> str \| None` | an anchor string or `None` | `None` |
| a domain-specific reliability tag on each page | `confidence_label(claim) -> str` | the label string | `"Standard"` |

A cooking gateway might override just two of these — route recipes by cuisine, and label by how thoroughly the recipe has been kitchen-tested:

```python
class CookingWikiGateway(WikiGateway):
    def route(self, claim):
        cuisine = (claim.get("cuisine") or "general").lower()
        return Path(self.topic) / "concepts" / cuisine / f"{slugify(claim['title'])}.md"

    def confidence_label(self, claim):
        # how many times this recipe has actually been cooked successfully
        return "Tested" if claim.get("times_cooked", 0) >= 3 else "Untested"
```

The other four hooks are deleted from the stub — they fall through to the base. That is the intended shape of a subclass: **small**.

### What you must NOT re-implement

Everything *below* those six hooks is the inherited engine, and it is load-bearing. Do **not** copy it into your subclass:

- the verb materializers — `create_page` / `append_validation_log_entry` / `register_in_theme_index` / `log`;
- the embedding + cosine-similarity dedup machinery;
- the reentrant `fcntl` write-lock that serializes markdown read-modify-writes;
- `strip_secrets` redaction, which runs on **every** write — a credential that lands in a claim never reaches disk;
- the audit row appended to `briefings/maintenance-logs/wiki-writes-<date>.jsonl`.

If you need to *extend* a verb (do something before the inherited write), override it and call `super()` — never reach around it. The trading reference does exactly this; see the worked example below.

---

## Step 3 — Wire it into `config.toml`

A subclass does nothing until the engine knows to load it. The wiring lives in your **project's** config, never in the plugin:

```toml
# <project>/.ultra-memory/config.toml
[maintenance]
wiki_gateway = "cooking_wiki:CookingWikiGateway"   # "module:Class"
```

The form is `module:Class`. The module is imported with your project's `scripts/` directory (and the project root) prepended to `sys.path`, so an in-tree `scripts/cooking_wiki.py` resolves as `cooking_wiki`. **Leave `wiki_gateway` unset** and the engine uses the built-in turnkey `WikiGateway` — exactly the no-code path from earlier.

A `path-form` value (e.g. `wiki_gateway = "scripts/cooking_wiki.py"`) is also accepted; the maintenance beats invoke it as `uv run <path> <verb>`. Both forms reach the same class.

### The rest of the maintenance seams (optional)

`config.toml`'s `[maintenance]` table is where a domain plugs *all* its project-specifics into the otherwise project-agnostic engine. Most are optional with sensible defaults:

| Key | Purpose | Default if unset |
|---|---|---|
| `wiki_gateway` | the audited write gateway (this chapter) | built-in turnkey `WikiGateway` |
| `topics` | known wiki topics (default-topic fallback) | inferred from the tree |
| `briefings_dir` | audit/digest output directory | `briefings` |
| `wiki_linter` | a consumer linter producing Stage-1 findings | the engine's generic lint |
| `wiki_merge_decider` | the grey-zone dedup merge decision | auto-merge only |
| `wiki_graph_extractor` | the graph-rebuild command template | no graph layer |
| `notifier` | fail-open maintenance-failure alert hook | a one-line stderr no-op |
| `model` | the OAuth model for maintenance LLM calls | `claude-sonnet-4-6` |

A domain that follows the reference Karpathy conventions — `concepts/` atomics, `<slug>-index.md` theme-indexes, 400/800-line caps — needs **none** of the optional schema overrides; every `WikiSchemaConfig` default already matches. You add a seam only when your wiki diverges. (The full variable reference lives in the developer docs; for everyday curation the table above is enough.)

Also tell the maintenance pipeline where your wiki lives, via the environment seam:

```bash
export ULTRA_MEMORY_WIKI_ROOTS="$HOME/.ultra-memory/wiki"   # comma/pathsep-separated
```

Unset and the wiki-maintenance beat is simply a no-op — a pure-memory install with no wiki to curate.

---

## Step 4 — Feed the domain through an ingestion adapter (optional)

You can write pages one verb-call at a time (chapter 09). But most domains have a *source* — a folder of PDFs, a YouTube channel, a notes export — and you want a repeatable pipeline that turns each source artifact into wiki pages. That is an **ingestion adapter**.

The contract is deliberately tiny: **one source = one adapter = three methods.** Everything downstream is the unchanged generic gateway.

```python
class IngestionAdapter(ABC):
    name: str           # provenance source_label on every write
    topic: str          # default target topic (overridable per run)

    @abstractmethod
    def fetch(self, **opts) -> Iterable[Raw]: ...        # pull source artifacts
    @abstractmethod
    def extract(self, raw) -> ExtractResult: ...         # one artifact → structured units
    @abstractmethod
    def to_proposals(self, units) -> (new, merge, side): ...  # units → page proposals
```

A driver, `run_adapter(adapter, *, topic=…, dry_run=…)`, handles the rest: it materializes **all** fetched + extracted units *before* calling `to_proposals` (so batch dedup sees the whole batch), then hands the proposals to `wiki_lib.ingest(...)` — the same routing / dedup / redaction / write / audit path your gateway already defines. The driver, not each adapter, owns stage ordering, so a new adapter cannot get it wrong.

The trading project's first adapter is YouTube:

```python
class YouTubeAdapter(IngestionAdapter):
    name = "scripts/youtube_to_wiki.py"
    topic = "trading"
    def fetch(self, *, since=None, **opts): ...       # list transcript files
    def extract(self, raw): ...                        # transcript → claims
    def to_proposals(self, units): ...                 # claims → (new, merge, side)
```

To add a cooking source — say a directory of recipe PDFs — you implement those same three methods on a `RecipePdfAdapter(IngestionAdapter)`, set `topic = "cooking"`, and reuse `run_adapter` verbatim. No copy-pasted scaffolding, no new write path. One critical invariant carries through: **nothing in the adapter layer calls an LLM directly** — any LLM step routes through ultra-memory's OAuth `claude`-CLI chokepoint, never an SDK or API key.

---

## The worked reference: `TradingWikiGateway`

Trading is **topic #1**, not a special case — it is exactly the domain you would build by following the four steps above, and it is the canonical example to read. Its gateway, `scripts/wiki_lib.py`, is a thin `class TradingWikiGateway(WikiGateway)` that overrides:

- **`route`** — delegates to a tag-based routing table (a claim's tags pick its theme directory) instead of the flat `concepts/<slug>` default;
- **`theme_for`** — the theme-index an atomic registers under;
- **`derive_anchor`** — stable SHA-1-derived in-page section anchors, so re-ingesting the same idea lands in the same section;
- **`confidence_label`** — maps the source to `[Standard]` / `[Recent-Regime]` labels the daily-briefing agent reads;
- **`dedup_check`** — turns dedup **on** via the inherited embedding machinery at a calibrated cosine band.

And it uses the **extend-pattern** for `create_page`: it runs its own one-line topic-genesis step, then calls the inherited write —

```python
def create_page(self, path, content, *, topic=None, wiki_root=None) -> str:
    t = topic or self.topic
    self._ensure_topic_genesis(t)          # Trading's own pre-step (scaffold an unknown topic)
    return super().create_page(path, content, topic=t, wiki_root=wiki_root)
```

That is the *whole* extension. The page write, the path-escape guard, the redaction, the lock, the audit row — all inherited. Trading's `config.toml` wires it in with one line (`wiki_gateway = "scripts/wiki_lib.py"`), declares its topics (`["trading", "programming"]`), and supplies the optional seams it actually diverges on (a project-specific linter, a calibrated grey-zone merge judge, a graph extractor). Read `scripts/wiki_lib.py` end to end as the reference for a real, production extension — it is small precisely because the engine does the heavy lifting.

---

## Recap

1. A **topic** is a walled-off subject; a **gateway** is the single audited write path into it.
2. The base `WikiGateway` is **turnkey** — if its defaults fit, write **zero code**.
3. To customize, **scaffold** a subclass and override **only** the six hooks that differ; never re-implement the inherited engine.
4. **Wire** the subclass with one line in `<project>/.ultra-memory/config.toml`.
5. For a repeatable source, write a three-method **ingestion adapter** and reuse `run_adapter`.

Your domain now exists and can be written to. Next, learn to keep it healthy: how to author pages through the gateway verbs, browse and retrieve them, and let the maintenance pipeline lint, dedup, and cross-link your topic — see **[Curating a domain](09-curating-a-domain.md)**.
