"""Offline tests for the FDE_SANDBOX=docker sandbox (roadmap S7, task 1.4).

No real Docker is ever touched: a FAKE `docker` CLI is placed on PATH.
The `_write_bin_shim` helper below is a LOCAL copy of the one in
tests/test_backends.py (a .cmd batch file on Windows, an executable
POSIX script elsewhere) — replicated here on purpose because tests/ is
a package and cross-test imports are fragile (Phase 2 plan deviation;
tests/test_backends.py stays untouched).
The fake logs its full argv to a file so tests can assert the exact
mount/network/image arguments, sleeps FDE_FAKE_DOCKER_DELAY seconds on
`run` (exercising the timeout path), and answers `kill`/`rm` instantly so
timeout teardown never hangs. It also answers the `version` subcommand
(the daemon preflight) WITHOUT logging it — so the first log line is
always the `run` invocation under test. `daemon_down=True` makes
`version` exit 1 (the fail-fast preflight path).

sandbox_active() reads FDE_SANDBOX at call time (per the S7 contract); the
tests additionally reload fde.sandbox after every env mutation so they
also hold if the module validates the env at import time (roadmap 1.1).
"""
import importlib
import os
import re
from pathlib import Path

import pytest

FAKE_DOCKER_VERSION = "docker 29.7.2-fake"
FAKE_DOCKER_RAN = "fake-docker-ran"


def _write_bin_shim(bin_dir: Path, name: str, body_cmd: str, body_sh: str) -> None:
    """Write a fake CLI: `<name>.cmd` on Windows, an executable POSIX script
    elsewhere (CI runners are Linux; a bare name does not resolve to
    `<name>.cmd` there — the backend's shutil.which lookup is exactly what
    these tests exercise). Local copy of tests/test_backends.py's helper."""
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(body_cmd, encoding="utf-8")
    else:
        script = bin_dir / name
        script.write_text(body_sh, encoding="utf-8")
        script.chmod(0o755)


