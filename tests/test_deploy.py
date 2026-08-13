"""S4T2/S4T3: deploy/rollback module (fde/deploy.py) — TDD.

Scratch repos are git clones of demo-app (committed state only, so working-tree
dirt in demo-app can never leak into tests): main+prod start at the buggy
commit, then gold.patch is applied and committed on main as the fix commit.
All servers run on dedicated test ports (8198 preview / 8199 prod) and every
test cleans up: servers stopped, worktrees pruned, runs/<id> removed.
"""
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from fde.deploy import (
    DeployError,
    discard_preview,
    discard_prod_worktree,
    fetch,
    health_check,
    preview_deploy,
    prod_deploy,
    rollback,
    start_server,
    stop_server,
)
from fde.runlog import events, run_dir, set_state, state

SCRATCH = Path(__file__).parent / "scratch_repos"
DEMO = Path(__file__).parent.parent / "demo-app"

PREVIEW_PORT = 8198
PROD_PORT = 8199
FIXED_FRAGMENT = "118"   # gold behavior: /tax?amount=100 -> total 118
BUGGY_FRAGMENT = "115"   # buggy behavior: hardcoded 15% -> total 115


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _port_in_use(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port_free(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.2)
    return not _port_in_use(port)


@pytest.fixture(autouse=True)
def free_test_ports():
    """Fail fast (not flake) if a stray server holds a test port."""
    for p in (PREVIEW_PORT, PROD_PORT):
        deadline = time.time() + 5
        while _port_in_use(p) and time.time() < deadline:
            time.sleep(0.2)
        assert not _port_in_use(p), f"test port {p} busy — stray server from a crashed run?"
    yield


@pytest.fixture
def demo_repo():
    """Scratch repo cloned from demo-app: prod at buggy, main at the fix commit."""
    repo = SCRATCH / f"deploy_repo_{uuid.uuid4().hex[:8]}"
    subprocess.run(["git", "clone", "-q", str(DEMO), str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    buggy = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "branch", "prod", buggy)
    _git(repo, "apply", "gold.patch")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix: read TAX_RATE from config.json")
    fix = _git(repo, "rev-parse", "HEAD").stdout.strip()
    yield repo, buggy, fix
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                   capture_output=True, text=True)
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def clean_run():
    """Track throwaway run ids under runs/ and remove them before/after."""
    ids = []

    def _use(run_id: str) -> str:
        ids.append(run_id)
        shutil.rmtree(Path("runs") / run_id, ignore_errors=True)
        return run_id

    yield _use
    for rid in ids:
        shutil.rmtree(Path("runs") / rid, ignore_errors=True)


def _to_approved(run_id: str):
    for s in ["reproducing", "reproved", "fixing", "fixed", "gating", "gated",
              "awaiting_approval", "approved"]:
        set_state(run_id, s)


def test_start_stop_server_lifecycle(clean_run, demo_repo):
    """start -> health (buggy 115) -> stop -> port free, pid gone, no stray node."""
    repo, buggy, _fix = demo_repo
    rid = clean_run("deploy-lifecycle")
    wt = str(run_dir(rid).resolve() / "buggy_wt")
    _git(repo, "worktree", "add", "--detach", wt, buggy)
    try:
        res = start_server(wt, PROD_PORT, rid)
        assert res["ok"] is True
        pid_file = run_dir(rid) / "server.pid"
        assert pid_file.exists()
        assert int(pid_file.read_text(encoding="utf-8").strip()) == res["pid"]
        assert (run_dir(rid) / "server.log").exists()

        assert health_check(PROD_PORT, BUGGY_FRAGMENT) is True
        assert health_check(PROD_PORT, FIXED_FRAGMENT) is False
        assert '"total":115' in fetch(PROD_PORT)

        assert stop_server(rid) is True
        assert not pid_file.exists()
        assert _wait_port_free(PROD_PORT)
        assert health_check(PROD_PORT, BUGGY_FRAGMENT) is False
        assert stop_server(rid) is False  # idempotent: nothing to stop
    finally:
        stop_server(rid)
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                       capture_output=True, text=True)


def test_preview_deploy_fix_commit_healthy(clean_run, demo_repo):
    """Preview on the fix commit serves the fixed behavior (118)."""
    repo, _buggy, fix = demo_repo
    rid = clean_run("deploy-preview")
    try:
        res = preview_deploy(rid, str(repo), fix, port=PREVIEW_PORT)
        assert res["started"] is True
        assert res["healthy"] is True
        assert health_check(PREVIEW_PORT, FIXED_FRAGMENT) is True
        assert (Path(res["worktree"]) / "server.js").exists()
        assert (run_dir(rid) / "preview.pid").exists()
    finally:
        discard_preview(rid, str(repo))
        assert _wait_port_free(PREVIEW_PORT)
        assert len(_git(repo, "worktree", "list").stdout.strip().splitlines()) == 1


