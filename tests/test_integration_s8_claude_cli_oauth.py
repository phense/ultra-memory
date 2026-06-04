"""Integration tests for seam S8-claude-cli-oauth.

The HARD project rule: *every* LLM call in this codebase must run on the operator's
Claude Max OAuth subscription via the local `claude` CLI — NEVER the metered
Anthropic API (no `anthropic` SDK, no `ANTHROPIC_API_KEY`, no `api.anthropic.com`,
no `client.messages.create`, no `cache_control`). `ultra_memory/claude_cli.py` is
the single chokepoint that enforces this; every other module that wants an LLM
call must route through `run_claude`.

These tests exercise that seam two ways:

1. Static, package-wide invariants (filesystem + AST over the whole shipped
   package) — turn the otherwise-unguarded OAuth-only rule into a regression
   test, and pin `claude_cli.py` as the *sole* place the metered-API surface is
   even named. This mirrors the existing `test_no_hardcoded_paths.py` guard.
2. Dynamic env-plumbing through the real `run_claude` -> `_child_env` seam with
   an injected capture-only runner (no subprocess, no network): the production
   `env=None` branch that inherits `os.environ`, recursion-marker stripping on
   the *inherited* env, fail-closed on a poisoned real environ, and the positive
   side of the contract — the OAuth credential actually reaching the child env.

Hermetic: no real `claude` process is ever spawned (runner is injected), no
network, no DB, no real `data/memory.db`.
"""
import ast
import pathlib
import re

import pytest

from ultra_memory import claude_cli
from ultra_memory.claude_cli import OAuthViolation


# --------------------------------------------------------------------------- #
# Shared helpers — reuse the package-walk pattern from test_no_hardcoded_paths #
# and the capture-runner pattern from test_claude_cli.                         #
# --------------------------------------------------------------------------- #

_PKG = pathlib.Path(__file__).resolve().parent.parent / "ultra_memory"

# The one module allowed to *name* the metered-API surface (it is the guard that
# refuses it). Keep the allowlist a single file so the chokepoint cannot drift.
_API_SURFACE_ALLOWLIST = {"claude_cli.py"}


