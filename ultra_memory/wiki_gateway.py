"""Project-agnostic wiki write-gateway. A consumer subclasses WikiGateway and
overrides only the project-specific hooks (route/theme_for/render_frontmatter/
dedup_check/derive_anchor/confidence_label). The base provides correct, simple,
no-LLM defaults so a pure install is turnkey."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any

from ultra_memory.redact_secrets import strip_secrets  # noqa: F401 — used in later tasks
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].rstrip("-") or "untitled"


class WikiGateway:
    def __init__(self, *, wiki_root: Path | None = None, topic: str = "default",
                 schema: WikiSchemaConfig | None = None):
        self.wiki_root = Path(wiki_root) if wiki_root else None
        self.topic = topic
        self.schema = schema or WikiSchemaConfig()

    # ── override points (simple, no-LLM defaults) ──
    def route(self, claim: dict[str, Any]) -> Path:
        title = claim.get("title") or claim.get("text") or "untitled"
        return Path(self.topic) / self.schema.atomics_subdir / f"{slugify(title)}.md"

    def theme_for(self, claim: dict[str, Any]) -> str:
        return claim.get("theme") or "general"

    def render_frontmatter(self, claim: dict[str, Any]) -> dict:
        return {"type": "mechanism", "title": claim.get("title", "untitled")}

    def dedup_check(self, text: str, topic: str):
        return None  # OFF by default; an override turns on embedding cosine

    def derive_anchor(self, claim: dict[str, Any], existing) -> str | None:
        return None

    def confidence_label(self, claim: dict[str, Any]) -> str:
        return "Standard"
