"""Preview/prod deploy + rollback: server process management + git ops (S4T2/S4T3).

Verification is curl-based (git-bash ships curl). Servers are ``node server.js``
children of the tool; each run records its PID in ``runs/<run_id>/<name>.pid``
and combined output in ``runs/<run_id>/server.log``. Stops kill the process
tree with ``taskkill /F /T`` (single slashes: this module calls taskkill via
subprocess, so MSYS path-mangling of ``/F`` does not apply).

State requirements (S4T3): ``prod_deploy`` requires runlog state ``approved``
and moves ``approved -> deploying -> deployed``; ``rollback`` requires
``deployed`` and moves ``deployed -> rolling_back -> rolled_back``. Both append
their vocabulary events (``deployed`` / ``rolled_back``) carrying the curl
verification result.

Deviations from the plan's literal wording (documented here on purpose):
- Branch fast-forward uses ``merge --ff-only`` executed in whichever worktree
  has the prod branch checked out (falling back to ``reset --hard`` when the
  merge refuses), instead of ``branch -f``, which errors when the branch is
  checked out anywhere and would silently desync a checked-out worktree.
- ``git revert`` and branch updates run through ``_run_on_branch``, which
  restores the primary checkout's original branch afterwards, so repeated runs
  never leave the fixture repo parked on ``prod``.
- The prod server runs from a detached worktree pinned to the prod ref
  (``runs/<run_id>/prod_worktree``), never the primary checkout: after a
  rollback the served tree must be the *reverted* prod code while the primary
  checkout stays untouched on ``main``.
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .config import load_user_config
from .runlog import append, run_dir, set_state, state


class DeployError(Exception):
    """Raised when a deploy/rollback precondition or git/process op fails."""


def _cfg() -> dict:
    return load_user_config().get("deploy", {})


def _git(cwd, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise DeployError(
            f"git {args[0]} failed (rc={r.returncode}): {r.stderr.strip()}"
        )
    return r


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port_free(port: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_open(port):
            return True
        time.sleep(0.2)
    return not _port_open(port)


def fetch(port: int, path: str = "/tax?amount=100") -> str:
    """curl the app; returns '' when unreachable (server down / curl missing)."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "3", f"http://127.0.0.1:{port}{path}"],
            capture_output=True, text=True, timeout=6)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def health_check(port: int, fragment: str, attempts: int = 8) -> bool:
    """True when the app body contains `fragment` (e.g. the expected total).

    Retries a few times: on Windows the very first curl of a cold node server
    can transiently exceed --max-time (AV scan / MSYS init), and CI runners
    are contended — a health check that passes a second later is a pass.
    """
    for i in range(attempts):
        if fragment in fetch(port):
            return True
        if i < attempts - 1:
            time.sleep(0.4)
    return False


def start_server(worktree: str, port: int, run_id: str,
                 pid_name: str = "server.pid", wait: float = 5.0) -> dict:
    """Run `node server.js` in `worktree` with PORT=port.

    PID -> runs/<run_id>/<pid_name>, combined output -> runs/<run_id>/server.log.
    Waits up to `wait` seconds for the port to accept connections.
    Returns {"ok": bool, "pid": int | None}.
    """
    d = run_dir(run_id)
    env = dict(os.environ)
    env["PORT"] = str(port)
    log = open(d / "server.log", "ab")
    try:
        proc = subprocess.Popen(["node", "server.js"], cwd=os.path.abspath(worktree),
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    except OSError as e:
        log.close()
        raise DeployError(f"could not spawn node: {e}") from e
    log.close()  # child holds its own inherited handle
    (d / pid_name).write_text(str(proc.pid), encoding="utf-8")
    ok = False
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # crashed (e.g. EADDRINUSE) — check server.log
        if _port_open(port):
            ok = True
            break
        time.sleep(0.2)
    return {"ok": ok, "pid": proc.pid}


def stop_server(run_id: str, pid_name: str = "server.pid") -> bool:
    """Kill the recorded PID's process tree (taskkill /F /T) and drop the pid file."""
    pid_file = run_dir(run_id) / pid_name
    if not pid_file.exists():
        return False
    pid = pid_file.read_text(encoding="utf-8").strip()
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                       capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    pid_file.unlink(missing_ok=True)
    return True


def _worktree_on_branch(repo: str, branch: str):
    """Path of the worktree that has `branch` checked out, or None."""
    r = _git(repo, "worktree", "list", "--porcelain")
    for block in r.stdout.split("\n\n"):
        path, branch_line = None, None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line.startswith("branch "):
                branch_line = line[len("branch "):]
        if branch_line == f"refs/heads/{branch}":
            return path
    return None


def _run_on_branch(repo: str, branch: str, fn):
    """Run fn(cwd) with `branch` checked out; restore the original branch after."""
    wt = _worktree_on_branch(repo, branch)
    if wt:
        return fn(wt)
    cur = _git(repo, "branch", "--show-current").stdout.strip()
    if cur == branch:
        return fn(repo)
    _git(repo, "checkout", "-q", branch)
    try:
        return fn(repo)
    finally:
        _git(repo, "checkout", "-q", cur)


def _ff_branch(repo: str, branch: str, commit: str) -> str:
    """Fast-forward `branch` to `commit`. Returns the branch's new HEAD sha."""

    def _ff(cwd: str) -> str:
        r = subprocess.run(["git", "-C", cwd, "merge", "--ff-only", commit],
                           capture_output=True, text=True)
        if r.returncode != 0:  # dirty worktree / not ff — move the ref, sync the tree
            _git(cwd, "reset", "--hard", commit)
        return _git(cwd, "rev-parse", "HEAD").stdout.strip()

    return _run_on_branch(repo, branch, _ff)


def _ensure_worktree(repo: str, path: str, ref: str) -> str:
    """(Re)create a detached worktree at `path` pinned to `ref`."""
    p = Path(path)
    if p.exists():
        r = subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", path],
                           capture_output=True, text=True)
        if r.returncode != 0 and p.exists():
            shutil.rmtree(p, ignore_errors=True)
        subprocess.run(["git", "-C", repo, "worktree", "prune"],
                       capture_output=True, text=True)
    _git(repo, "worktree", "add", "--detach", path, ref)
    return path


