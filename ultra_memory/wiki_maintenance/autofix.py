"""Deterministic Stage-1 auto-fixers (move-with-config).

Every fixer is pure: ``(text, ...) -> (new_text, detail|None)``. ``detail=None``
means no change (the caller must NOT write). Conservative by design — fixer 4
repoints a broken wikilink ONLY when rename history yields exactly one target.

Ported from a reference pipeline; the site-specifics are now WikiSchemaConfig seams:
  * the ``updated`` frontmatter field NAME  → ``schema.updated_field``
  * the auto-added section NAME             → ``schema.autoadded_section_name``
  * the anchor-collision suffix digit count → ``schema.anchor_suffix_digits``

The anchor suffix is DETERMINISTIC: ``int(sha1(claim)[:4], 16) % 10**n`` zero-padded
to ``n`` digits — re-runs on the same claim always produce the same suffix (no
PYTHONHASHSEED randomness). ``usedforsecurity=False`` bypasses CPython's FIPS-mode
restriction (SHA-1 is a stable hash here, not a security primitive).
"""
from __future__ import annotations

import hashlib
import re

from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


# --------------------------------------------------------------------------- #
# Fixer 1 — add a missing "<updated_field>:" field to YAML frontmatter.
# --------------------------------------------------------------------------- #

def fix_missing_updated(text: str, *, today: str,
                        schema: WikiSchemaConfig | None = None) -> tuple[str, dict | None]:
    """Add ``<updated_field>: <today>`` to YAML frontmatter if the field is absent.

    The search for an existing field is scoped to the frontmatter block only (not the
    body), so a body line like ``updated: see above`` does not suppress the fix. The
    closing fence is the first ``\\n---\\n`` (or a trailing ``\\n---``) after the
    opening ``---\\n``, so a YAML value like ``desc: ---x`` is never mistaken for it.
    """
    schema = schema or WikiSchemaConfig()
    field = schema.updated_field
    if not text.startswith("---\n"):
        return text, None
    after_open = 4  # len("---\n")
    rest = text[after_open:]
    idx = rest.find("\n---\n")
    if idx != -1:
        end_in_rest = idx
    elif rest.endswith("\n---"):
        end_in_rest = len(rest) - len("\n---")
    else:
        return text, None
    fm = rest[:end_in_rest]  # raw YAML text (no surrounding --- lines)
    if re.search(rf"^{re.escape(field)}:", fm, re.MULTILINE):
        return text, None
    new_fm = fm.rstrip("\n") + f"\n{field}: {today}\n"
    out = "---\n" + new_fm + text[after_open + end_in_rest:]
    return out, {"kind": "updated-field", "detail": f"added {field}: {today}"}


# --------------------------------------------------------------------------- #
# Fixer 2 — remove an empty auto-added section (section NAME is a schema seam).
# --------------------------------------------------------------------------- #

def autoadded_section_re(schema: WikiSchemaConfig | None = None) -> re.Pattern:
    """The regex matching the auto-added section (a ``###`` heading whose text is
    ``schema.autoadded_section_name``) plus its body up to the next real Markdown
    heading or EOF. Shared by ``fix_empty_autoadded_section`` and the scope detector
    (single source of truth for the section pattern)."""
    schema = schema or WikiSchemaConfig()
    name = re.escape(schema.autoadded_section_name)
    # Leading anchor (?:^|\n) so a file that BEGINS with the section also matches.
    # The body stops only at a real heading (hashes + space/tab), so a "\n#tag" line
    # is not treated as a section boundary.
    return re.compile(
        rf"(?:^|\n)###[ \t]+{name}[ \t]*\n"
        r"(?P<body>(?:(?!\n#{1,6}[ \t]).)*?)"
        r"(?=\n#{1,6}[ \t]|\Z)",
        re.DOTALL,
    )


def fix_empty_autoadded_section(text: str,
                                schema: WikiSchemaConfig | None = None) -> tuple[str, dict | None]:
    """Remove the auto-added section if its body is entirely blank/whitespace. A
    section whose body contains ANY non-blank content (bullets, prose, #tags, …) is
    kept. Handles only the FIRST matching section per call; the caller loops if a file
    can hold several."""
    schema = schema or WikiSchemaConfig()
    m = autoadded_section_re(schema).search(text)
    if not m:
        return text, None
    body = m.group("body")
    if any(ln.strip() for ln in body.splitlines()):
        return text, None
    out = text[: m.start()] + text[m.end():]
    return out, {"kind": "empty-section",
                 "detail": f"removed empty {schema.autoadded_section_name} section"}


# --------------------------------------------------------------------------- #
# Fixer 3 — resolve anchor collisions with a deterministic suffix.
# --------------------------------------------------------------------------- #

def fix_anchor_collision(*, anchor: str, claim: str, taken: set[str],
                         schema: WikiSchemaConfig | None = None) -> tuple[str, dict | None]:
    """Return a collision-free anchor for *claim*.

    If *anchor* is not in *taken*, return it unchanged (detail=None). Otherwise append
    a zero-padded ``schema.anchor_suffix_digits``-digit suffix derived from SHA-1 of
    the claim text. The returned anchor is NOT re-validated against *taken* — the
    caller ensures the new anchor is free (a second collision is vanishingly unlikely).
    """
    schema = schema or WikiSchemaConfig()
    if anchor not in taken:
        return anchor, None
    n = schema.anchor_suffix_digits
    digest = hashlib.sha1(claim.encode("utf-8"), usedforsecurity=False).hexdigest()
    suffix = f"{int(digest[:4], 16) % (10 ** n):0{n}d}"
    new_anchor = f"{anchor}-{suffix}"
    return new_anchor, {"kind": "anchor-collision", "detail": f"{anchor} -> {new_anchor}"}


# --------------------------------------------------------------------------- #
# Fixer 4 — repoint broken [[wikilinks]] (single rename target only).
# --------------------------------------------------------------------------- #

def fix_broken_wikilink(text: str, *, broken: str,
                        rename_targets: list[str]) -> tuple[str, dict | None]:
    """Replace every ``[[broken(|alias)?]]`` with ``[[target(|alias)?]]``.

    Conservative: repoints ONLY when *rename_targets* has exactly one entry. Zero
    targets (unknown slug) or ≥2 (ambiguous rename) → no-op (detail=None). Aliases are
    preserved: ``[[old|Display]]`` → ``[[new|Display]]``. The ``[[…]]`` convention is
    universal to a Karpathy-style wiki, so it is not a schema seam.
    """
    if len(rename_targets) != 1:
        return text, None
    target = rename_targets[0]
    pattern = re.compile(r"\[\[" + re.escape(broken) + r"((?:\|[^\]]*)?)\]\]")
    if not pattern.search(text):
        return text, None
    out = pattern.sub(lambda m: f"[[{target}{m.group(1)}]]", text)
    return out, {"kind": "broken-wikilink", "detail": f"[[{broken}]] -> [[{target}]]"}