def _make_fake_docker(tmp_path, monkeypatch, daemon_down: bool = False) -> dict:
    """Put a fake `docker` CLI on PATH.

    Appends its full argv (space-joined) to docker-args.txt on every call,
    answers `--version` AND the `version` subcommand (the daemon preflight
    — answered without logging so the log's first line stays the `run`
    invocation), and on `run` prints FAKE_DOCKER_RAN and exits 0.
    When FDE_FAKE_DOCKER_DELAY is set, `run` sleeps that many seconds
    first (the timeout path); `kill`/`rm` answer instantly. The log is
    APPENDED (`>>`) so the run invocation stays the first line even after
    the teardown rm. With `daemon_down=True`, `version` exits 1 (daemon
    unreachable — run_in_docker must fail fast).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_log = tmp_path / "docker-args.txt"
    _write_bin_shim(
        bin_dir, "docker",
        # Windows .cmd. %~1 strips per-arg quoting; the shift loop
        # re-joins with single spaces (cmd /c quote-stripping can split
        # spaced args — the rejoin round-trips them exactly). !var!
        # expansion requires setlocal enabledelayedexpansion.
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        'if "%1"=="--version" (\r\n'
        f"  echo {FAKE_DOCKER_VERSION}\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'if "%1"=="version" (\r\n'
        f'  if "{daemon_down}"=="True" exit /b 1\r\n'
        f"  echo {FAKE_DOCKER_VERSION}\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'set "_delay="\r\n'
        'if not "%FDE_FAKE_DOCKER_DELAY%"=="" '
        "set /a _delay=%FDE_FAKE_DOCKER_DELAY%+1\r\n"
        'set "cmd=%~1"\r\n'
        'set "line=%~1"\r\n'
        ":logloop\r\n"
        "shift\r\n"
        'if "%~1"=="" goto logged\r\n'
        'set "line=!line! %~1"\r\n'
        "goto logloop\r\n"
        ":logged\r\n"
        f'echo !line! >> "{args_log}"\r\n'
        'if "%cmd%"=="run" (\r\n'
        "  if defined _delay ping -n !_delay! 127.0.0.1 >nul\r\n"
        f"  echo {FAKE_DOCKER_RAN}\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'if "%cmd%"=="kill" (\r\n'
        "  echo fake-docker-killed\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'if "%cmd%"=="rm" (\r\n'
        "  echo fake-docker-removed\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        f"echo {FAKE_DOCKER_RAN}\r\n"
        "exit /b 0\r\n",
        # POSIX script
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then\n'
        f'  echo "{FAKE_DOCKER_VERSION}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "version" ]; then\n'
        f'  if [ "{daemon_down}" = "True" ]; then\n'
        "    exit 1\n"
        "  fi\n"
        f'  echo "{FAKE_DOCKER_VERSION}"\n'
        "  exit 0\n"
        "fi\n"
        f'echo "$*" >> "{args_log}"\n'
        'if [ "$1" = "run" ]; then\n'
        '  if [ -n "$FDE_FAKE_DOCKER_DELAY" ]; then\n'
        '    sleep "$FDE_FAKE_DOCKER_DELAY"\n'
        "  fi\n"
        f"  echo '{FAKE_DOCKER_RAN}'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "kill" ] || [ "$1" = "rm" ]; then\n'
        '  echo "fake-docker-$1"\n'
        "  exit 0\n"
        "fi\n"
        f"echo '{FAKE_DOCKER_RAN}'\n"
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    return {"args": args_log}


def _docker_argv(log: Path) -> str:
    """Normalized (single-space-joined) argv of the FIRST docker invocation.

    The fake logs every call (run, then the teardown rm); the first line is
    the one under test.
    """
    first = log.read_text(encoding="utf-8").splitlines()[0]
    return " ".join(first.split())


def _sandbox():
    """Import fde.sandbox, re-executed so module state tracks current env."""
    import fde.sandbox as sb
    importlib.reload(sb)
    return sb


def _harness():
    """Import fde.harness, re-executed so `from .sandbox import` re-binds."""
    import fde.harness as h
    importlib.reload(h)
    return h


def test_sandbox_inactive_by_default(monkeypatch):
    """No FDE_SANDBOX -> sandbox_active() is False (D1: unset = current)."""
    monkeypatch.delenv("FDE_SANDBOX", raising=False)
    sb = _sandbox()
    assert sb.sandbox_active() is False


def test_sandbox_active_when_docker(monkeypatch):
    """FDE_SANDBOX=docker -> sandbox_active() is True."""
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    sb = _sandbox()
    assert sb.sandbox_active() is True


def test_sandbox_unknown_value_raises(monkeypatch):
    """Unknown FDE_SANDBOX value -> ValueError naming the valid values."""
    monkeypatch.setenv("FDE_SANDBOX", "bogus")
    import fde.sandbox as sb
    with pytest.raises(ValueError) as excinfo:
        importlib.reload(sb)  # import-time validation, if the module does that
        sb.sandbox_active()   # call-time validation (the S7 contract)
    msg = str(excinfo.value)
    assert "docker" in msg
    assert '""' in msg or "''" in msg


def test_run_in_docker_argv(monkeypatch, tmp_path):
    """run_in_docker builds the exact docker-run argv (mount/network/image)."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    sb = _sandbox()
    cwd = tmp_path / "work"
    cwd.mkdir()

    res = sb.run_in_docker("echo hi", str(cwd), 30)

    # return shape identical to run_cmd
    assert set(res) >= {"rc", "out", "timed_out"}
    assert res["rc"] == 0
    assert res["timed_out"] is False
    assert FAKE_DOCKER_RAN in res["out"]

    argv = _docker_argv(logs["args"])
    assert argv.split()[0] == "run"
    assert "--rm" in argv
    assert re.search(r"--name fde-sandbox-[0-9a-f]{8}", argv)
    assert f"{os.path.abspath(str(cwd))}:/workspace" in argv
    assert "-w /workspace" in argv
    assert "--network none" in argv
    assert "testimg" in argv
    assert "bash -c echo hi" in argv
    # contract order: run --rm --name -v -w --network <image> bash -c <cmd>
    markers = ["--rm", "--name", f"{os.path.abspath(str(cwd))}:/workspace",
               "-w /workspace", "--network none", "bash"]
    positions = [argv.index(m) for m in markers]
    assert positions == sorted(positions)


