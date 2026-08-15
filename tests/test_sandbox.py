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


def _fake_worktree(tmp_path) -> tuple[Path, Path]:
    """A fixture repo (with .git) + a worktree whose .git is a FILE pointing
    at the fixture's gitdir — exactly what `git worktree add` produces."""
    fixture = tmp_path / "fixture"
    (fixture / ".git" / "worktrees").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(
        f"gitdir: {fixture}/.git/worktrees/wt1\n", encoding="utf-8")
    return fixture, wt


def test_sandbox_mounts_fixture_gitdir(tmp_path, monkeypatch):
    """Worktree .git FILE -> the fixture's .git dir is bind-mounted at
    /fde/gitdir and git is steered there via GIT_DIR/GIT_WORK_TREE -e flags
    (in-container git works; the .git file is NEVER rewritten, nothing is
    mounted under /workspace, host git stays untouched)."""
    fixture, wt = _fake_worktree(tmp_path)
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    sb = _sandbox()

    sb.run_in_docker("echo hi", str(wt), 30)

    argv = _docker_argv(logs["args"])
    # the fixture's .git dir mounted at the fixed /fde/gitdir path, POSIX-form
    # (the mount arg is space-quoted by subprocess, so the shim logs it whole)
    assert "--mount" in argv
    assert (f"type=bind,source={(fixture / '.git').as_posix()},"
            "target=/fde/gitdir") in argv
    # git steering via explicit -e flags. The .cmd fake strips everything
    # after '=' in UNQUOTED args (cmd.exe quirk), so assert the keys here;
    # the full GIT_DIR/GIT_WORK_TREE values are covered by
    # test_sandbox_git_env_steers_in_container_git and the real-docker
    # integration tests.
    assert "-e GIT_DIR" in argv
    assert "-e GIT_WORK_TREE" in argv
    # the .git file is untouched
    assert (wt / ".git").read_text(encoding="utf-8")         .startswith(f"gitdir: {fixture}/")
    # idempotent: second call derives the same mount, no churn
    sb.run_in_docker("echo hi", str(wt), 30)
    assert (wt / ".git").read_text(encoding="utf-8")         .startswith(f"gitdir: {fixture}/")


def test_sandbox_git_env_steers_in_container_git(tmp_path, monkeypatch):
    """GIT_DIR points at /fde/gitdir/worktrees/<name>, GIT_WORK_TREE at
    /workspace — the container's git uses the mounted gitdir instead of the
    unresolvable host path in the worktree's .git file."""
    fixture, wt = _fake_worktree(tmp_path)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    sb = _sandbox()

    mounts, env = sb._sandbox_git_env(str(wt))

    assert env["GIT_DIR"] == "/fde/gitdir/worktrees/wt1"
    assert env["GIT_WORK_TREE"] == "/workspace"
    assert mounts and "target=/fde/gitdir" in mounts[1]
    # plain dir (no .git file) -> no mounts, no env steering
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sb._sandbox_git_env(str(plain)) == ([], {})


def test_sandbox_posix_gitdir_same_fixed_mount():
    """POSIX gitdir (CI) — same fixed /fde/gitdir mount + GIT_DIR steering
    (no path-specific container paths needed)."""
    import fde.sandbox as sb
    mt = sb._mount_target(
        "/home/runner/work/ticket-to-fix/fixtures/tier6_escape/.git/worktrees/wt1")
    assert mt is not None
    git_dir, mount, name = mt
    assert git_dir.as_posix() == "/home/runner/work/ticket-to-fix/fixtures/tier6_escape/.git"
    assert mount == "/fde/gitdir"
    assert name == "wt1"


