# tests/test_wiki_gateway_cli.py
"""Tests for the WikiGateway argparse CLI (Task 9).

The cli() function accepts a gateway_cls (defaults to WikiGateway) and an
argv list, instantiates the gateway, dispatches to the appropriate method,
returns 0 on success / non-zero on failure.

Subcommands tested:
  create-page --path --topic --from-file
  append-validation-log --page --from-file --topic  (missing page → non-zero)
  register-index --slug --theme --summary --topic
  log --from-file | --message
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultra_memory.wiki_gateway import WikiGateway, cli, main


# ── create-page ──────────────────────────────────────────────────────────────


def test_cli_create_page_returns_0_and_file_exists(tmp_path):
    """cli create-page with a valid path returns 0 and the file exists."""
    # Prepare a from-file with content.
    content_file = tmp_path / "content.md"
    content_file.write_text("# Test\n\nSome content.\n")

    dest = tmp_path / "research" / "concepts" / "x.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    rc = cli(
        WikiGateway,
        [
            "create-page",
            "--path", str(dest),
            "--topic", "research",
            "--from-file", str(content_file),
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc == 0
    assert dest.exists()
    assert "Some content." in dest.read_text()


def test_cli_create_page_clobber_returns_nonzero(tmp_path):
    """cli create-page on an existing file returns non-zero (refuses to clobber)."""
    content_file = tmp_path / "content.md"
    content_file.write_text("# New content\n")

    dest = tmp_path / "research" / "concepts" / "existing.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("already here\n")

    rc = cli(
        WikiGateway,
        [
            "create-page",
            "--path", str(dest),
            "--topic", "research",
            "--from-file", str(content_file),
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc != 0


# ── append-validation-log ────────────────────────────────────────────────────


def test_cli_append_validation_log_missing_page_returns_nonzero(tmp_path):
    """cli append-validation-log on a missing page returns non-zero."""
    content_file = tmp_path / "entry.md"
    content_file.write_text("- 2026-06-01 | win | Test outcome\n")

    # Page does not exist.
    missing_page = tmp_path / "research" / "concepts" / "nonexistent.md"

    rc = cli(
        WikiGateway,
        [
            "append-validation-log",
            "--page", str(missing_page),
            "--from-file", str(content_file),
            "--topic", "research",
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc != 0


def test_cli_append_validation_log_appends_and_returns_0(tmp_path):
    """cli append-validation-log on an existing page appends + returns 0."""
    content_file = tmp_path / "entry.md"
    content_file.write_text("- 2026-06-01 | win | Great result\n")

    page = tmp_path / "research" / "concepts" / "my-strat.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: mechanism\ntitle: My Strat\n---\n\n## Empirical Validation Log\n\n"
    )

    rc = cli(
        WikiGateway,
        [
            "append-validation-log",
            "--page", str(page),
            "--from-file", str(content_file),
            "--topic", "research",
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc == 0
    assert "Great result" in page.read_text()


# ── log ───────────────────────────────────────────────────────────────────────


def test_cli_log_with_message_appends_to_log_md(tmp_path):
    """cli log --message appends to log.md and returns 0."""
    log_file = tmp_path / "log.md"
    log_file.write_text("# Log\n")

    rc = cli(
        WikiGateway,
        [
            "log",
            "--message", "Test log entry",
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc == 0
    assert "Test log entry" in log_file.read_text()


def test_cli_log_with_from_file(tmp_path):
    """cli log --from-file appends to log.md and returns 0."""
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("From-file log entry")
    log_file = tmp_path / "log.md"
    log_file.write_text("# Log\n")

    rc = cli(
        WikiGateway,
        [
            "log",
            "--from-file", str(msg_file),
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc == 0
    assert "From-file log entry" in log_file.read_text()


# ── register-index ───────────────────────────────────────────────────────────


def test_cli_register_index_creates_theme_index(tmp_path):
    """cli register-index creates the theme-index and returns 0."""
    rc = cli(
        WikiGateway,
        [
            "register-index",
            "--slug", "my-mechanism",
            "--theme", "macro",
            "--summary", "A summary of the mechanism.",
            "--topic", "research",
            "--wiki-root", str(tmp_path),
        ],
    )
    assert rc == 0
    # The theme-index should have been created.
    idx = tmp_path / "research" / "concepts" / "macro-index.md"
    assert idx.exists()
    assert "my-mechanism" in idx.read_text()


# ── main() entry point ────────────────────────────────────────────────────────


def test_main_is_callable_and_returns_int(tmp_path):
    """main() is importable and callable; --help exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    # --help exits with code 0.
    assert exc_info.value.code == 0


# ── Task 11: turnkey pure-plugin install writes a page end-to-end (subprocess) ─


