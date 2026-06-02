"""Project-agnostic wiki write-gateway. A consumer subclasses WikiGateway and
overrides only the project-specific hooks (route/theme_for/render_frontmatter/
dedup_check/derive_anchor/confidence_label). The base provides correct, simple,
no-LLM defaults so a pure install is turnkey."""
from __future__ import annotations
import contextlib
import fcntl
import json
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ultra_memory.redact_secrets import strip_secrets
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].rstrip("-") or "untitled"


class WikiGateway:
    """Project-agnostic wiki write-gateway. A consumer subclasses this and overrides
    ONLY the project-specific hooks below; everything else — the verb materializers
    (create_page / append_validation_log_entry / register_in_theme_index / log), the
    embedding+cosine machinery, the fcntl write-lock, secret redaction (strip_secrets),
    and the audit row — is inherited and MUST NOT be re-implemented. Wire a subclass in
    `<project>/.ultra-memory/config.toml` as `wiki_gateway = "<module>:<Class>"` (unset →
    this built-in turnkey gateway). Generate a starter subclass with
    `python -m ultra_memory.wiki_gateway scaffold`."""

    # ── embedding constants ──
    EMBED_DIM: int = 384  # BAAI/bge-small-en-v1.5 native dim
    EMBED_MODEL_NAME: str = "BAAI/bge-small-en-v1.5"

    # ── write-lock constants ──
    _WIKI_LOCK_FILENAME: str = ".wiki-write.lock"
    _WIKI_LOCK_TIMEOUT_S: float = 10.0
    _WIKI_LOCK_POLL_S: float = 0.02

    def __init__(self, *, wiki_root: Path | None = None, topic: str = "default",
                 schema: WikiSchemaConfig | None = None):
        self.wiki_root = Path(wiki_root) if wiki_root else None
        self.topic = topic
        self.schema = schema or WikiSchemaConfig()
        self._embed_model = None  # lazy-loaded per instance
        # Per-(thread, lock_path) reentrancy state: {(thread_id, lock_path): [fd, depth]}
        self._wiki_lock_state: dict[tuple[int, str], list] = {}
        # Per-batch redaction counter. Reset at ingest() entry (the batch boundary);
        # each content-write primitive increments it via _redact when it actually scrubs.
        self._redactions_this_batch: int = 0
        # Audit log path: a FILE path (test override) or a DIRECTORY (default).
        # _emit_audit writes one wiki-writes-<date>.jsonl per day under a directory.
        if self.wiki_root is not None:
            self.audit_log_path: Path = (
                self.wiki_root.parent / "briefings" / "maintenance-logs"
            )
        else:
            self.audit_log_path = Path("briefings") / "maintenance-logs"

    # ── reentrant write-lock ────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _wiki_write_lock(self):
        """Advisory exclusive lock over this gateway's wiki tree's markdown
        read-modify-writes.

        Cross-process via ``fcntl.flock`` on ``<wiki_root>/.wiki-write.lock``;
        reentrant within a single thread via a depth counter.

        Reentrancy: flock is per-open-file-description, so a second
        flock(LOCK_EX) on a different fd of the same file FROM THE SAME PROCESS
        would deadlock. The verbs nest (register_in_theme_index →
        _wire_theme_index_into_topic_index → _wire_topic_master_into_master_over_masters),
        so the lock is made reentrant per-thread via a depth counter: the OS
        flock is taken once on the outermost entry of a thread and released on
        its outermost exit; inner re-entries are no-ops.

        Fails OPEN on: no wiki_root, contention timeout, or any flock error
        (stderr diagnostic, proceeds unlocked). The gateway is fail-open
        everywhere — a write is never wedged.
        """
        import sys

        # wiki_root=None → skip the lock entirely (fail-open).
        if self.wiki_root is None:
            yield
            return

        lock_path = str(self.wiki_root / self._WIKI_LOCK_FILENAME)
        key = (threading.get_ident(), lock_path)
        state = self._wiki_lock_state.get(key)
        if state is not None:
            # Reentrant inner acquire on this thread — no-op, just bump depth.
            state[1] += 1
            try:
                yield
            finally:
                state[1] -= 1
                if state[1] <= 0:
                    self._wiki_lock_state.pop(key, None)
            return

        fd = None
        acquired = False
        try:
            self.wiki_root.mkdir(parents=True, exist_ok=True)
            fd = open(lock_path, "a+")
            deadline = time.monotonic() + self._WIKI_LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        print(
                            f"warning: wiki write-lock busy >{self._WIKI_LOCK_TIMEOUT_S}s "
                            f"({lock_path}); proceeding unlocked (fail-open)",
                            file=sys.stderr,
                        )
                        break
                    time.sleep(self._WIKI_LOCK_POLL_S)
        except Exception as e:  # fail-open: never wedge a write on a lock-setup error
            print(
                f"warning: wiki write-lock unavailable ({e}); proceeding unlocked",
                file=sys.stderr,
            )
            acquired = False

        self._wiki_lock_state[key] = [fd, 1]
        try:
            yield
        finally:
            self._wiki_lock_state.pop(key, None)
            if fd is not None:
                try:
                    if acquired:
                        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    fd.close()
                except Exception:
                    pass

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

    # ── semantic dedup + overlap ────────────────────────────────────────────────

    # Per-instance cache of all atomic mechanisms (populated lazily by
    # `load_all_atomic_mechanisms`; cleared by `_clear_atomics_cache`).
    _all_atomics_cache: "list[tuple[Path, str | None, list[float]]] | None" = None

    def find_overlap_match(
        self,
        claim_text: str,
        theme_dir: "Path",
        threshold: float,
    ) -> "tuple[Path, str | None, float] | None":
        """Return (best_path, anchor, cosine_similarity) if any atomic mechanism
        unit in the wiki concepts tree has cosine(embed(claim_text), embed(unit))
        >= threshold; else None.

        Two unit types are scanned (see `load_all_atomic_mechanisms`):
          - standalone atomic files (anchor = None)
          - ``{#anchor}`` sections inside type: concept hub pages

        Scans GLOBALLY across all theme directories, not just the target theme.
        The ``theme_dir`` parameter is preserved for signature compatibility but
        no longer constrains the scan scope.

        Theme-index and master-hub pages are excluded.
        """
        if not claim_text or not claim_text.strip():
            return None
        claim_vec = self.embed_with_cache(claim_text)  # transient — not cached by file path
        best: "tuple[Path, str | None, float] | None" = None
        for path, anchor, atomic_vec in self.load_all_atomic_mechanisms():
            sim = self.cosine_sim(claim_vec, atomic_vec)
            if sim >= threshold and (best is None or sim > best[2]):
                best = (path, anchor, sim)
        return best

    def _find_in_flight_match(
        self,
        claim_vec: "list[float]",
        in_flight: "list[tuple[list[float], Path]]",
        threshold: float,
    ) -> "tuple[Path, float] | None":
        """Return (atomic_path, similarity) for the best match in ``in_flight``
        if best similarity >= threshold; else None.

        ``in_flight`` holds (claim_vec, atomic_path) tuples for proposals
        already accumulated for the current video / batch.
        """
        best: "tuple[Path, float] | None" = None
        for other_vec, other_path in in_flight:
            sim = self.cosine_sim(claim_vec, other_vec)
            if sim >= threshold and (best is None or sim > best[1]):
                best = (other_path, sim)
        return best

    def judge_route(
        self,
        sim: float,
        claim_text: str,
        candidate_text: str,
        *,
        judge_enabled: bool,
        resolve_fn: "Any",
        cosine_floor: float | None = None,
    ) -> bool:
        """Decide whether a best-cosine on-disk candidate should be MERGED against.

        judge disabled  → legacy pure-cosine: merge iff sim >= cosine_floor.
        judge enabled   → sim >= dedup_upper: auto-merge (no judge call);
                          dedup_lower <= sim < dedup_upper: resolve_fn decides
                            (verdict 'same' → merge, 'different' → no merge);
                          sim < dedup_lower: no merge.
        resolve_fn(claim_text, candidate_text, sim) -> {"verdict": "same"|"different", ...}

        ``cosine_floor`` defaults to ``self.schema.dedup_lower`` when None.
        Keep ``resolve_fn`` consumer-injected — no LLM in the base.
        """
        if cosine_floor is None:
            cosine_floor = self.schema.dedup_lower
        if not judge_enabled:
            return sim >= cosine_floor
        if sim >= self.schema.dedup_upper:
            return True
        if sim < self.schema.dedup_lower:
            return False
        return resolve_fn(claim_text, candidate_text, sim)["verdict"] == "same"

    def _log_borderline_if_needed(
        self,
        kind: str,
        sim: float,
        threshold: float,
        source: "dict[str, Any]",
        claim_text: str,
        target: str,
        verdict: "str | None" = None,
    ) -> None:
        """Append a line to the borderline log if ``sim`` falls in the
        borderline band ``[threshold, dedup_upper)``. No-op otherwise.

        ``kind`` is "on-disk" or "in-flight"; ``verdict`` is the judge decision
        when the judge tier decided this borderline match.
        """
        import sys
        from datetime import datetime, timezone

        borderline_upper = self.schema.dedup_upper
        if not (threshold <= sim < borderline_upper):
            return
        try:
            if self.wiki_root is not None:
                log_path = (
                    self.wiki_root.parent
                    / "briefings"
                    / "maintenance-logs"
                    / f"ingest-borderline-merges-{datetime.now(timezone.utc).date().isoformat()}.log"
                )
            else:
                return  # no wiki_root → skip
            log_path.parent.mkdir(parents=True, exist_ok=True)
            snippet = claim_text.replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "…"
            verdict_col = f"\tverdict={verdict}" if verdict is not None else ""
            line = (
                f"{datetime.now(timezone.utc).isoformat()}\t{kind}\tsim={sim:.3f}\t"
                f"video={source.get('channel_name', '?')}/{source.get('video_id', '?')}\t"
                f"target={target}\tclaim={snippet}{verdict_col}\n"
            )
            with log_path.open("a") as fh:
                fh.write(line)
        except Exception as e:
            print(f"warning: failed to write borderline log: {e}", file=sys.stderr)

    def dedup_cross_video(
        self,
        proposals_new: "list[dict[str, Any]]",
        proposals_merge: "list[dict[str, Any]]",
        threshold: float,
    ) -> int:
        """Post-pass over ``proposals_new`` to demote cross-video paraphrase clones.

        Closes the gap between on-disk and in-flight dedup: when two videos produce
        paraphrases of the same mechanism and no canonical atomic exists yet on disk,
        both pass ``find_overlap_match`` AND ``_find_in_flight_match``. This function
        runs once after process_files() returns, with all proposals_new visible.

        Restricted to same-theme. Mutates ``proposals_new`` (removes demoted) and
        ``proposals_merge`` (appends demoted) in place. Returns count of demoted.
        """
        if len(proposals_new) < 2:
            return 0

        vecs = [self.embed_with_cache(p["claim"]["claim"]) for p in proposals_new]

        surviving: "list[dict[str, Any]]" = []
        surviving_vecs: "list[list[float]]" = []
        demoted = 0

        for p, v in zip(proposals_new, vecs):
            best_idx = -1
            best_sim = 0.0
            for i, sp in enumerate(surviving):
                if sp["theme"] != p["theme"]:
                    continue
                sim = self.cosine_sim(v, surviving_vecs[i])
                if sim >= threshold and sim > best_sim:
                    best_idx = i
                    best_sim = sim

            if best_idx >= 0:
                canonical = surviving[best_idx]
                proposals_merge.append({
                    "atomic_path": canonical["atomic_path"],
                    "section_anchor": None,
                    "claim": p["claim"],
                    "source": p["source"],
                    "theme": canonical["theme"],
                    "title": canonical["title"],
                    "overlap_score": best_sim,
                    "overlap_reason": f"embedding-cosine-cross-video (BAAI/bge-small-en-v1.5, sim={best_sim:.2f})",
                })
                self._log_borderline_if_needed(
                    kind="cross-video",
                    sim=best_sim,
                    threshold=threshold,
                    source=p["source"],
                    claim_text=p["claim"]["claim"],
                    target=str(canonical["atomic_path"]),
                )
                demoted += 1
            else:
                surviving.append(p)
                surviving_vecs.append(v)

        proposals_new[:] = surviving
        return demoted

    def _disambiguate_anchor(
        self, base_anchor: str, claim_text: str, colliding_path: "Path"
    ) -> "tuple[str, bool]":
        """Resolve a disambiguation anchor for a DISTINCT claim that landed on an
        already-taken atomic_path (a derive_anchor 4-hex collision).

        Returns ``(anchor, is_idempotent_hit)``:
          - ``is_idempotent_hit`` is False → anchor is a FRESH, free sibling.
          - ``is_idempotent_hit`` is True → anchor names an EXISTING sibling whose
            Mechanism text MATCHES ``claim_text`` (re-ingest of an already-
            disambiguated claim); the caller must SKIP, not mint a duplicate.

        Deterministic: walks successive 2-hex slices of SHA-1 of the claim text.
        """
        import hashlib

        incoming = (claim_text or "").strip()
        digest = hashlib.sha1(claim_text.encode("utf-8"), usedforsecurity=False).hexdigest()
        for width in (2, 4, 6, len(digest)):
            for i in range(0, max(1, len(digest) - width + 1)):
                disc = digest[i: i + width]
                candidate = f"{base_anchor}-{disc}"
                cand_path = colliding_path.with_name(f"{candidate}.md")
                if not cand_path.exists():
                    return candidate, False  # fresh sibling
                # Occupied — check if content matches (idempotent re-ingest).
                try:
                    existing_mech = self.extract_mechanism_text(cand_path.read_text())
                except Exception:
                    existing_mech = ""
                if existing_mech.strip() == incoming:
                    return candidate, True  # idempotent: already on disk, identical
                # Differing content — keep walking.
        # Pathological fallback: full digest is unique.
        return f"{base_anchor}-{digest}", False

    # ── redaction chokepoint + audit row (Task 7 / D7 / §15) ───────────────────

    def _redact(self, text: str) -> str:
        """Strip credential-shaped substrings before any wiki write (mandatory chokepoint).

        Secret-free prose passes through unchanged (strip_secrets is conservative), so the
        golden corpus is byte-identical. Counts each write that DID change into the
        per-batch counter for the audit line. The counter is only meaningful within an
        ingest()-bounded batch (ingest resets it); a primitive called outside ingest
        accumulates without reset and emits no audit row.
        """
        cleaned = strip_secrets(text)
        if cleaned != text:
            self._redactions_this_batch += 1
        return cleaned

    def _emit_audit(
        self,
        op: str,
        source_label: str,
        redactions: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one structured JSON row per gateway write/batch (§15 heartbeat).

        Row = {ts, op, source_label, redactions, **detail}. ``op`` names the operation
        ("ingest", "validation-log", "create-page", "log"); ``detail`` carries op-specific
        fields (e.g. {new_written, merged_added} for ingest, {page} for validation-log,
        {path} for create-page). ``detail`` keys MUST NOT shadow the reserved keys
        ts/op/source_label/redactions.
        Honors both a ``.jsonl`` FILE audit_log_path (test override) and a DIRECTORY (default).
        """
        target = (
            self.audit_log_path
            if str(self.audit_log_path).endswith(".jsonl")
            else self.audit_log_path / f"wiki-writes-{date.today().isoformat()}.jsonl"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        # FIX 5 (r2-bughunt): source_label (CLI --source-label) and detail (the
        # agent-supplied --path/--page) are agent-settable, so a credential-shaped
        # value would otherwise land VERBATIM in wiki-writes-*.jsonl — unlike every
        # content-write primitive, which scrubs via _redact. Route them through
        # strip_secrets here too. Note: use strip_secrets directly (NOT _redact) so the
        # audit-line scrub does not perturb the per-batch redaction COUNTER, which only
        # tracks content writes.
        safe_source_label = strip_secrets(source_label)
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "source_label": safe_source_label,
            "redactions": redactions,
        }
        if detail:
            row.update({
                k: (strip_secrets(v) if isinstance(v, str) else v)
                for k, v in detail.items()
            })
        with target.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    # ── write primitives + master-over-masters wiring (verb materializers) ──────

    # Section headers for the standard wiki structure.
    _VALIDATION_LOG_HEADER: str = "## Empirical Validation Log"
    _TOPIC_INDEX_THEME_SECTION: str = "## Theme indexes"
    _MASTER_TOPIC_SECTION: str = "## Topic masters"
    _AUTO_ADDED_HEADER: str = "### Recently auto-added (uncategorized)"
    _AUTO_ADDED_HINT: str = (
        "*Entries below were direct-written by the wiki gateway. "
        "Maintenance: move each into the proper topical section above and "
        "recalibrate `[Confidence]` if needed.*"
    )

    # Templates for auto-created pages.
    _THEME_INDEX_FRONTMATTER_TMPL: str = """\
---
type: theme-index
title: {title}
tags: [{theme}]
created: {today}
updated: {today}
---

# {title}

"""
    _THEME_INDEX_BODY_STUB: str = (
        "*Auto-created theme index. Add topical sections above and move bullets from "
        "`### Recently auto-added` as the index matures.*\n"
    )
    _TOPIC_INDEX_TMPL: str = """\
---
type: master-index
title: {title}
tags: [{topic}]
created: {today}
updated: {today}
---

# {title}

Topic root index. Theme-indexes link here; atomic pages link to their theme-index.
"""
    _MASTER_OVER_MASTERS_TMPL: str = (
        "# Wiki Index\n\n"
        "The master index of this wiki — a **master-over-masters**: it links each topic's "
        "master index (`wiki/<topic>/index.md`), never theme-indexes or atomics directly. "
        "Browse top-down: this file -> a topic master -> a theme-index -> the one atomic page.\n\n"
        "{section}\n\n"
    )

    @staticmethod
    def _require_under(path: Path, *roots: Path) -> Path:
        """Resolve `path` and assert it lives under at least one of `roots` (resolved).
        Raises ValueError otherwise. The single path-escape guard for agent-supplied paths."""
        rp = Path(path).resolve()
        for root in roots:
            try:
                rp.relative_to(Path(root).resolve())
                return rp
            except ValueError:
                continue
        raise ValueError(f"path {path} is not under any of {[str(r) for r in roots]}")

    def _topic_root(self, topic: str | None = None) -> Path:
        """The content root for `topic`: ``<wiki_root>/<topic>``.

        In the base class, topic is always a subdirectory of wiki_root.
        Subclasses can override for multi-layer (project/global) routing.
        """
        t = topic or self.topic
        if self.wiki_root is None:
            return Path(t)
        return self.wiki_root / t

    @staticmethod
    def _append_to_section(text: str, header: str, entry: str, *, hint: str | None = None) -> str:
        """Append `entry` at the end of the `header` section of `text`, creating the
        section (optionally with a one-line `hint` under the header) if it's missing.
        """
        if header in text:
            header_idx = text.index(header)
            after = text[header_idx + len(header):]
            next_section = re.search(r"\n(##? )", after)
            insertion_offset = (
                header_idx + len(header) + (next_section.start() if next_section else len(after))
            )
            prefix = text[:insertion_offset].rstrip() + "\n"
            suffix = text[insertion_offset:]
            return prefix + entry + suffix
        section = "\n\n" + f"{header}\n\n" + (f"{hint}\n\n" if hint else "") + f"{entry}"
        return text.rstrip() + section

    def create_atomic(self, path: Path, content: str) -> str:
        """Write a new atomic mechanism file, redacting secrets at the boundary.

        The ONLY mkdir site in the wiki-content write surface — sibling
        observability outputs (borderline log, audit log) mkdir separately.
        Routes content through _redact so a credential leaked into a claim never lands on disk.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._redact(content))
        return "written"

    def _wire_topic_master_into_master_over_masters(
        self, topic: str, *, master_root: Path | None = None
    ) -> None:
        """Link the topic master ``[[<topic>/index]]`` into ``<wiki_root>/index.md``.

        Creates the master-over-masters if absent. Appends ``- [[<topic>/index]]``
        under ``## Topic masters`` (creating the section if missing). Idempotent.
        """
        base = master_root if master_root is not None else self.wiki_root
        if base is None:
            return
        master = base / "index.md"
        self._require_under(master, base)

        link = f"[[{topic}/index]]"
        entry = f"- {link}\n"

        with self._wiki_write_lock():
            if not master.exists():
                master.parent.mkdir(parents=True, exist_ok=True)
                master.write_text(self._redact(
                    self._MASTER_OVER_MASTERS_TMPL.format(
                        section=f"{self._MASTER_TOPIC_SECTION}\n\n{entry}"
                    )
                ))
                return

            text = master.read_text()
            if link in text:
                return  # idempotent: already linked

            new_text = self._append_to_section(text, self._MASTER_TOPIC_SECTION, entry)
            master.write_text(self._redact(new_text))

    def _wire_theme_index_into_topic_index(
        self, theme: str, topic: str, root: Path
    ) -> None:
        """Link the theme-index ``[[<slug(theme)>-index]]`` into ``<root>/index.md``.

        Creates the topic master if absent (then wires it into the
        master-over-masters). Appends ``- [[<slug(theme)>-index]]`` under
        ``## Theme indexes``. Idempotent.
        """
        topic_index = root / "index.md"
        self._require_under(topic_index, root)

        with self._wiki_write_lock():
            if not topic_index.exists():
                today = date.today().isoformat()
                title = topic.replace("-", " ").replace("_", " ").title()
                topic_index.parent.mkdir(parents=True, exist_ok=True)
                topic_index.write_text(self._redact(
                    self._TOPIC_INDEX_TMPL.format(title=title, topic=topic, today=today)
                ))
            # Wire the topic master into the master-over-masters (idempotent).
            self._wire_topic_master_into_master_over_masters(topic)

            text = topic_index.read_text()
            link = f"[[{slugify(theme)}-index]]"
            if link in text:
                return  # idempotent

            entry = f"- {link}\n"
            new_text = self._append_to_section(text, self._TOPIC_INDEX_THEME_SECTION, entry)
            new_text = re.sub(
                r"^updated:\s*\S+",
                f"updated: {date.today().isoformat()}",
                new_text,
                count=1,
                flags=re.MULTILINE,
            )
            topic_index.write_text(self._redact(new_text))

    def theme_index_path(
        self, theme: str, wiki_root: Path | None = None, *, topic: str | None = None
    ) -> Path:
        """Return the canonical path for a theme-index file.

        ``<root>/concepts/<slugify(theme)>-index.md``
        """
        t = topic or self.topic
        root = wiki_root if wiki_root is not None else self._topic_root(t)
        return root / "concepts" / f"{slugify(theme)}-index.md"

    def append_to_theme_index(
        self,
        index_path: Path,
        anchor: str,
        theme: str,
        title: str,
        claim: dict[str, Any],
        confidence_label: str,
    ) -> str:
        """Idempotently append a one-line entry to the theme-index's
        `### Recently auto-added (uncategorized)` section. Creates the section if missing.

        Returns "added", "already-listed", or "error".
        """
        with self._wiki_write_lock():
            try:
                text = index_path.read_text()
            except Exception:
                return "error"

            wikilink = f"[[{theme}/{anchor}]]"
            if wikilink in text:
                return "already-listed"

            claim_text = (claim.get("claim") or "").strip()
            first_sentence = re.split(r"(?<=[.!?])\s+", claim_text, maxsplit=1)[0]
            if len(first_sentence) > 200:
                first_sentence = first_sentence[:197] + "…"

            entry = (
                f"- **{anchor}** — {title} — {first_sentence} "
                f"**[{confidence_label}]** → {wikilink}\n"
            )

            new_text = self._append_to_section(
                text, self._AUTO_ADDED_HEADER, entry, hint=self._AUTO_ADDED_HINT
            )

            today_str = date.today().isoformat()
            new_text = re.sub(
                r"^updated:\s*\S+",
                f"updated: {today_str}",
                new_text,
                count=1,
                flags=re.MULTILINE,
            )

            index_path.write_text(self._redact(new_text))
            return "added"

    def register_in_theme_index(
        self,
        atomic_slug: str,
        summary: str,
        theme: str,
        *,
        topic: str | None = None,
        wiki_root: Path | None = None,
    ) -> None:
        """Register a new atomic under the ``<slugify(theme)>-index.md`` theme-index.

        Creates the index and wires it into the topic master if it doesn't exist.
        Idempotent on the wikilink.
        """
        t = topic or self.topic
        root = wiki_root if wiki_root is not None else self._topic_root(t)
        target = self.theme_index_path(theme, root, topic=t)
        self._require_under(target, root)

        with self._wiki_write_lock():
            created_index = False
            if not target.exists():
                today = date.today().isoformat()
                title = slugify(theme).replace("-", " ").title()
                page_body = (
                    self._THEME_INDEX_FRONTMATTER_TMPL.format(
                        title=title, theme=slugify(theme), today=today
                    )
                    + self._THEME_INDEX_BODY_STUB
                    + "\n\n"
                    + self._AUTO_ADDED_HEADER
                    + "\n\n"
                    + self._AUTO_ADDED_HINT
                    + "\n"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(self._redact(page_body))
                created_index = True
                self._wire_theme_index_into_topic_index(theme, t, root)

            text = target.read_text()
            wikilink = f"[[{atomic_slug}]]"
            if wikilink in text:
                self._emit_audit(
                    "register-theme-index",
                    "wiki_gateway/register_in_theme_index",
                    self._redactions_this_batch,
                    {"theme": theme, "atomic_slug": atomic_slug, "result": "already-listed"},
                )
                return

            entry = f"- {wikilink} — {summary}\n"
            new_text = self._append_to_section(
                text, self._AUTO_ADDED_HEADER, entry, hint=self._AUTO_ADDED_HINT
            )

            today_str = date.today().isoformat()
            new_text = re.sub(
                r"^updated:\s*\S+",
                f"updated: {today_str}",
                new_text,
                count=1,
                flags=re.MULTILINE,
            )

            target.write_text(self._redact(new_text))
            self._emit_audit(
                "register-theme-index",
                "wiki_gateway/register_in_theme_index",
                self._redactions_this_batch,
                {
                    "theme": theme,
                    "atomic_slug": atomic_slug,
                    "index_created": created_index,
                    "result": "added",
                },
            )

    def append_validation_log_entry(
        self,
        page: Path,
        entry: str,
        *,
        topic: str | None = None,
        wiki_root: Path | None = None,
    ) -> str:
        """Append a (redacted) strategy-tagged entry to the page's
        `## Empirical Validation Log` section, creating the section if missing and
        bumping frontmatter `updated:`. Returns "added" or "already-logged".
        Raises ValueError if `page` is not under `wiki_root` or does not exist.
        """
        root = wiki_root if wiki_root is not None else self.wiki_root
        if root is None:
            root = Path(".")
        page = self._require_under(page, root)
        if not page.exists():
            raise ValueError(f"validation-log target page does not exist: {page}")
        with self._wiki_write_lock():
            text = page.read_text()
            entry_line = entry if entry.endswith("\n") else entry + "\n"
            redacted_entry = self._redact(entry_line).strip()
            if redacted_entry and redacted_entry in text:
                return "already-logged"
            new_text = self._append_to_section(
                text, self._VALIDATION_LOG_HEADER, entry_line
            )
            today_str = date.today().isoformat()
            new_text = re.sub(
                r"^updated:\s*\S+",
                f"updated: {today_str}",
                new_text,
                count=1,
                flags=re.MULTILINE,
            )
            page.write_text(self._redact(new_text))
            return "added"

    def create_page(
        self,
        path: Path,
        content: str,
        *,
        topic: str | None = None,
        wiki_root: Path | None = None,
    ) -> str:
        """Create a NEW concepts/ or synthesis/ page from agent-authored content.
        Returns "written". Raises ValueError if `path` is not under `root`
        or already exists (never clobbers).

        `path` may be absolute or relative. Relative paths are resolved relative to
        `root` (the effective `wiki_root` parameter), so a scaffold stub may call
        ``create_page(Path("topic/concepts/x.md"), ..., wiki_root=raw_wiki_root)``.
        """
        t = topic or self.topic
        root = wiki_root if wiki_root is not None else self._topic_root(t)
        # Resolve relative paths against root (not cwd) so scaffold stubs and callers
        # that pass the raw wiki_root + a topic-relative path work correctly.
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        path = self._require_under(path, root)
        if path.exists():
            raise ValueError(f"create-page refuses to clobber existing page: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._redact(content))
        return "written"

    def log_line(self, message: str, *, wiki_root: Path | None = None) -> str:
        """Append a single (redacted) human-readable line to `<root>/log.md`.
        Returns "no-log" when log.md is absent; "added" on success.
        """
        root = wiki_root if wiki_root is not None else self.wiki_root
        if root is None:
            return "no-log"
        log_path = root / "log.md"
        if not log_path.exists():
            return "no-log"
        today_str = date.today().isoformat()
        line = f"\n## [{today_str}] {self._redact(message).strip()}\n"
        with self._wiki_write_lock():
            with log_path.open("a") as fh:
                fh.write(line)
        return "added"

    def resolve_atomic_path(
        self, routed_page: str, claim: dict[str, Any], *, topic: str | None = None
    ) -> "tuple[Path, str]":
        """Return (atomic_path, theme_slug) for a routed page + claim.

        Base implementation: uses `route` hook to derive the canonical path.
        Subclasses override this method for Trading-specific anchor/theme logic.
        """
        t = topic or self.topic
        routed = self.route(claim)
        anchor = slugify(claim.get("title") or claim.get("text") or "untitled")
        theme = self.theme_for(claim)
        atomic_path = self._topic_root(t) / "concepts" / theme / f"{anchor}.md"
        return atomic_path, theme

    def append_video_to_sources(
        self,
        existing_path: Path,
        claim: dict[str, Any],
        source: dict[str, Any],
        section_anchor: str | None = None,
    ) -> str:
        """Public wrapper for appending a video citation to an existing atomic file.

        The base implementation is a no-op stub — the video-citation format is
        Trading-specific (``channel_name``, ``video_id`` fields). A subclass
        (TradingWikiGateway) overrides ``_append_video_to_sources_locked`` with
        the actual RMW logic. Returns "no-sources" by default.
        """
        with self._wiki_write_lock():
            return self._append_video_to_sources_locked(
                existing_path, claim, source, section_anchor
            )

    def _append_video_to_sources_locked(
        self,
        existing_path: Path,
        claim: dict[str, Any],
        source: dict[str, Any],
        section_anchor: str | None = None,
    ) -> str:
        """Inner (locked) body of append_video_to_sources.

        Base returns "no-sources" — overridden in TradingWikiGateway.
        """
        return "no-sources"

    def integrate_to_wiki(
        self,
        proposals_new: "list[dict[str, Any]]",
        proposals_merge: "list[dict[str, Any]]",
        dry_run: bool = False,
        *,
        topic: str | None = None,
        wiki_root: Path | None = None,
    ) -> "dict[str, int]":
        """Apply all proposals directly to the wiki.

        For each NEW: write the atomic file (via create_atomic + render_frontmatter hook);
        append a one-line entry to the theme-index.
        For each MERGE: call append_video_to_sources (override hook).

        Returns a stats dict with counters.
        """
        import sys as _sys

        t = topic or self.topic
        root = wiki_root if wiki_root is not None else self.wiki_root

        stats: dict[str, Any] = {
            "new_written": 0,
            "new_skipped_existing": 0,
            "merged_added": 0,
            "merged_already_cited": 0,
            "merged_no_sources": 0,
            "index_updated": 0,
            "index_already_listed": 0,
            "index_missing": 0,
            "errors": 0,
            "anchor_collisions_disambiguated": 0,
            "orphans": [],
        }

        for p in proposals_new:
            atomic_path: Path = p["atomic_path"]
            anchor: str = p.get("anchor", slugify(p.get("title", "untitled")))
            if atomic_path.exists():
                try:
                    existing_mech = self.extract_mechanism_text(atomic_path.read_text())
                except Exception:
                    existing_mech = ""
                incoming_mech = (p["claim"].get("claim") or "").strip()
                if existing_mech.strip() == incoming_mech:
                    stats["new_skipped_existing"] += 1
                    print(
                        f"warning: atomic already exists, skipping: {atomic_path}",
                        file=_sys.stderr,
                    )
                    continue
                anchor, is_idempotent_hit = self._disambiguate_anchor(
                    anchor, incoming_mech, atomic_path
                )
                atomic_path = atomic_path.with_name(f"{anchor}.md")
                if is_idempotent_hit:
                    stats["new_skipped_existing"] += 1
                    continue
                stats["anchor_collisions_disambiguated"] += 1

            # Use render_frontmatter hook (override point) for frontmatter dict,
            # then format a simple generic body. Subclasses override render_frontmatter
            # OR override integrate_to_wiki entirely for richer bodies.
            fm = self.render_frontmatter(p["claim"])
            fm_lines = "\n".join(f"{k}: {v}" for k, v in fm.items())
            content = f"---\n{fm_lines}\n---\n\n{p['claim'].get('claim', '')}\n"

            if dry_run:
                print(f"[dry-run] would write: {atomic_path}")
            else:
                try:
                    self.create_atomic(atomic_path, content)
                except Exception as e:
                    stats["errors"] += 1
                    print(f"error: failed to write {atomic_path}: {e}", file=_sys.stderr)
                    continue
            stats["new_written"] += 1

        for p in proposals_merge:
            existing_path: Path = p["atomic_path"]
            if dry_run:
                stats["merged_added"] += 1
                continue

            result = self.append_video_to_sources(
                existing_path, p["claim"], p["source"],
                section_anchor=p.get("section_anchor"),
            )
            if result == "added":
                stats["merged_added"] += 1
            elif result == "already-cited":
                stats["merged_already_cited"] += 1
            elif result == "no-sources":
                stats["merged_no_sources"] += 1
            else:
                stats["errors"] += 1

        return stats

    def ingest(
        self,
        proposals_new: "list[dict[str, Any]]",
        proposals_merge: "list[dict[str, Any]]",
        *,
        dry_run: bool = False,
        dedup_threshold: float = 0.83,
        source_label: str = "wiki_gateway",
        topic: str | None = None,
        wiki_root: Path | None = None,
    ) -> "dict[str, int]":
        """Run cross-video dedup -> integrate -> audit in the correct order.

        Resets the per-batch redaction counter; runs dedup_cross_video before
        integrate_to_wiki (C4 contract); emits an audit row regardless of dry_run.
        """
        self._redactions_this_batch = 0
        self.dedup_cross_video(proposals_new, proposals_merge, dedup_threshold)
        stats = self.integrate_to_wiki(
            proposals_new, proposals_merge, dry_run=dry_run,
            topic=topic, wiki_root=wiki_root,
        )
        self._emit_audit(
            "ingest", source_label, self._redactions_this_batch,
            {
                "new_written": stats.get("new_written", 0),
                "merged_added": stats.get("merged_added", 0),
            },
        )
        return stats

    # ── override points (simple, no-LLM defaults) ──
    def route(self, claim: dict[str, Any]) -> Path:
        """Where a new page lands on disk. DEFAULT: `<topic>/concepts/<slug(title)>.md`.
        Called by the base when materializing a new atomic. Override to route by theme,
        subdir, or a custom convention. Return a `Path` relative to the wiki root."""
        title = claim.get("title") or claim.get("text") or "untitled"
        return Path(self.topic) / self.schema.atomics_subdir / f"{slugify(title)}.md"

    def theme_for(self, claim: dict[str, Any]) -> str:
        """The theme-index a new atomic registers under. DEFAULT: `claim["theme"]` or
        "general". Called by `register_in_theme_index`. Override to derive the theme from
        tags/content. Return a str."""
        return claim.get("theme") or "general"

    def render_frontmatter(self, claim: dict[str, Any]) -> dict:
        """The YAML frontmatter dict for a new page. DEFAULT: `{"type":"mechanism",
        "title": claim["title"]}`. Called by the base before writing the page body.
        Override to add fields (tags, sources, dates). Return a dict."""
        return {"type": "mechanism", "title": claim.get("title", "untitled")}

    def dedup_check(self, text: str, topic: str):
        """Semantic dedup-on-write. DEFAULT: OFF (returns None → always create). Called
        before materializing a new page. Override to turn dedup on, e.g.
        `return self.find_overlap_match(text, Path(), 0.85)` (the embedding machinery is
        inherited). Return a match (to merge instead of create) or None."""
        return None  # OFF by default; an override turns on embedding cosine

    def derive_anchor(self, claim: dict[str, Any], existing) -> str | None:
        """A stable in-page section anchor (for concept pages with `### … {#anchor}`
        sections). DEFAULT: None (standalone atomic, no anchor). Called when routing a
        claim into an existing concept page. Override to mint a deterministic anchor.
        Return a str or None."""
        return None

    def confidence_label(self, claim: dict[str, Any]) -> str:
        """A confidence tag rendered on the page (e.g. the source's reliability).
        DEFAULT: "Standard". Called by the render path. Override to map speaker/validity
        to your own labels. Return a str."""
        return "Standard"


# ── scaffold generator ─────────────────────────────────────────────────────────

_SCAFFOLD_TEMPLATE = '''\
"""A custom WikiGateway extension for the {topic!r} knowledge wiki.

Wire it in <project>/.ultra-memory/config.toml:
    [maintenance]
    wiki_gateway = "{module}:{class_name}"   # or leave unset for the built-in turnkey gateway

Generated by `python -m ultra_memory.wiki_gateway scaffold`. Override ONLY the hooks
that differ from the defaults; the verb materializers, secret redaction, the write-lock,
and the audit row are INHERITED from WikiGateway (do not re-implement them)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ultra_memory.wiki_gateway import WikiGateway, cli


class {class_name}(WikiGateway):
    """{topic}-specific wiki write-gateway. See `python -m ultra_memory.wiki_gateway`
    + the `using-wiki-gateway` skill for the override contract."""

    def route(self, claim: dict[str, Any]) -> Path:
        # Where a new page lands. DEFAULT: <topic>/concepts/<slug(title)>.md
        # TODO: customize, or delete this method to keep the default.
        return super().route(claim)

    def theme_for(self, claim: dict[str, Any]) -> str:
        # The theme-index a new atomic registers under. DEFAULT: claim["theme"] or "general".
        # TODO: customize or delete.
        return super().theme_for(claim)

    def render_frontmatter(self, claim: dict[str, Any]) -> dict:
        # The YAML frontmatter for a new page. DEFAULT: {{"type": "mechanism", "title": ...}}.
        # TODO: customize or delete.
        return super().render_frontmatter(claim)

    def dedup_check(self, text: str, topic: str):
        # Semantic dedup-on-write. DEFAULT: OFF (None) → always create.
        # TODO: turn on, e.g. `return self.find_overlap_match(text, Path(), 0.85)`, or delete.
        return super().dedup_check(text, topic)

    def derive_anchor(self, claim: dict[str, Any], existing=None) -> str | None:
        # A stable in-page section anchor. DEFAULT: None.
        # TODO: customize or delete.
        return super().derive_anchor(claim, existing)

    def confidence_label(self, claim: dict[str, Any]) -> str:
        # A confidence tag rendered on the page. DEFAULT: "Standard".
        # TODO: customize or delete.
        return super().confidence_label(claim)


if __name__ == "__main__":
    raise SystemExit(cli({class_name}))
'''


def render_scaffold(*, class_name: str = "MyWikiGateway", topic: str = "mytopic",
                    module: str | None = None) -> str:
    """Render a ready-to-edit WikiGateway subclass stub (deterministic, no LLM)."""
    return _SCAFFOLD_TEMPLATE.format(
        class_name=class_name, topic=topic, module=module or "mymodule")


def scaffold_to_file(out_path, *, class_name: str = "MyWikiGateway",
                     topic: str = "mytopic") -> None:
    """Write the scaffold to *out_path*; the module name is derived from the filename stem."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_scaffold(class_name=class_name, topic=topic, module=out_path.stem),
        encoding="utf-8")


# ── argparse CLI (verb wire contract: the maintenance beats shell out to this) ─

def _cli_read_content(args) -> str:
    """Read verb content from --from-file (preferred) or --message."""
    if getattr(args, "from_file", None):
        return Path(args.from_file).read_text()
    return getattr(args, "message", None) or ""


def cli(gateway_cls=None, argv=None, *, wiki_root: Path | None = None) -> int:
    """Dispatch a wiki gateway CLI verb.

    ``gateway_cls`` defaults to ``WikiGateway`` (the turnkey base). Subclasses
    pass their own class so the consumer CLI shim is just::

        if __name__ == "__main__":
            from ultra_memory.wiki_gateway import cli
            raise SystemExit(cli(TradingWikiGateway))

    ``wiki_root`` lets tests inject a temp directory without needing a real wiki
    tree on disk; when None (production path) it falls back to:
      - the ``--wiki-root`` CLI arg if provided, else
      - the default resolved by the gateway's ``_topic_root()`` / ``log_line()`` logic.
    """
    import argparse
    import sys

    if gateway_cls is None:
        gateway_cls = WikiGateway

    # shared_parent carries --wiki-root so each subparser inherits it cleanly
    # (argparse's parents= mechanism avoids registering the same arg N times).
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--wiki-root",
        default=None,
        dest="wiki_root",
        help="Wiki root directory (overrides auto-resolved default).",
    )

    parser = argparse.ArgumentParser(
        prog="wiki_gateway",
        description="Audited wiki write gateway — agent CLI verbs.",
    )
    # --gateway-class is a top-level flag (before the subcommand) that lets the
    # maintenance beats invoke a consumer subclass through the built-in CLI.
    # Form: "module:Class" — e.g. "wiki_lib:TradingWikiGateway".  The module is
    # imported with <project_dir>/scripts + <project_dir> prepended to sys.path
    # (the same sys.path setup as _resolve_hook in wiki_curate.py).
    parser.add_argument(
        "--gateway-class",
        default=None,
        dest="gateway_class",
        metavar="MODULE:CLASS",
        help=(
            "Consumer gateway subclass to bind, e.g. 'wiki_lib:TradingWikiGateway'. "
            "The module is imported with the scripts/ directory on sys.path."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── create-page ──────────────────────────────────────────────────────────
    p_page = sub.add_parser(
        "create-page",
        parents=[shared],
        help="Create a new wiki/concepts|synthesis page from a file.",
    )
    p_page.add_argument("--path", required=True, help="Destination path for the new page.")
    p_page.add_argument("--topic", default="default", help="Topic (e.g. 'trading').")
    p_page.add_argument("--from-file", required=True, dest="from_file",
                        help="Path to a file whose contents become the page body.")
    p_page.add_argument("--source-label", default="wiki_gateway-cli")

    # ── append-validation-log ────────────────────────────────────────────────
    p_vlog = sub.add_parser(
        "append-validation-log",
        parents=[shared],
        help="Append an entry to a page's ## Empirical Validation Log.",
    )
    p_vlog.add_argument("--page", required=True, help="Path to the wiki page.")
    p_vlog.add_argument("--from-file", required=True, dest="from_file",
                        help="Path to a file whose contents become the log entry.")
    p_vlog.add_argument("--topic", default="default")
    p_vlog.add_argument("--source-label", default="wiki_gateway-cli")

    # ── register-index ───────────────────────────────────────────────────────
    p_ridx = sub.add_parser(
        "register-index",
        parents=[shared],
        help="Register an atomic slug under its theme's <slug(theme)>-index.md.",
    )
    p_ridx.add_argument("--slug", required=True, help="Atomic slug to register, e.g. real-yields-4827.")
    p_ridx.add_argument("--theme", required=True, help="Theme string, e.g. macro/monetary.")
    p_ridx.add_argument("--summary", required=True, help="One-line summary for the bullet entry.")
    p_ridx.add_argument("--topic", default="default")
    p_ridx.add_argument("--source-label", default="wiki_gateway-cli")

    # ── log ──────────────────────────────────────────────────────────────────
    p_log = sub.add_parser(
        "log",
        parents=[shared],
        help="Append a human run-summary line to wiki/log.md.",
    )
    g = p_log.add_mutually_exclusive_group(required=True)
    g.add_argument("--message", help="Log message string.")
    g.add_argument("--from-file", dest="from_file", help="Path to a file whose contents are the log message.")
    p_log.add_argument("--source-label", default="wiki_gateway-cli")

    # ── scaffold ─────────────────────────────────────────────────────────────
    sp_scaffold = sub.add_parser("scaffold", help="Emit a ready-to-edit WikiGateway subclass stub.")
    sp_scaffold.add_argument("--out", required=True, help="output .py path")
    sp_scaffold.add_argument("--class-name", default="MyWikiGateway")
    sp_scaffold.add_argument("--topic", default="mytopic")

    args = parser.parse_args(argv)

    # --gateway-class overrides the gateway_cls argument (the beats pass this flag
    # when they resolved a "module:Class" spec via _resolve_gateway).
    if getattr(args, "gateway_class", None):
        spec = args.gateway_class
        import importlib
        mod_name, _, cls_name = spec.partition(":")
        if mod_name and cls_name:
            # Prepend scripts/ and project dir to sys.path so an in-tree module
            # is importable (same setup as _resolve_hook in wiki_curate.py).
            import os
            project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd())))
            for p in (str(project_dir / "scripts"), str(project_dir)):
                if p not in sys.path:
                    sys.path.insert(0, p)
            try:
                resolved_cls = getattr(importlib.import_module(mod_name), cls_name)
                gateway_cls = resolved_cls
            except Exception as exc:
                print(
                    f"warning: --gateway-class could not load {spec!r}: {exc!r}; "
                    "falling back to WikiGateway",
                    file=sys.stderr,
                )

    if args.cmd == "scaffold":
        scaffold_to_file(args.out, class_name=args.class_name, topic=args.topic)
        print(f"scaffolded {args.class_name}(WikiGateway) → {args.out}")
        return 0

    # Resolve wiki_root: explicit kwarg > --wiki-root arg > None (gateway default).
    resolved_root: Path | None = wiki_root
    if resolved_root is None and getattr(args, "wiki_root", None):
        resolved_root = Path(args.wiki_root)

    topic = getattr(args, "topic", "default")
    gw = gateway_cls(wiki_root=resolved_root, topic=topic)
    # Emit audit rows into wiki_root/../briefings/maintenance-logs or the gateway default.

    # NOTE on wiki_root vs topic root: the gateway verb methods (create_page,
    # register_in_theme_index, etc.) treat their `wiki_root` parameter as the
    # TOPIC root (i.e. <wiki_root>/<topic>), not the raw wiki root. Since we
    # already initialized the gateway with self.wiki_root=resolved_root, the
    # methods' default (wiki_root=None) correctly calls self._topic_root(topic)
    # = self.wiki_root/topic. So we pass wiki_root=None to let the gateway
    # handle the resolution — we only set resolved_root on the instance.

    if args.cmd == "create-page":
        gw._redactions_this_batch = 0
        path = Path(args.path)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            content = _cli_read_content(args)
            gw.create_page(path, content, topic=topic)
            gw._emit_audit(
                "create-page",
                getattr(args, "source_label", "wiki_gateway-cli"),
                gw._redactions_this_batch,
                {"path": str(path)},
            )
            print(f"created page {path}")
            return 0
        except Exception as e:
            print(f"error: create-page failed: {e}", file=sys.stderr)
            return 1

    if args.cmd == "append-validation-log":
        gw._redactions_this_batch = 0
        page = Path(args.page)
        if not page.is_absolute():
            page = Path.cwd() / page
        try:
            entry = _cli_read_content(args)
            result = gw.append_validation_log_entry(page, entry, topic=topic)
            gw._emit_audit(
                "validation-log",
                getattr(args, "source_label", "wiki_gateway-cli"),
                gw._redactions_this_batch,
                {"page": str(page), "result": result},
            )
            if result == "already-logged":
                print(f"validation-log entry already present in {page} (idempotent no-op)")
            else:
                print(f"appended validation-log entry to {page}")
            return 0
        except Exception as e:
            print(f"error: append-validation-log failed: {e}", file=sys.stderr)
            return 1

    if args.cmd == "register-index":
        gw._redactions_this_batch = 0
        try:
            gw.register_in_theme_index(
                args.slug,
                args.summary,
                args.theme,
                topic=topic,
            )
            gw._emit_audit(
                "register-index",
                getattr(args, "source_label", "wiki_gateway-cli"),
                gw._redactions_this_batch,
                {"slug": args.slug, "theme": args.theme},
            )
            print(f"registered [[{args.slug}]] in theme-index for '{args.theme}'")
            return 0
        except Exception as e:
            print(f"error: register-index failed: {e}", file=sys.stderr)
            return 1

    if args.cmd == "log":
        gw._redactions_this_batch = 0
        try:
            message = _cli_read_content(args)
            result = gw.log_line(message)
            gw._emit_audit(
                "log",
                getattr(args, "source_label", "wiki_gateway-cli"),
                gw._redactions_this_batch,
            )
            print(f"appended wiki/log.md line ({result})")
            return 0
        except Exception as e:
            print(f"error: log failed: {e}", file=sys.stderr)
            return 1

    return 2


def main(argv=None) -> int:
    """Entry point for ``python -m ultra_memory.wiki_gateway``.

    Dispatches to the built-in ``WikiGateway`` CLI. To use a consumer subclass,
    call ``cli(TradingWikiGateway)`` directly in the consumer's ``__main__``::

        if __name__ == "__main__":
            from ultra_memory.wiki_gateway import cli
            raise SystemExit(cli(TradingWikiGateway))
    """
    return cli(WikiGateway, argv)


if __name__ == "__main__":
    raise SystemExit(main())