def test_sandbox_plain_dir_single_mount(tmp_path, monkeypatch):
    """No .git file -> no extra mount, no git env (plain-dir behavior)."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    sb = _sandbox()
    cwd = tmp_path / "work"
    cwd.mkdir()

    sb.run_in_docker("echo hi", str(cwd), 30)

    argv = _docker_argv(logs["args"])
    assert "--mount" not in argv
    assert argv.count("-v") == 1


# --- Layer 1: FDE_SANDBOX=required (mandatory) + =host (explicit opt-in) ---

def test_sandbox_active_when_required(monkeypatch):
    """FDE_SANDBOX=required -> sandbox_active() is True (docker mandatory)."""
    monkeypatch.setenv("FDE_SANDBOX", "required")
    sb = _sandbox()
    assert sb.sandbox_active() is True


def test_sandbox_active_when_host(monkeypatch):
    """FDE_SANDBOX=host -> sandbox_active() is False (explicit host opt-in)."""
    monkeypatch.setenv("FDE_SANDBOX", "host")
    sb = _sandbox()
    assert sb.sandbox_active() is False


def test_required_mode_daemon_down_fails_fast(tmp_path, monkeypatch):
    """required: daemon unreachable -> RuntimeError NAMING FDE_SANDBOX=required
    explicitly — never a silent fallback to host (the mode's whole point)."""
    _make_fake_docker(tmp_path, monkeypatch, daemon_down=True)
    monkeypatch.setenv("FDE_SANDBOX", "required")
    sb = _sandbox()

    with pytest.raises(RuntimeError) as excinfo:
        sb.run_in_docker("echo hi", str(tmp_path), 30)

    msg = str(excinfo.value)
    assert "FDE_SANDBOX=required" in msg
    assert "daemon" in msg


def test_required_mode_missing_docker_binary_fails_fast(monkeypatch, tmp_path):
    """required: docker CLI not on PATH -> RuntimeError naming
    FDE_SANDBOX=required (fail fast, no fallback)."""
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("FDE_SANDBOX", "required")
    sb = _sandbox()

    with pytest.raises(RuntimeError) as excinfo:
        sb.run_in_docker("echo hi", str(tmp_path), 30)

    assert "FDE_SANDBOX=required" in str(excinfo.value)


def test_host_mode_warns_once_to_stderr(monkeypatch, capsys):
    """FDE_SANDBOX=host -> loud stderr warning on FIRST use only (module-level
    flag: once per process, never per call)."""
    monkeypatch.setenv("FDE_SANDBOX", "host")
    sb = _sandbox()

    sb.sandbox_active()
    sb.sandbox_active()  # second use must not re-warn

    err = capsys.readouterr().err
    assert err.count("FDE_SANDBOX=host") == 1
    assert "not isolated" in err
    assert "FDE_SANDBOX=required" in err


def test_host_mode_warns_from_run_cmd(monkeypatch, tmp_path, capsys):
    """run_cmd in host mode surfaces the warning through the routing path
    (the harness never sees the mode — sandbox_active owns it)."""
    monkeypatch.setenv("FDE_SANDBOX", "host")
    _sandbox()
    harness = _harness()
    cwd = tmp_path / "work"
    cwd.mkdir()

    res = harness.run_cmd("echo hi", str(cwd), 30)

    assert res["rc"] == 0
    assert "FDE_SANDBOX=host" in capsys.readouterr().err


def test_required_mode_routes_run_cmd_like_docker(tmp_path, monkeypatch):
    """required routes run_cmd through docker with an argv IDENTICAL to docker
    mode (same predicate, same container flags) — only failure messages differ.
    The container name is a fresh uuid per run, so it is normalized out."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    _sandbox()
    harness = _harness()
    cwd = tmp_path / "work"
    cwd.mkdir()

    monkeypatch.setenv("FDE_SANDBOX", "docker")
    r1 = harness.run_cmd("echo hi", str(cwd), 30)
    assert r1["rc"] == 0 and FAKE_DOCKER_RAN in r1["out"]

    monkeypatch.setenv("FDE_SANDBOX", "required")
    r2 = harness.run_cmd("echo hi", str(cwd), 30)
    assert r2["rc"] == 0 and FAKE_DOCKER_RAN in r2["out"]

    lines = logs["args"].read_text(encoding="utf-8").splitlines()
    # log layout: [run1, rm1-teardown, run2, rm2-teardown] — compare the two
    # run invocations (indices 0 and 2), normalizing the fresh uuid name
    docker_argv = re.sub(r"fde-sandbox-[0-9a-f]{8}",
                         "fde-sandbox-<name>", lines[0])
    required_argv = re.sub(r"fde-sandbox-[0-9a-f]{8}",
                           "fde-sandbox-<name>", lines[2])
    assert docker_argv == required_argv


# --- Layer 2: container hardening flags ------------------------------------

def test_hardening_flags_defaults_in_argv(tmp_path, monkeypatch):
    """run_in_docker appends --memory/--cpus/--pids-limit/--read-only/--tmpfs
    with defaults, ordered AFTER --security-opt and BEFORE the image."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    sb = _sandbox()
    cwd = tmp_path / "work"
    cwd.mkdir()

    sb.run_in_docker("echo hi", str(cwd), 30)

    argv = _docker_argv(logs["args"])
    for m in ("--memory 1g", "--cpus 2", "--pids-limit 256",
              "--read-only", "--tmpfs /tmp"):
        assert m in argv
    markers = ["--security-opt no-new-privileges", "--memory 1g",
               "--cpus 2", "--pids-limit 256", "--read-only",
               "--tmpfs /tmp", "testimg", "bash"]
    positions = [argv.index(m) for m in markers]
    assert positions == sorted(positions)


def test_hardening_flags_env_overrides(tmp_path, monkeypatch):
    """FDE_SANDBOX_MEMORY / FDE_SANDBOX_CPUS / FDE_SANDBOX_PIDS override the
    defaults in the argv; --read-only and --tmpfs /tmp stay fixed."""
    logs = _make_fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("FDE_SANDBOX", "docker")
    monkeypatch.setenv("FDE_SANDBOX_IMAGE", "testimg")
    monkeypatch.setenv("FDE_SANDBOX_MEMORY", "512m")
    monkeypatch.setenv("FDE_SANDBOX_CPUS", "1.5")
    monkeypatch.setenv("FDE_SANDBOX_PIDS", "128")
    sb = _sandbox()
    cwd = tmp_path / "work"
    cwd.mkdir()

    sb.run_in_docker("echo hi", str(cwd), 30)

    argv = _docker_argv(logs["args"])
    assert "--memory 512m" in argv
    assert "--cpus 1.5" in argv
    assert "--pids-limit 128" in argv
    assert "--read-only" in argv
    assert "--tmpfs /tmp" in argv


# --- Real-docker integration tests (skip when no daemon) -----------------

def _docker_available() -> bool:
    """True when the docker daemon answers AND the fde-sandbox:latest image
    exists (the integration tests exercise the real image; the sandbox CI
    job builds it, so on a fresh runner they skip instead of failing with
    docker rc 125 'repository does not exist')."""
    try:
        import subprocess
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10)
        if not (r.returncode == 0 and bool(r.stdout.strip())):
            return False
        r = subprocess.run(
            ["docker", "image", "inspect", "fde-sandbox:latest"],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(),
                    reason="docker daemon not reachable")
