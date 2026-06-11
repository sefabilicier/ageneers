"""
Security sanitizer — runs before any user-controlled text reaches the LLM.

Defends against:
1. Prompt injection: phrases that try to override the system prompt.
2. Secret leakage: strips tokens / keys that may have been pasted by mistake.
3. Excessive length: truncates descriptions that exceed the safe context budget.

Design: fail-open with redaction — we sanitise and continue rather than blocking
the whole pipeline, but every redaction is logged for audit.
"""

from __future__ import annotations

import re

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

MAX_DESCRIPTION_CHARS = 4_000   # ~1 000 tokens; plenty for a task description

# ─────────────────────────────────────────────────────────────────────────────
# Prompt injection patterns
# ─────────────────────────────────────────────────────────────────────────────

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # Classic override attempts
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|above|prior)\s+instructions?", re.I),
    re.compile(r"forget\s+(all\s+)?(previous|above|prior)\s+instructions?", re.I),
    # System prompt exfiltration
    re.compile(r"(print|repeat|reveal|show|output)\s+(your\s+)?(system\s+prompt|instructions?)", re.I),
    # Role hijacking
    re.compile(r"you\s+are\s+now\s+(a\s+)?(?!a developer|an? engineer)", re.I),
    re.compile(r"act\s+as\s+(a\s+)?(?!a developer|an? engineer)", re.I),
    # Jailbreak markers
    re.compile(r"\bDAN\b"),
    re.compile(r"jailbreak", re.I),
]

# ─────────────────────────────────────────────────────────────────────────────
# Secret / credential patterns  (redacted before LLM sees them)
# ─────────────────────────────────────────────────────────────────────────────

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # GitHub PAT (classic and fine-grained)
    (re.compile(r"gh[ps]_[A-Za-z0-9]{36,}"), "[REDACTED_GITHUB_TOKEN]"),
    # Generic API key patterns
    (re.compile(r"(?i)(api[_\-]?key|apikey|secret[_\-]?key)\s*[=:]\s*\S+"), "[REDACTED_API_KEY]"),
    # AWS
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    # Generic hex/base64 secrets > 32 chars following key= or token=
    (re.compile(r"(?i)(token|password|passwd|pwd)\s*[=:]\s*[A-Za-z0-9+/=_\-]{20,}"), "[REDACTED_SECRET]"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_user_input(text: str) -> str:
    """
    Sanitise a user-supplied string before it is embedded in an LLM prompt.

    Steps:
        1. Truncate to MAX_DESCRIPTION_CHARS
        2. Redact secrets
        3. Flag (but do NOT silently drop) prompt injection attempts —
           the phrase is replaced with a visible placeholder so the LLM
           sees that something was removed.
    """
    if not text:
        return text

    # 1. Truncate
    if len(text) > MAX_DESCRIPTION_CHARS:
        logger.warning("sanitizer.truncated", original_len=len(text), limit=MAX_DESCRIPTION_CHARS)
        text = text[:MAX_DESCRIPTION_CHARS] + "\n[... truncated for safety ...]"

    # 2. Redact secrets
    for pattern, placeholder in _SECRET_PATTERNS:
        if pattern.search(text):
            logger.warning("sanitizer.secret_redacted", pattern=pattern.pattern[:40])
            text = pattern.sub(placeholder, text)

    # 3. Flag prompt injection
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            logger.warning("sanitizer.injection_detected", pattern=pattern.pattern[:60])
            text = pattern.sub("[INJECTION_ATTEMPT_REMOVED]", text)

    return text


def is_safe_repo_url(url: str, allowlist: list[str]) -> bool:
    """
    Validate that a repository URL belongs to an allowed owner/org.

    Args:
        url       : e.g. "https://github.com/my-org/my-repo"
        allowlist : list of allowed GitHub usernames or org names

    Returns True if the URL owner is in the allowlist, False otherwise.
    """
    if not allowlist:
        return True  # allowlist not configured — open mode

    match = re.match(r"https?://github\.com/([^/]+)/", url)
    if not match:
        return False

    owner = match.group(1).lower()
    return owner in [a.lower() for a in allowlist]