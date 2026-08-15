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
import fde.runlog

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
        f"echo({reply}\r\n"
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
# agent-in-container mode (FDE_AGENT_CONTAINER=1, Layer 5)
# --------------------------------------------------------------------------- #
# FDE_AGENT_CONTAINER=1 routes the agent CLI ITSELF into the fde-agent
# container via sandbox.run_agent_in_docker: `docker run ... <image>
# <bare binary> <flags> <prompt>` — no .cmd shim resolution, no host
# binary. The fake docker below answers the daemon preflight (`version`)
# without logging it (so the log's first line is the run under test),
# logs its argv, and prints a canned agent reply on `run`.

def _make_fake_agent_docker(tmp_path, monkeypatch, reply: str) -> dict:
    """Put a fake `docker` CLI on PATH for container-mode agent runs:
    answers --version + version (silent, unlogged), logs its argv to
    docker-args.txt, prints `reply` on `run`, answers kill/rm instantly."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_log = tmp_path / "docker-args.txt"
    _write_bin_shim(
        bin_dir, "docker",
        "@echo off\r\n"
        'if "%1"=="--version" (\r\n'
        "  echo docker 29.7.2-fake\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'if "%1"=="version" (\r\n'
        "  echo docker 29.7.2-fake\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        f'echo %* >> "{args_log}"\r\n'
        f"echo({reply}\r\n"
        "exit /b 0\r\n",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  echo docker 29.7.2-fake\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "version" ]; then\n'
        "  echo docker 29.7.2-fake\n"
        "  exit 0\n"
        "fi\n"
        f'echo "$*" >> "{args_log}"\n'
        f"echo '{reply}'\n"
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    return {"args": args_log}


def _docker_run_argv(log: Path) -> str:
    """Normalized (single-space-joined) argv of the first docker
    invocation — the run under test (preflight calls are unlogged)."""
    return " ".join(log.read_text(encoding="utf-8").split())


def test_container_mode_deepseek_routes_bare_dsh_through_docker(
        monkeypatch, tmp_path):
    """FDE_AGENT_CONTAINER=1 -> codex_exec routes `dsh --profile headless
    <prompt>` — the BARE binary name, no shim resolution, no .cmd anywhere
    — through the container; summary + result shape are unchanged."""
    logs = _make_fake_agent_docker(
        tmp_path, monkeypatch, "fake dsh reply: repro test written")
    monkeypatch.setenv("FDE_AGENT_CONTAINER", "1")
    monkeypatch.setattr(agents, "BACKEND", "deepseek")
    prompt = "write ONE failing test file"

    res = agents.codex_exec(prompt, cwd=str(tmp_path))

    # same contract as the host path: rc/out/timed_out/summary/duration_ms
    assert res["rc"] == 0
    assert res["timed_out"] is False
    assert res["summary"] == "fake dsh reply: repro test written"
    assert "duration_ms" in res
    argv = _docker_run_argv(logs["args"])
    assert argv.split()[0] == "run"
    assert "dsh --profile headless" in argv
    assert "write ONE failing test file" in argv
    assert ".cmd" not in argv  # bare names resolve inside the image


def test_container_mode_claude_routes_bare_claude_through_docker(
        monkeypatch, tmp_path):
    """FDE_AGENT_CONTAINER=1 -> the claude backend routes the bare `claude`
    binary + headless flags through the container; summary parsed from the
    JSON reply exactly like the host path."""
    logs = _make_fake_agent_docker(tmp_path, monkeypatch, FAKE_REPLY)
    monkeypatch.setenv("FDE_AGENT_CONTAINER", "1")
    monkeypatch.setattr(agents, "BACKEND", "claude")

    res = agents.codex_exec("write ONE failing test file", cwd=str(tmp_path))

    assert res["rc"] == 0
    assert res["summary"] == "fake claude reply: repro test written"
    argv = _docker_run_argv(logs["args"])
    assert ("claude -p --output-format json --dangerously-skip-permissions"
            in argv)


def test_container_mode_codex_routes_bare_codex_through_docker(
        monkeypatch, tmp_path):
    """FDE_AGENT_CONTAINER=1 -> the codex path routes `codex exec --json -s
    danger-full-access <prompt>` through the container — no -C cwd (the
    container workdir is /workspace), no host Popen."""
    logs = _make_fake_agent_docker(
        tmp_path, monkeypatch,
        '{"type":"chat_message","payload":{"message":'
        '{"content":"fake codex reply"}}}')
    monkeypatch.setenv("FDE_AGENT_CONTAINER", "1")
    monkeypatch.setattr(agents, "BACKEND", "codex")

    res = agents.codex_exec("prompt", cwd=str(tmp_path))

    assert res["rc"] == 0
    assert res["summary"] == "fake codex reply"
    argv = _docker_run_argv(logs["args"])
    assert "codex exec --json -s danger-full-access prompt" in argv
    assert "-C" not in argv.split()


def test_container_mode_version_probe_through_docker(monkeypatch, tmp_path):
    """codex_version() in container mode runs [binary, --version] inside
    the container and returns the first non-empty line."""
    logs = _make_fake_agent_docker(tmp_path, monkeypatch, "dsh 0.1.0-fake")
    monkeypatch.setenv("FDE_AGENT_CONTAINER", "1")
    monkeypatch.setattr(agents, "BACKEND", "deepseek")

    assert agents.codex_version() == "dsh 0.1.0-fake"

    argv = _docker_run_argv(logs["args"])
    assert "dsh --version" in argv


def test_container_mode_version_unknown_when_no_output(
        monkeypatch, tmp_path):
    """Best-effort version probe: an empty container reply -> 'unknown',
    never a crash."""
    _make_fake_agent_docker(tmp_path, monkeypatch, "")
    monkeypatch.setenv("FDE_AGENT_CONTAINER", "1")
    monkeypatch.setattr(agents, "BACKEND", "deepseek")

    assert agents.codex_version() == "unknown"


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


def test_bootstrap_env_pulls_deepseek_key_from_env_file(tmp_path, monkeypatch):
    """FDE_AGENT_ENV_FILE bootstrap must cover DEEPSEEK_API_KEY (the env var
    the dsh backend's deepseek-official provider reads), not just codex's
    OPENCODE_GO_* keys."""
    env_file = tmp_path / "env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-ds-test\nOPENCODE_GO_API_KEY=sk-cx-test\n",
        encoding="utf-8")
    monkeypatch.setenv("FDE_AGENT_ENV_FILE", str(env_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    env: dict = {}
    agents._bootstrap_env(env)
    assert env["DEEPSEEK_API_KEY"] == "sk-ds-test"
    assert env["OPENCODE_GO_API_KEY"] == "sk-cx-test"


def test_bootstrap_env_does_not_override_existing_env(tmp_path, monkeypatch):
    """setdefault semantics: a key already in the environment wins over the
    env file (machine-level exports take precedence)."""
    env_file = tmp_path / "env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-file\n", encoding="utf-8")
    monkeypatch.setenv("FDE_AGENT_ENV_FILE", str(env_file))
    env = {"DEEPSEEK_API_KEY": "sk-exported"}
    agents._bootstrap_env(env)
    assert env["DEEPSEEK_API_KEY"] == "sk-exported"


def test_deepseek_prompt_survives_cmd_shim(tmp_path, monkeypatch):
    """Windows regression (observed 2026-08-15 on real dsh): the npm
    `dsh.CMD` shim re-parses argv through cmd.exe, which mangles multi-line
    prompts containing quotes — real repro prompts carry symptom strings
    like `"row 7 malformed"` — so the agent receives garbage and can never
    create the repro file (silent loop failure, 0 attempts). The backend
    must bypass the .cmd shim and invoke node + the JS entry directly.

    The fake reproduces npm's global-install layout: `dsh.cmd` shim →
    `node node_modules/@deepseek-ai/dsh/lib/bin.js`, which logs its argv
    verbatim. Assert the full prompt (quotes + newlines) arrives intact.
    On POSIX the extensionless sh script runs node directly — same
    guarantee, fallback path."""
    bin_dir = tmp_path / "bin"
    entry = bin_dir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    entry.parent.mkdir(parents=True)
    argv_log = tmp_path / "argv.json"
    entry.write_text(
        "const fs=require('fs');"
        f"fs.writeFileSync({str(argv_log)!r}, JSON.stringify(process.argv.slice(2)));"
        "console.log('{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"fake\","
        "\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}');",
        encoding="utf-8")
    (bin_dir / "dsh.cmd").write_text(
        '@ECHO off\r\nSET dp0=%~dp0\r\n'
        'IF EXIST "%dp0%\\node.exe" (SET "_prog=%dp0%\\node.exe") ELSE (SET "_prog=node")\r\n'
        'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & '
        '"%_prog%" "%dp0%\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js" %*\r\n',
        encoding="utf-8")
    sh = bin_dir / "dsh"
    sh.write_text(
        '#!/bin/sh\n'
        'exec node "$(dirname "$0")/node_modules/@deepseek-ai/dsh/lib/bin.js" "$@"\n',
        encoding="utf-8")
    sh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setattr(agents, "BACKEND", "deepseek")
    prompt = 'Symptom to reproduce: "row 7 malformed"\nwrite the failing test'
    res = agents.codex_exec(prompt, str(tmp_path))
    assert res["rc"] == 0
    logged = json.loads(argv_log.read_text(encoding="utf-8"))
    assert logged == ["--profile", "headless", prompt]


def test_repro_loop_adopts_worktree_file_from_confined_agent(tmp_path, monkeypatch):
    """Regression (2026-08-15, real dsh): workspace-confined agents
    (dsh/claude with workspace-write permission) cannot write one level
    above their cwd, so the repro test lands INSIDE the worktree while the
    loop checks the run dir — the file was never seen and every attempt was
    rejected ("harness rejected repro test after 3 attempts", zero
    test_result events, while the file sat in the worktree). The loop now
    adopts a same-named file found in the worktree; the run-level copy
    stays the source of truth (it survives worktree resets between fix
    rounds)."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(fde.runlog, "RUNS_DIR", runs_dir)
    run_id = "20260815-test-0001"
    wt = tmp_path / "worktree"
    wt.mkdir()
    ticket = {"system": "tier3_ingest", "symptom": "row 7 malformed",
              "expected": "errors list populated",
              "actual": "errors list empty"}
    manifest = {"app_type": "js", "test_cmd": "node --test",
                "install_cmd": None}

    def confined_agent(prompt, cwd):
        # the dsh harness writes inside its workspace root (cwd) — never
        # one level up in the run dir
        (Path(cwd) / "repro.test.js").write_text(
            "// confined agent wrote here", encoding="utf-8")
        return {"rc": 0, "out": "ok", "timed_out": False,
                "summary": "wrote the test", "duration_ms": 1}

    monkeypatch.setattr(agents, "codex_exec", confined_agent)
    monkeypatch.setattr(
        agents, "verify_repro",
        lambda *a, **k: {"pass": True,
                         "checks": {"a": {"ok": True, "rc": 0},
                                    "b": {"ok": True, "rc": 0},
                                    "c": {"ok": True, "rc": 0}},
                         "duration_ms": 1})
    result = agents.repro_loop(run_id, str(tmp_path / "repo"), str(wt),
                               manifest, ticket)
    assert result["ok"] is True
    assert result["attempts"] == 1
    adopted = runs_dir / run_id / "repro.test.js"
    assert adopted.read_text(encoding="utf-8") == "// confined agent wrote here"
    assert (wt / "repro.test.js").read_text(encoding="utf-8") \
        == "// confined agent wrote here"
