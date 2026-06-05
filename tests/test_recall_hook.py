"""Recall-Reflex Phase 3 — the UserPromptSubmit Tier-2 hook.

On a CONCRETE error signature in the prompt, the hook itself calls recall() and
injects prior art as additionalContext. Tier-2 only (the fuzzy Tier-1 nag is not
built). Fail-open, knowledge-only (privacy-safe + no fastembed load), frugal (<=3).
"""
import io
import json

from ultra_memory import memory_lib, wiki_sync
from ultra_memory.hooks import recall_prompt


def _ready_db(tmp_path, slug, title, body):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    conn.execute("INSERT INTO meta(key,value) VALUES('import_complete','1') "
                 "ON CONFLICT(key) DO UPDATE SET value='1'")
    root = tmp_path / "wiki" / "trading" / "concepts"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{slug}.md").write_text(
        f"---\ntype: mechanism\ntitle: {title}\n---\n\n{body}\n", encoding="utf-8")
    wiki_sync.wiki_sync(conn, [tmp_path / "wiki"], embedder=None, ts=1)
    conn.commit()
    conn.close()
    return tmp_path / "m.db"


def _run(payload, monkeypatch, dbp):
    monkeypatch.setenv("ULTRA_MEMORY_DB", str(dbp))
    out = io.StringIO()
    rc = recall_prompt.main(io.StringIO(json.dumps(payload)), out)
    return rc, out.getvalue()


# --- detect_signature -------------------------------------------------------

def test_detect_signature_fires_on_traceback_and_exception():
    sig = recall_prompt.detect_signature(
        "Traceback (most recent call last):\n  File 'x.py', line 3\nValueError: bad")
    assert sig and "ValueError" in sig


def test_detect_signature_none_on_plain_question():
    assert recall_prompt.detect_signature(
        "How do I add a dark-mode toggle to the settings page?") is None
    assert recall_prompt.detect_signature("") is None


# --- the hook ---------------------------------------------------------------

def test_hook_injects_prior_art_on_error_signature(tmp_path, monkeypatch):
    dbp = _ready_db(
        tmp_path, "fastembed-x",
        "fastembed onnxruntime NoSuchFile model_optimized.onnx",
        "TMPDIR purge of the fastembed cache; pin via persistent_cache_dir")
    payload = {"prompt": "I hit onnxruntime NoSuchFile: .../model_optimized.onnx "
                         "— No such file or directory"}
    rc, out = _run(payload, monkeypatch, dbp)
    assert rc == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "fastembed-x" in data["hookSpecificOutput"]["additionalContext"]


def test_hook_no_injection_on_plain_question(tmp_path, monkeypatch):
    dbp = _ready_db(tmp_path, "p", "title", "body")
    rc, out = _run({"prompt": "what is a good name for a cat?"}, monkeypatch, dbp)
    assert rc == 0 and out.strip() == ""


def test_hook_killswitch_disables_injection(tmp_path, monkeypatch):
    dbp = _ready_db(tmp_path, "fastembed-x", "onnxruntime NoSuchFile", "body")
    monkeypatch.setenv("RECALL_HOOK_DISABLE", "1")
    rc, out = _run({"prompt": "ValueError: boom in x.py:3"}, monkeypatch, dbp)
    assert rc == 0 and out.strip() == ""


def test_hook_fail_open_on_unready_db(tmp_path, monkeypatch):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")  # exists but NOT import_complete
    conn.close()
    rc, out = _run({"prompt": "ValueError: boom in x.py:3"}, monkeypatch, tmp_path / "m.db")
    assert rc == 0 and out.strip() == ""
