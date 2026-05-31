"""Human-correction inbox importer (spec §14).

Peter types directive lines into a watched inbox file (e.g. `data/memory_inbox.md`);
this importer applies them to the DB via `memory_lib` (audited) and clears the file.
Deterministic: only `pin`/`unpin`/`verify` directives are auto-applied — free-text
prose is NOT interpreted (no LLM here), it is preserved under an "Unprocessed"
section so a human correction is never silently lost.

Directive grammar (one per line):
    pin <id>        unpin <id>        verify <id>
Lines that are blank or start with `#` are comments. Anything else is a note.
"""
from pathlib import Path

from . import memory_lib

_DIRECTIVES = {"pin", "unpin", "verify"}

_HEADER = (
    "# Memory inbox — type one directive per line; on import they apply + this file clears.\n"
    "#   pin <id>   / unpin <id>   — (un)pin a memory (pinned memories inject into every SessionStart gist)\n"
    "#   verify <id>               — stamp it reconfirmed-true as of today\n"
    "# Free-text lines are NOT auto-applied (no LLM here); they are preserved under\n"
    "# 'Unprocessed' below for manual handling.\n"
)


def parse_inbox(text):
    """Parse inbox text into a list of {op, id} directives and {op:'note', text} items.
    Blank lines and `#` comments are dropped."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        verb = parts[0].lower()
        if verb in _DIRECTIVES and len(parts) == 2 and parts[1].strip():
            out.append({"op": verb, "id": parts[1].strip()})
        else:
            out.append({"op": "note", "text": line})
    return out


def import_inbox(conn, inbox_path, *, ts):
    """Apply the inbox's directives via memory_lib, then rewrite the file to a clean
    header (preserving any notes under an Unprocessed section). Returns a summary
    dict {applied, notes, errors, skipped}. Missing file → a no-op zero summary."""
    path = Path(inbox_path)
    summary = {"applied": 0, "notes": 0, "errors": [], "skipped": 0}
    if not path.is_file():
        return summary

    items = parse_inbox(path.read_text(encoding="utf-8"))
    notes = []
    for it in items:
        op = it["op"]
        if op == "note":
            notes.append(it["text"])
            continue
        mid = it["id"]
        try:
            if op == "pin":
                memory_lib.set_pinned(conn, id=mid, pinned=True, ts=ts, reason="inbox pin")
            elif op == "unpin":
                memory_lib.set_pinned(conn, id=mid, pinned=False, ts=ts, reason="inbox unpin")
            elif op == "verify":
                memory_lib.set_verified(conn, id=mid, ts=ts)
            summary["applied"] += 1
        except KeyError as exc:
            summary["errors"].append(f"{op} {mid}: {exc}")
        except Exception as exc:  # pragma: no cover - defensive: never let one bad line wedge the import
            summary["errors"].append(f"{op} {mid}: {exc!r}")

    summary["notes"] = len(notes)

    # Rewrite the file: clean header, plus any unprocessed notes preserved for review.
    new_text = _HEADER
    if notes:
        new_text += "\n## Unprocessed (review manually)\n" + "\n".join(notes) + "\n"
    path.write_text(new_text, encoding="utf-8")
    return summary
