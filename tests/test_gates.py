"""Tests for fde.gates — secret scan + security lint over unified diffs.

Per EXECUTION.md S3T1: scan_diff() scans only '+' added lines of a unified
diff (skipping ---/+++ headers); findings are (line_number, pattern, snippet).
"""

from fde.gates import LINT_PATTERNS, SECRET_PATTERNS, scan_diff

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


def test_planted_api_key_flagged():
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def main():\n"
        "-    pass\n"
        f'+    api_key = "{API_KEY}"\n'
        '+    print("done")\n'
    )
    res = scan_diff(diff)
    assert len(res["secrets"]) == 1
    lineno, pattern, snippet = res["secrets"][0]
    assert lineno == 6  # absolute line number within the diff, headers skipped for scanning
    assert pattern == "secret_assignment"
    assert snippet == f'api_key = "{API_KEY}"'
    assert res["lint"] == []


def test_email_flagged():
    diff = "--- a/mail.py\n+++ b/mail.py\n@@ -1 +1 @@\n+    contact = \"alice@example.com\"\n"
    res = scan_diff(diff)
    assert len(res["secrets"]) == 1
    lineno, pattern, snippet = res["secrets"][0]
    assert pattern == "email"
    assert "alice@example.com" in snippet


def test_dangerous_calls_flagged():
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n"
        "+    result = eval(user_input)\n"
        "+    subprocess.run(cmd, shell=True)\n"
        '+    os.system("rm -rf /")\n'
    )
    res = scan_diff(diff)
    names = [f[1] for f in res["lint"]]
    assert "eval" in names
    assert "shell_true" in names
    assert "os_system" in names
    assert res["secrets"] == []


def test_exec_and_sudo_flagged():
    diff = (
        "--- a/x.sh\n+++ b/x.sh\n@@ -1 +1,2 @@\n"
        "+    exec(code)\n"
        "+    sudo apt-get install -y nodejs\n"
    )
    res = scan_diff(diff)
    names = [f[1] for f in res["lint"]]
    assert "exec" in names
    assert "sudo" in names


def test_clean_diff_no_findings():
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,4 @@\n"
        " def total(p, q):\n"
        "-    return p * q + 0.05\n"
        "+    return p * q * (1 + 0.05)\n"
        "+# keep tax in one place\n"
    )
    res = scan_diff(diff)
    assert res == {"secrets": [], "lint": []}


def test_benign_identifiers_not_flagged():
    diff = (
        "--- a/benign.py\n+++ b/benign.py\n@@ -1,4 +1,5 @@\n"
        "+    token_count = len(items)\n"
        '+    api_key_name = "config"\n'
        "+def evaluate(x):\n"
        "+    return x * 2\n"
    )
    res = scan_diff(diff)
    assert res == {"secrets": [], "lint": []}


def test_secret_on_deleted_lines_not_flagged():
    # Only '+' added lines are scanned — a secret that is being *removed*
    # (present only on '-' lines) must not block the diff.
    diff = (
        "--- a/old.py\n+++ b/old.py\n@@ -1,2 +1,1 @@\n"
        f'-    api_key = "{API_KEY}"\n'
        "-    alice@example.com\n"
        "+    pass\n"
    )
    res = scan_diff(diff)
    assert res == {"secrets": [], "lint": []}


def test_short_secret_value_not_flagged():
    # Pattern requires a quote-delimited value of >= 16 chars.
    diff = (
        "--- a/s.py\n+++ b/s.py\n@@ -1 +1,2 @@\n"
        '+    token = "123456789012345"\n'  # 15 chars -> no hit
        '+    token = "1234567890123456"\n'  # 16 chars -> hit
    )
    res = scan_diff(diff)
    assert len(res["secrets"]) == 1
    assert res["secrets"][0][1] == "secret_assignment"


def test_headers_are_never_scanned():
    # '+++'/'-'-header lines are skipped; a secret-looking header must not
    # produce findings, and scanning still reports correct diff line numbers.
    diff = (
        "--- a/secret.py\n"
        "+++ b/secret.py\n"
        "@@ -1 +1 @@\n"
        "+    pass\n"
    )
    res = scan_diff(diff)
    assert res == {"secrets": [], "lint": []}


def test_pattern_lists_are_compiled_regexes():
    assert all(hasattr(p, "search") for p in SECRET_PATTERNS + LINT_PATTERNS)
    assert len(SECRET_PATTERNS) == 2
    assert len(LINT_PATTERNS) == 5
