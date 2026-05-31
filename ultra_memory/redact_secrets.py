"""Pure secret-stripper — mandatory pre-persist chokepoint.

Conservative by design: redact obvious credentials, leave normal prose intact.
The keyword=value rule requires a delimiter AND a credential-shaped value (quoted,
or carrying entropy/digits) so hyphen-joined prose like "institutional-grade-
discipline" is never mangled.
"""
import re

_REDACTED = "[REDACTED]"

_PRIVATE_TAG = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

# PEM private-key blocks (whole block → one [REDACTED]).
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL | re.IGNORECASE,
)

# URI userinfo (scheme://user:password@host) — scrub the credential, keep host/path.
_URI_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s:/@]+@")

# Simple credential-shaped tokens redacted wholesale.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),
    re.compile(r"glpat-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"npm_[0-9A-Za-z]{36}"),
    re.compile(r"dop_v1_[a-f0-9]{64}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    # Provider-prefixed keys (M4).
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),                # Stripe
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),        # SendGrid
    re.compile(r"\bAC[0-9a-fA-F]{32}\b"),                                 # Twilio SID
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),   # Slack webhook
]

# keyword=value / keyword: value — value must be credential-shaped (see _looks_credential).
# keyword vocabulary includes `username` (the Webshare rotating-proxy USERNAME is a
# named secret); the value floor is 6 (not 12) so short credentials like
# `password=p4ssvalue` no longer slip through. `_looks_credential` still guards
# against mangling hyphen-joined prose.
_KEYVALUE = re.compile(
    r"(?i)(?P<pre>(?:api[_-]?key|secret|token|password|passwd|pwd|username)\s*[=:]\s*)"
    r"(?P<q>['\"]?)(?P<val>[A-Za-z0-9._\-/+]{6,})(?P=q)"
)
_HYPHEN_WORDS = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)+")


def _looks_credential(val, quoted):
    """A value after a secret-ish keyword is treated as a real credential only if it
    is quoted, or carries entropy (a digit) — and is NOT just hyphen-joined words."""
    if _HYPHEN_WORDS.fullmatch(val):
        return False  # 'institutional-grade-discipline' = prose, not a secret
    if quoted:
        return True
    return any(c.isdigit() for c in val) or "-" not in val


def _keyvalue_sub(m):
    if _looks_credential(m.group("val"), bool(m.group("q"))):
        q = m.group("q")
        return f"{m.group('pre')}{q}{_REDACTED}{q}"
    return m.group(0)


def _uri_sub(m):
    return m.group(1) + _REDACTED + "@"


def strip_secrets(text):
    """Return `text` with private tags and credential-shaped substrings redacted.

    Pure function. Returns the input unchanged for falsy input (None / "").
    """
    if not text:
        return text
    text = _PRIVATE_TAG.sub(_REDACTED, text)
    text = _PEM.sub(_REDACTED, text)
    text = _URI_USERINFO.sub(_uri_sub, text)
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    text = _KEYVALUE.sub(_keyvalue_sub, text)
    return text
