"""Pluggable agent-backend dispatch (FDE_AGENT_BACKEND=codex|mock|claude).

The claude backend is exercised end-to-end against a FAKE `claude` CLI — a
claude.cmd batch file placed on PATH (Windows can't resolve a bare `claude`
name to `claude.cmd`, which is exactly why the backend resolves the binary
via shutil.which) — so the tests are offline and deterministic. Unknown
backend values must raise a clear error instead of silently falling through
to codex: that silent fallthrough is the bug this module fixes.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fde.agents as agents

ROOT = Path(__file__).resolve().parent.parent

FAKE_REPLY = json.dumps({
    "type": "result", "subtype": "success",
    "result": "fake claude reply: repro test written",
    "usage": {"input_tokens": 5, "output_tokens": 3},
})


def _write_bin_shim(bin_dir: Path, name: str, body_cmd: str, body_sh: str) -> None:
    """Write a fake CLI: `<name>.cmd` on Windows, an executable POSIX script
    elsewhere (CI runners are Linux; a bare name does not resolve to
    `<name>.cmd` there — the backend's shutil.which lookup is exactly what
    these tests exercise)."""
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(body_cmd, encoding="utf-8")
    else:
        script = bin_dir / name
        script.write_text(body_sh, encoding="utf-8")
        script.chmod(0o755)


def _make_fake_bin(tmp_path, monkeypatch, name: str, version: str,
                   reply: str) -> dict:
    """Put a fake `<name>` CLI on PATH: answers `--version`, logs argv and
    cwd to files, prints `reply` — the headless one-shot shape every
    backend in this module expects."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_log = tmp_path / "args.txt"
    cwd_log = tmp_path / "cwd.txt"
    _write_bin_shim(
        bin_dir, name,
        "@echo off\r\n"
        f'if "%1"=="--version" (\r\n'
        f"  echo {version}\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        f'echo %* > "{args_log}"\r\n'
        f'echo %CD% > "{cwd_log}"\r\n'
        f"echo {reply}\r\n"
        "exit /b 0\r\n",
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then\n'
        f'  echo "{version}"\n'
        "  exit 0\n"
        "fi\n"
        f'echo "$*" > "{args_log}"\n'
        f'echo "$PWD" > "{cwd_log}"\n'
        f"echo '{reply}'\n"
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    return {"args": args_log, "cwd": cwd_log}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put a fake `claude` CLI on PATH (see _make_fake_bin)."""
    return _make_fake_bin(tmp_path, monkeypatch, "claude",
                          "claude-cli 2.0.0-fake", FAKE_REPLY)


@pytest.fixture
def fake_dsh(tmp_path, monkeypatch):
    """Put a fake `dsh` (DeepSeek Harness) CLI on PATH."""
    return _make_fake_bin(tmp_path, monkeypatch, "dsh",
                          "dsh 0.1.0-fake",
                          "fake dsh reply: repro test written")


# --------------------------------------------------------------------------- #
# claude backend
# --------------------------------------------------------------------------- #

def test_claude_backend_invokes_cli_with_headless_flags_and_parses_output(
        fake_claude, monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "BACKEND", "claude")
    prompt = "write ONE failing test file"
    res = agents.codex_exec(prompt, cwd=str(tmp_path))

    # parsed result, same contract as codex_exec
    assert res["rc"] == 0
    assert res["timed_out"] is False
    assert res["summary"] == "fake claude reply: repro test written"

    # invoked the resolved `claude` binary with the headless flag set
    tokens = fake_claude["args"].read_text(encoding="utf-8").split()
    assert tokens[0] == "-p"
    assert "--output-format" in tokens
    assert "json" in tokens
    assert "--dangerously-skip-permissions" in tokens
    assert prompt in fake_claude["args"].read_text(encoding="utf-8")

    # ran in the worktree (analogue of codex's -C cwd)
    assert fake_claude["cwd"].read_text(encoding="utf-8").strip() \
        == str(tmp_path)


def test_claude_backend_version_reports_cli_version(fake_claude, monkeypatch):
    monkeypatch.setattr(agents, "BACKEND", "claude")
    assert agents.codex_version() == "claude-cli 2.0.0-fake"


def test_claude_backend_401_raises_agent_auth_error(tmp_path, monkeypatch):
    """Provider 401 must surface as 'agent auth failed', never a harness
    rejection — same contract as the codex path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_bin_shim(
        bin_dir, "claude",
        "@echo off\r\n"
        "echo Error: authentication failed: 401 invalid api key\r\n"
        "exit /b 1\r\n",
        "#!/bin/sh\n"
        'echo "Error: authentication failed: 401 invalid api key"\n'
        "exit 1\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setattr(agents, "BACKEND", "claude")
    with pytest.raises(agents.AgentAuthError, match="agent auth failed"):
        agents.codex_exec("prompt", cwd=str(tmp_path))


def test_claude_backend_missing_binary_raises_clear_error(
        tmp_path, monkeypatch):
    """A missing claude CLI is a clear config error at call time, not an
    import crash and not a silent fallthrough."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no claude anywhere
    monkeypatch.setattr(agents, "BACKEND", "claude")
    with pytest.raises(RuntimeError, match=r"`claude` CLI was not found"):
        agents.codex_exec("prompt", cwd=str(tmp_path))


# --------------------------------------------------------------------------- #
# deepseek backend (dsh)
# --------------------------------------------------------------------------- #

def test_deepseek_backend_invokes_headless_profile_and_uses_answer(
        fake_dsh, monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "BACKEND", "deepseek")
    prompt = "write ONE failing test file"
    res = agents.codex_exec(prompt, cwd=str(tmp_path))

    # same rc/out/summary contract as the other backends
    assert res["rc"] == 0
    assert res["timed_out"] is False
    assert res["summary"] == "fake dsh reply: repro test written"

    # invoked `dsh --profile headless "<prompt>"` in the worktree
    tokens = fake_dsh["args"].read_text(encoding="utf-8").split()
    assert tokens[0] == "--profile"
    assert tokens[1] == "headless"
    assert prompt in fake_dsh["args"].read_text(encoding="utf-8")
    assert fake_dsh["cwd"].read_text(encoding="utf-8").strip() == str(tmp_path)


def test_deepseek_backend_version_reports_cli_version(fake_dsh, monkeypatch):
    monkeypatch.setattr(agents, "BACKEND", "deepseek")
    assert agents.codex_version() == "dsh 0.1.0-fake"


def test_deepseek_backend_401_raises_agent_auth_error(tmp_path, monkeypatch):
    """Provider 401 must surface as 'agent auth failed', never a harness
    rejection — same contract as the codex and claude paths."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_bin_shim(
        bin_dir, "dsh",
        "@echo off\r\n"
        "echo Error: authentication failed: 401 invalid api key\r\n"
        "exit /b 1\r\n",
        "#!/bin/sh\n"
        'echo "Error: authentication failed: 401 invalid api key"\n'
        "exit 1\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setattr(agents, "BACKEND", "deepseek")
    with pytest.raises(agents.AgentAuthError, match="agent auth failed"):
        agents.codex_exec("prompt", cwd=str(tmp_path))


def test_deepseek_backend_missing_binary_raises_clear_error(
        tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no dsh anywhere
    monkeypatch.setattr(agents, "BACKEND", "deepseek")
    with pytest.raises(RuntimeError, match=r"`dsh` CLI was not found"):
        agents.codex_exec("prompt", cwd=str(tmp_path))


# --------------------------------------------------------------------------- #
# unknown backend values
# --------------------------------------------------------------------------- #

def test_unknown_backend_raises_clear_error(monkeypatch):
    monkeypatch.setattr(agents, "BACKEND", "openai")
    with pytest.raises(ValueError, match=r"codex\|mock\|claude\|deepseek"):
        agents.codex_exec("prompt", cwd=".")
    with pytest.raises(ValueError, match="unknown FDE_AGENT_BACKEND"):
        agents.codex_version()


# --------------------------------------------------------------------------- #
# mock backend unchanged
# --------------------------------------------------------------------------- #

def test_mock_backend_dispatch_routes_to_mock_exec(monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "BACKEND", "mock")
    seen = {}

    def fake_mock(prompt, cwd, timeout):
        seen["prompt"] = prompt
        return {"rc": 0, "out": "[mock]", "timed_out": False,
                "summary": "mock backend: deterministic offline step",
                "duration_ms": 1}

    monkeypatch.setattr(agents, "_mock_exec", fake_mock)
    res = agents.codex_exec("write ONE failing test file", cwd=str(tmp_path))
    assert res["summary"] == "mock backend: deterministic offline step"
    assert seen["prompt"] == "write ONE failing test file"
    # mock never touches a real binary
    assert agents.codex_version() == "mock"


# --------------------------------------------------------------------------- #
# codex default unchanged
# --------------------------------------------------------------------------- #

def test_codex_default_dispatch_unchanged(monkeypatch, tmp_path):
    """BACKEND == \"codex\" (the default) must build exactly the historic
    codex argv — no claude/mock involvement, no env sniffing."""
    monkeypatch.setattr(agents, "BACKEND", "codex")
    captured = {}

    class FakeProc:
        def __init__(self, flags, **kwargs):
            captured["flags"] = flags
            self.returncode = 0
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(agents.subprocess, "Popen", FakeProc)
    res = agents.codex_exec("prompt", cwd=str(tmp_path))
    assert res["rc"] == 0
    assert captured["flags"] == [
        "codex", "exec", "--json", "-s", "danger-full-access",
        "-C", str(tmp_path), "prompt"]


def test_default_backend_is_codex_when_env_unset():
    """FDE_AGENT_BACKEND unset → import-time default is codex (fresh
    interpreter so the module-level env read is exercised for real)."""
    env = os.environ.copy()
    env.pop("FDE_AGENT_BACKEND", None)
    r = subprocess.run(
        [sys.executable, "-c", "import fde.agents; print(fde.agents.BACKEND)"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "codex"
