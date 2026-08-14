"""Session bootstrap: the fixture repos and demo-app are git repos by design,
but their .git dirs are gitignored, so a fresh checkout (CI) has none. The
pipeline's worktree creation and the deploy tests' clones both need a real
repo with a baseline commit. Initialize once per session; a no-op when the
repos already exist (local dev, prior runs).
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = [d for d in (ROOT / "fixtures").iterdir() if d.is_dir()]
REPOS = [ROOT / "demo-app", *FIXTURES]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, text=True)


@pytest.fixture(scope="session", autouse=True)
def _ensure_git_repos():
    for repo in REPOS:
        if not (repo / "fde.yaml").is_file() or (repo / ".git").exists():
            continue
        # baseline commit on main: the harness adds detached worktrees of
        # `main` and the deploy tests clone demo-app for their scratch repos
        _git(repo, "init", "-b", "main")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=fde", "-c", "user.email=fde@local",
             "commit", "-m", "baseline")
