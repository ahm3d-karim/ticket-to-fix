"""Optional Docker sandbox for pipeline command execution (S7).

``FDE_SANDBOX=docker`` routes every ``harness.run_cmd`` invocation (install,
test, git apply/restore/diff — the whole pipeline) through
:func:`run_in_docker`: an ephemeral ``fde-sandbox`` container running
``bash -c <cmd>`` with the worktree mounted at ``/workspace`` and networking
disabled (``--network none``), so the repo's "sandbox" claim is literal
instead of a worktree-with-timeouts approximation.

Unset or empty ``FDE_SANDBOX`` = host mode, current behavior untouched
(the one-command demo never depends on Docker). Unknown values raise
``ValueError`` at call time — the same reject-unknowns dispatch pattern as
the agent backend (fde/agents.py ``_dispatch_backend``), never a silent
fallthrough. The docker CLI itself is resolved per call via
``shutil.which``; a missing binary raises ``RuntimeError`` instead of a
confusing ``FileNotFoundError`` from Popen.

The container name is derived from a fresh ``uuid4()`` so concurrent runs
never collide; teardown (``docker kill`` on timeout, ``docker rm -f``
always) is best-effort and idempotent — ``--rm`` covers the normal exit
path as well. Return shape is identical to ``harness.run_cmd``:
``{"rc", "out", "timed_out"}``.
"""
import os
import shutil
import subprocess
import uuid

VALID_SANDBOXES = ("", "docker")


def sandbox_active() -> bool:
    """True when ``FDE_SANDBOX=docker`` (read at call time).

    Reads the env var on every call (not at import) so tests can
    monkeypatch ``os.environ`` freely. Empty/unset -> False (host mode).
    Any other value raises ValueError naming the valid values.
    """
    value = os.environ.get("FDE_SANDBOX", "")
    if value not in VALID_SANDBOXES:
        raise ValueError(
            f"unknown FDE_SANDBOX {value!r} — expected one of "
            f"{', '.join(repr(v) for v in VALID_SANDBOXES)} "
            "(empty string is the default)")
    return value == "docker"


_MISSING_DOCKER_MSG = (
    "FDE_SANDBOX=docker but the docker CLI was not found on PATH — "
    "start Docker Desktop or unset FDE_SANDBOX")


def _docker_binary() -> str:
    """Resolve the docker CLI; raise a clear error when it is missing."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError(_MISSING_DOCKER_MSG)
    return docker


def _best_effort(argv) -> None:
    """Run a teardown command, swallowing every failure (cleanup only)."""
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except Exception:
        pass


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
        raise RuntimeError(_MISSING_DOCKER_MSG)
    name = "fde-sandbox-" + uuid.uuid4().hex[:8]
    argv = [
        docker, "run", "--rm", "--name", name,
        "-v", f"{os.path.abspath(cwd)}:/workspace",
        "-w", "/workspace",
        "--network", "none",
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
