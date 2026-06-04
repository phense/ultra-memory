"""Read-only `knowledge` MCP core (spec §13). No LLM.

The MCP is a **privilege boundary**: an untrusted caller (subagent / cron) may
recall only non-sensitive knowledge (`project`/`reference`), never `user`/
`feedback` memories. Read-path `strip_secrets` is defense-in-depth; every recall
writes an access-log audit row so exfiltration is auditable
(addresses `feedback_subagents_can_leak_secrets` as a TOOL constraint, not prose).

The stdio server (`main`) is a thin wrapper over `knowledge_recall`; all the
testable logic lives in pure functions here.
"""
from . import memory_lib, memory_query
from .redact_secrets import strip_secrets

# Type allowlists per caller class — the privilege boundary (§13).
SAFE_TYPES = ("project", "reference")
ALL_TYPES = ("project", "reference", "user", "feedback")
_TRUSTED = frozenset({"orchestrator", "owner"})

# Sentinel distinguishing "no topic argument supplied" (legacy SP-1 memory-only
# recall) from "topic-scoped recall, all-topics" (`agent_topics=None`). The
# all-topics sentinel is a real, meaningful value (the orchestrator), so it can't
# overload `None` — hence a dedicated sentinel for "argument absent".
_NO_TOPIC_ARG = object()


def allowed_types_for(caller_class):
    """Memory types a caller_class may recall. Trusted (orchestrator/owner) → all;
    everything else (subagent/cron/unknown/None) → SAFE_TYPES (fail-closed)."""
    return ALL_TYPES if caller_class in _TRUSTED else SAFE_TYPES


def filter_links_for_caller(conn, links, *, caller_class):
    """Apply the type wall to a memory row's `links` (the SIDEBAND past the
    primary-row type wall). The type wall gates the PRIMARY row's type but NOT the
    `dst_type`/`dst_id` of edges hanging off an allowed row — so a subagent
    recalling an allowed project/reference memory that links to a user/feedback
    memory would otherwise receive that forbidden memory's id+type.

    A TRUSTED caller (orchestrator/owner) keeps ALL links unchanged. A type-scoped
    caller (subagent/cron/unknown) keeps only edges whose BOTH endpoints
    (src + dst, when the endpoint is a `memory`) resolve to an ALLOWED type.
    FAIL-CLOSED: an endpoint whose type cannot be resolved (a dangling/unknown
    memory id) → the edge is DROPPED. Non-`memory` endpoints (e.g. `knowledge`
    wiki pages) are not type-walled and pass on that side.

    Project-agnostic: resolves the endpoint type via the engine's own `memories`
    table (`SELECT type FROM memories WHERE id=?`), no consumer import.
    """
    if links is None:
        return links
    allowed = set(allowed_types_for(caller_class))
    # The full set ⇒ trusted: nothing to drop (and no per-edge DB hit).
    if allowed >= set(ALL_TYPES):
        return links

    cache = {}

    def _endpoint_allowed(kind, mid, declared_type):
        # FAIL-CLOSED on an ambiguous endpoint kind (R3 bughunt FIX 5): ONLY an
        # EXPLICIT 'knowledge' kind bypasses the type wall (knowledge wiki pages are
        # not the secret-bearing user/feedback rows). A None / missing / unknown kind
        # must NOT be treated as a safe non-memory endpoint and blindly kept — that
        # leaked a forbidden user/feedback dst_id to a type-scoped subagent. Treat any
        # non-'knowledge' kind as a memory endpoint, so it is resolved via the
        # live-row re-read below (which fail-closes on an unresolvable id).
        if kind not in ("memory", "knowledge"):
            kind = "memory"
        # Only `memory` endpoints are type-walled. A 'knowledge' endpoint passes.
        if kind != "memory":
            return True
        # Trust the live row's type over the edge's stored `*_type` (which can be
        # stale / NULL) — re-read fail-closed, mirroring the SP-7 assert_mutable
        # provenance discipline (the stored copy is a hint, the row is the truth).
        if mid in cache:
            t = cache[mid]
        else:
            row = conn.execute(
                "SELECT type FROM memories WHERE id=?", (mid,)).fetchone()
            t = row["type"] if row is not None else None
            cache[mid] = t
        if t is None:
            return False  # unresolvable endpoint → fail-closed (drop)
        return t in allowed

    kept = []
    for edge in links:
        # `links` rows carry dst_kind/dst_id (+ dst_type); src is always this
        # returned memory (`src_kind='memory'`), but guard the src side too if a
        # src_kind/src_id ever travels on the edge dict.
        if not _endpoint_allowed(edge.get("dst_kind"), edge.get("dst_id"),
                                 edge.get("dst_type")):
            continue
        src_kind = edge.get("src_kind", "memory")
        src_id = edge.get("src_id")
        if src_id is not None and not _endpoint_allowed(
                src_kind, src_id, edge.get("src_type")):
            continue
        kept.append(edge)
    return kept


