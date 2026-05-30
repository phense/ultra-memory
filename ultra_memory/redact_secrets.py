"""Pure secret-stripper — mandatory pre-persist chokepoint.

Conservative by design: redact obvious credentials, leave normal prose intact.
The key=value rule requires a delimiter AND a >=12-char value so prose like
"the api key concept" is never mangled.
"""
import re

_REDACTED = "[REDACTED]"

_PRIVATE_TAG = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

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
    re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?[A-Za-z0-9._\-]{12,}"
    ),
]


def strip_secrets(text):
    """Return `text` with private tags and credential-shaped substrings redacted.

    Pure function. Returns the input unchanged for falsy input (None / "").
    """
    if not text:
        return text
    text = _PRIVATE_TAG.sub(_REDACTED, text)
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text
