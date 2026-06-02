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
    # ── embedding constants ──
    EMBED_DIM: int = 384  # BAAI/bge-small-en-v1.5 native dim
    EMBED_MODEL_NAME: str = "BAAI/bge-small-en-v1.5"

    def __init__(self, *, wiki_root: Path | None = None, topic: str = "default",
                 schema: WikiSchemaConfig | None = None):
        self.wiki_root = Path(wiki_root) if wiki_root else None
        self.topic = topic
        self.schema = schema or WikiSchemaConfig()
        self._embed_model = None  # lazy-loaded per instance

    # ── embedding + cosine machinery ──

    @staticmethod
    def _text_sha256(text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _get_embed_model(self):
        """Lazy-load the fastembed model (optional dep — absent → callers degrade gracefully)."""
        if self._embed_model is None:
            from fastembed import TextEmbedding
            self._embed_model = TextEmbedding(model_name=self.EMBED_MODEL_NAME)
        return self._embed_model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed a list of texts. Returns one 384-dim vector per input
        as Python floats (not numpy float32) so the result is JSON-serializable
        for the on-disk cache.
        """
        if not texts:
            return []
        model = self._get_embed_model()
        return [[float(x) for x in v] for v in model.embed(texts)]

    def _valid_embedding(self, vec) -> bool:
        """Cheap structural check before trusting a cached embedding.
        Catches: truncated cache writes, manual file edits, model-version drift.
        """
        return (
            isinstance(vec, list)
            and len(vec) == self.EMBED_DIM
            and all(isinstance(x, (int, float)) for x in vec[:4])  # sample-only, not full
        )

    def _get_embed_db(self):
        """Return (embed_cache_module, db_path) for the embedding cache.

        This is the override seam: a subclass can return a different backend
        (e.g. a Trading-project-specific path). The base resolves against
        wiki_root if set, else the plugin default.

        Returns:
            tuple: (ec_module, db_path: Path)
        """
        from ultra_memory import wiki_embed_cache as ec
        if self.wiki_root is not None:
            db_path = self.wiki_root / "wiki_embeds.db"
        else:
            db_path = ec.DB_PATH
        return ec, db_path

    def embed_with_cache(self, text: str, cache_key: str | None = None) -> list[float]:
        """Embed a single text, using the SQLite cache if `cache_key` is
        provided and the cached entry's sha256 matches the current text.

        `cache_key` is typically an absolute path (atomic-mechanism files).
        When None (e.g. the transient claim_vec lookup inside
        find_overlap_match), no cache read/write happens.
        """
        sha = self._text_sha256(text)
        ec, db_path = self._get_embed_db()
        if cache_key is not None:
            try:
                entry = ec.get_atomic(cache_key, model_name=self.EMBED_MODEL_NAME, db_path=db_path)
                if entry and entry[0] == sha and self._valid_embedding(entry[1]):
                    return entry[1]
            except Exception:
                pass  # cache miss / error → re-embed
        [vec] = self.embed_texts([text])
        if cache_key is not None:
            try:
                ec.put_atomic(cache_key, sha, vec, self.EMBED_MODEL_NAME, db_path=db_path)
            except Exception:
                pass  # cache write failure is non-fatal
        return vec

    @staticmethod
    def cosine_sim(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two non-zero vectors of equal length.

        Returns 0.0 on length mismatch — without this guard, zip() silently
        truncates to the shorter vector and produces a garbage score that looks
        like a valid similarity. With dedup at 0.85, a corrupted cache entry
        could thus false-positive-merge unrelated mechanisms.
        """
        import math
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)

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
