import pytest
from ultra_memory import claude_cli
from ultra_memory.claude_cli import OAuthViolation, ClaudeCliError


class FakeProc:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(captured, proc=None):
    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc or FakeProc()
    return runner


BASE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123"}


def test_builds_expected_argv():
    captured = {}
    out = claude_cli.run_claude(
        "user prompt", model="claude-haiku-4-5", system="sys",
        runner=make_runner(captured), env=dict(BASE_ENV),
    )
    assert out == "ok"
    assert captured["cmd"] == [
        "claude", "--model", "claude-haiku-4-5",
        "--system-prompt", "sys",
        "-p", "user prompt", "--output-format", "text",
    ]


def test_omits_system_flag_when_absent():
    captured = {}
    claude_cli.run_claude("p", model="m", runner=make_runner(captured), env=dict(BASE_ENV))
    assert "--system-prompt" not in captured["cmd"]


def test_strips_recursion_env():
    captured = {}
    env = dict(BASE_ENV, CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="abc",
               CLAUDE_CODE_ENTRYPOINT="cli", CLAUDE_CODE_EXECPATH="/x")
    claude_cli.run_claude("p", model="m", runner=make_runner(captured), env=env)
    child_env = captured["kwargs"]["env"]
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH"):
        assert k not in child_env


def test_refuses_when_anthropic_api_key_set():
    env = dict(BASE_ENV, ANTHROPIC_API_KEY="sk-ant-xxx")
    with pytest.raises(OAuthViolation):
        claude_cli.run_claude("p", model="m", runner=make_runner({}), env=env)


def test_refuses_when_oauth_token_missing():
    with pytest.raises(OAuthViolation):
        claude_cli.run_claude("p", model="m", runner=make_runner({}), env={})


def test_empty_anthropic_api_key_is_allowed():
    captured = {}
    env = dict(BASE_ENV, ANTHROPIC_API_KEY="")
    claude_cli.run_claude("p", model="m", runner=make_runner(captured), env=env)
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]


def test_nonzero_returncode_raises():
    with pytest.raises(ClaudeCliError):
        claude_cli.run_claude(
            "p", model="m", env=dict(BASE_ENV),
            runner=make_runner({}, proc=FakeProc(returncode=2, stderr="boom")),
        )
