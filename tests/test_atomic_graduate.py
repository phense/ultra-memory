"""Atomic Graduation (Recall-Reflex 5.2) — the fenced atomic_graduate beat.

The drain is DETERMINISTIC (no LLM); gateway_run / signal_match / recall_fn / embedder
are injected (production binds the real ones; here, recorders/stubs).

As of the 2026-06-05 cluster-dedup refactor the unit of work is a CLUSTER, not a single
candidate: candidates + the existing on-disk `## Signal` (`knowledge_signal`) seed vectors
are greedily union-find clustered at `cluster_cos` (default 0.80, env-tunable). A cluster
that contains an existing seed MERGES (one validation-log entry on the nearest seed page);
a seedless cluster CREATEs ONE atomic (representative = longest body) and resolves the rest
as clustered. The eval-gate, blast-radius cap, kill-switch, topic-normalization, and
per-cluster fail-open are all preserved. With NO embedder the pass falls open to the legacy
per-candidate three-way dedup-gate (clustering needs vectors).
"""
import struct
from pathlib import Path

from ultra_memory import memory_lib, retrieval_core
from ultra_memory.maintenance import atomic_graduate as ag
from ultra_memory.maintenance import session_ingest as si

TS = "2026-06-05T00:00:00"


def _db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "m.db"))


def _seed(conn, **over):
    c = {"kind": "gotcha", "signal": "onnxruntime NoSuchFile model.onnx",
         "title": "fastembed cache purge", "body": "pin via persistent_cache_dir",
         "topic": "trading"}
    c.update(over)
    si._save_atomic_candidates(conn, [c], session_id="S1", ts=TS)


def _gw():
    calls = []

    def gw(verb, args, content):
        calls.append((verb, list(args), content))
    gw.calls = calls
    return gw


# --------------------------------------------------------------------------- #
# A controllable fake embedder. Each text is mapped to a unit 384-d vector that
# lies along a chosen "axis" so cosine between two texts is fully determined by
# their axes: same axis -> cosine 1.0 (will cluster); different orthogonal axes ->
# cosine 0.0 (will NOT cluster). `axis_of(text)` maps a text to an int axis; by
# default each distinct text gets its own axis (everything is distinct/orthogonal).
# Tests override `axis_of` to engineer clusters.
# --------------------------------------------------------------------------- #
def _embedder(axis_of=None):
    seen = {}

    def axis(t):
        if axis_of is not None:
            return axis_of(t)
        if t not in seen:
            seen[t] = len(seen)
        return seen[t]

    def embed(texts):
        out = []
        for t in texts:
            v = [0.0] * retrieval_core.EMBED_DIM
            v[axis(t) % retrieval_core.EMBED_DIM] = 1.0
            out.append(v)
        return out
    return embed


def _install_seed_page(conn, *, slug, topic, signal, embedder):
    """Make an EXISTING wiki page discoverable as a clustering seed: a unified_index
    row + a `knowledge_signal` vector for its observable (exactly the shape
    best_signal_match / the new seed-loader read)."""
    ag._index_new_page(conn, slug=slug, topic=topic, title=slug, signal=signal,
                       body=f"body for {slug}", embedder=embedder)


# --------------------------------------------------------------------------- #
# Clustering creates: candidate-vs-candidate dedup (spec test-plan #1, #7).
# --------------------------------------------------------------------------- #

def test_near_dup_candidates_create_one_atomic(tmp_path):
    # Two candidates with IDENTICAL-axis signals, no existing seed page → ONE create,
    # the second resolved as clustered (NOT a second atomic). (spec #1)
    conn = _db(tmp_path)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "effort ultracode toggle off",
         "title": "ultracode A", "body": "short body", "topic": "trading"},
        {"kind": "gotcha", "signal": "effort ultracode toggle is off",
         "title": "ultracode B", "body": "a much longer more detailed body here",
         "topic": "trading"}],
        session_id="S1", ts=TS)
    emb = _embedder(axis_of=lambda t: 0)   # both signals share one axis → cluster
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3)
    assert res["created"] == 1
    assert res.get("clustered") == 1   # the second is resolved as clustered
    assert [c[0] for c in gw.calls].count("create-page") == 1
    assert si.pending_atomic_candidates(conn) == []   # BOTH resolved, none grey-stuck
    conn.close()


