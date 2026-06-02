---
name: using-wiki-gateway
description: Use when adding a durable knowledge wiki to a project, or writing/customizing a wiki write-gateway — subclassing WikiGateway to control how pages are routed, deduped, frontmattered, anchored, or confidence-labeled. Teaches the 6-hook override contract, the wiki-gateway scaffold command, and the .ultra-memory/config.toml wiring. Trigger before writing or editing a WikiGateway subclass / a consumer wiki gateway.
---

# Using the wiki gateway

ultra-memory's durable knowledge **wiki** is written through `ultra_memory.wiki_gateway.WikiGateway` —
the generic engine (page materialization, embedding dedup, the fcntl write-lock, secret redaction, the
audit row). A project customizes it by **subclassing** and overriding a few hooks; everything else is
inherited and must not be re-implemented.

## Start here: scaffold a subclass

```bash
python -m ultra_memory.wiki_gateway scaffold --out scripts/my_wiki.py --class-name MyWikiGateway --topic mytopic
```

(or the `/wiki-gateway-scaffold` slash command). It emits a `class MyWikiGateway(WikiGateway)` stub with
all 6 hooks (each defaulting to `super()`), their contracts, and the config snippet.

## The 6 override hooks (override ONLY what differs)

- **`route(claim) -> Path`** — where a new page lands. Default `<topic>/concepts/<slug(title)>.md`.
- **`theme_for(claim) -> str`** — the theme-index a new atomic registers under. Default `claim["theme"]` or "general".
- **`render_frontmatter(claim) -> dict`** — the page's YAML frontmatter. Default `{"type":"mechanism","title":…}`.
- **`dedup_check(text, topic) -> match|None`** — semantic dedup-on-write. Default OFF; turn on with `self.find_overlap_match(...)`.
- **`derive_anchor(claim, existing) -> str|None`** — a stable in-page section anchor. Default None.
- **`confidence_label(claim) -> str`** — a confidence tag on the page. Default "Standard".

## Inherited — do NOT re-implement

`create_page` / `append_validation_log_entry` / `register_in_theme_index` / `log`, the embedding+cosine
machinery, the reentrant fcntl write-lock, `strip_secrets` redaction (runs on every write), and the
audit-jsonl row. To EXTEND a verb, override it and call `super()` (see the Trading reference).

## Wire it in

`<project>/.ultra-memory/config.toml`:

```toml
[maintenance]
wiki_gateway = "my_wiki:MyWikiGateway"   # module:Class — or unset for the built-in turnkey gateway
```

## Worked reference: `TradingWikiGateway`

Trading's `scripts/wiki_lib.py` is `class TradingWikiGateway(WikiGateway)` — it overrides `route`
(theme-routing table), `derive_anchor` (SHA-1 stable anchors), `confidence_label`
(`[Standard]`/`[Recent-Regime]`), `dedup_check` (embedding cosine at a 0.83 band), and uses the
**extend-pattern** for `create_page` (`self._ensure_topic_genesis(t); return super().create_page(...)` —
its own genesis step, then the inherited write). Read it as the canonical example of a real extension.
