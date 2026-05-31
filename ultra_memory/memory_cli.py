"""CLI behind the /memory-* slash commands (spec §14): recall / pin / verify / edit
/ inbox. Write verbs use NO LLM; recall uses the injected (tests) or lazy fastembed
embedder. The DB path comes from config (ULTRA_MEMORY_DB or --db), never cwd.

All deps (db_path, embedder, dim, ts) are injectable so tests need no env / fastembed.
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

from . import memory_inbox, memory_lib, memory_query
from . import retrieval_core as rc


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _resolve_db(explicit):
    p = explicit or os.environ.get("ULTRA_MEMORY_DB")
    if not p:
        raise SystemExit("memory: no DB — set ULTRA_MEMORY_DB or pass --db (paths via config, not cwd).")
    return p


def _build_parser():
    ap = argparse.ArgumentParser(prog="memory", description="ultra-memory correction CLI (/memory-*).")
    ap.add_argument("--db", default=None, help="memory.db path (default: $ULTRA_MEMORY_DB).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recall", help="rank active memories for a query (JSON)")
    r.add_argument("--query", required=True)
    r.add_argument("--top-k", type=int, default=5)
    p = sub.add_parser("pin", help="pin a memory (or --unpin)")
    p.add_argument("--id", required=True)
    p.add_argument("--unpin", action="store_true")
    v = sub.add_parser("verify", help="stamp a memory reconfirmed-true today")
    v.add_argument("--id", required=True)
    e = sub.add_parser("edit", help="replace a memory's body from a file (type/title/fields preserved)")
    e.add_argument("--id", required=True)
    e.add_argument("--from-file", required=True)
    i = sub.add_parser("inbox", help="apply + clear the correction inbox")
    i.add_argument("--path", default=None)
    s = sub.add_parser("save", help="create/update a durable memory from a body file")
    s.add_argument("--id", required=True)
    s.add_argument("--type", default="reference",
                   help="user | feedback | project | reference (privilege-scoped on recall)")
    s.add_argument("--title", required=True)
    s.add_argument("--from-file", required=True,
                   help="path to the memory body (avoids shell-escaping prose)")
    s.add_argument("--description", default=None)
    s.add_argument("--node-type", default="memory")
    return ap


def main(argv=None, *, db_path=None, embedder=None, dim=None, ts=None):
    args = _build_parser().parse_args(argv)
    ts = ts or _now()
    db = _resolve_db(db_path or args.db)
    conn = memory_lib.open_memory_db(db)
    try:
        if args.cmd == "recall":
            emb = embedder or rc.default_embedder()
            d = dim if dim is not None else rc.EMBED_DIM
            results = memory_query.query_memories(
                conn, args.query, embedder=emb, dim=d, top_k=args.top_k, now_ts=ts)
            print(json.dumps({"results": results}, indent=2))
            return 0

        if args.cmd == "pin":
            try:
                memory_lib.set_pinned(conn, id=args.id, pinned=not args.unpin, ts=ts,
                                      reason="cli unpin" if args.unpin else "cli pin")
            except KeyError:
                print(f"pin: no memory with id {args.id!r}", file=sys.stderr)
                return 1
            print(f"{'unpinned' if args.unpin else 'pinned'} {args.id}")
            return 0

        if args.cmd == "verify":
            try:
                memory_lib.set_verified(conn, id=args.id, ts=ts)
            except KeyError:
                print(f"verify: no memory with id {args.id!r}", file=sys.stderr)
                return 1
            print(f"verified {args.id}")
            return 0

        if args.cmd == "edit":
            row = conn.execute("SELECT * FROM memories WHERE id=?", (args.id,)).fetchone()
            if row is None:
                print(f"edit: no memory with id {args.id!r}", file=sys.stderr)
                return 1
            try:
                body = Path(args.from_file).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"edit: cannot read --from-file {args.from_file!r}: {exc}",
                      file=sys.stderr)
                return 1
            # Re-save through the gateway, PRESERVING every other field (save_memory's
            # UPDATE overwrites them all, so omitting one would wipe it).
            memory_lib.save_memory(
                conn, id=args.id, type=row["type"], title=row["title"], body=body, ts=ts,
                description=row["description"], index_hook=row["index_hook"],
                node_type=row["node_type"], file_slug=row["file_slug"],
                sort_order=row["sort_order"], created_at=row["created_at"],
                origin_session_id=row["origin_session_id"])
            print(f"edited {args.id}")
            return 0

        if args.cmd == "save":
            try:
                body = Path(args.from_file).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"save: cannot read --from-file {args.from_file!r}: {exc}",
                      file=sys.stderr)
                return 1
            memory_lib.save_memory(
                conn, id=args.id, type=args.type, title=args.title, body=body, ts=ts,
                description=args.description, node_type=args.node_type)
            print(f"saved {args.id}")
            return 0

        if args.cmd == "inbox":
            inbox_path = (args.path or os.environ.get("ULTRA_MEMORY_INBOX")
                          or str(Path(db).parent / "memory_inbox.md"))
            summary = memory_inbox.import_inbox(conn, inbox_path, ts=ts)
            print(json.dumps(summary))
            return 1 if summary.get("errors") else 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