def test_representative_is_longest_body(tmp_path):
    # The created atomic's body is the LONGEST candidate body in the cluster. (spec #7)
    conn = _db(tmp_path)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "rglob hidden dir skip",
         "title": "rglob A", "body": "tiny", "topic": "trading"},
        {"kind": "gotcha", "signal": "rglob hidden directory skipped",
         "title": "rglob B",
         "body": "THE LONGEST BODY: rglob silently skips dotted .hidden dirs unless "
                 "you walk them explicitly — the full reproduction and the fix.",
         "topic": "trading"}],
        session_id="S1", ts=TS)
    emb = _embedder(axis_of=lambda t: 0)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3)
    assert res["created"] == 1
    cp = next(c for c in gw.calls if c[0] == "create-page")
    assert "THE LONGEST BODY" in cp[2]
    conn.close()


def test_distinct_candidates_each_create(tmp_path):
    # Genuinely distinct candidates (orthogonal signals < cluster_cos) → each its own
    # create. (spec #3)
    conn = _db(tmp_path)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "alpha distinct one", "title": "A",
         "body": "body a", "topic": "trading"},
        {"kind": "gotcha", "signal": "beta distinct two", "title": "B",
         "body": "body b", "topic": "trading"}],
        session_id="S1", ts=TS)
    emb = _embedder()   # default: each distinct text → its own orthogonal axis
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3)
    assert res["created"] == 2
    assert [c[0] for c in gw.calls].count("create-page") == 2
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


# --------------------------------------------------------------------------- #
# Clustering merges: candidate-vs-existing-seed dedup (spec test-plan #2, #5).
# --------------------------------------------------------------------------- #

def test_cluster_with_seed_merges_once(tmp_path):
    # Three candidates clustering with an EXISTING seed page → 0 creates, 1 merge
    # (ONE validation-log entry), all three resolved. (spec #2)
    conn = _db(tmp_path)
    emb = _embedder(axis_of=lambda t: 0)   # seed + all candidates share one axis
    _install_seed_page(conn, slug="fastembed-tmpdir", topic="trading",
                       signal="seed observable", embedder=emb)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "cand observable one", "title": "C1",
         "body": "b1", "topic": "trading"},
        {"kind": "gotcha", "signal": "cand observable two", "title": "C2",
         "body": "b2", "topic": "trading"},
        {"kind": "gotcha", "signal": "cand observable three", "title": "C3",
         "body": "b3", "topic": "trading"}],
        session_id="S1", ts=TS)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw,
        signal_match=lambda *a, **k: None, wiki_root=tmp_path / "wiki",
        embedder=emb, cap=3)
    assert res["created"] == 0 and res["merged"] == 1
    avl = [c for c in gw.calls if c[0] == "append-validation-log"]
    assert len(avl) == 1
    assert "n=3" in avl[0][2]   # the merge entry records the cluster size
    assert si.pending_atomic_candidates(conn) == []   # all three resolved
    conn.close()


def test_merge_targets_nearest_seed(tmp_path):
    # Two seed pages; the candidate clusters with both but is NEAREST one → that page
    # gets the validation-log entry.
    conn = _db(tmp_path)
    # near seed at axis 0; far seed at axis 1. candidate at axis 0 → clusters with both
    # only if within cos; we make the candidate share axis 0 with near, and add a small
    # axis-0 tilt to far so it also clusters but at lower cosine.

    def axis_of(t):
        if "FAR" in t:
            return 1
        return 0
    emb = _embedder(axis_of=axis_of)
    # near seed (axis 0, cosine 1.0 to candidate)
    _install_seed_page(conn, slug="near-seed", topic="trading",
                       signal="near seed observable", embedder=emb)
    # far seed: give it a vector that's a blend so it clusters but at lower cosine.
    nv = emb(["candidate observable"])[0]
    fv = [0.0] * retrieval_core.EMBED_DIM
    fv[0] = 0.9
    fv[1] = (1 - 0.81) ** 0.5
    _write_signal_vec(conn, slug="far-seed", topic="trading", vec=fv)
    _write_index_row(conn, slug="far-seed", topic="trading")
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "candidate observable", "title": "C",
         "body": "b", "topic": "trading"}], session_id="S1", ts=TS)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3)
    assert res["merged"] == 1 and res["created"] == 0
    avl = next(c for c in gw.calls if c[0] == "append-validation-log")
    page_arg = avl[1][avl[1].index("--page") + 1]
    assert "near-seed" in page_arg   # nearest (cosine 1.0) wins over far-seed
    conn.close()


