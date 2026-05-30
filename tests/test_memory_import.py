from ultra_memory import memory_import as mi

_SAMPLE = '''---
name: feedback-x
description: "HARD RULE — never use the API; keep at 08:30 sharp"
metadata:
  node_type: memory
  type: feedback
  originSessionId: abc-123
---

Body line one.

Body --- with a dashes line.
'''


def test_split_frontmatter_extracts_fields_and_body():
    fm, body = mi.split_frontmatter(_SAMPLE)
    assert fm["name"] == "feedback-x"
    assert fm["description"] == "HARD RULE — never use the API; keep at 08:30 sharp"
    assert fm["metadata"]["type"] == "feedback"
    assert fm["metadata"]["node_type"] == "memory"
    assert fm["metadata"]["originSessionId"] == "abc-123"
    assert body.startswith("Body line one.")
    assert "--- with a dashes line." in body  # body delimiters not mis-parsed


def test_split_frontmatter_no_frontmatter_returns_text():
    fm, body = mi.split_frontmatter("just text\nno fm")
    assert fm == {} and body == "just text\nno fm"


def test_parse_memory_index_reads_title_and_hook():
    text = (
        "- [Claude OAuth-only](feedback_claude_oauth_only.md) — every LLM call uses OAuth\n"
        "- [No hook here](bare.md)\n"
    )
    idx = mi.parse_memory_index(text)
    assert idx["feedback_claude_oauth_only"]["title"] == "Claude OAuth-only"
    assert idx["feedback_claude_oauth_only"]["hook"] == "every LLM call uses OAuth"
    assert idx["bare"]["title"] == "No hook here"
    assert idx["bare"]["hook"] is None