def _py_files():
    """Every shipped .py under ultra_memory/, excluding bytecode caches."""
    return [
        p
        for p in sorted(_PKG.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


class FakeProc:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(captured, proc=None):
    """A subprocess.run-compatible runner that records args instead of spawning."""

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc or FakeProc()

    return runner


# --------------------------------------------------------------------------- #
# 1. Static invariant: no anthropic SDK anywhere in the shipped package.       #
# --------------------------------------------------------------------------- #

def test_package_has_no_anthropic_sdk_import():
    """No module in ultra_memory/ may import the `anthropic` SDK (OAuth-only).

    AST-parse every shipped .py and fail on any `import anthropic[...]` or
    `from anthropic[...] import ...`. This is the regression guard for the
    HARD OAuth-only rule: the only sanctioned LLM path is the `claude` CLI
    subprocess in claude_cli.py, which imports nothing from anthropic.
    """
    offenders = []
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level>0 (relative) imports have module possibly None; skip those.
                modules = [node.module] if node.module else []
            else:
                continue
            for mod in modules:
                if mod and mod.split(".")[0] == "anthropic":
                    offenders.append(
                        f"{p.relative_to(_PKG.parent)}:{node.lineno}: import {mod}"
                    )
    assert not offenders, (
        "anthropic SDK import(s) found in shipped ultra_memory/ code — violates "
        "the HARD OAuth-only rule. All LLM calls must route through the `claude` "
        "CLI via claude_cli.run_claude, never the anthropic SDK:\n"
        + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- #
# 2. Static invariant: the metered-API surface is named ONLY in claude_cli.py. #
# --------------------------------------------------------------------------- #

# Any occurrence of these tokens means code is *aware of* the metered API path.
# They are permitted only inside the guard file (which exists to refuse them).
_API_SURFACE = re.compile(
    r"ANTHROPIC_API_KEY"
    r"|api\.anthropic\.com"
    r"|messages\.create"
    r"|cache_control"
)


def test_no_metered_api_surface_outside_claude_cli():
    """`ANTHROPIC_API_KEY` / api.anthropic.com / messages.create / cache_control
    may appear ONLY in the allowlisted chokepoint (claude_cli.py).

    Locks the single place the metered API is even mentioned, so a future module
    can't quietly reach for the API key or the SDK's `messages.create` /
    `cache_control` surface. Mirrors the hardcoded-paths guard structure.
    """
    offenders = []
    for p in _py_files():
        if p.name in _API_SURFACE_ALLOWLIST:
            continue
        for lineno, line in enumerate(
            p.read_text(encoding="utf-8").splitlines(), 1
        ):
            if _API_SURFACE.search(line):
                offenders.append(
                    f"{p.relative_to(_PKG.parent)}:{lineno}: {line.strip()}"
                )
    assert not offenders, (
        "Metered-API surface named outside the claude_cli.py chokepoint — "
        "OAuth-only rule says the API must never be reached. Route through "
        "claude_cli.run_claude instead:\n" + "\n".join(offenders)
    )


def test_claude_cli_actually_names_the_guarded_surface():
    """Sanity anchor for the allowlist: claude_cli.py really is where the
    ANTHROPIC_API_KEY guard lives. Without this, the allowlist could silently be
    over-broad (e.g. if the guard were deleted, the negative test above would
    still pass and the protection would be gone)."""
    guard = (_PKG / "claude_cli.py").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in guard, (
        "claude_cli.py no longer references ANTHROPIC_API_KEY — the OAuth-only "
        "guard appears to have been removed; the allowlist is now meaningless."
    )


# --------------------------------------------------------------------------- #
# 3. Dynamic seam: env=None inherits os.environ, strips recursion markers,     #
#    and fails closed on a poisoned real environ.                              #
# --------------------------------------------------------------------------- #

def test_env_none_inherits_os_environ_and_strips_recursion_markers(monkeypatch):
    """Production call site uses env=None -> _child_env reads os.environ.

    Exercises the real env=None branch (claude_cli.py:27) end-to-end through
    run_claude with an injected capture runner: the OAuth token from the live
    environ must reach the child, while the inherited Claude-Code recursion
    markers (issue-#149 class) must be stripped before the child is spawned.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-from-environ")
    # Simulate running *inside* a Claude Code session (the recursion hazard).
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-xyz")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", "/path/to/claude")
    # Make sure no metered key is hanging around in this test's environ.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured = {}
    out = claude_cli.run_claude(
        "prompt", model="m", runner=make_runner(captured), env=None
    )
    assert out == "ok"

    child_env = captured["kwargs"]["env"]
    # OAuth credential inherited from the real environ reaches the child.
    assert child_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-from-environ"
    # Recursion markers stripped from the *inherited* env (not just an explicit dict).
    for marker in (
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
    ):
        assert marker not in child_env, f"{marker} leaked into child env via env=None"


def test_env_none_refuses_when_real_environ_has_api_key(monkeypatch):
    """If the inherited process environ carries ANTHROPIC_API_KEY, the env=None
    path must fail closed (OAuthViolation) before any runner is invoked — a
    poisoned environ must never silently route through the metered API."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-from-environ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-refused")

    captured = {}
    with pytest.raises(OAuthViolation):
        claude_cli.run_claude(
            "prompt", model="m", runner=make_runner(captured), env=None
        )
    # Fail-closed: the runner must NOT have been called.
    assert "cmd" not in captured, "runner ran despite ANTHROPIC_API_KEY in environ"


# --------------------------------------------------------------------------- #
# 4. Dynamic seam: the OAuth credential is forwarded to the child env.         #
# --------------------------------------------------------------------------- #

def test_oauth_token_forwarded_to_child_env():
    """Positive half of the contract: the explicit-env path forwards
    CLAUDE_CODE_OAUTH_TOKEN into the dict handed to the subprocess runner, so the
    child `claude` process can authenticate via OAuth. Existing tests only cover
    the *absence* (refusal) path; this closes the credential-delivery half."""
    captured = {}
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-abc", "UNRELATED": "keepme"}
    claude_cli.run_claude(
        "prompt", model="m", runner=make_runner(captured), env=env
    )
    child_env = captured["kwargs"]["env"]
    assert child_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-abc"
    # Unrelated vars are passed through (we sanitize, not whitelist).
    assert child_env.get("UNRELATED") == "keepme"
    # And the metered key is never present in a clean child env.
    assert "ANTHROPIC_API_KEY" not in child_env