def test_preview_deploy_buggy_commit_unhealthy(clean_run, demo_repo):
    """Preview on the buggy commit starts but is NOT healthy for the fix fragment."""
    repo, buggy, _fix = demo_repo
    rid = clean_run("deploy-preview-buggy")
    try:
        res = preview_deploy(rid, str(repo), buggy, port=PREVIEW_PORT)
        assert res["started"] is True
        assert res["healthy"] is False
        assert health_check(PREVIEW_PORT, BUGGY_FRAGMENT) is True  # buggy served
    finally:
        discard_preview(rid, str(repo))
        assert _wait_port_free(PREVIEW_PORT)


def test_prod_deploy_requires_approval(clean_run, demo_repo):
    """prod_deploy without 'approved' state raises and mutates nothing."""
    repo, buggy, fix = demo_repo
    rid = clean_run("deploy-noapproval")
    with pytest.raises(DeployError, match="requires approval"):
        prod_deploy(rid, str(repo), fix, port=PROD_PORT)
    assert state(rid) == "submitted"
    assert _git(repo, "rev-parse", "prod").stdout.strip() == buggy  # branch untouched
    assert _wait_port_free(PROD_PORT)


def test_prod_deploy_approved_cycle(clean_run, demo_repo):
    """Approved -> prod fast-forwarded to fix, server serves 118, state deployed."""
    repo, buggy, fix = demo_repo
    rid = clean_run("deploy-prod")
    _to_approved(rid)
    try:
        res = prod_deploy(rid, str(repo), fix, port=PROD_PORT)
        assert res["healthy"] is True
        assert state(rid) == "deployed"
        assert _git(repo, "rev-parse", "prod").stdout.strip() == fix  # ff'd to fix
        assert (run_dir(rid) / "server.pid").exists()
        assert '"total":118' in fetch(PROD_PORT)

        evs = events(rid)
        assert evs[-1]["event"] == "deployed"
        assert evs[-1]["data"]["commit"] == fix
        assert evs[-1]["data"]["port"] == PROD_PORT
        assert evs[-1]["data"]["health"]["healthy"] is True
        assert "fix: read TAX_RATE" in _git(repo, "log", "--oneline", "prod").stdout
    finally:
        stop_server(rid)
        assert _wait_port_free(PROD_PORT)


def test_rollback_requires_deployed_then_rolls_back(clean_run, demo_repo):
    """rollback guard (needs deployed); then deployed -> rolled_back, curl shows 115."""
    repo, buggy, fix = demo_repo
    rid = clean_run("deploy-rollback")
    _to_approved(rid)
    with pytest.raises(DeployError, match="requires deployed"):
        rollback(rid, str(repo), fix, port=PROD_PORT)
    assert state(rid) == "approved"  # guard raised before any state change

    prod_deploy(rid, str(repo), fix, port=PROD_PORT)
    try:
        res = rollback(rid, str(repo), fix, port=PROD_PORT)
        assert res["healthy"] is True
        assert state(rid) == "rolled_back"
        assert '"total":115' in fetch(PROD_PORT)  # pre-fix behavior restored

        evs = events(rid)
        assert evs[-1]["event"] == "rolled_back"
        assert evs[-1]["data"]["fix_commit"] == fix
        assert evs[-1]["data"]["health"]["healthy"] is True

        # prod history: revert commit sits directly on top of the fix commit
        prod_log = _git(repo, "log", "--oneline", "prod").stdout.strip().splitlines()
        assert "Revert" in prod_log[0]
        assert _git(repo, "rev-parse", "prod~1").stdout.strip() == fix
    finally:
        stop_server(rid)
        assert _wait_port_free(PROD_PORT)


def test_full_cycle_cleanup_no_leftovers(clean_run, demo_repo):
    """Preview + deploy + rollback, then everything is torn down cleanly."""
    repo, _buggy, fix = demo_repo
    rid = clean_run("deploy-cleanup")
    _to_approved(rid)
    try:
        preview_deploy(rid, str(repo), fix, port=PREVIEW_PORT)
        prod_deploy(rid, str(repo), fix, port=PROD_PORT)
        rollback(rid, str(repo), fix, port=PROD_PORT)
    finally:
        discard_preview(rid, str(repo))
        discard_prod_worktree(rid, str(repo))
        stop_server(rid, pid_name="preview.pid")

    # all test ports free — no stray node processes left behind
    assert _wait_port_free(PREVIEW_PORT)
    assert _wait_port_free(PROD_PORT)
    # worktrees pruned: only the primary checkout remains
    assert len(_git(repo, "worktree", "list").stdout.strip().splitlines()) == 1
    # primary checkout untouched: still on main, clean tree
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    # run dir exists during the run; never committed to the tool repo
    assert run_dir(rid).exists()
    assert subprocess.run(["git", "ls-files", "runs"],
                          capture_output=True, text=True).stdout.strip() == ""
