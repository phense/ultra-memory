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


# --- M4: missing credential classes ---

def test_redacts_pem_private_key():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEpAIBAAKCAQEAabcdef0123456789\n"
           "-----END RSA PRIVATE KEY-----")
    out = strip_secrets(pem)
    assert "MIIEpAIBAAKCAQEA" not in out and R in out


def test_redacts_connection_string_userinfo():
    out = strip_secrets("db postgres://admin:s3cr3tPass@db.example.com:5432/app done")
    assert "admin:s3cr3tPass" not in out
    assert R in out
    assert "db.example.com" in out  # host/path preserved, only userinfo scrubbed


def test_redacts_provider_prefixes():
    assert R in strip_secrets("stripe sk_live_0123456789abcdefABCDEFghij")
    assert R in strip_secrets("twilio ACdeadbeefdeadbeefdeadbeefdeadbeef")
    assert R in strip_secrets(
        "sendgrid SG.0123456789abcdefABCDEF.0123456789abcdefABCDEF0123456789abcdef")


def test_redacts_passwd_pwd_assignments():
    assert R in strip_secrets("passwd=SuperSecret123")
    assert R in strip_secrets("pwd: anotherSecret9")


# --- M5: greedy keyword:colon rule must not eat hyphenated prose ---

def test_keyvalue_leaves_hyphenated_prose():
    prose = "secret = institutional-grade-discipline"
    assert strip_secrets(prose) == prose  # hyphen-joined words are not a credential


def test_keyvalue_still_redacts_real_values():
    assert R in strip_secrets('api_key="abcd1234efgh5678"')   # quoted
    assert R in strip_secrets("password: hunter2hunter2")     # has entropy/digits
