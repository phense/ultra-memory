"""Generic wiki-maintenance — slice 6: adjudicate (the OAuth Stage-2 decision).

Reads the Stage-1 worklist, makes ALL decisions (grey-zone dedup verdicts + ONE
batched LLM call per root), THEN applies writes through the consumer gateway. No LLM
call ever runs inside a write. The LLM call is injected (`claude_call`) so the OAuth
chokepoint (run_claude) is the default but tests never spawn a process. The decision
prompt, the redirect-stub template, the topic derivation and the merge threshold are
schema/config seams; the gateway is the consumer's wiki_lib.
"""
import json
from pathlib import Path

from ultra_memory.wiki_maintenance import adjudicate as adj
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def _write_wl(tmp_path, items=None, autofixes=None, wiki_root="wiki"):
    w = wl.new_worklist(wiki_root, generated_at="2026-06-02")
    for it in items or []:
        w["items"].append({**{"source": "wiki", "kind": "cross-link", "atomic_path": "wiki/c/a.md",
                              "section_anchor": None, "theme": None, "title": "A", "claim": "c",
                              "candidate_path": None, "candidate_text": None, "cosine": None,
                              "evidence": "", "priority": 3, "root": None}, **it})
    for fx in autofixes or []:
        w["auto_fixes_applied"].append(fx)
    p = tmp_path / "wl.json"
    wl.write_worklist(w, p)
    return p


# --------------------------------------------------------------------------- #
# skip-if-empty / no-items — ZERO LLM calls.
# --------------------------------------------------------------------------- #

def test_empty_worklist_makes_no_llm_call(tmp_path):
    calls = []
    p = _write_wl(tmp_path)
    rc = adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                        claude_call=lambda *a, **k: calls.append(1) or "{}")
    assert rc == 0 and calls == []


def test_only_autofixes_no_llm_call(tmp_path):
    calls = []
    p = _write_wl(tmp_path, autofixes=[{"kind": "x", "path": "p", "detail": "d"}])
    rc = adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                        claude_call=lambda *a, **k: calls.append(1) or "{}")
    assert rc == 0 and calls == []


# --------------------------------------------------------------------------- #
# Phase 2 — bundled LLM decision + apply.
# --------------------------------------------------------------------------- #

def test_bundled_call_parses_and_applies(tmp_path):
    p = _write_wl(tmp_path, items=[{"kind": "cross-link", "atomic_path": "wiki/c/a.md"}])
    applied = []
    fake_apply = {"edit": lambda a: applied.append(a), "create-page": lambda a: None,
                  "log": lambda a: None, "redirect-stub": lambda a: None}

    def claude_call(prompt, **kw):
        return json.dumps({"actions": [
            {"op": "edit", "page": "wiki/c/a.md", "old_string": "x", "new_string": "y"}]})

    rc = adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                        claude_call=claude_call, apply_fns=fake_apply, sys_prompt="SYS")
    assert rc == 0 and len(applied) == 1 and applied[0]["page"] == "wiki/c/a.md"


def test_parse_failure_writes_nothing_returns_1(tmp_path):
    p = _write_wl(tmp_path, items=[{"kind": "cross-link"}])
    applied = []
    fake_apply = {"edit": lambda a: applied.append(a), "create-page": lambda a: None,
                  "log": lambda a: None, "redirect-stub": lambda a: None}
    rc = adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                        claude_call=lambda *a, **k: "not json at all", apply_fns=fake_apply,
                        sys_prompt="SYS")
    assert rc == 1 and applied == []


def test_unknown_op_and_malformed_action_skipped(tmp_path):
    p = _write_wl(tmp_path, items=[{"kind": "cross-link"}])
    applied = []
    fake_apply = {"edit": lambda a: applied.append(a), "create-page": lambda a: None,
                  "log": lambda a: None, "redirect-stub": lambda a: None}

    def claude_call(prompt, **kw):
        return json.dumps({"actions": [
            {"op": "bogus", "x": 1},                                   # unknown op
            {"op": "edit", "page": "p", "old_string": 5, "new_string": "y"},  # non-str
            {"op": "edit", "page": "ok.md", "old_string": "a", "new_string": "b"}]})

    rc = adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                        claude_call=claude_call, apply_fns=fake_apply, sys_prompt="SYS")
    assert rc == 0 and len(applied) == 1 and applied[0]["page"] == "ok.md"


