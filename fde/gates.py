"""Automated gates: secret scan + security lint for agent-produced diffs.

Pure-stdlib regex implementation (no gitleaks dependency — if the
``gitleaks`` binary is on PATH, :func:`gitleaks_available` reports it, but
the regex path is the default and the only one required to work).

``scan_diff()`` operates on unified diff text (e.g. ``git diff`` output).
Only lines that were ADDED (``+`` lines) are scanned; the ``---``/``+++``
file-header lines and everything else are skipped. Each finding is a tuple
``(line_number, pattern_name, snippet)`` where ``line_number`` is the 1-based
position of the line within the diff text.
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
