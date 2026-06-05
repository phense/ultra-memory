"""detect_dedup — embed-cosine dedup over precomputed vectors (move-with-config).

For each NEW atomic, find its single best candidate among all other atomics by cosine
similarity, then classify against the schema's dedup band:

  cosine >= ``schema.dedup_upper`` (0.86)  → greyzone-dedup, priority 1 (auto-merge)
  cosine >= ``schema.dedup_lower`` (0.78)  → greyzone-dedup, priority 3 (grey-zone pair)
  cosine <  ``schema.dedup_lower``         → dropped (no item)

Pure / fastembed-free on the hot path: the vectors, the cosine function, and the
``text_of`` reader are all injected. The orchestrator binds the real cosine
(``retrieval_core.cosine``) and the real vectors (loaded from the engine's embedding
cache). Atomics without a cached vector are counted + reported, never embedded here.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def run(
    w: dict,
    *,
    new_atomics: list[str],
    vecs: dict[str, tuple[str | None, list[float]]],
    text_of,                      # callable: path -> str
    cosine=None,                  # callable(vec, vec) -> float; default retrieval_core.cosine
    schema: WikiSchemaConfig | None = None,
    signal_vecs: dict | None = None,   # path -> (sha|None, vec) for the ## Signal channel
) -> None:
    """Populate worklist *w* with dedup findings for each new atomic.

    *vecs* maps ``path -> (sha|None, vector)`` for every atomic with a cached vector.
    *cosine* defaults to the engine's pure-python cosine (lazy import keeps this module
    importable without the retrieval extra). The candidate selection breaks an EXACT
    cosine tie on the lexicographically-smaller path so the result is independent of the
    cache's row order.
    """
    schema = schema or WikiSchemaConfig()
    signal_vecs = signal_vecs or {}
    if cosine is None:
        from ultra_memory.retrieval_core import cosine as cosine  # lazy
    lower = schema.dedup_lower
    upper = schema.dedup_upper

    atomics_without_vec: list[str] = []

    for new_path in new_atomics:
        if new_path not in vecs:
            atomics_without_vec.append(new_path)
            continue

        _, new_vec = vecs[new_path]
        new_sig = signal_vecs.get(new_path)  # (sha|None, vec) or None

        best_path: str | None = None
        best_cosine: float = 0.0
        for other_path, (_, other_vec) in vecs.items():
            if other_path == new_path:
                continue
            sim = cosine(new_vec, other_vec)
            # Recall-Reflex signal axis: if BOTH carry a ## Signal vector, a high
            # signal cosine can lift the pair (same observable, different prose).
            # Take the max so a strong match on EITHER axis surfaces.
            other_sig = signal_vecs.get(other_path)
            if new_sig is not None and other_sig is not None:
                sig = cosine(new_sig[1], other_sig[1])
                if sig > sim:
                    sim = sig
            # Strict > keeps the existing selection; the tie clause determinizes an
            # EXACT cosine tie on path (vecs has no inherent order). Smaller path wins.
            if sim > best_cosine or (
                sim == best_cosine and best_path is not None and other_path < best_path
            ):
                best_cosine = sim
                best_path = other_path

        if best_cosine < lower or best_path is None:
            continue

        if best_cosine >= upper:
            priority = 1
            evidence = f"auto-merge candidate; cosine={best_cosine:.4f}"
        else:
            priority = 3
            evidence = f"grey-zone pair; cosine={best_cosine:.4f}"

        wl.add_item(
            w,
            kind="greyzone-dedup",
            atomic_path=new_path,
            title=Path(new_path).stem,
            claim=text_of(new_path),
            candidate_path=best_path,
            candidate_text=text_of(best_path),
            cosine=round(best_cosine, 6),
            evidence=evidence,
            priority=priority,
            kinds=schema.kinds,
        )

    if atomics_without_vec:
        count = len(atomics_without_vec)
        print(
            f"[detect_dedup] WARNING: {count} new atomic(s) have no cached vector "
            f"and were skipped: {atomics_without_vec[:5]}"
            + (" ..." if count > 5 else ""),
            file=sys.stderr,
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="detect_dedup (generic wiki-maintenance).")
    ap.add_argument("--worklist", required=True,
                    help="Stage-1 worklist JSON (from detect_scope) to read new_atomics from.")
    ap.add_argument("--out", required=True, help="Output worklist JSON path.")
    args = ap.parse_args(argv)

    existing = wl.read_worklist(Path(args.worklist))
    new_atomics: list[str] = existing.get("new_atomics", [])
    if not new_atomics:
        print("[detect_dedup] no new atomics — nothing to do.", file=sys.stderr)
        wl.write_worklist(existing, Path(args.out))
        return 0

    # The CLI path has no embedding cache wired in (that is the orchestrator's job);
    # without vectors dedup is a no-op passthrough. Kept for parity with the other
    # detectors' standalone CLIs.
    def text_of(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return ""

    run(existing, new_atomics=new_atomics, vecs={}, text_of=text_of)
    wl.finalize(existing)
    wl.write_worklist(existing, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
