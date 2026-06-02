"""Gate: the WikiGateway class + all 6 override hooks must carry non-empty docstrings —
they ARE the documented extension contract (spec §4.2). Without this gate the contract
can silently regress to nothing (it shipped with zero docstrings in Phase 1)."""
import ast
import inspect
from ultra_memory import wiki_gateway

HOOKS = {"route", "theme_for", "render_frontmatter",
         "dedup_check", "derive_anchor", "confidence_label"}

def _wikigateway_classdef():
    tree = ast.parse(inspect.getsource(wiki_gateway))
    return next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "WikiGateway")

def test_class_has_docstring():
    assert ast.get_docstring(_wikigateway_classdef()), "WikiGateway needs a class docstring"

def test_all_six_hooks_have_nonempty_docstrings():
    cls = _wikigateway_classdef()
    docs = {m.name: ast.get_docstring(m) for m in cls.body
            if isinstance(m, ast.FunctionDef) and m.name in HOOKS}
    missing = sorted(h for h in HOOKS if not (docs.get(h) or "").strip())
    assert not missing, f"override hooks missing a docstring: {missing}"
