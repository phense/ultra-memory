"""Structural gates: the skill must auto-trigger (frontmatter description) and teach the
full 6-hook contract with a worked reference; the slash command must reach the scaffold verb."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "using-wiki-gateway" / "SKILL.md"
CMD = ROOT / "commands" / "wiki-gateway-scaffold.md"
HOOKS = ["route", "theme_for", "render_frontmatter",
         "dedup_check", "derive_anchor", "confidence_label"]

def test_skill_has_frontmatter_and_names_all_hooks():
    text = SKILL.read_text()
    assert text.startswith("---"), "SKILL.md needs YAML frontmatter"
    assert "name: using-wiki-gateway" in text
    assert "description:" in text
    for h in HOOKS:
        assert h in text, f"skill must name the {h} hook"
    assert "TradingWikiGateway" in text, "skill must point at the Trading worked reference"
    assert "scaffold" in text

def test_command_shells_the_scaffold_verb():
    text = CMD.read_text()
    assert text.startswith("---") and "description:" in text
    assert "ultra_memory.wiki_gateway scaffold" in text
    assert "$CLAUDE_PLUGIN_ROOT" in text  # follows the plugin command pattern
