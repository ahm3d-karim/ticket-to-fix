"""S1T1: git worktree create/discard (fde/worktree.py)."""
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from fde.worktree import create_worktree, discard_worktree

# throwaway scratch repos live here (gitignored)
SCRATCH = Path(__file__).parent / "scratch_repos"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


@pytest.fixture
def scratch_repo():
    """A tiny git repo (main branch, one commit with a file)."""
    repo = SCRATCH / f"wt_repo_{uuid.uuid4().hex[:8]}"
    repo.mkdir(parents=True)
    try:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        (repo / "app.txt").write_text("hello\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        yield repo
    finally:
        shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def clean_run():
    """Tracks throwaway run ids under runs/ and removes them before/after."""
    ids = []

    def _use(run_id: str) -> str:
        ids.append(run_id)
        shutil.rmtree(Path("runs") / run_id, ignore_errors=True)
        return run_id

    yield _use
    for rid in ids:
        shutil.rmtree(Path("runs") / rid, ignore_errors=True)


def _norm(p: Path) -> str:
    return str(p.resolve()).replace("\\", "/")


def test_create_worktree_checks_out_main_content(scratch_repo, clean_run):
    run_id = clean_run("test-worktree-1")
    wt = create_worktree(str(scratch_repo), run_id)
    wt_path = Path(wt)
    assert wt_path.is_dir()
    # worktree holds a full checkout of main's commit
    assert (wt_path / "app.txt").read_text(encoding="utf-8") == "hello\n"
    assert (wt_path / ".git").exists()
    # registered in the source repo's worktree list
    out = _git(scratch_repo, "worktree", "list").stdout
    assert _norm(wt_path) in out


def test_discard_worktree_removes_it(scratch_repo, clean_run):
    run_id = clean_run("test-worktree-2")
    wt = create_worktree(str(scratch_repo), run_id)
    wt_path = Path(wt)
    assert wt_path.exists()

    discard_worktree(str(scratch_repo), run_id)

    assert not wt_path.exists()
    lines = _git(scratch_repo, "worktree", "list").stdout.strip().splitlines()
    assert len(lines) == 1  # only the primary worktree remains
    assert _norm(wt_path) not in _git(scratch_repo, "worktree", "list").stdout


def test_discard_worktree_force_removes_dirty_worktree(scratch_repo, clean_run):
    run_id = clean_run("test-worktree-3")
    wt = create_worktree(str(scratch_repo), run_id)
    wt_path = Path(wt)
    (wt_path / "untracked.txt").write_text("dirty", encoding="utf-8")
    (wt_path / "app.txt").write_text("modified", encoding="utf-8")

    discard_worktree(str(scratch_repo), run_id)

    assert not wt_path.exists()
    lines = _git(scratch_repo, "worktree", "list").stdout.strip().splitlines()
    assert len(lines) == 1
