"""S1/S3 CLI wiring: `fde bench` subcommand + verification summary in `fde diff`.

Both tests drive the real CLI as a subprocess (``python -m fde.cli``) with
FDE_AGENT_BACKEND=mock, so the agent steps are deterministic and offline.
They run against the real fixture corpus (like tests/test_bench.py) and
leave their runs under the gitignored runs/ dir.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _cli(*args: str, backend: str = "mock") -> subprocess.CompletedProcess:
    """Run one fde CLI step under the venv python, from the repo root."""
    env = os.environ.copy()
    env["FDE_AGENT_BACKEND"] = backend
    return subprocess.run([str(PY), "-m", "fde.cli", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


def test_bench_cli_reports_corpus_for_one_fixture():
    r = _cli("bench", "--backend", "mock", "--fixture", "tier1_checkout")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    # table header + fixture row + summary line
    assert "fixture" in out.lower()
    assert "tier1_checkout" in out
    assert "awaiting_approval" in out
    assert "summary:" in out
    assert "1 fixture(s)" in out


def test_bench_cli_backend_defaults_to_env():
    # --backend is optional: FDE_AGENT_BACKEND=mock in the environment is the
    # default, same contract as fde/bench.py's own CLI.
    r = _cli("bench", "--fixture", "tier2_billing")
    assert r.returncode == 0, r.stderr
    assert "tier2_billing" in r.stdout
    assert "awaiting_approval" in r.stdout


def test_diff_includes_verification_summary():
    # Full pipeline via the CLI with the mock backend, then `fde diff` must
    # print the existing evidence package PLUS the S3 verification summary.
    ticket = ROOT / "fixtures" / "tier1_checkout" / "ticket.md"
    r = _cli("submit", str(ticket))
    assert r.returncode == 0, r.stderr
    run_id = r.stdout.strip().splitlines()[-1]

    r = _cli("repro", run_id)
    assert r.returncode == 0, r.stderr
    r = _cli("fix", run_id)
    assert r.returncode == 0, r.stderr

    d = _cli("diff", run_id)
    assert d.returncode == 0, d.stderr
    out = d.stdout
    # existing evidence package is still there
    assert "changed files:" in out
    assert "gates:" in out
    assert "agent what/why:" in out
    # S3 verification summary block appended after it
    assert "verification summary:" in out
    assert "repro first-try" in out
    assert "fix rounds" in out
    assert "verdict: verified" in out
