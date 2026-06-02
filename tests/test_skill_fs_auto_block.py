"""Model B — the managed `<!-- BEGIN/END auto-learnings -->` region in skill_fs.

A generated SKILL.md is a FROZEN eval-gated frontmatter trigger + a managed body
block refreshed weekly. skill_fs owns the markers + the splice; the frontmatter is
NEVER touched by a refresh (the frozen-trigger safety invariant).
"""
from pathlib import Path

import pytest

from ultra_memory.maintenance import skill_fs as sf

TS = "2026-06-02T00:00:00Z"


def _skill(slug="gen-foo", description="Use when doing the foo thing in tests.",
           body="# Foo\n\nDo foo.", paths=None, auto_block=None):
    return sf.GeneratedSkill(slug=slug, description=description, body=body,
                             paths=paths, index_hook="backtest",
                             source_lesson_ids=["L1"], auto_learnings_block=auto_block)


def _gen_skill_on_disk(repo, slug="gen-foo", auto_block="### Seed\n\nseed body.\n"):
    target = sf.skill_md_path(repo, slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = sf.render_skill_md(_skill(slug=slug, auto_block=auto_block))
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# render_skill_md — markers.
# --------------------------------------------------------------------------- #

def test_render_emits_markers_when_auto_block_present():
    text = sf.render_skill_md(_skill(auto_block="### L1\n\nlesson one.\n"))
    assert sf.AUTO_BEGIN in text
    assert sf.AUTO_END in text
    # the lesson sits between the markers; the procedure body sits before BEGIN.
    begin = text.index(sf.AUTO_BEGIN)
    assert "Do foo." in text[:begin]
    assert "### L1" in text[begin:text.index(sf.AUTO_END)]


def test_render_no_markers_when_auto_block_absent():
    """Back-compat: a skill with no auto block renders exactly as before (no markers)."""
    text = sf.render_skill_md(_skill(auto_block=None))
    assert sf.AUTO_BEGIN not in text
    assert sf.AUTO_END not in text


# --------------------------------------------------------------------------- #
# splice_auto_block — pure string op.
# --------------------------------------------------------------------------- #

def test_splice_replaces_only_between_markers():
    text = sf.render_skill_md(_skill(auto_block="### OLD\n\nold.\n"))
    out = sf.splice_auto_block(text, "### NEW\n\nnew.\n")
    assert "### NEW" in out
    assert "### OLD" not in out
    # the procedure shell + frontmatter are untouched.
    assert "Do foo." in out


def test_splice_preserves_frontmatter_byte_identical():
    text = sf.render_skill_md(_skill(auto_block="### OLD\n\nold.\n"))
    fm_end = text.index("\n---", 4) + len("\n---\n")
    fm_before = text[:fm_end]
    out = sf.splice_auto_block(text, "### NEW\n\nnew.\n")
    assert out[:fm_end] == fm_before


def test_splice_appends_when_markers_absent():
    """A SKILL.md predating Model B (no markers) self-heals: the block is appended,
    frontmatter + body preserved."""
    text = sf.render_skill_md(_skill(auto_block=None))
    assert sf.AUTO_BEGIN not in text
    out = sf.splice_auto_block(text, "### NEW\n\nnew.\n")
    assert sf.AUTO_BEGIN in out and sf.AUTO_END in out
    assert "### NEW" in out
    assert "Do foo." in out


def test_splice_is_idempotent():
    text = sf.render_skill_md(_skill(auto_block="### OLD\n\nold.\n"))
    once = sf.splice_auto_block(text, "### NEW\n\nnew.\n")
    twice = sf.splice_auto_block(once, "### NEW\n\nnew.\n")
    assert once == twice


# --------------------------------------------------------------------------- #
# rewrite_auto_block — the on-disk gateway (structural-gated, atomic, audited).
# --------------------------------------------------------------------------- #

def test_rewrite_updates_file_between_markers(tmp_path):
    repo = tmp_path
    target = _gen_skill_on_disk(repo, auto_block="### OLD\n\nold.\n")
    out = sf.rewrite_auto_block(repo, "gen-foo", "### FRESH\n\nfresh.\n", ts=TS)
    assert out == target
    txt = target.read_text()
    assert "### FRESH" in txt
    assert "### OLD" not in txt
    assert "Do foo." in txt


def test_rewrite_freezes_frontmatter(tmp_path):
    """The frozen-trigger invariant: a refresh NEVER changes the frontmatter
    (description/name/paths) — only the marked body region."""
    repo = tmp_path
    target = _gen_skill_on_disk(repo, auto_block="### OLD\n\nold.\n")
    before = target.read_text()
    fm_before = before[: before.index("\n---", 4) + len("\n---\n")]
    sf.rewrite_auto_block(repo, "gen-foo", "### FRESH\n\ndifferent.\n", ts=TS)
    after = target.read_text()
    assert after[: len(fm_before)] == fm_before
    assert "Use when doing the foo thing in tests." in after


def test_rewrite_refuses_non_generated_path(tmp_path):
    """A static skill path can never be a refresh target (structural guard)."""
    repo = tmp_path
    static = repo / ".claude" / "skills" / "risk-manager"
    static.mkdir(parents=True)
    (static / "SKILL.md").write_text("---\nname: risk-manager\n---\n\nbody")
    with pytest.raises(sf.SkillWriteError):
        sf.rewrite_auto_block(repo, "risk-manager", "### X\n\nx.\n", ts=TS)


def test_rewrite_emits_audit_row(tmp_path):
    repo = tmp_path
    _gen_skill_on_disk(repo, auto_block="### OLD\n\nold.\n")
    audit_dir = tmp_path / "audit"
    sf.rewrite_auto_block(repo, "gen-foo", "### NEW\n\nn.\n", ts=TS, audit_dir=audit_dir)
    rows = list((audit_dir).glob("sp10-writes-*.jsonl"))
    assert rows, "expected an audit jsonl"
    assert "refresh-block" in rows[0].read_text()


def test_rewrite_missing_file_raises(tmp_path):
    with pytest.raises(sf.SkillWriteError):
        sf.rewrite_auto_block(tmp_path, "gen-absent", "### X\n\nx.\n", ts=TS)


# --------------------------------------------------------------------------- #
# Review fixes — frozen-trigger robustness against marker strings in frontmatter
# + fail-closed on unbalanced markers.
# --------------------------------------------------------------------------- #

def _text_with_markers_in_description():
    return (
        "---\n"
        "name: gen-foo\n"
        f"description: Use the {sf.AUTO_BEGIN} and {sf.AUTO_END} markers in tests.\n"
        "created_by: background_review\n"
        "---\n\n"
        "# proc\n\n"
        + sf._marked_block("### OLD\n\nold.\n") + "\n"
    )


def test_splice_ignores_markers_in_frontmatter_description():
    """CRITICAL: if the FROZEN description itself documents the marker strings, a
    refresh must splice the BODY region only — never the frontmatter copy."""
    text = _text_with_markers_in_description()
    out = sf.splice_auto_block(text, "### NEW\n\nnew.\n")
    # the frontmatter description line is byte-preserved
    assert f"description: Use the {sf.AUTO_BEGIN} and {sf.AUTO_END} markers in tests." in out
    # the body region was replaced
    assert "### NEW" in out and "### OLD" not in out
    # exactly one BEGIN/END pair in the BODY (frontmatter copy + one body pair = 2 each)
    assert out.count(sf.AUTO_BEGIN) == 2 and out.count(sf.AUTO_END) == 2


def test_rewrite_preserves_frontmatter_with_markers_in_description(tmp_path):
    repo = tmp_path
    d = sf.skill_dir(repo, "gen-foo")
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text(_text_with_markers_in_description())
    sf.rewrite_auto_block(repo, "gen-foo", "### FRESH\n\nfresh.\n", ts=TS)
    out = md.read_text()
    assert f"description: Use the {sf.AUTO_BEGIN} and {sf.AUTO_END} markers in tests." in out
    assert "### FRESH" in out and "### OLD" not in out


def test_validate_frontmatter_rejects_markers_in_description():
    assert sf.validate_frontmatter("gen-foo", f"bad {sf.AUTO_BEGIN} desc")
    assert sf.validate_frontmatter("gen-foo", f"bad {sf.AUTO_END} desc")


def test_validate_frontmatter_rejects_markers_in_paths():
    assert sf.validate_frontmatter("gen-foo", "ok", [f"src/{sf.AUTO_BEGIN}/**"])


def test_rewrite_refuses_unbalanced_markers(tmp_path):
    """Fail-closed: a corrupted body with one orphan marker is left UNTOUCHED (a
    refresh that appended a second block would compound the corruption)."""
    repo = tmp_path
    d = sf.skill_dir(repo, "gen-foo")
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    before = ("---\nname: gen-foo\ndescription: d.\ncreated_by: background_review\n---\n\n"
              f"# proc\n\n{sf.AUTO_BEGIN}\n### X\n\nx.\n")   # BEGIN, no END
    md.write_text(before)
    with pytest.raises(sf.SkillWriteError):
        sf.rewrite_auto_block(repo, "gen-foo", "### NEW\n\nn.\n", ts=TS)
    assert md.read_text() == before                          # untouched
