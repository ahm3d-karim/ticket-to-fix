"""Tests for fde.gates — secret scan + security lint over unified diffs.

scan_diff() scans only '+' added lines of a unified
diff (skipping ---/+++ headers); findings are (line_number, pattern, snippet).
The scrubbing helpers (redact_secrets, scrub_ticket, redact_event_data)
replace secret-looking substrings with <REDACTED> while leaving prose alone.
"""

from fde.gates import (
    LINT_PATTERNS,
    SECRET_PATTERNS,
    redact_event_data,
    redact_secrets,
    scan_diff,
    scrub_ticket,
)

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


# --- redact_secrets ---------------------------------------------------------

OPENAI_KEY = "sk-abc123DEF456ghi789JKL012mno345"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GH_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
GH_PAT = "github_pat_ABCDEFGH_abcdefghijklmnopqrstuvwxyz0123456789_XYZ"
HEX_TOKEN = "a1b2c3d4e5f6" * 4  # 48 chars, digits -> high entropy
B64_TOKEN = "dGhpc2lzYXNlY3JldHRva2VudmVyeWxvbmcxMjM0NTY="  # 44 chars
PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEpQIBAAKCAQEA1T4vC3\n"
    "-----END RSA PRIVATE KEY-----\n"
)


def test_redact_openai_key():
    assert redact_secrets(f"Call the API with {OPENAI_KEY} now.") == (
        "Call the API with <REDACTED> now."
    )


def test_redact_aws_access_key():
    assert redact_secrets(f"aws_access_key_id = {AWS_KEY}") == (
        "aws_access_key_id = <REDACTED>"
    )


def test_redact_github_tokens():
    assert redact_secrets(f"classic {GH_TOKEN} fine-grained {GH_PAT} end") == (
        "classic <REDACTED> fine-grained <REDACTED> end"
    )


def test_redact_pem_private_key_block():
    assert redact_secrets(PEM_BLOCK + "trailing line stays") == (
        "<REDACTED>\ntrailing line stays"
    )


def test_redact_pem_openssh_variant():
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "AAAA1234\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    assert redact_secrets(text) == "<REDACTED>"


def test_redact_high_entropy_hex_token():
    assert redact_secrets(f"digest {HEX_TOKEN} end") == "digest <REDACTED> end"


def test_redact_high_entropy_base64_token():
    assert redact_secrets(f"sig {B64_TOKEN} end") == "sig <REDACTED> end"


def test_redact_prose_unchanged():
    text = (
        "Please ask the sky why the mask looks risky. "
        "The secret was never written down. "
        "She skips the token_count check on Tuesdays. "
        "supercalifragilisticexpialidocious is a long word."
    )
    assert redact_secrets(text) == text


def test_redact_is_idempotent():
    samples = [
        f"key {OPENAI_KEY} here",
        AWS_KEY,
        GH_TOKEN,
        GH_PAT,
        PEM_BLOCK,
        f"digest {HEX_TOKEN} end",
        "Just a normal sentence about secrets and tokens.",
    ]
    for sample in samples:
        once = redact_secrets(sample)
        assert redact_secrets(once) == once


# --- scrub_ticket -----------------------------------------------------------

def test_scrub_ticket_redacts_string_values_deeply():
    ticket = {
        "title": f"Fix login using {OPENAI_KEY}",
        "meta": {"token": GH_TOKEN},
        "labels": [AWS_KEY, "urgent"],
        "count": 3,
        "ok": True,
    }
    out = scrub_ticket(ticket)
    assert out["title"] == "Fix login using <REDACTED>"
    assert out["meta"]["token"] == "<REDACTED>"
    assert out["labels"] == ["<REDACTED>", "urgent"]
    assert out["count"] == 3
    assert out["ok"] is True


def test_scrub_ticket_nested_structures():
    ticket = {
        "comments": [
            {"author": "alice", "body": f"key {OPENAI_KEY}"},
            {"author": "bob", "body": "no secrets"},
        ],
        "tags": [HEX_TOKEN],
    }
    out = scrub_ticket(ticket)
    assert out["comments"][0]["body"] == "key <REDACTED>"
    assert out["comments"][0]["author"] == "alice"
    assert out["comments"][1]["body"] == "no secrets"
    assert out["tags"] == ["<REDACTED>"]


def test_scrub_ticket_returns_new_dict_and_leaves_original_untouched():
    ticket = {"body": f"key {OPENAI_KEY}"}
    out = scrub_ticket(ticket)
    assert out is not ticket
    assert out["body"] == "key <REDACTED>"
    out["body"] = "mutated"
    assert ticket["body"] == f"key {OPENAI_KEY}"


def test_scrub_ticket_keys_not_redacted():
    ticket = {OPENAI_KEY: "value"}
    out = scrub_ticket(ticket)
    assert OPENAI_KEY in out
    assert out[OPENAI_KEY] == "value"


# --- redact_event_data ------------------------------------------------------

def test_redact_event_data_marks_when_changed():
    data = {"detail": f"token {GH_TOKEN}"}
    out = redact_event_data(data)
    assert out["detail"] == "token <REDACTED>"
    assert out["secrets_redacted"] is True


def test_redact_event_data_no_marker_when_clean():
    data = {"detail": "all clear, nothing secret here", "count": 2}
    out = redact_event_data(data)
    assert out == {"detail": "all clear, nothing secret here", "count": 2}
    assert "secrets_redacted" not in out


def test_redact_event_data_nested_marker():
    data = {"checks": [{"ok": True, "detail": AWS_KEY}]}
    out = redact_event_data(data)
    assert out["checks"][0]["detail"] == "<REDACTED>"
    assert out["checks"][0]["ok"] is True
    assert out["secrets_redacted"] is True


def test_redact_event_data_original_untouched():
    data = {"detail": f"key {OPENAI_KEY}", "nested": {"x": AWS_KEY}}
    redact_event_data(data)
    assert data["detail"] == f"key {OPENAI_KEY}"
    assert data["nested"]["x"] == AWS_KEY


def test_redact_event_data_none_passthrough():
    assert redact_event_data(None) is None