def knowledge_recall(conn, query, *, caller_class, embedder, top_k=5, dim=None,
                     now_ts=None, ts=None, audit=True):
    """Recall memories for `query`, restricted to the caller_class's allowed types.

    Over-fetches then type-filters so the allowlist does not silently shrink the
    result set below `top_k`. Returns JSON-serialisable dicts.
    """
    allowed = set(allowed_types_for(caller_class))
    top_k = max(0, min(int(top_k), 100))
    # Scope by type IN SQL (not only the post-rank filter below) so a sensitive-heavy
    # store can't starve an allowed caller by filling a fixed candidate window with
    # higher-ranked denied rows. The post-filter stays as defense-in-depth.
    kwargs = {"embedder": embedder, "top_k": max(top_k * 4, top_k),
              "include_types": sorted(allowed), "now_ts": now_ts}
    if dim is not None:
        kwargs["dim"] = dim
    raw = memory_query.query_memories(conn, query, **kwargs)
    out = []
    for r in raw:
        if r["type"] not in allowed:
            continue
        row = conn.execute("SELECT body FROM memories WHERE id=?", (r["id"],)).fetchone()
        body = row["body"] if row else ""
        # Read-path redaction is defense-in-depth: catches a secret that entered
        # the DB by a path other than the save_memory write-time chokepoint (§13).
        out.append({
            "id": r["id"],
            "title": strip_secrets(r["title"] or ""),
            "type": r["type"],
            "snippet": strip_secrets(body or ""),
            "score": r["score"],
            "stale": r["stale"],
            # Extend the type wall to the row's edges: an allowed row's `links` must
            # not leak the id/type of a forbidden user/feedback endpoint (FIX 3).
            "links": filter_links_for_caller(
                conn, r["links"], caller_class=caller_class),
        })
        if len(out) >= top_k:
            break
    # Audit every recall with the caller's identity so exfiltration is traceable.
    # SP-8 substrate (§5.1): also thread the GENERIC session id (env, graceful-None)
    # onto each row, so a later attribution step can join recall → session outcome.
    # Unset env → NULL session_id → harmless (no attribution possible), never errors.
    audit_ts = ts or now_ts
    if audit and audit_ts:
        import os
        session_id = session_id_from_env(os.environ)
        for item in out:
            # Best-effort audit (mirrors unified_query._audit_hits): record_access
            # goes through _write_txn, which can raise (e.g. WriteSpooled under write
            # contention) — that must NOT turn a SUCCEEDED read into a recall error
            # on the read-only MCP. The read result survives an audit-write failure.
            try:
                memory_lib.record_access(
                    conn, target_kind="memory", target_id=item["id"],
                    ts=audit_ts, context=f"knowledge_recall:{caller_class}",
                    session_id=session_id)
            except Exception:
                pass
    return out


