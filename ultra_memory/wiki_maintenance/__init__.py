"""Generic LLM-wiki maintenance — project-agnostic curation of a Karpathy-style
wiki (markdown + YAML frontmatter + [[wikilinks]] + index pages). A consumer's wiki
schema is one WikiSchemaConfig; the detectors are generic algorithms over
(wiki_root, schema). See docs/superpowers/specs/2026-06-02-generic-wiki-maintenance-design.md
(in the consumer repo). Ships behind config; a pure-memory install (no wiki_roots)
is a no-op."""