def test_no_grey_stuck(tmp_path):
    # A 0.81-vs-seed candidate (the OLD grey-zone SKIP) now CLUSTERS with the seed and
    # MERGES — it is resolved, never perpetually skipped. (spec #5)
    conn = _db(tmp_path)
    emb = _embedder()
    # Seed vector at axis 0; candidate vector at cosine ~0.81 to it (above 0.80).
    sv = [0.0] * retrieval_core.EMBED_DIM
    sv[0] = 1.0
    _write_signal_vec(conn, slug="grey-seed", topic="trading", vec=sv)
    _write_index_row(conn, slug="grey-seed", topic="trading")
    cv = [0.0] * retrieval_core.EMBED_DIM
    cv[0] = 0.81
    cv[1] = (1 - 0.81 ** 2) ** 0.5

    def emb2(texts):
        return [cv for _ in texts]
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "grey zone candidate", "title": "G",
         "body": "b", "topic": "trading"}], session_id="S1", ts=TS)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb2, cap=3)
    assert res["merged"] == 1 and res["created"] == 0 and res["skipped"] == 0
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


# --------------------------------------------------------------------------- #
# Blast-radius cap (spec test-plan #4).
# --------------------------------------------------------------------------- #

def test_blast_radius_cap_on_seedless_clusters(tmp_path):
    # N seedless clusters with cap=2 → 2 creates, the rest unresolved for next run.
    conn = _db(tmp_path)
    for i in range(5):
        _seed(conn, signal=f"distinct signal number {i}", title=f"candidate {i}",
              body=f"body {i}")
    emb = _embedder()   # all distinct/orthogonal → 5 seedless clusters
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=2)
    assert res["created"] == 2
    assert len(si.pending_atomic_candidates(conn)) == 3   # rest unresolved
    conn.close()


def test_cap_does_not_limit_merges(tmp_path):
    # Merges are NOT capped (additive/safe). cap=1, but 3 seed-clusters all merge.
    conn = _db(tmp_path)
    for i in range(3):
        slug = f"seed-{i}"
        _write_signal_vec(conn, slug=slug, topic="trading",
                          vec=_axis_vec(i))
        _write_index_row(conn, slug=slug, topic="trading")
        si._save_atomic_candidates(conn, [
            {"kind": "gotcha", "signal": f"cand for {i}", "title": f"C{i}",
             "body": "b", "topic": "trading"}], session_id="S1", ts=TS)

    # Seed vectors are injected directly above; the embedder only embeds candidate
    # signals — "cand for i" lands on seed-i's axis so each candidate clusters with its
    # own seed (3 seed-clusters, all merge — even at cap=1, since merges aren't capped).
    def emb(texts):
        out = []
        for t in texts:
            # "cand for 0/1/2" → axis 0/1/2
            ax = next((i for i in range(3) if t.endswith(str(i))), 99)
            out.append(_axis_vec(ax))
        return out
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=1)
    assert res["merged"] == 3 and res["created"] == 0
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


# --------------------------------------------------------------------------- #
# Eval-gate preserved (findable-or-quarantine).
# --------------------------------------------------------------------------- #

_SLUG = ag._slugify("fastembed cache purge", "onnxruntime NoSuchFile model.onnx")


def test_eval_gate_keeps_findable(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    emb = _embedder()
    gw = _gw(); q = []
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3,
        recall_fn=lambda signal, **k: [{"slug": _SLUG}],   # the new slug IS findable
        index_fn=lambda **k: None, quarantine_fn=lambda path: q.append(path))
    assert res["created"] == 1 and res["quarantined"] == 0 and q == []
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


def test_eval_gate_quarantines_unfindable(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    emb = _embedder()
    gw = _gw(); q = []
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3,
        recall_fn=lambda signal, **k: [{"slug": "some-other-page"}],   # new slug NOT present
        index_fn=lambda **k: None, quarantine_fn=lambda path: q.append(path))
    assert res["created"] == 1 and res["quarantined"] == 1 and len(q) == 1
    assert si.pending_atomic_candidates(conn) == []   # resolved (page exists, quarantined)
    conn.close()


# --------------------------------------------------------------------------- #
# Kill-switch, fail-open, topic-normalization (preserved invariants).
# --------------------------------------------------------------------------- #

def test_disabled_by_killswitch(tmp_path):
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={"ATOMIC_GRADUATE_DISABLE": "1"}, gateway_run=gw,
        signal_match=lambda *a, **k: None, wiki_root=tmp_path / "wiki",
        embedder=_embedder(), cap=3)
    assert res["mode"] == "disabled" and gw.calls == []
    assert len(si.pending_atomic_candidates(conn)) == 1
    conn.close()


