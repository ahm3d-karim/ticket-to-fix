"""Optional Docker sandbox for pipeline command execution (S7).

``FDE_SANDBOX=docker`` routes every ``harness.run_cmd`` invocation (install,
test, git apply/restore/diff — the whole pipeline) through
:func:`run_in_docker`: an ephemeral ``fde-sandbox`` container running
``bash -c <cmd>`` with the worktree mounted at ``/workspace``, networking
disabled (``--network none``), all capabilities dropped (``--cap-drop
ALL``), new privileges blocked (``--security-opt no-new-privileges``),
resource limits applied (``--memory``/``--cpus``/``--pids-limit``,
overridable via ``FDE_SANDBOX_MEMORY``/``FDE_SANDBOX_CPUS``/``FDE_SANDBOX_PIDS``)
and a read-only rootfs (``--read-only`` with a writable ``--tmpfs /tmp`` for
node/git/python) — so the repo's "sandbox" claim is literal instead of a
worktree-with-timeouts approximation.

Unset or empty ``FDE_SANDBOX`` = host mode, current behavior untouched
(the one-command demo never depends on Docker). ``FDE_SANDBOX=host`` is the
explicit opt-in to that same host mode — it behaves identically but prints
a loud once-per-process warning to stderr. ``FDE_SANDBOX=required`` makes
docker MANDATORY: same routing as ``docker``, but the daemon/binary
fail-fast errors name ``FDE_SANDBOX=required`` explicitly — there is never
a host fallback. Unknown values raise ``ValueError`` at call time — the
same reject-unknowns dispatch pattern as the agent backend (fde/agents.py
``_dispatch_backend``), never a silent fallthrough. The docker CLI itself
is resolved per call via ``shutil.which``; a missing binary raises
``RuntimeError`` instead of a confusing ``FileNotFoundError`` from Popen.
The daemon is preflighted per call (``_daemon_ready``); when it is
unreachable, ``run_in_docker`` fails fast with ``RuntimeError`` — never a
silent fallback.

The container name is derived from a fresh ``uuid4()`` so concurrent runs
never collide; teardown (``docker kill`` on timeout, ``docker rm -f``
always) is best-effort and idempotent — ``--rm`` covers the normal exit
path as well. Return shape is identical to ``harness.run_cmd``:
``{"rc", "out", "timed_out"}``.
"""
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

VALID_SANDBOXES = ("", "docker", "required", "host")

_HOST_MODE_WARN = (
    "FDE_SANDBOX=host: running harness commands on the HOST — not isolated. "
    "Set FDE_SANDBOX=required for the secure posture.")
_HOST_MODE_WARNED = False


def _validate_sandbox_value(value: str) -> str:
    """Reject unknown FDE_SANDBOX values (shared by every entry point)."""
    if value not in VALID_SANDBOXES:
        raise ValueError(
            f"unknown FDE_SANDBOX {value!r} — expected one of "
            f"{', '.join(repr(v) for v in VALID_SANDBOXES)} "
            "(empty string is the default)")
    return value


def _warn_host_mode_once() -> None:
    """Print the FDE_SANDBOX=host warning to stderr exactly once per process."""
    global _HOST_MODE_WARNED
    if _HOST_MODE_WARNED:
        return
    _HOST_MODE_WARNED = True
    print(_HOST_MODE_WARN, file=sys.stderr)


def sandbox_active() -> bool:
    """True when ``FDE_SANDBOX`` is ``docker`` or ``required`` (read at call
    time).

    Reads the env var on every call (not at import) so tests can
    monkeypatch ``os.environ`` freely. Empty/unset -> False (host mode).
    ``host`` -> False too, but with a loud once-per-process stderr warning
    (explicit opt-in, never silent). Any other value raises ValueError
    naming the valid values.
    """
    value = _validate_sandbox_value(os.environ.get("FDE_SANDBOX", ""))
    if value == "host":
        _warn_host_mode_once()
        return False
    return value in ("docker", "required")


