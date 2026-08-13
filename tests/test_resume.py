"""S5: `fde resume` — restart an interrupted run from its last completed step.

Synthetic interrupted runs live under runs/ (gitignored) with a scratch
fixture repo under tests/scratch_repos/ (gitignored), mirroring the
test_harness pattern. The mock backend (monkeypatched fde.agents.BACKEND)
makes the resumed pipeline deterministic and offline: repro first-try, fix
first-round, gates passed, awaiting_approval.
"""
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import fde.agents
from fde.cli import cmd_resume
from fde.runlog import events, state
from fde.worktree import create_worktree, discard_worktree

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "tests" / "scratch_repos"
RUNS = Path("runs")  # runlog.RUNS_DIR, resolved from the repo root (test cwd)

BUGGY = "const TAX = 0.05;\nmodule.exports = { total: (p, q) => p * q + TAX };\n"
FIXED = "const TAX = 0.05;\nmodule.exports = { total: (p, q) => p * q * (1 + TAX) };\n"
# strict suite: fails on buggy code, passes with gold applied (tier2 shape)
SUITE_TEST = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert');\n"
    "const { total } = require('../calc.js');\n"
    "test('total applies 5% tax', () => { assert.equal(total(10, 3), 31.5); });\n"
)
# a repro test that passes once the fix (gold.patch) is applied — what a
# completed fix phase needs; the fix loop runs it AFTER the agent step
PASSING_REPRO = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert');\n"
    "test('resumed repro passes with gold', () => { assert.equal(1, 1); });\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _build_repo(repo: Path) -> None:
    """Buggy calc.js on main + gold.patch (the perfect fix) + strict suite."""
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    (repo / "calc.js").write_text(BUGGY, encoding="utf-8")
    (repo / "test").mkdir()
    (repo / "test" / "total.test.js").write_text(SUITE_TEST, encoding="utf-8")
    (repo / "fde.yaml").write_text(
        "install_cmd: ''\n"
        "test_cmd: 'node --test'\n"
        "run_cmd: 'node calc.js'\n"
        "app_type: js\n",
        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    (repo / "calc.js").write_text(FIXED, encoding="utf-8")
    gold = _git(repo, "diff", "--", "calc.js").stdout
    (repo / "gold.patch").write_text(gold, encoding="utf-8")
    _git(repo, "checkout", "--", "calc.js")


def _ticket(repo: Path) -> str:
    """A valid ticket whose `system` field resolves to the scratch repo."""
    system = str(repo.relative_to(ROOT)).replace("\\", "/")
    return (f"---\nid: TT-RESUME\nseverity: high\nsystem: {system}\n"
            'expected: "3 items at $10 should total $31.50"\n'
            'actual: "total comes to $30.05"\n'
            'symptom: "total should be 31.5"\n---\n# Resume scratch ticket\n')


def _sc(from_, to):
    return ("state_changed", {"from": from_, "to": to})


def _repro(attempt, verdict_):
    return ("test_result", {"stage": "repro", "attempt": attempt,
                            "verdict": verdict_, "checks": {}})


@pytest.fixture
def resume_repo():
    """Throwaway scratch fixture repo (gitignored under tests/scratch_repos/)."""
    repo = SCRATCH / f"resume_repo_{uuid.uuid4().hex[:8]}"
    repo.mkdir(parents=True)
    try:
        _build_repo(repo)
        yield repo
    finally:
        shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def interrupted_run(resume_repo):
    """Build a synthetic interrupted run dir under runs/; clean up after."""
    ids = []

    def _make(run_id: str, log, with_worktree: bool = False,
              run_state: str = "reproducing"):
        d = RUNS / run_id
        shutil.rmtree(d, ignore_errors=True)
        ids.append(run_id)
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"state": run_state}),
                                      encoding="utf-8")
        (d / "ticket.md").write_text(_ticket(resume_repo), encoding="utf-8")
        lines = [json.dumps({"ts": f"2026-08-14T00:00:{i:02d}",
                             "run_id": run_id, "event": ev, "data": data})
                 for i, (ev, data) in enumerate(log)]
        (d / "run.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if with_worktree:
            create_worktree(str(resume_repo), run_id)
        return run_id

    yield _make
    for rid in ids:
        try:
            discard_worktree(str(resume_repo), rid)
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(RUNS / rid, ignore_errors=True)


def test_resume_reproducing_recreates_worktree_and_completes(
        resume_repo, interrupted_run, monkeypatch):
    """Interrupted mid-repro, worktree lost -> resume recreates it, re-enters
    the repro loop with the remaining attempt budget, then runs the fix
    phase to awaiting_approval."""
    monkeypatch.setattr(fde.agents, "BACKEND", "mock")
    run_id = interrupted_run("resume-repro-1", [
        ("ticket_parsed", {"ticket_id": "TT-RESUME"}),
        _sc("submitted", "reproducing"),
        _repro(1, False),  # one harness rejection before the process died
    ])
    assert not (RUNS / run_id / "worktree").exists()

    rc = cmd_resume(SimpleNamespace(run_id=run_id))

    assert rc == 0
    assert state(run_id) == "awaiting_approval"
    evs = events(run_id)
    # 'resumed' event appended, tagged with the state the run was stuck in
    assert any(e["event"] == "resumed" and e["data"].get("from") == "reproducing"
               for e in evs)
    # worktree recreated from the fixture repo + audited
    assert (RUNS / run_id / "worktree").is_dir()
    assert sum(1 for e in evs if e["event"] == "worktree_created") == 1
    # repro loop re-entered with the remaining budget (1 of 3 already used):
    # the interrupted rejection + the resumed acceptance
    repros = [e for e in evs if e["event"] == "test_result"
              and e["data"].get("stage") == "repro"]
    assert [e["data"]["verdict"] for e in repros] == [False, True]
    assert any(e["event"] == "repro_test_written" and e["data"]["attempts"] == 1
               for e in evs)
    # fix phase ran to completion
    assert any(e["event"] == "fix_attempt" and e["data"]["ok"] for e in evs)
    assert any(e["event"] == "gates_passed" for e in evs)


def test_resume_reuses_existing_worktree(resume_repo, interrupted_run,
                                         monkeypatch):
    """A live worktree in the run dir is reused, not recreated."""
    monkeypatch.setattr(fde.agents, "BACKEND", "mock")
    run_id = interrupted_run("resume-reuse-1", [
        ("ticket_parsed", {"ticket_id": "TT-RESUME"}),
        _sc("submitted", "reproducing"),
        ("worktree_created", {"repo": str(resume_repo),
                              "worktree": str(RUNS / "resume-reuse-1" / "worktree")}),
        _repro(1, False),
    ], with_worktree=True)
    wt = RUNS / run_id / "worktree"
    assert wt.is_dir()

    rc = cmd_resume(SimpleNamespace(run_id=run_id))

    assert rc == 0
    assert state(run_id) == "awaiting_approval"
    evs = events(run_id)
    assert any(e["event"] == "resumed" for e in evs)
    # exactly the one worktree_created from the original run: no recreation
    assert sum(1 for e in evs if e["event"] == "worktree_created") == 1
    assert wt.is_dir()


def test_resume_fixing_skips_repro_and_enters_fix_loop(
        resume_repo, interrupted_run, monkeypatch):
    """Interrupted mid-fix (repro already accepted): resume must NOT re-run
    the repro loop; it goes straight into the fix phase."""
    monkeypatch.setattr(fde.agents, "BACKEND", "mock")
    run_id = interrupted_run("resume-fix-1", [
        ("ticket_parsed", {"ticket_id": "TT-RESUME"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"),
        _sc("reproved", "fixing"),
    ], run_state="fixing")
    (RUNS / run_id / "repro.test.js").write_text(PASSING_REPRO, encoding="utf-8")

    rc = cmd_resume(SimpleNamespace(run_id=run_id))

    assert rc == 0
    assert state(run_id) == "awaiting_approval"
    evs = events(run_id)
    assert any(e["event"] == "resumed" and e["data"].get("from") == "fixing"
               for e in evs)
    # the accepted repro was reused: still exactly one repro test_result
    repros = [e for e in evs if e["event"] == "test_result"
              and e["data"].get("stage") == "repro"]
    assert len(repros) == 1 and repros[0]["data"]["verdict"] is True
    assert any(e["event"] == "fix_attempt" and e["data"]["ok"] for e in evs)
    assert any(e["event"] == "gates_passed" for e in evs)


def test_resume_fixing_with_green_fix_commits_without_new_round(
        resume_repo, interrupted_run, monkeypatch):
    """The fix loop already completed (fix_attempt ok) before the process
    died: resume skips the loop, commits the green worktree and gates."""
    monkeypatch.setattr(fde.agents, "BACKEND", "mock")
    run_id = interrupted_run("resume-green-1", [
        ("ticket_parsed", {"ticket_id": "TT-RESUME"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"),
        _sc("reproved", "fixing"),
        ("fix_attempt", {"round": 1, "ok": True, "rc": 0, "diff_bytes": 100,
                         "duration_ms": 50, "summary": ""}),
    ], run_state="fixing", with_worktree=True)
    wt = RUNS / run_id / "worktree"
    # the green fix is already applied in the worktree (killed before commit)
    subprocess.run(["git", "-C", str(wt), "apply", str(resume_repo / "gold.patch")],
                   check=True, capture_output=True, text=True)
    (RUNS / run_id / "repro.test.js").write_text(PASSING_REPRO, encoding="utf-8")

    rc = cmd_resume(SimpleNamespace(run_id=run_id))

    assert rc == 0
    assert state(run_id) == "awaiting_approval"
    evs = events(run_id)
    # no new fix round was burned
    assert sum(1 for e in evs if e["event"] == "fix_attempt") == 1
    assert any(e["event"] == "gates_passed" for e in evs)
    # the committed fix is the gold fix (a stray worktree reset would wipe it)
    commit = (RUNS / run_id / "fix_commit.txt").read_text(encoding="utf-8").strip()
    show = subprocess.run(["git", "-C", str(resume_repo), "show", commit],
                          capture_output=True, text=True, check=True)
    assert "(1 + TAX)" in show.stdout


def test_resume_refuses_non_interruptible_state(resume_repo, interrupted_run,
                                                capsys):
    """Anything not 'reproducing'/'fixing' is refused with a clear message,
    and no 'resumed' event is appended."""
    run_id = interrupted_run("resume-refuse-1", [
        ("ticket_parsed", {"ticket_id": "TT-RESUME"}),
        _sc("submitted", "reproducing"),
        _sc("reproducing", "reproved"),
        _sc("reproved", "fixing"),
        _sc("fixing", "fixed"),
        _sc("fixed", "gating"),
        _sc("gating", "gated"),
        _sc("gated", "awaiting_approval"),
    ], run_state="awaiting_approval")

    rc = cmd_resume(SimpleNamespace(run_id=run_id))

    assert rc == 1
    err = capsys.readouterr().err
    assert "awaiting_approval" in err
    assert "only runs stuck in 'reproducing' or 'fixing' can be resumed" in err
    assert not any(e["event"] == "resumed" for e in events(run_id))


def test_resume_unknown_run(capsys):
    rc = cmd_resume(SimpleNamespace(run_id="resume-no-such-run"))
    assert rc == 1
    assert "run not found" in capsys.readouterr().err
    shutil.rmtree(RUNS / "resume-no-such-run", ignore_errors=True)
