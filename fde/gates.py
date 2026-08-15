"""Automated gates: secret scan + security lint for agent-produced diffs.

Pure-stdlib regex implementation (no gitleaks dependency — if the
``gitleaks`` binary is on PATH, :func:`gitleaks_available` reports it, but
the regex path is the default and the only one required to work).

``scan_diff()`` operates on unified diff text (e.g. ``git diff`` output).
Only lines that were ADDED (``+`` lines) are scanned; the ``---``/``+++``
file-header lines and everything else are skipped. Each finding is a tuple
``(line_number, pattern_name, snippet)`` where ``line_number`` is the 1-based
position of the line within the diff text.

Scrubbing helpers (:func:`redact_secrets`, :func:`scrub_ticket`,
:func:`redact_event_data`) replace secret-looking substrings (API keys, AWS
access keys, GitHub tokens, PEM private-key blocks, high-entropy runs) with
``<REDACTED>`` while leaving ordinary prose untouched. They are pure
functions — wiring them into the pipeline is the caller's job.
"""

import re
import shutil

# --- Secret patterns --------------------------------------------------------
# 1. api key / secret / token / password assignments with a quote-delimited
#    value of >= 16 chars. The value-length requirement avoids flagging
#    benign identifiers such as ``token_count`` or ``api_key_name``.
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][A-Za-z0-9_\-/+=]{16,}[\"']"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]
_SECRET_NAMES = ["secret_assignment", "email"]

# --- Lint patterns ----------------------------------------------------------
# ``\b`` word boundaries keep ``evaluate``/``execute`` from matching
# ``eval(``/``exec(``.
LINT_PATTERNS = [
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"shell\s*=\s*True"),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsudo\b"),
]
_LINT_NAMES = ["eval", "exec", "shell_true", "os_system", "sudo"]


def _scan(added_lines: list, patterns: list, names: list) -> list:
    findings = []
    for lineno, text in added_lines:
        for pat, name in zip(patterns, names):
            if pat.search(text):
                findings.append((lineno, name, text))
    return findings


def scan_diff(diff_text: str) -> dict:
    """Scan a unified diff for planted secrets and dangerous constructs.

    Args:
        diff_text: raw unified diff (``git diff`` output).

    Returns:
        ``{"secrets": [...], "lint": [...]}`` where each finding is a tuple
        ``(line_number, pattern_name, snippet)``. Only ``+`` added lines are
        scanned; ``---``/``+++`` header lines are skipped.
    """
    added = []
    for lineno, line in enumerate(diff_text.splitlines(), start=1):
        if line.startswith("+++") or line.startswith("---"):
            continue  # unified-diff file headers, never scan them
        if line.startswith("+"):
            added.append((lineno, line[1:].strip()))
    return {
        "secrets": _scan(added, SECRET_PATTERNS, _SECRET_NAMES),
        "lint": _scan(added, LINT_PATTERNS, _LINT_NAMES),
    }


def gitleaks_available() -> bool:
    """True if the optional ``gitleaks`` binary is on PATH (regex is default)."""
    return shutil.which("gitleaks") is not None


# --- Secret scrubbing helpers ------------------------------------------------
# ``redact_secrets`` replaces secret-looking substrings with a fixed
# ``<REDACTED>`` token. The pattern list is intentionally separate from
# ``SECRET_PATTERNS`` (which drives ``scan_diff`` and is length-locked by its
# tests); the assignment pattern is reused by reference.
REDACTED_TOKEN = "<REDACTED>"

# PEM private-key blocks first: the whole block (headers, body, footers).
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
# Credential assignments (reused from the scan patterns above).
_ASSIGNMENT = SECRET_PATTERNS[0]
# Named credential formats. The lookbehind keeps ``mask-...``-style prose
# substrings of ``sk-`` from matching.
_OPENAI_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN = re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
# Generic high-entropy tokens: >= 32 chars of mostly-alphanumeric text
# (base64 ``+/=`` allowed), further vetted by _is_high_entropy() so ordinary
# prose is never touched. Lookarounds (not ``\b``) so trailing ``=`` padding
# is included in the match.
_ENTROPY_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{32,}(?![A-Za-z0-9_\-+/=])"
)