# --------------------------------------------------------------------------- #
# Phase 1 — grey-zone dedup verdicts.
# --------------------------------------------------------------------------- #

def test_auto_merge_greyzone_emits_redirect_stub(tmp_path):
    # cosine >= dedup_upper → the default merge_decider auto-merges → redirect-stub
    p = _write_wl(tmp_path, items=[{
        "kind": "greyzone-dedup", "atomic_path": "wiki/c/dup.md", "claim": "dup text",
        "candidate_path": "wiki/c/orig.md", "candidate_text": "orig text", "cosine": 0.92,
        "priority": 1}])
    stubs = []
    fake_apply = {"edit": lambda a: None, "create-page": lambda a: None,
                  "log": lambda a: None, "redirect-stub": lambda a: stubs.append(a)}
    rc = adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                        claude_call=lambda *a, **k: '{"actions": []}', apply_fns=fake_apply,
                        sys_prompt="SYS")
    assert rc == 0 and len(stubs) == 1
    assert stubs[0]["page"] == "wiki/c/dup.md" and stubs[0]["canonical"] == "orig"


def test_greyzone_merge_decider_injectable(tmp_path):
    # an in-band pair is NOT auto-merged by default, but an injected decider can
    p = _write_wl(tmp_path, items=[{
        "kind": "greyzone-dedup", "atomic_path": "wiki/c/dup.md", "claim": "a",
        "candidate_path": "wiki/c/orig.md", "candidate_text": "b", "cosine": 0.80,
        "priority": 3}])
    stubs = []
    fake_apply = {"edit": lambda a: None, "create-page": lambda a: None,
                  "log": lambda a: None, "redirect-stub": lambda a: stubs.append(a)}
    # default: no merge
    adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                   claude_call=lambda *a, **k: '{"actions": []}', apply_fns=fake_apply,
                   sys_prompt="SYS")
    assert stubs == []
    # injected decider: always merge
    adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                   claude_call=lambda *a, **k: '{"actions": []}', apply_fns=fake_apply,
                   sys_prompt="SYS", merge_decider=lambda cosine, claim, cand: True)
    assert len(stubs) == 1


# --------------------------------------------------------------------------- #
# root threading + apply implementations.
# --------------------------------------------------------------------------- #

def test_actions_stamped_with_group_root(tmp_path):
    p = _write_wl(tmp_path, items=[{"kind": "cross-link", "root": "/x/wiki"}])
    seen = []
    fake_apply = {"edit": lambda a: seen.append(a), "create-page": lambda a: None,
                  "log": lambda a: None, "redirect-stub": lambda a: None}
    adj.adjudicate(p, gateway=tmp_path / "gw.py", model="m",
                   claude_call=lambda *a, **k: json.dumps({"actions": [
                       {"op": "edit", "page": "wiki/c/a.md", "old_string": "x", "new_string": "y"}]}),
                   apply_fns=fake_apply, sys_prompt="SYS")
    assert seen and seen[0]["root"] == "/x/wiki"


def test_real_apply_edit_exact_once(tmp_path):
    page = tmp_path / "wiki" / "c" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("hello world", encoding="utf-8")
    fns = adj.real_apply_fns(gateway=tmp_path / "gw.py", cwd=tmp_path)
    fns["edit"]({"op": "edit", "page": "wiki/c/a.md", "old_string": "world", "new_string": "there"})
    assert page.read_text() == "hello there"


def test_real_apply_redirect_stub_uses_schema_type(tmp_path):
    page = tmp_path / "wiki" / "c" / "dup.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\ntitle: Dup\ncreated: 2026-01-01\n---\nbody", encoding="utf-8")
    schema = WikiSchemaConfig(redirect_type="merged-into")
    fns = adj.real_apply_fns(gateway=tmp_path / "gw.py", cwd=tmp_path, today="2026-06-02", schema=schema)
    fns["redirect-stub"]({"op": "redirect-stub", "page": "wiki/c/dup.md", "canonical": "orig"})
    out = page.read_text()
    assert "type: merged-into" in out and "redirect_to: [[orig]]" in out
    assert "Merged into [[orig]]." in out