def test_per_cluster_fail_open(tmp_path):
    # A cluster whose create raises leaves THAT cluster unresolved, but does not abort
    # the pass nor lose the other (distinct) cluster.
    conn = _db(tmp_path)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "boom signal", "title": "BOOM",
         "body": "b", "topic": "trading"},
        {"kind": "gotcha", "signal": "ok signal", "title": "OK",
         "body": "b", "topic": "trading"}],
        session_id="S1", ts=TS)
    emb = _embedder()

    def gw_selective(verb, args, content):
        if verb == "create-page" and content and "BOOM" in content:
            raise RuntimeError("gateway boom")
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw_selective, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3)
    assert res["created"] == 1   # OK created; BOOM failed open
    pend = si.pending_atomic_candidates(conn)
    assert len(pend) == 1 and pend[0]["title"] == "BOOM"   # only BOOM unresolved → retry
    conn.close()


def test_topic_normalized_to_a_valid_topic(tmp_path):
    # The extraction may put a THEME ("tooling") in the topic field; it must be
    # normalized to a known topic (not create a spurious wiki/tooling/ topic tree).
    conn = _db(tmp_path)
    _seed(conn, topic="tooling")
    emb = _embedder()
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3,
        valid_topics={"trading", "programming"}, default_topic="trading")
    assert res["created"] == 1
    cp = next(c for c in gw.calls if c[0] == "create-page")
    path_arg = cp[1][cp[1].index("--path") + 1]
    topic_arg = cp[1][cp[1].index("--topic") + 1]
    assert "/trading/concepts/" in path_arg and "/tooling/" not in path_arg
    assert topic_arg == "trading"
    conn.close()


def test_clusters_are_topic_scoped(tmp_path):
    # Two candidates with the SAME-axis signal but DIFFERENT normalized topics must NOT
    # cluster (clustering is per-topic, preserving the dedup-gate's topic scope) → two
    # creates.
    conn = _db(tmp_path)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "same axis obs", "title": "T1",
         "body": "b", "topic": "trading"},
        {"kind": "gotcha", "signal": "same axis obs", "title": "P1",
         "body": "b", "topic": "programming"}],
        session_id="S1", ts=TS)
    emb = _embedder(axis_of=lambda t: 0)   # identical axis — would cluster if not scoped
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3,
        valid_topics={"trading", "programming"}, default_topic="trading")
    assert res["created"] == 2   # different topics → separate clusters
    conn.close()


# --------------------------------------------------------------------------- #
# Embedder-None fail-open to the legacy per-candidate three-way dedup-gate.
# --------------------------------------------------------------------------- #

def test_embedder_none_falls_back_to_per_candidate_create(tmp_path):
    # No embedder → clustering impossible → the legacy per-candidate path: a novel
    # candidate (signal_match None) still creates. (spec #6)
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=None, cap=3)
    assert res["created"] == 1
    verbs = [c[0] for c in gw.calls]
    assert "create-page" in verbs and "register-index" in verbs
    cp = next(c for c in gw.calls if c[0] == "create-page")
    assert "## Signal" in cp[2] and "onnxruntime NoSuchFile" in cp[2]
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


def test_embedder_none_per_candidate_merges_on_high_cosine(tmp_path):
    # The legacy per-candidate MERGE branch is still reachable with embedder=None.
    conn = _db(tmp_path); _seed(conn)
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw,
        signal_match=lambda *a, **k: ("existing-slug", 0.95),
        wiki_root=tmp_path / "wiki", embedder=None, cap=3)
    assert res["merged"] == 1 and res["created"] == 0
    assert [c[0] for c in gw.calls] == ["append-validation-log"]
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


def test_embedder_raises_falls_open_and_still_drains(tmp_path):
    # The embedder itself raising (not None) must fall open to the per-candidate path,
    # still draining. (spec #6)
    conn = _db(tmp_path); _seed(conn)

    def boom(texts):
        raise RuntimeError("embed boom")
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=boom, cap=3)
    assert res["created"] == 1
    assert si.pending_atomic_candidates(conn) == []
    conn.close()


# --------------------------------------------------------------------------- #
# Env override of the cluster threshold (spec test-plan #8).
# --------------------------------------------------------------------------- #

