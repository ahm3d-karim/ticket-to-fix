"""S1T2: 3-state verification harness (fde/harness.py).

Scratch fixture is a mini tier-1 (node, no deps): buggy calc.js on main,
gold.patch that fixes it, one always-green suite test. verify_repro must:
  A: repro test FAILS on buggy code with the ticket symptom in the output
  B: repro test PASSES with gold.patch applied
  C: full test suite PASSES with gold.patch applied
"""
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from fde.harness import norm, run_cmd, symptom_in_output, verify_repro
from fde.runlog import events
from fde.worktree import create_worktree, discard_worktree

SCRATCH = Path(__file__).parent / "scratch_repos"

BUGGY = "const TAX = 0.05;\nmodule.exports = { total: (p, q) => p * q + TAX };\n"
FIXED = "const TAX = 0.05;\nmodule.exports = { total: (p, q) => p * q * (1 + TAX) };\n"
BROKEN = "const TAX = 0.05;\nmodule.exports = { total: (p, q) => p * q + TAX * 0 };\n"

SUITE_TEST = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert');\n"
    "const { total } = require('../calc.js');\n"
    "test('total sanity', () => { assert.equal(typeof total, 'function'); });\n"
)

GOOD_REPRO = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert');\n"
    "const { total } = require('./calc.js');\n"
    "test('total is 31.5', () => {\n"
    "  assert.equal(total(10, 3), 31.5, 'total should be 31.5');\n"
    "});\n"
)

TRIVIAL_REPRO = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert');\n"
    "test('trivial', () => { assert.equal(true, true); });\n"
)

WRONG_SYMPTOM_REPRO = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert');\n"
    "const { total } = require('./calc.js');\n"
    "test('total is 31.5', () => {\n"
    "  assert.equal(total(10, 3), 31.5, 'invoice 100 should be 118');\n"
    "});\n"
)

MANIFEST = {
    "install_cmd": "",
    "test_cmd": "node --test",
    "run_cmd": 'node -e "console.log(require(\'./calc.js\').total(10,3))"',
    "app_type": "js",
}

