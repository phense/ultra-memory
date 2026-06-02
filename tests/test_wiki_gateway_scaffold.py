"""The scaffold must emit a stub that IMPORTS, INSTANTIATES, and write-smoke-passes out
of the box (its defaults produce a valid page) — and contains all 6 override hooks."""
import importlib.util
from pathlib import Path
from ultra_memory import wiki_gateway
from ultra_memory.wiki_gateway import WikiGateway, cli

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
    """Generate a scaffold stub, import the class, then drive it via the REAL CLI
    entry point — exactly like test_wiki_gateway_cli.py does.  This proves the
    stub works end-to-end without touching the engine's create_page contract."""
    # 1. Generate the scaffold stub.
    out = tmp_path / "my_gw.py"
    wiki_gateway.scaffold_to_file(out, class_name="MyGw", topic="research")
    # 2. Import and verify the stub class is present and is a WikiGateway subclass.
    mod = _load(out, "my_gw")
    assert hasattr(mod, "MyGw"), "scaffold stub is missing MyGw class"
    assert issubclass(mod.MyGw, WikiGateway), "MyGw must subclass WikiGateway"

    # 3. Set up a valid wiki tree: dest must be under <wiki_root>/research/concepts/
    #    to satisfy create_page's _require_under(path, root/"concepts", root/"synthesis").
    wiki_root = tmp_path / "wiki"
    concepts_dir = wiki_root / "research" / "concepts"
    concepts_dir.mkdir(parents=True)
    dest = concepts_dir / "x.md"

    # 4. Prepare a content file.
    content_file = tmp_path / "content.md"
    content_file.write_text("**Mechanism**: smoke test.\n")

    # 5. Call the REAL CLI with the scaffold class — same pattern as test_wiki_gateway_cli.py.
    rc = cli(
        mod.MyGw,
        [
            "create-page",
            "--path", str(dest),
            "--topic", "research",
            "--from-file", str(content_file),
            "--wiki-root", str(wiki_root),
        ],
    )
    assert rc == 0, f"cli(MyGw, create-page) returned {rc}"
    assert dest.exists(), "create-page did not write the destination file"
    assert "smoke test" in dest.read_text()