_REDACT_SPECS = [
    (_PEM_BLOCK, REDACTED_TOKEN),
    (_ASSIGNMENT, REDACTED_TOKEN),
    (_OPENAI_KEY, REDACTED_TOKEN),
    (_AWS_ACCESS_KEY, REDACTED_TOKEN),
    (_GITHUB_TOKEN, REDACTED_TOKEN),
    (_ENTROPY_TOKEN, None),  # vetted by _is_high_entropy
]


def _is_high_entropy(token: str) -> bool:
    """Vet a long alphanumeric run: redact token-looking text, not prose.

    A run qualifies when it has at least one digit, mixes upper- and
    lowercase, or contains ``-``/``_`` — i.e. it looks generated. Pure
    lowercase words (even 30+ char ones like
    ``supercalifragilisticexpialidocious``) pass through untouched.

    Pure-lowercase-HEX runs are exempt even with digits: commit SHAs and
    hashes (40-char ``[0-9a-f]``, exactly the shape of the audit trail's
    fix/deploy evidence) must survive redaction. Real secrets of that
    shape are rare — practical keys carry a prefix (``sk-``, ``AKIA``,
    ``ghp_``), mixed case, or separators, all caught by their own
    patterns above.
    """
    has_digit = any(c.isdigit() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_sep = "-" in token or "_" in token
    if not has_digit and not (has_upper and has_lower) and not has_sep:
        return False
    if has_lower and not has_upper and not has_sep:
        # pure lowercase hex (commit SHA / hash shape) — audit evidence
        return not all(c in "0123456789abcdef" for c in token)
    return True


def redact_secrets(text: str) -> str:
    """Replace secret-looking substrings with ``<REDACTED>``.

    Covers PEM private-key blocks, OpenAI-style ``sk-`` keys, AWS access-key
    IDs, GitHub tokens (classic + fine-grained), credential assignments, and
    long high-entropy alphanumeric runs. Ordinary prose — even containing the
    word "secret" or "sk" as a substring — passes through unchanged.
    Idempotent: ``redact_secrets(redact_secrets(x)) == redact_secrets(x)``.
    """
    result = text
    for pattern, replacement in _REDACT_SPECS:
        if replacement is None:
            result = pattern.sub(
                lambda m: REDACTED_TOKEN if _is_high_entropy(m.group(0)) else m.group(0),
                result,
            )
        else:
            result = pattern.sub(replacement, result)
    return result


def _redact_walk(value):
    """Deep-copy ``value`` redacting every string; returns ``(copy, changed)``.

    Keys are never redacted — only values (recursively through nested
    dicts/lists). Non-string scalars pass through by identity.
    """
    if isinstance(value, str):
        out = redact_secrets(value)
        return out, out != value
    if isinstance(value, dict):
        out = {}
        changed = False
        for key, item in value.items():
            out[key], item_changed = _redact_walk(item)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, list):
        out = []
        changed = False
        for item in value:
            redacted, item_changed = _redact_walk(item)
            out.append(redacted)
            changed = changed or item_changed
        return out, changed
    return value, False


def scrub_ticket(ticket: dict) -> dict:
    """Return a deep copy of a parsed ticket with all string values redacted.

    Keys are never redacted; every string value (recursively, through nested
    dicts/lists) passes through :func:`redact_secrets`. The input dict is not
    modified.
    """
    return _redact_walk(ticket)[0]


def redact_event_data(data: dict | None) -> dict | None:
    """Deep copy of event data with string values redacted (writer hook).

    If any redaction actually happened, the copy carries a
    ``secrets_redacted: True`` marker so audit consumers can see it. ``None``
    passes through as ``None``.
    """
    if data is None:
        return None
    out, changed = _redact_walk(data)
    if changed:
        out["secrets_redacted"] = True
    return out