TICKET = {
    "id": "TT-SCRATCH",
    "severity": "high",
    "system": "scratch",
    "expected": "3 items at $10 should total $31.50",
    "actual": "total comes to $30.05",
    "symptom": "total should be 31.5",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def make_gold_patch(repo: Path, fixed_source: str) -> Path:
    """git diff of fixed vs committed buggy code -> repo/gold.patch, restore buggy."""
    (repo / "calc.js").write_text(fixed_source, encoding="utf-8")
    diff = _git(repo, "diff", "--", "calc.js").stdout
    (repo / "gold.patch").write_text(diff, encoding="utf-8")
    _git(repo, "checkout", "--", "calc.js")
    return repo / "gold.patch"


@pytest.fixture
def buggy_repo():
    """Mini tier-1 scratch repo (buggy main) under tests/scratch_repos/."""
    repo = SCRATCH / f"harness_repo_{uuid.uuid4().hex[:8]}"
    repo.mkdir(parents=True)
    try:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        # keep line endings LF so git apply of LF patches is deterministic
        (repo / ".gitattributes").write_text("* -text\n", encoding="utf-8")
        (repo / "calc.js").write_text(BUGGY, encoding="utf-8")
        (repo / "test").mkdir()
        (repo / "test" / "total.test.js").write_text(SUITE_TEST, encoding="utf-8")
        (repo / "fde.yaml").write_text(
            "install_cmd: ''\n"
            "test_cmd: 'node --test'\n"
            "run_cmd: 'node -e \"console.log(require(''./calc.js'').total(10,3))\"'\n"
            "app_type: js\n",
            encoding="utf-8",
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        yield repo
    finally:
        shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def harness_run(buggy_repo):
    """Worktree + agent-written repro test; cleans up runs/<id> after."""
    ids = []

    def _setup(run_id: str, repro_source: str):
        shutil.rmtree(Path("runs") / run_id, ignore_errors=True)
        ids.append(run_id)
        wt = create_worktree(str(buggy_repo), run_id)
        repro = Path("runs") / run_id / "repro.test.js"
        repro.write_text(repro_source, encoding="utf-8")
        return wt, str(repro)

    yield _setup
    for rid in ids:
        try:
            discard_worktree(str(buggy_repo), rid)
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(Path("runs") / rid, ignore_errors=True)


# --- norm / symptom_in_output ------------------------------------------------

def test_norm_collapses_whitespace_and_case():
    assert norm("  Total Should  Be 31.5 ") == "total should be 31.5"
    assert norm("a\n\nb\t c") == "a b c"


def test_symptom_in_output_matches_normalized_substring():
    out = "AssertionError: total should be 31.5\nexpected 31.5 to equal 30.05"
    assert symptom_in_output("total should be 31.5", out)
    # case/whitespace differences don't matter
    assert symptom_in_output("Total   Should be 31.5", out)
    # wrong symptom text does not match
    assert not symptom_in_output("invoice 100 should be 118", out)


# --- run_cmd -----------------------------------------------------------------

def test_run_cmd_echo():
    r = run_cmd("echo hello harness", ".")
    assert r["rc"] == 0
    assert "hello harness" in r["out"]
    assert r["timed_out"] is False


def test_run_cmd_reports_nonzero_rc():
    r = run_cmd("echo boom; exit 3", ".")
    assert r["rc"] == 3
    assert "boom" in r["out"]
    assert r["timed_out"] is False


def test_run_cmd_timeout_kills_process_tree():
    r = run_cmd("sleep 30", ".", timeout=2)
    assert r["timed_out"] is True
    assert r["rc"] == -1


# --- verify_repro: 3-state contract ------------------------------------------

def test_verify_repro_good_repro_test_passes(buggy_repo, harness_run):
    make_gold_patch(buggy_repo, FIXED)
    run_id = "test-harness-good"
    wt, repro = harness_run(run_id, GOOD_REPRO)

    v = verify_repro(str(buggy_repo), wt, MANIFEST, TICKET, repro)

    assert v["pass"] is True
    assert v["checks"]["a"]["ok"] is True   # fails w/ symptom on buggy code
    assert v["checks"]["b"]["ok"] is True   # passes with gold applied
    assert v["checks"]["c"]["ok"] is True   # full suite green with gold
    # worktree restored after B/C: no state-A poisoning
    st = run_cmd("git status --porcelain", wt)
    assert st["out"].strip() == ""


def test_verify_repro_trivial_test_rejected_on_state_a(buggy_repo, harness_run):
    make_gold_patch(buggy_repo, FIXED)
    run_id = "test-harness-trivial"
    wt, repro = harness_run(run_id, TRIVIAL_REPRO)

    v = verify_repro(str(buggy_repo), wt, MANIFEST, TICKET, repro)

    assert v["pass"] is False
    assert v["checks"]["a"]["ok"] is False  # passes on buggy code -> not a repro
    assert v["checks"]["b"] is None
    assert v["checks"]["c"] is None


def test_verify_repro_wrong_symptom_rejected_on_state_a(buggy_repo, harness_run):
    make_gold_patch(buggy_repo, FIXED)
    run_id = "test-harness-wrongsym"
    wt, repro = harness_run(run_id, WRONG_SYMPTOM_REPRO)

    v = verify_repro(str(buggy_repo), wt, MANIFEST, TICKET, repro)

    assert v["pass"] is False
    assert v["checks"]["a"]["ok"] is False  # fails, but not for the right reason
    assert "symptom" in v["checks"]["a"]["detail"]
    assert v["checks"]["b"] is None


def test_verify_repro_broken_gold_patch_rejected(buggy_repo, harness_run):
    make_gold_patch(buggy_repo, BROKEN)  # applies cleanly but code still wrong
    run_id = "test-harness-broken"
    wt, repro = harness_run(run_id, GOOD_REPRO)

    v = verify_repro(str(buggy_repo), wt, MANIFEST, TICKET, repro)

    assert v["pass"] is False
    assert v["checks"]["a"]["ok"] is True   # symptom reproduced fine
    assert v["checks"]["b"]["ok"] is False  # gold does not make repro pass
    assert v["checks"]["c"] is None


def test_verify_repro_appends_test_result_events(buggy_repo, harness_run):
    make_gold_patch(buggy_repo, FIXED)
    run_id = "test-harness-events"
    wt, repro = harness_run(run_id, GOOD_REPRO)

    v = verify_repro(str(buggy_repo), wt, MANIFEST, TICKET, repro)
    assert v["pass"] is True

    results = [e for e in events(run_id) if e["event"] == "test_result"]
    assert len(results) == 1
    d = results[0]["data"]
    assert d["pass"] is True
    assert isinstance(d["duration_ms"], int) and d["duration_ms"] >= 0
    assert set(d["checks"]) == {"a", "b", "c"}
    for name in ("a", "b", "c"):
        c = d["checks"][name]
        assert c["ok"] is True
        assert isinstance(c["duration_ms"], int)
        assert c["snippet"], f"check {name} should carry an output snippet"
