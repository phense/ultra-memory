import pytest

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


# ---------------------------------------------------------------------------
# LOCKING SUITE A — every major token format, at its REAL vendor-spec length,
# MUST be redacted. Each token below is sized to its documented real-world
# length (e.g. a GitHub classic PAT carries 36 chars after `ghp_`, well past
# the `{20,}` floor; a Google API key is exactly 39 chars). This pins the
# patterns so a future tightening edit can't silently shrink coverage.
#
# Provenance of the `ghp_` chassis finding: the prefix pattern is
# `gh[pousr]_[A-Za-z0-9]{20,}` and a realistic classic PAT (36 body chars) IS
# redacted (asserted below). The chassis "ghp_ not stripped" report was a
# short-placeholder false alarm — a token with <20 body chars (not a valid
# GitHub token) is the only thing the rule lets through, which is correct
# conservative behavior, not a gap.
# ---------------------------------------------------------------------------

# (vendor, realistic-spec token) — bodies sized to the documented real length.
_REALISTIC_TOKENS = [
    ("anthropic_sk_ant", "sk-ant-api03-" + "aB3" * 20),
    ("github_classic_ghp", "ghp_0123456789abcdefghijABCDEFGHIJ012345"),  # 36 body
    ("github_fine_pat", "github_pat_11ABCDE0z0aBcDeFgHiJ_" + "kLmNoPqRsT" * 5),
    ("aws_akia", "AKIAIOSFODNN7EXAMPLE"),
    ("google_aiza", "AIza0123456789abcdefghijklmnopqrstuvwxy"),  # 39 total
    ("slack_xoxb", "xoxb-1234567890-abcdefghij"),
    ("gitlab_glpat", "glpat-0123456789abcdefXYZ_"),
    ("npm_token", "npm_0123456789abcdefABCDEFghijklmnopqrst"),  # 36 body
    ("digitalocean_dop", "dop_v1_" + "a0" * 32),  # 64 hex
    ("stripe_sk_live", "sk_live_0123456789abcdefABCDEF"),
    ("stripe_rk_live", "rk_live_0123456789abcdefABCDEF"),
    ("sendgrid_sg", "SG.0123456789abcdefABCD."
                    "0123456789abcdefABCDEF0123456789abcdef"),
    ("twilio_ac_sid", "ACdeadbeefdeadbeefdeadbeefdeadbeef"),  # 32 hex
    ("slack_webhook",
     "https://hooks.slack.com/services/T00000000/B00000000/"
     "XXXXXXXXXXXXXXXXXXXXXXXX"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4"),
    ("bearer", "Authorization: Bearer abcdef0123456789ABCDEF"),
]


@pytest.mark.parametrize("name,token", _REALISTIC_TOKENS,
                         ids=[n for n, _ in _REALISTIC_TOKENS])
def test_realistic_token_is_redacted(name, token):
    out = strip_secrets(f"prefix {token} suffix")
    assert R in out, f"{name}: realistic token NOT redacted"
    assert token not in out, f"{name}: raw token survived redaction"


def test_realistic_github_classic_pat_redacted():
    """Direct lock for the chassis 'ghp_ not stripped' finding: a real GitHub
    classic PAT has 36 chars after `ghp_` (>= the {20,} floor) and IS stripped."""
    pat = "ghp_" + "x" * 36
    assert strip_secrets(pat) == R


# ---------------------------------------------------------------------------
# LOCKING SUITE B — NO OVER-REDACTION. `strip_secrets` runs on EVERY wiki write
# (Trading's wiki_lib) and EVERY memory write, so wrongly redacting legitimate
# content is a data-corruption bug. These representative samples — kebab-case
# slugs, wikilinks, git SHAs, prose, code, paths, numbers, dates, timestamps —
# MUST pass through byte-for-byte UNCHANGED. The critical invariant is that
# standalone kebab-case is never touched: `_HYPHEN_WORDS` is referenced ONLY
# inside `_looks_credential`, which fires only after a secret-keyword+delimiter
# match — never on bare slugs/wikilinks (asserted structurally below).
# ---------------------------------------------------------------------------

_LEGIT_CONTENT = [
    # kebab-case wiki slugs / wikilinks
    "macro-monetary-index",
    "[[hawkish-fed-bear-flattening]]",
    "agriculture-supply/diesel-3685",
    "kebab-case-slug-with-many-parts-here",
    "[[german-tax-us-options-yearend-trap]] close-out 2026-12-30",
    "macro-transmission-mechanisms.md and vol-vibes-calendar/config.json",
    # git SHAs (full + short)
    "8c8f439f462d122138b312395f6a01f68262679b",
    "bc7cf27",
    "version v2.0.0 of hard-rules; commit d7407d8 chore(memory)",
    # normal prose containing secret-ish *words* (no delimiter+credential value)
    "The credit-spread strategy uses a trailing-ladder profit-management approach.",
    "The bearer of risk; an api_key concept; a token of appreciation.",
    "secret-sauce of the strategy is the trailing ladder",
    "token-bucket rate limiting; api-key rotation policy",
    # secret-keyword + delimiter but pure-alpha kebab VALUE => prose, not credential
    "token: bull-put-credit-spread",
    "secret = hawkish-fed-bear-flattening",
    "username: macro-monetary-index",
    "key: support-resistance-zones",
    # code identifiers, file paths
    "def compute_iv_rank(close_prices): return None  # snake_case",
    "see scripts/wiki_lib.py and trading-strategies/_global/hard-rules.json",
    # numbers, prices, dates, ISO timestamps
    "Entry price was 4385.25, stop at 4350, target 4500.",
    "RSI(14) crossed above 70 at 2026-05-31T13:30:00Z on the 4-hour chart",
]


@pytest.mark.parametrize("sample", _LEGIT_CONTENT)
def test_legitimate_content_is_unchanged(sample):
    assert strip_secrets(sample) == sample, "legitimate content was over-redacted"


def test_no_over_redaction_in_combined_document():
    """A whole multi-line wiki-style note must survive untouched — guards against
    cross-line / greedy interactions that per-sample tests could miss."""
    doc = "\n".join(_LEGIT_CONTENT)
    assert strip_secrets(doc) == doc


def test_hyphen_words_never_applied_standalone():
    """Structural lock: `_HYPHEN_WORDS` (the kebab matcher) must stay confined to
    the credential-value check. If a future edit applies it in `strip_secrets`
    directly, every kebab-case slug in the live wiki would be redacted."""
    import inspect

    from ultra_memory import redact_secrets as m

    assert "_HYPHEN_WORDS" not in inspect.getsource(m.strip_secrets)
    assert "_HYPHEN_WORDS" in inspect.getsource(m._looks_credential)