def test_real_apply_create_page_shells_gateway(tmp_path):
    calls = []

    class R:
        returncode = 0
        stderr = ""

    def runner(cmd, **kw):
        calls.append((cmd, kw.get("cwd")))
        return R()

    fns = adj.real_apply_fns(gateway=Path("/g/wiki_lib.py"), runner=runner, cwd=tmp_path,
                             default_topic="trading")
    fns["create-page"]({"op": "create-page", "path": "wiki/trading/concepts/x.md", "content": "# X"})
    assert calls and "create-page" in calls[0][0]
    assert "--topic" in calls[0][0] and calls[0][0][calls[0][0].index("--topic") + 1] == "trading"


# --------------------------------------------------------------------------- #
# M1: gateway_prefix — the RESOLVED argv prefix replaces the hardcoded ["uv","run",gw].
# --------------------------------------------------------------------------- #

def _capture_runner():
    calls = []

    class R:
        returncode = 0
        stderr = ""

    def runner(cmd, **kw):
        calls.append((cmd, kw.get("cwd")))
        return R()

    return runner, calls


def test_real_apply_create_page_uses_builtin_prefix(tmp_path):
    """gateway_prefix=['python','-m','ultra_memory.wiki_gateway'] → the create-page
    argv is prefix + [verb, …, --from-file, tmp], NOT ['uv','run',<gw>,…]."""
    runner, calls = _capture_runner()
    prefix = ["python", "-m", "ultra_memory.wiki_gateway"]
    fns = adj.real_apply_fns(gateway_prefix=prefix, runner=runner, cwd=tmp_path,
                             default_topic="trading")
    fns["create-page"]({"op": "create-page", "path": "wiki/trading/concepts/x.md", "content": "# X"})
    cmd = calls[0][0]
    assert cmd[:3] == prefix
    assert cmd[3] == "create-page"
    assert "uv" not in cmd and "run" not in cmd
    assert "--from-file" in cmd


def test_real_apply_create_page_uses_gateway_class_prefix(tmp_path):
    """A --gateway-class prefix is preserved verbatim ahead of the verb."""
    runner, calls = _capture_runner()
    prefix = ["python", "-m", "ultra_memory.wiki_gateway",
              "--gateway-class", "wiki_lib:TradingWikiGateway"]
    fns = adj.real_apply_fns(gateway_prefix=prefix, runner=runner, cwd=tmp_path)
    fns["log"]({"op": "log", "message": "hi"})
    cmd = calls[0][0]
    assert cmd[:5] == prefix
    assert cmd[5] == "log"
    assert "--gateway-class" in cmd


def test_real_apply_prefix_none_falls_back_to_uv_run_gateway(tmp_path):
    """Back-compat: no gateway_prefix → the legacy ['uv','run',<gateway>] argv. Existing
    callers that pass only `gateway=` keep working byte-identically."""
    runner, calls = _capture_runner()
    fns = adj.real_apply_fns(gateway=Path("/g/wiki_lib.py"), runner=runner, cwd=tmp_path)
    fns["log"]({"op": "log", "message": "hi"})
    cmd = calls[0][0]
    assert cmd[:3] == ["uv", "run", "/g/wiki_lib.py"]
    assert cmd[3] == "log"


def test_adjudicate_threads_gateway_prefix_into_apply(tmp_path):
    """adjudicate(gateway_prefix=…) flows the prefix into real_apply_fns so a create-page
    action shells the resolved prefix (the end-to-end M1 wire)."""
    runner, calls = _capture_runner()
    p = _write_wl(tmp_path, items=[{"kind": "synthesis-candidate", "title": "S"}])
    prefix = ["python", "-m", "ultra_memory.wiki_gateway",
              "--gateway-class", "wiki_lib:TradingWikiGateway"]
    rc = adj.adjudicate(
        p, gateway_prefix=prefix, model="m", runner=runner,
        claude_call=lambda *a, **k: json.dumps({"actions": [
            {"op": "create-page", "path": "wiki/trading/synthesis/s.md", "content": "# S"}]}),
        fallback_cwd=tmp_path)
    assert rc == 0
    assert calls, "no gateway verb was shelled"
    cmd = calls[0][0]
    assert cmd[:5] == prefix and "create-page" in cmd


# --------------------------------------------------------------------------- #
# build_sys + guard.
# --------------------------------------------------------------------------- #

def test_build_sys_is_schema_driven_and_literal_free():
    sys_prompt = adj.build_sys(WikiSchemaConfig(), ["momentum-index", "vol-index"])
    assert "momentum-index" in sys_prompt
    low = sys_prompt.lower()
    assert "trading-strategies" not in low and "earnings-plays" not in low


def test_no_trading_or_path_literal():
    src = Path(adj.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src
