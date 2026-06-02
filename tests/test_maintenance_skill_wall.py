"""Tests for the SP-10 SkillUnit extension to aggressive_wall.py.

Invariants:
  * a NEW gen- target (path not yet on disk) is MUTABLE (fresh induction allowed);
  * an EXISTING generated skill (frontmatter created_by:background_review) is mutable;
  * an existing file with created_by:human in a gen- dir is FORBIDDEN (never overwrite);
  * a static skill path (risk-manager), a two-level _generated layout, and an
    archive path are all FORBIDDEN (never write over a static skill / wrong scope);
  * a protected slug (sp10_skill_protect meta flag) is FORBIDDEN;
  * the wall NEVER trusts an echoed created_by.
"""
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_wall as aw  # noqa: E402
from ultra_memory.maintenance import skill_fs as sf  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402

TS = "2026-06-01T00:00:00Z"


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _write_skill(root, slug, created_by="background_review"):
    skill = sf.GeneratedSkill(slug=slug, description="a generated skill",
                              body="# body", index_hook=slug)
    if created_by == "background_review":
        return sf.create(skill, repo_root=root, ts=TS)
    # write a non-generated-provenance file by hand into a gen- dir
    p = sf.skill_md_path(root, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {slug}\ndescription: x\ncreated_by: {created_by}\n---\n\nbody\n")
    return p


def test_new_generated_target_is_mutable(tmp_path):
    root = tmp_path / "repo"
    target = sf.skill_md_path(root, "gen-foo")  # does NOT exist yet
    assert not target.exists()
    aw.assert_mutable(_conn(tmp_path), aw.SkillUnit(slug="gen-foo", path=target))  # no raise


def test_existing_generated_skill_is_mutable(tmp_path):
    root = tmp_path / "repo"
    p = _write_skill(root, "gen-foo", created_by="background_review")
    aw.assert_mutable(_conn(tmp_path), aw.SkillUnit(slug="gen-foo", path=p))  # no raise


def test_existing_human_file_in_gen_dir_forbidden(tmp_path):
    root = tmp_path / "repo"
    p = _write_skill(root, "gen-foo", created_by="human")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(_conn(tmp_path), aw.SkillUnit(slug="gen-foo", path=p))


def test_static_skill_path_forbidden(tmp_path):
    root = tmp_path / "repo"
    static = root / ".claude" / "skills" / "risk-manager" / "SKILL.md"
    static.parent.mkdir(parents=True, exist_ok=True)
    static.write_text("---\nname: risk-manager\ndescription: x\n---\n\nbody\n")
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(_conn(tmp_path), aw.SkillUnit(slug="risk-manager", path=static))


def test_two_level_and_archive_paths_forbidden(tmp_path):
    root = tmp_path / "repo"
    conn = _conn(tmp_path)
    two = root / ".claude" / "skills" / "_generated" / "gen-foo" / "SKILL.md"
    arch = sf.archive_root(root) / "gen-foo" / "SKILL.md"
    for p in (two, arch):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\nname: gen-foo\ndescription: x\ncreated_by: background_review\n---\n\nb\n")
        with pytest.raises(aw.ForbiddenTargetError):
            aw.assert_mutable(conn, aw.SkillUnit(slug="gen-foo", path=p))


def test_protected_slug_forbidden(tmp_path):
    root = tmp_path / "repo"
    conn = _conn(tmp_path)
    p = _write_skill(root, "gen-foo", created_by="background_review")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 ("sp10_skill_protect:gen-foo", "1"))
    conn.commit()
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(conn, aw.SkillUnit(slug="gen-foo", path=p))


def test_echoed_created_by_ignored(tmp_path):
    root = tmp_path / "repo"
    p = _write_skill(root, "gen-foo", created_by="human")  # real provenance = human
    # an LLM-echoed 'background_review' must NOT flip it mutable
    with pytest.raises(aw.ForbiddenTargetError):
        aw.assert_mutable(_conn(tmp_path),
                          aw.SkillUnit(slug="gen-foo", path=p,
                                       echoed_created_by="background_review"))


def test_under_generated_root_parity_with_skill_fs(tmp_path):
    root = tmp_path / "repo"
    for slug in ("gen-foo", "gen-a-b"):
        p = sf.skill_md_path(root, slug)
        assert aw._under_generated_root(p) == sf.is_generated_skill_path(p) is True
    static = root / ".claude" / "skills" / "risk-manager" / "SKILL.md"
    assert aw._under_generated_root(static) == sf.is_generated_skill_path(static) is False
