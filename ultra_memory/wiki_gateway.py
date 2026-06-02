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

    # ── page-loading + section parsing ─────────────────────────────────────────

    # Regex to extract the `**Mechanism**:` block from an atomic file.
    _MECHANISM_RE = re.compile(
        r"\*\*Mechanism\*\*[^\n:]*:\s*(.+?)(?=\n\s*\n|\n\*\*[A-Z]|\n---|\Z)",
        re.DOTALL,
    )

    # Regex to match `### Title {#anchor-slug}` section headers inside concept pages.
    _SECTION_HEADER_RE = re.compile(
        r"^###\s+.+?\s*\{#([a-z0-9][a-z0-9-]*)\}\s*$",
        re.MULTILINE,
    )

    # Regex to extract `type: <value>` from frontmatter.
    _FRONTMATTER_TYPE_RE = re.compile(r"^type:\s*(\S+)", re.MULTILINE)

    # Separator for section cache keys: `<path>#<anchor>`.
    _SECTION_KEY_SEP = "#"

    def extract_mechanism_text(self, md_text: str) -> str:
        """Extract the raw `**Mechanism**:` block text from an atomic file.

        Falls back to the whole body (post-frontmatter) if the Mechanism block
        is not found. Used as the input string to the embedding model.
        """
        m = self._MECHANISM_RE.search(md_text)
        if m:
            return m.group(1).strip()
        body = md_text
        if body.startswith("---\n"):
            parts = body.split("---\n", 2)
            if len(parts) >= 3:
                body = parts[2]
        return body.strip()

    def _file_is_concept_with_sections(self, md_text: str) -> bool:
        """True iff the frontmatter declares `type: concept` AND the body has
        at least one `### Title {#anchor}` section header.

        Concept pages without anchored sections (rare; usually still being
        drafted) are skipped — there's no per-section Sources line to merge
        into.
        """
        head = md_text[:600]
        m = self._FRONTMATTER_TYPE_RE.search(head)
        if not (m and m.group(1).strip() == "concept"):
            return False
        return bool(self._SECTION_HEADER_RE.search(md_text))

    def extract_concept_sections(self, md_text: str) -> list[tuple[str, str]]:
        """Return [(anchor, mechanism_text), ...] for every anchored section
        in a concept-page body.

        For each `### Title {#anchor}` header, the section body runs from the
        line after the header up to (but not including) the next `### .+?
        {#anchor}` header OR the next `## ` heading OR end-of-file. From that
        slice we extract the `**Mechanism**:` block (same regex as atomic
        files); fall back to the slice itself if no Mechanism block is found
        (handles `**Pattern**:` / `**Observation**:` sections like SWF
        `swf-allocation-distribution`).

        Sections without any extractable body are dropped — embedding empty
        strings degenerates cosine to undefined.
        """
        matches = list(self._SECTION_HEADER_RE.finditer(md_text))
        if not matches:
            return []

        # Locate next-section boundaries.
        next_h2_re = re.compile(r"^##\s+", re.MULTILINE)
        boundaries: list[tuple[int, int]] = []
        for i, m in enumerate(matches):
            section_start = m.end()
            if i + 1 < len(matches):
                section_end = matches[i + 1].start()
            else:
                section_end = len(md_text)
            # Truncate further if a top-level `## ` heading appears inside the
            # slice (e.g. a new subsection group); the anchor only owns text
            # up to the next ## heading.
            h2 = next_h2_re.search(md_text, section_start, section_end)
            if h2:
                section_end = h2.start()
            boundaries.append((section_start, section_end))

        out: list[tuple[str, str]] = []
        for (anchor_match, (start, end)) in zip(matches, boundaries):
            anchor = anchor_match.group(1)
            slice_text = md_text[start:end]
            mech_match = self._MECHANISM_RE.search(slice_text)
            if mech_match:
                body = mech_match.group(1).strip()
            else:
                body = slice_text.strip()
            if body:
                out.append((anchor, body))
        return out

    def _file_is_atomic_mechanism(self, md_text: str) -> bool:
        """True iff the file's frontmatter declares `type: mechanism`.

        Theme-index (`type: theme-index`) and master-hub (`type: master-index`)
        pages are deliberately excluded — they hold one-line summary bullets,
        not canonical mechanism content. Matching against summary text
        produced systematic false positives in the 2026-05-24 maintenance
        pass (95% delete rate against one-line theme-index entries).
        """
        head = md_text[:600]
        m = self._FRONTMATTER_TYPE_RE.search(head)
        return bool(m and m.group(1).strip() == "mechanism")

    def load_all_atomic_mechanisms(
        self, *, topic: str | None = None
    ) -> list[tuple[Path, str | None, list[float]]]:
        """Load every atomic mechanism unit in the entire wiki/<topic>/concepts/ tree
        (recursive). `topic` selects the write-base subtree; defaults to `self.topic`.
        Two unit types are returned in one list:

          1. **Standalone atomics** — files with `type: mechanism` frontmatter.
             anchor is None; embedding is over the `**Mechanism**:` block.
          2. **Concept-page sections** — `### Title {#anchor}` blocks inside
             files with `type: concept` frontmatter. anchor is the slug;
             embedding is over the section's `**Mechanism**:`/`**Pattern**:`
             block (or whole slice if neither matches).

        Theme-indexes and master-hubs (`type: theme-index` / `type: master-index`)
        are skipped — their one-line summary bullets produced systematic false
        positives in the 2026-05-24 maintenance pass (95% delete rate).

        Embeddings are persisted in the embed cache (see `_get_embed_db()`)
        keyed by absolute path (or `<path>#<anchor>` for sections) + sha256(mechanism_text)
        + model_name.
        """
        import sys

        topic = topic or self.topic
        if self.wiki_root is None:
            return []

        concepts_root = self.wiki_root / topic / "concepts"
        if not concepts_root.exists():
            return []

        ec, db_path = self._get_embed_db()
        ec.init_db(db_path)

        # Pass 1: walk concepts, collect (path, anchor, mechanism_text) units,
        # plus the cached embedding if its sha256 matches. Bulk-load the whole
        # cache once from SQLite — much faster than per-file SELECTs.
        cache = ec.load_all_atomics(model_name=self.EMBED_MODEL_NAME, db_path=db_path)
        to_embed: list[tuple[str, str]] = []  # (cache_key, mechanism_text)
        placeholders: list[tuple[Path, str | None, str, str, list[float] | None]] = []
        # entry: (path, anchor, cache_key, mechanism_text, cached_embedding_or_None)

        for f in sorted(concepts_root.rglob("*.md")):
            try:
                text = f.read_text()
            except Exception:
                continue

            units: list[tuple[str | None, str]] = []
            if self._file_is_atomic_mechanism(text):
                units.append((None, self.extract_mechanism_text(text)))
            elif self._file_is_concept_with_sections(text):
                units.extend(self.extract_concept_sections(text))
            else:
                continue

            for anchor, mech in units:
                if not mech:
                    continue
                cache_key = str(f) if anchor is None else f"{f}{self._SECTION_KEY_SEP}{anchor}"
                sha = self._text_sha256(mech)
                entry = cache.get(cache_key)
                if entry and entry[0] == sha and self._valid_embedding(entry[1]):
                    placeholders.append((f, anchor, cache_key, mech, entry[1]))
                else:
                    placeholders.append((f, anchor, cache_key, mech, None))
                    to_embed.append((cache_key, mech))

        # Pass 2: batch-embed all uncached texts in one model call, then bulk-
        # upsert into SQLite in a single transaction.
        if to_embed:
            print(
                f"embedding {len(to_embed)} new/changed atomic mechanism(s) "
                f"(cached: {len(placeholders) - len(to_embed)})…",
                file=sys.stderr,
            )
            vectors = self.embed_texts([t for _, t in to_embed])
            new_rows: list[tuple[str, str, list[float]]] = []
            new_idx = 0
            out: list[tuple[Path, str | None, list[float]]] = []
            for path, anchor, cache_key, mech, cached_vec in placeholders:
                if cached_vec is not None:
                    out.append((path, anchor, cached_vec))
                else:
                    vec = vectors[new_idx]
                    new_idx += 1
                    new_rows.append((cache_key, self._text_sha256(mech), vec))
                    out.append((path, anchor, vec))
            ec.put_many_atomics(new_rows, model_name=self.EMBED_MODEL_NAME, db_path=db_path)
        else:
            out = [(p, a, v) for p, a, _, _, v in placeholders if v is not None]

        return out

    def _locate_section_slice(self, text: str, anchor: str) -> tuple[int, int] | None:
        """Return (start, end) byte offsets covering the body of the
        `### Title {#anchor}` section — from the line after the header to the
        next `### .+? {#...}` header or next `## ` heading or end-of-file.

        Returns None if the anchor isn't found.
        """
        header_re = re.compile(
            rf"^###\s+.+?\s*\{{#{re.escape(anchor)}\}}\s*$", re.MULTILINE
        )
        header = header_re.search(text)
        if not header:
            return None
        start = header.end()

        next_section_re = re.compile(r"^###\s+.+?\{#[a-z0-9-]+\}", re.MULTILINE)
        nxt = next_section_re.search(text, start)
        end = nxt.start() if nxt else len(text)

        h2_re = re.compile(r"^##\s+", re.MULTILINE)
        h2 = h2_re.search(text, start, end)
        if h2:
            end = h2.start()
        return start, end

    def _candidate_text_for(self, md_text: str, anchor: str | None) -> str:
        """Mechanism text of the matched dedup candidate, for the judge.

        Standalone atomic (anchor None) → the file's `**Mechanism**` block.
        Concept-section match → the matched `{#anchor}` section's text (so the judge
        sees the EXACT unit that scored the cosine hit, not the file's first block).
        Falls back to the file's mechanism block if the anchor isn't found. Never raises.
        """
        if anchor is None:
            return self.extract_mechanism_text(md_text)
        for anc, text in self.extract_concept_sections(md_text):
            if anc == anchor:
                return text
        return self.extract_mechanism_text(md_text)

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