def run_query_tool(arguments, *, conn, embedder, caller_class, dim=None,
                   now_ts=None, ts=None, agent_topics=_NO_TOPIC_ARG):
    """MCP tool handler: map {query, top_k} → recall → one JSON TextContent.
    Returns a structured {"error": ...} payload (never raises) on a missing/invalid
    query so a malformed tool call can't crash the server loop.

    ADDITIVE cross-store routing (SP-3 Stage 6, §5.6): when `agent_topics` is
    provided (a topic-scoped caller — a set, or the orchestrator's `None`
    all-topics sentinel), route to `unified_query.unified_recall` so the caller's
    recall spans BOTH the memory store and the topic-scoped Expert-Knowledge mirror,
    fail-closed on the (type × topic) wall. When `agent_topics` is NOT supplied
    (the default `_NO_TOPIC_ARG` sentinel — the legacy SP-1 invocation), behavior is
    UNCHANGED: pure memory-store `knowledge_recall`. So every existing knowledge-MCP
    test keeps passing.

    `mcp` is imported lazily so the core recall logic stays importable without the
    optional `mcp` extra installed.
    """
    import json

    from mcp.types import TextContent

    args = arguments or {}
    query = args.get("query")
    if not query or not isinstance(query, str):
        return [TextContent(type="text", text=json.dumps(
            {"error": "missing required 'query' string argument"}))]
    try:
        top_k = int(args.get("top_k", 5))
    except (TypeError, ValueError):
        top_k = 5
    try:
        if agent_topics is _NO_TOPIC_ARG:
            results = knowledge_recall(
                conn, query, caller_class=caller_class, embedder=embedder,
                top_k=top_k, dim=dim, now_ts=now_ts, ts=ts)
        else:
            from . import unified_query
            recall_kwargs = {
                "caller_class": caller_class, "agent_topics": agent_topics,
                "embedder": embedder, "top_k": top_k, "now_ts": now_ts, "ts": ts}
            if dim is not None:
                recall_kwargs["dim"] = dim
            results = unified_query.unified_recall(conn, query, **recall_kwargs)
    except Exception as exc:  # degrade ONE query, never kill the server loop (§13)
        # R4 FIX 4: the client-facing error must NOT leak the raw exception string
        # — str(exc) can embed internal filesystem/DB paths (a fastembed model path,
        # a sqlite OperationalError with the db filename). strip_secrets does NOT
        # redact paths, so routing the message through it is insufficient. Return a
        # FIXED generic string across the privilege boundary; log the detail LOCALLY
        # (stderr) so server-side debugging info is preserved, not leaked.
        import sys
        import traceback
        print(f"[knowledge_mcp] recall failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return [TextContent(type="text", text=json.dumps(
            {"error": "recall failed (internal error)"}))]
    return [TextContent(type="text", text=json.dumps({"results": results}))]


# ---------------------------------------------------------------------------
# Config (paths via env, never cwd — the MCP-launcher-ignores-cwd trap, §13) +
# the stdio server entry point.
# ---------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Raised when required MCP config (e.g. the memory.db path) is absent."""


def caller_class_from_env(env):
    """Fail-closed caller class: only an explicit ULTRA_MEMORY_CALLER_CLASS unlocks
    a privilege class; everything else (the cron/subagent role marker, or nothing)
    is the untrusted 'subagent' (SAFE_TYPES only)."""
    cc = (env.get("ULTRA_MEMORY_CALLER_CLASS") or "").strip()
    return cc or "subagent"


# SP-8 substrate (§5.1): the generic session-id env read, sibling of
# `caller_class_from_env`. The canonical implementation lives in `memory_lib` (the
# lowest layer, where `record_access` consumes it — no import cycle); re-exported
# here so the recall path's env-dimension reads sit side by side.
session_id_from_env = memory_lib.session_id_from_env