_MISSING_DOCKER_MSG = (
    "FDE_SANDBOX=docker but the docker CLI was not found on PATH — "
    "start Docker Desktop or unset FDE_SANDBOX")

_DAEMON_DOWN_MSG = (
    "FDE_SANDBOX=docker but the docker daemon is not reachable — "
    "start Docker Desktop or unset FDE_SANDBOX")

_MISSING_DOCKER_REQUIRED_MSG = (
    "FDE_SANDBOX=required but the docker CLI was not found on PATH — "
    "FDE_SANDBOX=required demands the docker daemon and there is no host "
    "fallback; start Docker Desktop or unset FDE_SANDBOX")

_DAEMON_DOWN_REQUIRED_MSG = (
    "FDE_SANDBOX=required but the docker daemon is not reachable — "
    "FDE_SANDBOX=required demands the docker daemon and there is no host "
    "fallback; start Docker Desktop or unset FDE_SANDBOX")


def _sandbox_mode() -> str:
    """The raw FDE_SANDBOX value (message selection inside run_in_docker)."""
    return os.environ.get("FDE_SANDBOX", "")


def _missing_docker_msg() -> str:
    if _sandbox_mode() == "required":
        return _MISSING_DOCKER_REQUIRED_MSG
    return _MISSING_DOCKER_MSG


def _daemon_down_msg() -> str:
    if _sandbox_mode() == "required":
        return _DAEMON_DOWN_REQUIRED_MSG
    return _DAEMON_DOWN_MSG


