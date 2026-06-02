"""Generic wiki-maintenance — foundation slice: the WikiSchemaConfig seam +
the generic wiki utilities (frontmatter split, file walk, git helper). Project-
agnostic: no Trading literal, the schema is all defaults-overridable config.
"""
import subprocess
from pathlib import Path

from ultra_memory.wiki_maintenance import schema_config as sc
from ultra_memory.wiki_maintenance import wiki_util as wu


# --------------------------------------------------------------------------- #
# split_frontmatter — the load-bearing parser (contract ported verbatim).
# --------------------------------------------------------------------------- #

def test_split_normal_page():
    fm, raw, body = wu.split_frontmatter("---\ntype: concept\ntitle: X\n---\n\nBody here.\n")
    assert fm == {"type": "concept", "title": "X"}
    assert body == "\nBody here.\n"


def test_split_no_frontmatter():
    assert wu.split_frontmatter("just body, no fm") == ({}, "", "just body, no fm")


def test_split_frontmatter_only_page():
    fm, raw, body = wu.split_frontmatter("---\ntype: redirect\n---")
    assert fm == {"type": "redirect"} and body == ""


def test_split_malformed_yaml_failsafe():
    assert wu.split_frontmatter("---\n: : bad\n---\nbody")[0] == {}


def test_split_non_dict_yaml_failsafe():
    assert wu.split_frontmatter("---\n- a\n- b\n---\nbody") == ({}, "", "---\n- a\n- b\n---\nbody")


def test_split_normalizes_crlf():
    fm, _, body = wu.split_frontmatter("---\r\ntype: concept\r\n---\r\nbody")
    assert fm == {"type": "concept"} and "body" in body


def test_split_inline_dashes_not_a_fence():
    fm, _, _ = wu.split_frontmatter("---\nkey: ---foo\ntitle: Y\n---\nbody")
    assert fm == {"key": "---foo", "title": "Y"}


# --------------------------------------------------------------------------- #
# wiki_md_files + git_lines.
# --------------------------------------------------------------------------- #

def test_wiki_md_files_sorted_recursive(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "z.md").write_text("z")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "ignore.txt").write_text("x")
    out = wu.wiki_md_files(tmp_path)
    # full-path sort: 'a/z.md' < 'b.md' (the 'a' dir component sorts first)
    assert [p.name for p in out] == ["z.md", "b.md"]


def test_git_lines_takes_repo_root(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.md").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    lines = wu.git_lines("log", "--oneline", repo_root=tmp_path)
    assert len(lines) == 1 and "init" in lines[0]


# --------------------------------------------------------------------------- #
# WikiSchemaConfig — the seam.
# --------------------------------------------------------------------------- #

def test_defaults_match_trading_schema():
    s = sc.WikiSchemaConfig()
    assert s.type_field == "type" and s.theme_field == "theme"
    assert s.page_soft_cap_lines == 400 and s.page_hard_cap_lines == 800
    assert s.dedup_lower == 0.78 and s.dedup_upper == 0.86
    assert "theme-index" in s.index_types and "greyzone-dedup" in s.kinds


def test_theme_slug_and_index_filename():
    s = sc.WikiSchemaConfig()
    assert s.theme_slug("Options/Positioning Risk") == "options-positioning-risk"
    assert s.index_filename("Macro Transmission") == "macro-transmission-index.md"


def test_load_overrides_from_dict():
    s = sc.load_wiki_schema({"page_soft_cap_lines": 250, "theme_field": "category",
                             "index_types": ["idx"], "dedup_upper": 0.9})
    assert s.page_soft_cap_lines == 250 and s.theme_field == "category"
    assert s.index_types == ("idx",) and s.dedup_upper == 0.9
    assert s.title_field == "title"          # untouched default


def test_load_none_is_defaults():
    assert sc.load_wiki_schema(None) == sc.WikiSchemaConfig()


def test_load_ignores_unknown_keys_failopen():
    s = sc.load_wiki_schema({"bogus_key": 1, "page_soft_cap_lines": 123})
    assert s.page_soft_cap_lines == 123


# --------------------------------------------------------------------------- #
# Portability guard — no consumer literal in the generic foundation.
# --------------------------------------------------------------------------- #

def test_no_trading_or_path_literal():
    for mod in (sc, wu):
        src = Path(mod.__file__).read_text().lower()
        assert "trading" not in src
        assert "/users/" not in src