def db_path_from_env(env):
    """Resolve the memory.db path: an explicit override, else the FIXED global
    user-path. NEVER cwd (the safety property: a wrong/empty db next to the process
    is never opened), and (since 2026-06-01) NEVER a project-local path.

    Resolution order — the local-vs-project fallback was deliberately abandoned: the
    knowledge fabric always lives at ONE fixed user-path (``~/.ultra-memory/``), the
    single global store shared by every project:
      1. ``ULTRA_MEMORY_DB`` (the explicit override), if set + non-blank.
      2. else ``~/.ultra-memory/memory.db`` (the fixed global default).

    The path is only RESOLVED, never created here — ``open_memory_db`` downstream does
    the create+migrate, and an empty store recalls nothing gracefully. Blank values are
    treated as unset (fall through). The ``ConfigError`` class is kept for callers that
    reference it, but this resolver no longer raises (zero-config install)."""
    from pathlib import Path
    raw = (env.get("ULTRA_MEMORY_DB") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".ultra-memory" / "memory.db"


def _open_db_for_mcp(db_path):
    """Open the memory.db for the stdio MCP, surviving a FRESH install.

    On a clean machine ``~/.ultra-memory/`` does not exist yet, and the MCP starts
    on the post-install restart BEFORE ``/memory-setup`` creates it — ``sqlite3.connect``
    then raises "unable to open database file" and the server silently never registers
    (the headline release blocker). Create the parent dir defensively (idempotent), then
    open **and migrate** so the schema exists: the MCP itself writes ``access_log`` audit
    rows on every recall, and an empty-but-migrated store recalls nothing gracefully.
    A mkdir failure is logged to stderr, not fatal — ``open_memory_db`` surfaces the real
    error if the path is genuinely unusable."""
    import sys
    from pathlib import Path
    parent = Path(db_path).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"ultra-memory knowledge MCP: could not create {parent}: {e}",
              file=sys.stderr)
    return memory_lib.open_memory_db(db_path)


def lazy_embedder(factory=None):
    """A callable embedder that defers the (heavy) fastembed build to its FIRST call.

    The stdio server must answer `initialize` inside the client's connect timeout
    (Claude Code: 30s). Building fastembed eagerly in `main()` raced that timeout —
    and, worse, a missing model file (e.g. an OS temp purge) crashed the whole
    server at startup instead of degrading a single query (knowledge MCP failure,
    2026-05-31). Deferring the build lets the server connect instantly; the model
    loads on the first `knowledge_query`. Memoised: built once, then reused (warm).
    `factory` is injectable for tests; default = retrieval_core.default_embedder.
    """
    holder = {}

    def embed(texts):
        fn = holder.get("fn")
        if fn is None:
            if factory is not None:
                fn = factory()
            else:
                from . import retrieval_core
                fn = retrieval_core.default_embedder()
            holder["fn"] = fn
        return fn(texts)

    return embed


def knowledge_tools():
    """The MCP tool catalog. Lazy `mcp` import keeps the recall core importable
    without the optional `mcp` extra."""
    from mcp.types import Tool
    return [Tool(
        name="knowledge_query",
        description=(
            "Recall durable project / trading knowledge from the ultra-memory store. "
            "Returns ranked memories (id, title, type, snippet, score, links). Access is "
            "type-scoped to the caller's privilege class (untrusted callers get "
            "project/reference facts only, never user/feedback memories)."),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Natural-language question or topic to recall."},
                "top_k": {"type": "integer", "default": 5,
                          "description": "Max results to return (default 5)."},
            },
            "required": ["query"],
        },
    )]


def main():
    """Stdio entry point for the read-only `knowledge` MCP. Reads config from env
    (paths via config, not cwd), opens memory.db, and serves `knowledge_query`. The
    embedder is LAZY (built on the first query, not at startup) so the server answers
    `initialize` instantly and a cold/missing model degrades one query rather than
    killing the connection. No LLM on this path."""
    import asyncio
    import datetime
    import json
    import os

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent

    db_path = db_path_from_env(os.environ)
    caller_class = caller_class_from_env(os.environ)
    conn = _open_db_for_mcp(db_path)
    embedder = lazy_embedder()

    server = Server("ultra-memory-knowledge")

    @server.list_tools()
    async def _list_tools():  # pragma: no cover - thin stdio wiring
        return knowledge_tools()

    @server.call_tool()
    async def _call_tool(name, arguments):  # pragma: no cover - thin stdio wiring
        if name != "knowledge_query":
            return [TextContent(type="text",
                                text=json.dumps({"error": f"unknown tool: {name}"}))]
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return run_query_tool(arguments, conn=conn, embedder=embedder,
                              caller_class=caller_class, now_ts=now, ts=now)

    async def _run():  # pragma: no cover - thin stdio wiring
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
