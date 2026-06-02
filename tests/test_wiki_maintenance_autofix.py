"""Generic wiki-maintenance — slice 3: autofix (move-with-config). The deterministic
Stage-1 auto-fixers. Every site-specific value (the `updated` field name, the
auto-added-section name, the anchor-suffix digit count) is a WikiSchemaConfig seam;
the algorithms are generic. Pure functions: (text, ...) -> (new_text, detail|None).
"""
from pathlib import Path

from ultra_memory.wiki_maintenance import autofix as af
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


# --------------------------------------------------------------------------- #
# fix_missing_updated — add the `updated:` field (name is a schema seam).
# --------------------------------------------------------------------------- #

def test_adds_missing_updated_default_field():
    text = "---\ntype: concept\ntitle: X\n---\n\nbody\n"
    out, detail = af.fix_missing_updated(text, today="2026-06-02")
    assert detail is not None and detail["kind"] == "updated-field"
    assert "updated: 2026-06-02" in out


def test_updated_noop_when_present():
    text = "---\ntype: concept\nupdated: 2020-01-01\n---\n\nbody"
    out, detail = af.fix_missing_updated(text, today="2026-06-02")
    assert detail is None and out == text


def test_updated_respects_custom_field_name():
    schema = WikiSchemaConfig(updated_field="last_touched")
    text = "---\ntype: concept\ntitle: X\n---\n\nbody\n"
    out, detail = af.fix_missing_updated(text, today="2026-06-02", schema=schema)
    assert "last_touched: 2026-06-02" in out
    # a page that already has the custom field is a no-op
    text2 = "---\nlast_touched: 2020-01-01\n---\nbody"
    assert af.fix_missing_updated(text2, today="2026-06-02", schema=schema)[1] is None


def test_updated_scoped_to_frontmatter_not_body():
    # a body line "updated: see above" must NOT suppress the fix
    text = "---\ntype: concept\n---\n\nupdated: see above\n"
    out, detail = af.fix_missing_updated(text, today="2026-06-02")
    assert detail is not None and "updated: 2026-06-02" in out.split("---\n", 2)[1]


def test_updated_fence_robust_to_dashes_in_value():
    text = "---\ndesc: ---x\ntype: concept\n---\nbody"
    out, detail = af.fix_missing_updated(text, today="2026-06-02")
    assert detail is not None and "updated: 2026-06-02" in out


def test_updated_noop_when_no_frontmatter():
    assert af.fix_missing_updated("plain body", today="2026-06-02") == ("plain body", None)


# --------------------------------------------------------------------------- #
# fix_empty_autoadded_section — section NAME is a schema seam.
# --------------------------------------------------------------------------- #

def test_removes_empty_autoadded_section_default_name():
    text = "## Things\n\nfoo\n\n### Recently auto-added (uncategorized)\n\n\n"
    out, detail = af.fix_empty_autoadded_section(text)
    assert detail is not None and detail["kind"] == "empty-section"
    assert "Recently auto-added" not in out


def test_keeps_nonempty_autoadded_section():
    text = "### Recently auto-added (uncategorized)\n\n- **slug** a real bullet\n"
    out, detail = af.fix_empty_autoadded_section(text)
    assert detail is None and out == text


def test_autoadded_section_respects_custom_name():
    schema = WikiSchemaConfig(autoadded_section_name="Inbox (unsorted)")
    text = "### Inbox (unsorted)\n\n   \n"
    out, detail = af.fix_empty_autoadded_section(text, schema=schema)
    assert detail is not None and "Inbox (unsorted)" not in out
    # the default name no longer matches this custom-named wiki
    assert af.fix_empty_autoadded_section(text)[1] is None


def test_autoadded_section_re_helper_matches_by_name():
    schema = WikiSchemaConfig(autoadded_section_name="Inbox (unsorted)")
    rx = af.autoadded_section_re(schema)
    assert rx.search("### Inbox (unsorted)\n\nbody")
    assert not rx.search("### Recently auto-added (uncategorized)\n\nbody")


# --------------------------------------------------------------------------- #
# fix_anchor_collision — suffix-digit count is a schema seam, deterministic.
# --------------------------------------------------------------------------- #

def test_anchor_unchanged_when_free():
    new_anchor, detail = af.fix_anchor_collision(anchor="foo", claim="c", taken=set())
    assert new_anchor == "foo" and detail is None


def test_anchor_suffixed_deterministically_when_taken():
    a1, d1 = af.fix_anchor_collision(anchor="foo", claim="my claim", taken={"foo"})
    a2, d2 = af.fix_anchor_collision(anchor="foo", claim="my claim", taken={"foo"})
    assert d1 is not None and a1 == a2          # deterministic, no PYTHONHASHSEED dep
    assert a1.startswith("foo-") and len(a1.split("-")[-1]) == 4


def test_anchor_suffix_digit_count_is_schema_seam():
    schema = WikiSchemaConfig(anchor_suffix_digits=6)
    a1, _ = af.fix_anchor_collision(anchor="foo", claim="c", taken={"foo"}, schema=schema)
    assert len(a1.split("-")[-1]) == 6


# --------------------------------------------------------------------------- #
# fix_broken_wikilink — single-rename-target rule; aliases preserved.
# --------------------------------------------------------------------------- #

def test_broken_wikilink_repoints_single_target():
    out, detail = af.fix_broken_wikilink("see [[old]] here", broken="old", rename_targets=["new"])
    assert detail is not None and out == "see [[new]] here"


def test_broken_wikilink_preserves_alias():
    out, _ = af.fix_broken_wikilink("[[old|Display]]", broken="old", rename_targets=["new"])
    assert out == "[[new|Display]]"


def test_broken_wikilink_noop_on_zero_or_ambiguous_targets():
    assert af.fix_broken_wikilink("[[old]]", broken="old", rename_targets=[])[1] is None
    assert af.fix_broken_wikilink("[[old]]", broken="old", rename_targets=["a", "b"])[1] is None


# --------------------------------------------------------------------------- #
# Portability guard.
# --------------------------------------------------------------------------- #

def test_no_trading_or_path_literal():
    src = Path(af.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src