def test_cluster_cos_param_respected(tmp_path):
    # With cluster_cos lowered, two candidates at cosine ~0.5 now CLUSTER (one create);
    # at the default 0.80 they would not.
    conn = _db(tmp_path)
    si._save_atomic_candidates(conn, [
        {"kind": "gotcha", "signal": "axis a", "title": "A", "body": "b1",
         "topic": "trading"},
        {"kind": "gotcha", "signal": "axis b", "title": "B", "body": "b2 longer",
         "topic": "trading"}],
        session_id="S1", ts=TS)
    va = _axis_vec(0)
    vb = [0.0] * retrieval_core.EMBED_DIM
    vb[0] = 0.5
    vb[1] = (1 - 0.25) ** 0.5   # cosine(va, vb) = 0.5

    def emb(texts):
        return [va if t == "axis a" else vb for t in texts]
    gw = _gw()
    res = ag.run_atomic_graduate_pass(
        conn, ts=TS, env={}, gateway_run=gw, signal_match=lambda *a, **k: None,
        wiki_root=tmp_path / "wiki", embedder=emb, cap=3, cluster_cos=0.40)
    assert res["created"] == 1   # 0.5 ≥ 0.40 → clustered
    conn.close()


def test_beat_reads_cluster_cos_env(tmp_path, monkeypatch):
    # beat() must thread ATOMIC_GRADUATE_CLUSTER_COS via _float_env into the pass.
    captured = {}

    def fake_pass(conn, **kw):
        captured.update(kw)
        return {"mode": "ran", "created": 0, "merged": 0, "skipped": 0,
                "quarantined": 0}
    monkeypatch.setattr(ag, "run_atomic_graduate_pass", fake_pass)
    conn = _db(tmp_path); _seed(conn)

    class _Cfg:
        topics = ["trading"]
        atomic_graduate_themes = {}
        project_dir = str(tmp_path)
        wiki_gateway = "x"

    # Stub the heavy resolution so beat reaches run_atomic_graduate_pass deterministically.
    import ultra_memory.maintenance.wiki_curate as wc
    monkeypatch.setattr(wc, "_active_roots", lambda config: [str(tmp_path / "wiki")])
    monkeypatch.setattr(wc, "_resolve_gateway", lambda gw, config: ["echo"])
    monkeypatch.setattr(retrieval_core, "default_embedder",
                        lambda: (lambda texts: [[0.0] for _ in texts]))
    ag.beat(conn, _Cfg(), TS, {"ATOMIC_GRADUATE_CLUSTER_COS": "0.66"})
    assert captured.get("cluster_cos") == 0.66
    conn.close()


# --------------------------------------------------------------------------- #
# beat short-circuits.
# --------------------------------------------------------------------------- #

def test_beat_disabled_by_killswitch():
    # Kill-switch short-circuits before touching conn/config.
    assert ag.beat(None, None, TS, {"ATOMIC_GRADUATE_DISABLE": "1"})["mode"] == "disabled"


def test_beat_no_candidates_is_noop(tmp_path):
    conn = _db(tmp_path)   # empty store → beat returns before resolving config/gateway
    res = ag.beat(conn, None, TS, {})
    assert res["mode"] == "ran" and res["created"] == 0
    conn.close()


def test_no_consumer_specific_literals_in_engine():
    """The engine is domain-agnostic: topic + theme come from config, never baked in.
    Mirrors detect_dedup's project-agnostic guard."""
    src = Path(ag.__file__).read_text().lower()
    for bad in ("trading", "tooling", "strategy-methodology", "/users/"):
        assert bad not in src, f"consumer-specific literal {bad!r} leaked into the engine"


# --------------------------------------------------------------------------- #
# Local test helpers for direct vector seeding.
# --------------------------------------------------------------------------- #

def _axis_vec(axis):
    v = [0.0] * retrieval_core.EMBED_DIM
    v[axis % retrieval_core.EMBED_DIM] = 1.0
    return v


def _write_signal_vec(conn, *, slug, topic, vec):
    blob = struct.pack(f"{len(vec)}f", *vec)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings "
        "(target_kind, target_id, model_name, dim, vector, content_sha256) "
        "VALUES ('knowledge_signal', ?, ?, ?, ?, 'x')",
        (slug, retrieval_core.EMBED_MODEL, len(vec), blob))
    conn.commit()


def _write_index_row(conn, *, slug, topic):
    conn.execute(
        "INSERT OR REPLACE INTO unified_index "
        "(slug, topic, page_type, title, snippet, bm25_text, frontmatter, path, "
        "content_sha256, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (slug, topic, "mechanism", slug, "", "", "{}",
         f"/wiki/{topic}/concepts/{slug}.md", "x", ""))
    conn.commit()