def test_real_container_echo():
    """The sandbox container actually runs commands (needs fde-sandbox:latest
    built — see README 'Docker sandbox')."""
    from fde.sandbox import run_in_docker
    r = run_in_docker("echo container-ok", ".", 60)
    assert r["rc"] == 0
    assert "container-ok" in r["out"]


@pytest.mark.skipif(not _docker_available(),
                    reason="docker daemon not reachable")
def test_real_container_local_write_ok():
    """Inside the container /tmp is writable — the containment is about the
    HOST, not the container's own filesystem."""
    from fde.sandbox import run_in_docker
    r = run_in_docker("touch /tmp/fde-probe && echo wrote", ".", 60)
    assert r["rc"] == 0
    assert "wrote" in r["out"]


@pytest.mark.skipif(not _docker_available(),
                    reason="docker daemon not reachable")
def test_real_container_has_no_network():
    """--network none: a TCP connect from inside the container must fail."""
    from fde.sandbox import run_in_docker
    code = ("require('net').connect(53,'1.1.1.1')"
            ".on('error',()=>process.exit(0))"
            ".on('connect',()=>process.exit(1))")
    r = run_in_docker(f"node -e \"{code}\"", ".", 60)
    assert r["rc"] == 0  # connect failed -> exited 0 via the error handler
