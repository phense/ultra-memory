"""The scaffold must emit a stub that IMPORTS, INSTANTIATES, and write-smoke-passes out
of the box (its defaults produce a valid page) — and contains all 6 override hooks."""
import importlib.util
from pathlib import Path
from ultra_memory import wiki_gateway

HOOKS = ["route", "theme_for", "render_frontmatter",
         "dedup_check", "derive_anchor", "confidence_label"]

def _load(path: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_scaffold_text_has_all_six_hooks_and_config_snippet():
    text = wiki_gateway.render_scaffold(class_name="MyGw", topic="research")
    for h in HOOKS:
        assert f"def {h}(" in text, f"scaffold missing hook {h}"
    assert "class MyGw(WikiGateway)" in text
    assert 'wiki_gateway = "' in text  # the config.toml snippet
    assert "research" in text

def test_scaffold_stub_imports_instantiates_and_writes(tmp_path):
    out = tmp_path / "my_gw.py"
    wiki_gateway.scaffold_to_file(out, class_name="MyGw", topic="research")
    mod = _load(out, "my_gw")
    gw = mod.MyGw(wiki_root=tmp_path / "wiki", topic="research")     # instantiates
    # turnkey defaults: create_page lands a valid page
    (tmp_path / "wiki" / "research" / "concepts").mkdir(parents=True)
    res = gw.create_page(Path("research/concepts/x.md"), "**Mechanism**: smoke.\n",
                         topic="research", wiki_root=tmp_path / "wiki")
    assert res == "written"
    assert (tmp_path / "wiki" / "research" / "concepts" / "x.md").is_file()