def preview_deploy(run_id: str, repo: str, commit: str,
                   port: int | None = None, fragment: str = "118") -> dict:
    """Check out `commit` into runs/<run_id>/preview_worktree and serve it on the preview port."""
    port = port or int(_cfg().get("preview_port", 8123))
    wt = str(run_dir(run_id).resolve() / "preview_worktree")
    _git(repo, "worktree", "add", "--detach", wt, commit)
    started = start_server(wt, port, run_id, pid_name="preview.pid")
    healthy = started["ok"] and health_check(port, fragment)
    return {"url": f"http://127.0.0.1:{port}", "healthy": healthy,
            "worktree": wt, "started": started["ok"]}


def discard_preview(run_id: str, repo: str):
    """Stop the preview server and remove the preview worktree."""
    stop_server(run_id, pid_name="preview.pid")
    wt = str(run_dir(run_id).resolve() / "preview_worktree")
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                   capture_output=True, text=True)


def discard_prod_worktree(run_id: str, repo: str):
    """Stop the prod server and remove runs/<run_id>/prod_worktree."""
    stop_server(run_id)
    wt = str(run_dir(run_id).resolve() / "prod_worktree")
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                   capture_output=True, text=True)


def prod_deploy(run_id: str, repo: str, commit: str,
                port: int | None = None, prod_branch: str | None = None) -> dict:
    """Fast-forward prod to the fix commit, restart the server, verify, mark deployed.

    Requires runlog state ``approved`` (S4T3). Records a ``deployed`` event
    carrying the curl verification result.
    """
    port = port or int(_cfg().get("port", 8124))
    prod_branch = prod_branch or _cfg().get("prod_branch", "prod")
    if state(run_id) != "approved":
        raise DeployError("deployment requires approval (run state must be 'approved')")
    set_state(run_id, "deploying")
    try:
        stop_server(run_id)
        _wait_port_free(port)
        new_head = _ff_branch(repo, prod_branch, commit)
        wt = _ensure_worktree(repo, str(run_dir(run_id).resolve() / "prod_worktree"),
                              prod_branch)
        started = start_server(wt, port, run_id)
        if not started["ok"]:
            raise DeployError(
                f"server failed to start on port {port}; see runs/{run_id}/server.log")
        body = fetch(port)
        healthy = "118" in body
        set_state(run_id, "deployed")
        append(run_id, "deployed", {"commit": commit, "prod_head": new_head, "port": port,
                                    "health": {"healthy": healthy, "body": body.strip()}})
        return {"commit": commit, "port": port, "healthy": healthy,
                "url": f"http://127.0.0.1:{port}", "prod_head": new_head}
    except Exception:
        if state(run_id) == "deploying":
            try:
                set_state(run_id, "failed")
            except ValueError:
                pass
        raise


def rollback(run_id: str, repo: str, fix_commit: str,
             port: int | None = None, prod_branch: str | None = None) -> dict:
    """Revert the fix commit on prod, restart the server, verify pre-fix behavior.

    Requires runlog state ``deployed`` (S4T3). Records a ``rolled_back`` event
    carrying the curl verification result.
    """
    port = port or int(_cfg().get("port", 8124))
    prod_branch = prod_branch or _cfg().get("prod_branch", "prod")
    if state(run_id) != "deployed":
        raise DeployError("rollback requires deployed state (run state must be 'deployed')")
    set_state(run_id, "rolling_back")
    try:
        def _revert(cwd: str) -> str:
            _git(cwd, "revert", "--no-edit", fix_commit)
            return _git(cwd, "rev-parse", "HEAD").stdout.strip()

        rev_head = _run_on_branch(repo, prod_branch, _revert)
        stop_server(run_id)
        _wait_port_free(port)
        wt = _ensure_worktree(repo, str(run_dir(run_id).resolve() / "prod_worktree"),
                              prod_branch)
        started = start_server(wt, port, run_id)
        if not started["ok"]:
            raise DeployError(
                f"server failed to start on port {port}; see runs/{run_id}/server.log")
        body = fetch(port)
        healthy = "115" in body
        set_state(run_id, "rolled_back")
        append(run_id, "rolled_back", {"fix_commit": fix_commit, "revert_head": rev_head,
                                       "port": port,
                                       "health": {"healthy": healthy, "body": body.strip()}})
        return {"fix_commit": fix_commit, "port": port, "healthy": healthy,
                "url": f"http://127.0.0.1:{port}", "revert_head": rev_head}
    except Exception:
        if state(run_id) == "rolling_back":
            try:
                set_state(run_id, "failed")
            except ValueError:
                pass
        raise