def _docker_binary() -> str:
    """Resolve the docker CLI; raise a clear error when it is missing."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError(_missing_docker_msg())
    return docker


def _daemon_ready(docker: str) -> bool:
    """True when the docker server answers `version` within 10s."""
    try:
        r = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _best_effort(argv) -> None:
    """Run a teardown command, swallowing every failure (cleanup only)."""
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def gold_path_in_sandbox(gold: str, wt: str) -> str:
    """Return the gold.patch path usable from inside the sandbox.

    Host mode: the path is returned unchanged (byte-identical behavior).

    Sandbox mode: the container only sees the worktree mount at
    ``/workspace``, so a patch living in the fixture repo (outside the
    mount) is copied into the worktree first and the in-worktree path is
    returned. The copy is untracked — the per-round ``git clean -fd``
    restore removes it — and is (re)created on every call (idempotent
    overwrite), so verify_repro's state B and state C each get a fresh copy
    after the restore between states.
    """
    if not sandbox_active():
        return gold
    dst = Path(wt).resolve() / ".fde-gold.patch"
    shutil.copy2(gold, dst)
    return str(dst)


def _gitdir_from_worktree(wt: str) -> str | None:
    """Parse ``<wt>/.git`` (a FILE, as worktrees have) for its gitdir line.

    Returns the absolute host path of the fixture's gitdir
    (``<fixture>/.git/worktrees/<name>``), or None when cwd is not a
    worktree (no .git file) or the line is unreadable.
    """
    try:
        dotgit = Path(wt).resolve() / ".git"
        if not dotgit.is_file():
            return None
        for line in dotgit.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("gitdir:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _mount_target(gitdir: str) -> tuple[Path, str, str] | None:
    """Compute (git_dir, container_mount, worktree_name) for a gitdir line.

    The fixture's ``.git`` DIRECTORY is mounted at the fixed container path
    ``/fde/gitdir`` and the container runs git with ``GIT_DIR`` pointing at
    ``/fde/gitdir/worktrees/<name>`` (see :func:`_sandbox_git_env`) — the
    worktree's own ``.git`` FILE is never rewritten, nothing is mounted
    under ``/workspace``, and the fixture's source tree never appears in the
    container (no node --test discovery pollution, no git clean side
    effects, host git and bench cleanup keep working untouched).
    """
    gd = Path(gitdir)
    # gitdir = <fixture>/.git/worktrees/<name>
    if len(gd.parents) < 3:
        return None
    git_dir = gd.parents[1]             # <fixture>/.git
    return git_dir, _GITDIR_MOUNT, gd.name


_GITDIR_MOUNT = "/fde/gitdir"


def _sandbox_git_env(cwd: str) -> tuple[list[str], dict]:
    """(--mount argv pieces, env overrides) so in-container git works.

    The harness's worktrees carry a ``.git`` FILE pointing at the fixture's
    gitdir via an absolute HOST path — unresolvable inside the container.
    Instead of rewriting it, the fixture's ``.git`` dir is bind-mounted at
    ``/fde/gitdir`` and git is steered there with ``GIT_DIR`` +
    ``GIT_WORK_TREE`` (env wins over discovery). Returns empty lists/dicts
    when cwd is not a worktree or the gitdir is malformed (single-mount
    behavior preserved).
    """
    gitdir = _gitdir_from_worktree(cwd)
    if not gitdir:
        return [], {}
    mt = _mount_target(gitdir)
    if mt is None:
        return [], {}
    git_dir, mount, name = mt
    if not git_dir.is_dir():
        return [], {}
    # source must be POSIX-form: WindowsPath str() yields backslashes, which
    # the docker CLI misreads as a volume name instead of a bind source
    return (["--mount",
             f"type=bind,source={git_dir.as_posix()},target={mount}"],
            {"GIT_DIR": f"{mount}/worktrees/{name}",
             "GIT_WORK_TREE": "/workspace"})


def run_in_docker(cmd: str, cwd: str, timeout: int = 60) -> dict:
    """Run ``bash -c <cmd>`` inside an ephemeral sandbox container.

    Returns ``{"rc", "out", "timed_out"}`` exactly like ``harness.run_cmd``:
    ``out`` is stdout + stderr combined, ``rc`` is docker run's exit code.
    On timeout the container is killed (best-effort) and the result is
    ``{"rc": -1, "out": "[timeout]", "timed_out": True}``. The container is
    always removed in a finally (``docker rm -f`` is idempotent; ``--rm``
    covers the normal path too).
    """
    docker = _docker_binary()
    if docker is None:  # mock/monkeypatched _docker_binary may return None
        raise RuntimeError(_missing_docker_msg())
    if not _daemon_ready(docker):
        raise RuntimeError(_daemon_down_msg())
    name = "fde-sandbox-" + uuid.uuid4().hex[:8]
    volumes = [f"{os.path.abspath(cwd)}:/workspace"]
    extra_mounts, git_env = _sandbox_git_env(cwd)
    volume_args = [a for v in volumes for a in ("-v", v)]
    # docker run does NOT inherit the CLI process env — git steering must be
    # explicit -e flags for the container
    env_args = [a for k, v in git_env.items() for a in ("-e", f"{k}={v}")]
    # container hardening: resource limits (env-overridable) + read-only
    # rootfs with a writable tmpfs /tmp (node/git/python need it; the
    # worktree and gitdir mounts are already writable volumes)
    hardening = [
        "--memory", os.environ.get("FDE_SANDBOX_MEMORY") or "1g",
        "--cpus", os.environ.get("FDE_SANDBOX_CPUS") or "2",
        "--pids-limit", os.environ.get("FDE_SANDBOX_PIDS") or "256",
        "--read-only",
        "--tmpfs", "/tmp",
    ]
    argv = [
        docker, "run", "--rm", "--name", name,
        *volume_args,
        *extra_mounts,
        *env_args,
        "-w", "/workspace",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        *hardening,
        os.environ.get("FDE_SANDBOX_IMAGE", "fde-sandbox:latest"),
        "bash", "-c", cmd,
    ]
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
            return {"rc": proc.returncode, "out": (out or "") + (err or ""),
                    "timed_out": False}
        except subprocess.TimeoutExpired:
            _best_effort([docker, "kill", name])
            return {"rc": -1, "out": "[timeout]", "timed_out": True}
    finally:
        _best_effort([docker, "rm", "-f", name])
