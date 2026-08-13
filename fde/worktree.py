"""Git worktree management for Ticket-to-Fix runs.

Each run gets its own worktree at ``runs/<run_id>/worktree`` (the ``runs/``
dir is gitignored) so the harness can apply gold.patch and run tests without
touching the fixture's primary checkout.

Deviation from the plan's literal code: ``git worktree add <path> main``
refuses (rc=128) when ``main`` is checked out in the primary worktree, which
is the normal state of every fixture repo. We add the worktree **detached at
main's tip** instead (``--detach main``): same tree, no branch created, no
primary checkout mutated, safe to repeat across runs. The harness never
commits to the worktree, so detached HEAD is equivalent for everything that
happens here (``git apply``, ``git checkout .``, ``git clean -fd``).
"""
import subprocess
from pathlib import Path

from .runlog import run_dir


def _wt_path(run_id: str) -> str:
    # absolute: relative worktree paths resolve against the *repo's* working
    # tree, not the process CWD — an absolute path lands in runs/<id>/ exactly
    return str((run_dir(run_id) / "worktree").resolve())


def create_worktree(repo: str, run_id: str) -> str:
    """Add a worktree at runs/<run_id>/worktree on main (detached). Returns its path."""
    wt = _wt_path(run_id)
    subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", wt, "main"],
                   check=True, capture_output=True, text=True)
    return wt


def discard_worktree(repo: str, run_id: str):
    """Remove the run's worktree (--force: dirty/untracked files are fine)."""
    wt = _wt_path(run_id)
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                   check=True, capture_output=True, text=True)
