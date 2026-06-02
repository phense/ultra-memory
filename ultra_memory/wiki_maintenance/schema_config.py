"""The wiki-schema seam — every LLM-wiki convention as one overridable config.

A 10-module analysis of a reference wiki-maintenance pipeline found every detector to
be a generic algorithm whose only site-specifics are SCHEMA: frontmatter field names,
the required-fields-per-page-type map, the index-naming convention, topic layout, size
caps, dedup thresholds, stale markers, and the worklist KINDS taxonomy. They all live
here. A consumer overrides any field via its `.ultra-memory/config.toml` `[wiki]`
table; absent → the reference default (so a consumer whose wiki follows the reference
conventions needs almost no config). The ENGINE names no consumer concept — these are
plain values, not consumer imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace  # noqa: F401

# The worklist finding taxonomy (generic; a consumer may extend/trim).
KINDS_DEFAULT = (
    "greyzone-dedup", "cross-link", "recategorize", "recalibrate", "contradiction",
    "synthesis-candidate", "summarize", "stale-archive", "index-create", "index-split",
)


@dataclass(frozen=True)
class WikiSchemaConfig:
    """All wiki-schema seams. Defaults = the reference LLM-wiki schema (the
    Karpathy-style conventions a `wiki/SCHEMA.md` documents)."""
    # Frontmatter field NAMES (a consumer may name them differently).
    type_field: str = "type"
    title_field: str = "title"
    theme_field: str = "theme"
    anchor_field: str = "anchor"
    updated_field: str = "updated"
    status_field: str = "status"
    # Required frontmatter: base (every page) + per-page-type extras.
    base_required_fm: tuple = ("type", "title")
    type_required_fm: dict = field(default_factory=dict)   # page_type -> (fields…)
    # Page-type taxonomy.
    index_types: tuple = ("theme-index", "master-index")
    redirect_type: str = "redirect"
    # Index naming: a theme value → its index filename.
    index_name_template: str = "{slug}-index.md"
    # Topic layout (top-level dir per topic; atomics under `atomics_subdir`).
    topic_subdirs: tuple = ("concepts", "synthesis", "sources", "entities")
    topic_master_index: str = "index.md"
    atomics_subdir: str = "concepts"
    # Size caps (lines).
    page_soft_cap_lines: int = 400
    page_hard_cap_lines: int = 800
    index_oversize_lines: int = 300
    # Semantic dedup.
    dedup_lower: float = 0.78
    dedup_upper: float = 0.86
    embed_model: str = "BAAI/bge-small-en-v1.5"
    # Lifecycle / stale markers.
    stale_status_marker: str = "superseded"
    conflict_section_headings: tuple = ("Conflicts-with", "Variant")
    resolution_marker_regex: str = r"<!--\s*resolved:.*?-->"
    # Auto-fix patterns.
    autoadded_section_name: str = "Recently auto-added (uncategorized)"
    anchor_suffix_digits: int = 4
    # Worklist taxonomy.
    kinds: tuple = KINDS_DEFAULT

    def theme_slug(self, theme: str) -> str:
        """A theme value → its slug (the index-naming basis). Lowercase, `/`+space
        → `-`. Matches the reference wiki convention; override the whole template per
        consumer if a different scheme is needed."""
        return str(theme).strip().lower().replace("/", "-").replace(" ", "-")

    def index_filename(self, theme: str) -> str:
        """The theme-index filename for a theme value (e.g. 'macro-transmission-index.md')."""
        return self.index_name_template.format(slug=self.theme_slug(theme))


def load_wiki_schema(raw=None) -> WikiSchemaConfig:
    """Build a WikiSchemaConfig from a consumer override dict (the `[wiki]` config
    table). Only known fields are accepted; a list override is coerced to a tuple
    where the default is a tuple. Fail-open: a non-dict / a bad value → the default
    schema (a malformed wiki config must never wedge maintenance)."""
    if not isinstance(raw, dict):
        return WikiSchemaConfig()
    defaults = WikiSchemaConfig()
    kw: dict = {}
    known = {f.name for f in fields(WikiSchemaConfig)}
    for name, value in raw.items():
        if name not in known:
            continue
        cur = getattr(defaults, name)
        if isinstance(cur, tuple) and isinstance(value, list):
            value = tuple(value)
        kw[name] = value
    try:
        return replace(defaults, **kw)
    except Exception:
        return defaults
