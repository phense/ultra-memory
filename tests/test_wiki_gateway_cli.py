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