def test_turnkey_create_page_subprocess(tmp_path):
    """Spec §7 turnkey-integration proof: NO consumer gateway, run the built-in
    via subprocess.  create-page must land a valid frontmatter'd, redacted page
    AND write an audit row in briefings/maintenance-logs/wiki-writes-*.jsonl.

    The page content includes a credential-shaped token (sk-ant-xxx) to verify
    that the redaction chokepoint fires end-to-end through the subprocess path.
    """
    import subprocess
    import sys

    # Set up the temp wiki tree: the destination must live under
    # <wiki_root>/<topic>/concepts/ for create_page's _require_under guard.
    topic = "research"
    concepts_dir = tmp_path / topic / "concepts"
    concepts_dir.mkdir(parents=True)

    dest = concepts_dir / "liquidity-spiral.md"

    # Content includes a credential-shaped token to prove redaction fires.
    content_file = tmp_path / "content.md"
    content_file.write_text(
        "---\ntype: mechanism\ntitle: Liquidity Spiral\n---\n\n"
        "**Mechanism**: Forced selling amplifies price drops. "
        "api_key=sk-ant-abc123 is redacted.\n"
    )

    python = sys.executable  # the test-runner's Python = plugin venv
    result = subprocess.run(
        [
            python, "-m", "ultra_memory.wiki_gateway",
            "create-page",
            "--path", str(dest),
            "--topic", topic,
            "--from-file", str(content_file),
            "--wiki-root", str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"create-page subprocess failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # The page must exist and contain valid YAML frontmatter.
    assert dest.exists(), "create-page did not write the destination file"
    page_text = dest.read_text()
    assert page_text.startswith("---\n"), "page missing YAML frontmatter fence"
    assert "type:" in page_text, "page frontmatter missing 'type:' key"
    assert "Liquidity Spiral" in page_text, "page missing expected title text"

    # Redaction: the raw credential token must not appear on disk.
    assert "sk-ant-abc123" not in page_text, (
        "redaction failed: raw credential token found in written page"
    )
    # The rest of the content should be present (only the secret is stripped).
    assert "Forced selling" in page_text, "page content unexpectedly mangled"

    # Audit row: briefings/maintenance-logs/wiki-writes-<date>.jsonl must exist
    # and contain a row with op=create-page for this path.
    audit_dir = tmp_path.parent / "briefings" / "maintenance-logs"
    # The gateway writes audit rows to wiki_root/../briefings/maintenance-logs/.
    # Since wiki_root=tmp_path, the audit dir is tmp_path.parent/briefings/...
    jsonl_files = list(audit_dir.glob("wiki-writes-*.jsonl")) if audit_dir.exists() else []
    assert jsonl_files, (
        f"no wiki-writes-*.jsonl audit file found under {audit_dir}"
    )
    audit_text = "".join(f.read_text() for f in jsonl_files)
    audit_rows = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
    create_rows = [r for r in audit_rows if r.get("op") == "create-page"]
    assert create_rows, "no audit row with op='create-page' found"
    assert any(str(dest) in r.get("path", "") for r in create_rows), (
        "create-page audit row missing the expected path"
    )


def test_turnkey_append_validation_log_idempotent_subprocess(tmp_path):
    """Spec §7 turnkey append-validation-log idempotency: run twice via subprocess,
    the second call must succeed (rc=0) and NOT duplicate the entry.
    """
    import subprocess
    import sys

    topic = "research"
    concepts_dir = tmp_path / topic / "concepts"
    concepts_dir.mkdir(parents=True)

    # Seed the page with a validation-log section so the gateway can append to it.
    page = concepts_dir / "vol-contraction.md"
    page.write_text(
        "---\ntype: mechanism\ntitle: Vol Contraction\nupdated: 2026-01-01\n---\n\n"
        "## Empirical Validation Log\n\n"
    )

    entry_file = tmp_path / "entry.md"
    entry_file.write_text("- 2026-06-02 | win | IV crush post-earnings +8%\n")

    python = sys.executable

    def _run_append():
        return subprocess.run(
            [
                python, "-m", "ultra_memory.wiki_gateway",
                "append-validation-log",
                "--page", str(page),
                "--topic", topic,
                "--from-file", str(entry_file),
                "--wiki-root", str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )

    # First call: must return 0 and append the entry.
    r1 = _run_append()
    assert r1.returncode == 0, (
        f"first append-validation-log failed (rc={r1.returncode}):\n"
        f"stdout: {r1.stdout}\nstderr: {r1.stderr}"
    )
    text_after_first = page.read_text()
    assert "IV crush" in text_after_first, "entry not appended after first call"

    # Second call (idempotent): must also return 0 and NOT duplicate.
    r2 = _run_append()
    assert r2.returncode == 0, (
        f"second append-validation-log failed (rc={r2.returncode}):\n"
        f"stdout: {r2.stdout}\nstderr: {r2.stderr}"
    )
    text_after_second = page.read_text()
    # Entry must appear exactly once.
    assert text_after_second.count("IV crush") == 1, (
        "idempotency violated: entry duplicated on second append"
    )
