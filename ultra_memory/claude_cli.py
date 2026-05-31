"""OAuth-only Claude CLI chokepoint — the single LLM call path in ultra-memory.

Env-sanitized to (a) honor the OAuth-only hard rule (refuse if ANTHROPIC_API_KEY
is set; require CLAUDE_CODE_OAUTH_TOKEN) and (b) prevent the #149 recursion class
by stripping inherited Claude-Code session markers. Off-session use only.
"""
import os
import subprocess

_STRIP_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
)


class OAuthViolation(RuntimeError):
    """Raised when the env would route the call through the metered API or lacks OAuth."""


class ClaudeCliError(RuntimeError):
    """Raised when the claude CLI exits non-zero."""


def _child_env(base):
    env = dict(os.environ if base is None else base)
    for key in _STRIP_ENV:
        env.pop(key, None)
    if env.get("ANTHROPIC_API_KEY", "").strip():
        raise OAuthViolation(
            "ANTHROPIC_API_KEY is set; refusing to run (OAuth-only hard rule)."
        )
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        raise OAuthViolation(
            "CLAUDE_CODE_OAUTH_TOKEN missing; cannot authenticate via OAuth."
        )
    # Drop an empty ANTHROPIC_API_KEY so it can never be reintroduced downstream.
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def run_claude(prompt, *, model, system=None, claude_bin="claude",
               timeout=120, runner=subprocess.run, env=None):
    """Run one off-session claude CLI call on the OAuth subscription.

    `runner` is injectable (subprocess.run-compatible) so tests never spawn a process.
    Returns stdout text; raises OAuthViolation / ClaudeCliError.
    """
    if not model or not str(model).strip():
        raise ValueError("run_claude: a non-empty model is required")
    child_env = _child_env(env)
    cmd = [claude_bin, "--model", model]
    if system is not None:
        cmd += ["--system-prompt", system]
    cmd += ["-p", prompt, "--output-format", "text"]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, env=child_env)
    except FileNotFoundError as exc:
        raise ClaudeCliError(
            f"claude binary not found ({claude_bin!r}); is the CLI installed and on PATH?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude exited {proc.returncode}: {(proc.stderr or '')[:500]}"
        )
    return proc.stdout