def test_run_in_docker_timeout(monkeypatch, tmp_path):
    """Fake docker sleeps past the timeout -> timed_out True, rc -1."""
    _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_FAKE_DOCKER_DELAY", "5")
    sb = _sandbox()

    res = sb.run_in_docker("echo hi", str(tmp_path), 1)

    assert res["timed_out"] is True
    assert res["rc"] == -1


def test_missing_docker_binary_raises(monkeypatch, tmp_path):
    """No docker on PATH -> RuntimeError naming FDE_SANDBOX (no fallback)."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    import fde.sandbox as sb
    with pytest.raises(RuntimeError) as excinfo:
        importlib.reload(sb)  # in case the binary is resolved at import
        sb.run_in_docker("echo hi", str(tmp_path), 30)
    assert "FDE_SANDBOX" in str(excinfo.value)


def test_run_cmd_host_mode_not_routed(monkeypatch, tmp_path):
    """Host mode (FDE_SANDBOX unset) must never reach run_in_docker."""
    monkeypatch.delenv("FDE_SANDBOX", raising=False)
    sb = _sandbox()

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("run_in_docker must not be called in host mode")

    monkeypatch.setattr(sb, "run_in_docker", _must_not_be_called)
    harness = _harness()  # re-binds `from .sandbox import run_in_docker`

    cwd = tmp_path / "work"
    cwd.mkdir()
    res = harness.run_cmd("echo hi", str(cwd), 30)

    assert res["rc"] == 0
    assert res["timed_out"] is False
    assert "hi" in res["out"]


def test_run_cmd_docker_mode_routes(monkeypatch, tmp_path):
    """FDE_SANDBOX=docker -> run_cmd executes through the fake docker."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    _sandbox()
    harness = _harness()

    cwd = tmp_path / "work"
    cwd.mkdir()
    res = harness.run_cmd("echo hi", str(cwd), 30)

    assert res["rc"] == 0
    assert FAKE_DOCKER_RAN in res["out"]
    argv = _docker_argv(logs["args"])
    assert argv.split()[0] == "run"


def test_argv_has_hardening_flags(tmp_path, monkeypatch):
    """run_in_docker argv carries --cap-drop ALL and --security-opt
    no-new-privileges, ordered after --network none (Phase 2 hardening)."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    sb = _sandbox()
    cwd = tmp_path / "work"
    cwd.mkdir()

    sb.run_in_docker("echo hi", str(cwd), 30)

    argv = _docker_argv(logs["args"])
    assert "--cap-drop ALL" in argv
    assert "--security-opt no-new-privileges" in argv
    # contract order: ... --network none --cap-drop ALL --security-opt ...
    markers = ["--network none", "--cap-drop ALL",
               "--security-opt no-new-privileges"]
    positions = [argv.index(m) for m in markers]
    assert positions == sorted(positions)


def test_daemon_down_raises(tmp_path, monkeypatch):
    """Daemon unreachable (`version` exits 1) -> RuntimeError naming
    FDE_SANDBOX — fail fast, never a silent fallback (constraint 5)."""
    _make_fake_docker(tmp_path, monkeypatch, daemon_down=True)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    sb = _sandbox()

    with pytest.raises(RuntimeError) as excinfo:
        sb.run_in_docker("echo hi", str(tmp_path), 30)

    msg = str(excinfo.value)
    assert "daemon is not reachable" in msg
    assert "FDE_SANDBOX" in msg


def test_volume_arg_uses_abs_windows_form(tmp_path, monkeypatch):
    """Lock test: the -v mount is always the ABSOLUTE cwd form, even when
    run_in_docker is given a relative cwd (path contract, Windows-safe)."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    sb = _sandbox()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(tmp_path)

    sb.run_in_docker("echo hi", "work", 30)  # relative cwd on purpose

    argv = _docker_argv(logs["args"])
    assert f"-v {os.path.abspath(str(work))}:/workspace" in argv
    assert "-v work:/workspace" not in argv
