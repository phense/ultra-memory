"""Tests for skill_fs.py — SP-10 Stage 1, the SKILL.md materializer/gateway.

Invariants under test:
  * FLAT layout: `.claude/skills/gen-<slug>/SKILL.md` is a generated-skill path;
    a static skill, a two-level `_generated/` layout, and an archived skill are NOT;
  * frontmatter validation (gen- name regex, <=64 / <=1024, paths list);
  * render carries `created_by: background_review` + `paths:` and round-trips;
  * create writes atomically + refuses a non-generated target;
  * archive MOVES (never deletes) + a static guard asserts no rm/anthropic.
"""
import sys
from pathlib import Path

import pytest
import yaml


from ultra_memory.maintenance import skill_fs as sf  # noqa: E402

TS = "2026-06-01T00:00:00Z"


def _skill(slug="gen-foo", description="Use when doing the foo thing in tests.",
           body="# Foo\n\nDo foo.", paths=None, index_hook="backtest"):
    return sf.GeneratedSkill(slug=slug, description=description, body=body,
                             paths=paths, index_hook=index_hook,
                             source_lesson_ids=["L1", "L2"])


def test_validate_frontmatter_ok():
    assert sf.validate_frontmatter("gen-foo", "a description") is None
    assert sf.validate_frontmatter("gen-foo-bar-2", "d", ["scripts/**"]) is None


def test_validate_frontmatter_bad_name():
    assert sf.validate_frontmatter("foo", "d")            # no gen- prefix
    assert sf.validate_frontmatter("gen-Foo", "d")        # uppercase
    assert sf.validate_frontmatter("gen--foo", "d")       # double hyphen
    assert sf.validate_frontmatter("gen-foo-", "d")       # trailing hyphen
    assert sf.validate_frontmatter("gen-foo_bar", "d")    # underscore
    assert sf.validate_frontmatter("gen-" + "a" * 70, "d")  # too long


def test_validate_frontmatter_bad_description_and_paths():
    assert sf.validate_frontmatter("gen-foo", "")
    assert sf.validate_frontmatter("gen-foo", "   ")
    assert sf.validate_frontmatter("gen-foo", "x" * 1025)
    assert sf.validate_frontmatter("gen-foo", "d", ["ok", ""])   # empty path entry
    assert sf.validate_frontmatter("gen-foo", "d", "scripts/**")  # not a list


def test_render_round_trips_with_provenance_and_paths():
    text = sf.render_skill_md(_skill(paths=["scripts/**", "tests/**"]))
    assert text.startswith("---\n")
    block = text[4:text.index("\n---", 4)]
    fm = yaml.safe_load(block)
    assert fm["name"] == "gen-foo"
    assert fm["created_by"] == "background_review"
    assert fm["description"].startswith("Use when")
    assert fm["paths"] == ["scripts/**", "tests/**"]
    assert "Do foo." in text


def test_is_generated_skill_path(tmp_path):
    root = tmp_path / "repo"
    gen = sf.skill_md_path(root, "gen-foo")
    assert sf.is_generated_skill_path(gen) is True
    # a static skill dir
    static = root / ".claude" / "skills" / "risk-manager" / "SKILL.md"
    assert sf.is_generated_skill_path(static) is False
    # a two-level _generated layout (the rejected design)
    two = root / ".claude" / "skills" / "_generated" / "gen-foo" / "SKILL.md"
    assert sf.is_generated_skill_path(two) is False
    # an archived skill
    arch = sf.archive_root(root) / "gen-foo" / "SKILL.md"
    assert sf.is_generated_skill_path(arch) is False


def test_create_writes_atomically(tmp_path):
    root = tmp_path / "repo"
    target = sf.create(_skill(paths=["scripts/**"]), repo_root=root, ts=TS,
                       audit_dir=tmp_path / "audit")
    assert target == sf.skill_md_path(root, "gen-foo")
    assert target.is_file()
    text = target.read_text()
    fm = yaml.safe_load(text[4:text.index("\n---", 4)])
    assert fm["name"] == "gen-foo" and fm["created_by"] == "background_review"
    # the gateway re-confirms the structural invariant
    assert sf.is_generated_skill_path(target)
    # audit row written + redaction applied (no exception)
    audit = list((tmp_path / "audit").glob("sp10-writes-*.jsonl"))
    assert audit and "create" in audit[0].read_text()


def test_create_refuses_non_generated_slug(tmp_path):
    root = tmp_path / "repo"
    with pytest.raises(sf.SkillWriteError):
        sf.create(_skill(slug="risk-manager"), repo_root=root, ts=TS)


def test_archive_moves_never_deletes(tmp_path):
    root = tmp_path / "repo"
    sf.create(_skill(), repo_root=root, ts=TS)
    src = sf.skill_dir(root, "gen-foo")
    assert src.is_dir()
    dest = sf.archive(_skill().slug, repo_root=root, ts=TS)
    assert not src.exists()              # moved out of the scan tree
    assert (dest / "SKILL.md").is_file()  # content preserved (never rm)
    # a second archive of a re-created skill does not collide-overwrite
    sf.create(_skill(), repo_root=root, ts=TS)
    dest2 = sf.archive(_skill().slug, repo_root=root, ts=TS)
    assert dest2 != dest and (dest2 / "SKILL.md").is_file()


def test_archive_refuses_non_generated(tmp_path):
    with pytest.raises(sf.SkillWriteError):
        sf.archive("risk-manager", repo_root=tmp_path / "repo", ts=TS)


def test_static_no_rm_no_anthropic_all_sp10_modules():
    """Spec §8 guard, across EVERY SP-10 module: no destructive call on a knowledge
    artifact, no anthropic SDK / API. skill_eval's ONE `cmd_file.unlink()` (the
    ephemeral probe command-file, not a knowledge artifact) is the sole allowed
    exception."""
    import importlib
    mods = ["skill_fs", "skill_synthesize", "skill_eval", "synthesize_run",
            "synthesize_bounds"]
    for name in mods:
        src = Path(importlib.import_module(
            f"ultra_memory.maintenance.{name}").__file__).read_text()
        for tok in ("os.remove(", "shutil.rmtree(", "os.unlink(", "rm -rf",
                    "import anthropic", "ANTHROPIC_API_KEY", "messages.create",
                    "cache_control"):
            # ANTHROPIC_API_KEY appears only inside skill_eval's docstring describing
            # the OAuth guard that REFUSES it — allow that single doc mention.
            if tok == "ANTHROPIC_API_KEY" and name == "skill_eval":
                continue
            assert tok not in src, f"forbidden token {tok!r} in {name}.py"
        n_unlink = src.count(".unlink(")
        if name == "skill_eval":
            assert n_unlink == 1, "skill_eval should have exactly one (probe) unlink"
        else:
            assert n_unlink == 0, f"{name}.py must not call .unlink()"
