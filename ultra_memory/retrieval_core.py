"""Right-sized retrieval primitives (spec §8, D11).

Pure and dependency-light: cosine + RRF in stdlib math; vectors (de)serialised
with struct. The real embedder (fastembed bge-small-en-v1.5, 384d) is an OPTIONAL
extra, lazy-imported in default_embedder(), so unit tests never load a model —
they inject a tiny fake embedder instead.
"""
import hashlib
import math
import struct

EMBED_MODEL = "bge-small-en-v1.5"
EMBED_DIM = 384


def cosine(a, b):
    """Cosine similarity of two equal-length float vectors. 0.0 if either is zero."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def cosine_search(query_vec, items, *, top_k=None):
    """Rank (id, vector) items by cosine to query_vec. Returns [(id, score)] desc."""
    scored = [(item_id, cosine(query_vec, vec)) for item_id, vec in items]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored if top_k is None else scored[:top_k]


def rrf_fuse(rankings, *, k=60):
    """Reciprocal-rank fusion of multiple ranked id-lists. Returns [(id, score)] desc.

    Built here for the wiki side; Phase-1 memory retrieval is cosine-only (D11).
    """
    scores = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda t: t[1], reverse=True)


def pack_vector(vec):
    """Serialise a float vector to a compact float32 blob."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob, dim=EMBED_DIM):
    """Inverse of pack_vector for a known dim."""
    return list(struct.unpack(f"{dim}f", blob))


def content_sha256(text):
    """Stable content hash for embedding-cache invalidation. None/'' → hash of ''."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_or_embed(conn, *, target_kind, target_id, text, embedder,
                 model_name=EMBED_MODEL, dim=EMBED_DIM):
    """Return the cached embedding for (kind,id,model), recomputing iff the content
    hash changed. Enforces the (model_name, dim) invariant. Miss-path is one short
    write txn (spec §6). `embedder` is list[str] -> list[list[float]]."""
    sha = content_sha256(text)
    row = conn.execute(
        "SELECT dim, vector, content_sha256 FROM embeddings "
        "WHERE target_kind=? AND target_id=? AND model_name=?",
        (target_kind, target_id, model_name),
    ).fetchone()
    if row is not None:
        if row["dim"] != dim:
            raise ValueError(
                f"(model,dim) invariant broken for {target_kind}:{target_id} "
                f"— cached dim {row['dim']} != requested {dim}")
        if row["content_sha256"] == sha:
            return unpack_vector(row["vector"], dim)
    vec = embedder([text])[0]
    if len(vec) != dim:
        raise ValueError(f"embedder returned dim {len(vec)} != expected {dim}")
    blob = pack_vector(vec)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO embeddings "
            "(target_kind, target_id, model_name, dim, vector, content_sha256) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(target_kind, target_id, model_name) DO UPDATE SET "
            "dim=excluded.dim, vector=excluded.vector, content_sha256=excluded.content_sha256",
            (target_kind, target_id, model_name, dim, blob, sha),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return [float(x) for x in vec]


def default_embedder(model_name="BAAI/bge-small-en-v1.5"):
    """Lazy fastembed embedder. Optional extra: install ultra-memory[retrieval].

    Returns list[str] -> list[list[float]]. Not exercised by unit tests (they inject
    a fake) — keeps the model download off the test path."""
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "fastembed not installed; install the 'retrieval' extra "
            "(uv pip install -e '.[retrieval]') or inject an embedder"
        ) from exc
    model = TextEmbedding(model_name=model_name)

    def _embed(texts):
        return [[float(x) for x in v] for v in model.embed(list(texts))]

    return _embed
