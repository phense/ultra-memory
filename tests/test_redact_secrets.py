from ultra_memory.redact_secrets import strip_secrets

R = "[REDACTED]"


def test_redacts_anthropic_key():
    assert strip_secrets("key sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF here") == f"key {R} here"


def test_redacts_github_pat():
    assert R in strip_secrets("ghp_0123456789abcdefghijABCDEFGHIJ0123")
    assert R in strip_secrets("github_pat_11ABCDEFG0123456789_abcdefghij")


def test_redacts_aws_and_google():
    assert R in strip_secrets("AKIAIOSFODNN7EXAMPLE")
    assert R in strip_secrets("AIzaSyA0123456789abcdefghijklmnopqrstuv")


def test_redacts_jwt_and_bearer():
    jwt = "eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSM"
    assert R in strip_secrets(jwt)
    assert R in strip_secrets("Authorization: Bearer abcdef0123456789ABCDEF")


def test_redacts_keyvalue_assignment():
    assert R in strip_secrets('api_key="abcd1234efgh5678"')
    assert R in strip_secrets("password: hunter2hunter2")


def test_strips_private_tags():
    assert strip_secrets("a <private>secret stuff</private> b") == f"a {R} b"


def test_leaves_prose_untouched():
    prose = "The API key concept matters; we discuss the bearer of risk and tokens broadly."
    assert strip_secrets(prose) == prose


def test_handles_empty_and_none():
    assert strip_secrets("") == ""
    assert strip_secrets(None) is None
