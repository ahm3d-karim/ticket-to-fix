"""Agent orchestration: pluggable agent backends + repro/fix loops.

Backends (FDE_AGENT_BACKEND): ``codex`` (default, non-interactive Codex
CLI), ``claude`` (non-interactive Claude Code CLI), ``deepseek`` (DeepSeek
Harness ``dsh --profile headless``), ``mock`` (deterministic
offline stand-in). Every agent step funnels through ``codex_exec``, which
dispatches on the configured backend; unknown values raise instead of
silently falling through to codex.

Contracts:
- codex exec runs in the run's worktree only; paths in prompts are MSYS-safe
  (forward slashes, C:/ form).
- The repro loop writes ONE failing test file; the 3-state harness verdict
  (harness.verify_repro) decides acceptance — never the agent's word.
- The fix loop iterates max FIX_ROUNDS times; each round reads the worktree
  diff, runs the repro test then the full suite, and resets the worktree
  before the next attempt. Every attempt is audit-logged.
"""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import load_repo_manifest
from .harness import run_cmd, verify_repro
from .runlog import append, run_dir
from .sandbox import gold_path_in_sandbox
from .ticket import parse_ticket

BACKEND = os.environ.get("FDE_AGENT_BACKEND", "codex")
VALID_BACKENDS = ("codex", "mock", "claude", "deepseek")
REPRO_ATTEMPTS = 3
FIX_ROUNDS = 8
ROUND_TIMEOUT = 900
REPRO_FILES = {"js": "repro.test.js", "py": "repro_test.py"}


def _dispatch_backend() -> str:
    """Resolve the configured agent backend; reject unknown values.

    The one dispatch point every agent step funnels through: the backend
    name (env var or module default) maps 1:1 to an invoker. Anything else
    raises a clear error instead of silently falling through to codex — the
    historical bug where `FDE_AGENT_BACKEND=openai` quietly ran codex.
    """
    backend = BACKEND
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"unknown FDE_AGENT_BACKEND {backend!r} — expected one of "
            f"{'|'.join(VALID_BACKENDS)} (codex is the default)")
    return backend


class AgentAuthError(Exception):
    """The model provider rejected the API key (401). Raised so the failure
    surfaces as 'agent auth failed', never as a harness rejection."""


def codex_version() -> str:
    """Best-effort version string for the active backend ("unknown" on any
    failure — never a crash; the mock never touches a real binary)."""
    backend = _dispatch_backend()
    if backend == "mock":
        return "mock"
    binary = {"claude": _claude_binary(), "deepseek": _deepseek_binary()}.get(
        backend, "codex")
    try:
        r = subprocess.run([binary, "--version"], capture_output=True,
                           text=True, timeout=15)
        return (r.stdout or r.stderr).strip().splitlines()[0] or "unknown"
    except Exception:
        return "unknown"


def ensure_git_repo(repo: Path) -> None:
    """Fixture repos are git repos by design, but their .git dirs are
    gitignored, so a fresh checkout (CI) has none. Initialize with a baseline
    commit on `main` when missing — the harness adds detached worktrees of
    `main`, which requires a repo with a commit. No-op when present."""
    if (repo / ".git").exists():
        return
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fde",
                    "-c", "user.email=fde@local", "commit", "-m", "baseline"],
                   check=True, capture_output=True, text=True)


def resolve_repo(system: str) -> str:
    """Map a ticket's `system` field to a repo dir carrying fde.yaml."""
    for cand in (Path("fixtures") / system, Path(system)):
        if (cand / "fde.yaml").exists():
            ensure_git_repo(cand)
            return str(cand.resolve())
    raise FileNotFoundError(
        f"no repo with fde.yaml found for system '{system}' "
        f"(looked in fixtures/{system} and {system})")


def ensure_git_identity(repo: str) -> None:
    """Fixture repos are fresh git inits — give them a local identity if unset."""
    for key, val in (("user.name", "fde"), ("user.email", "fde@local")):
        r = subprocess.run(["git", "-C", repo, "config", "--get", key],
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "-C", repo, "config", key, val], check=True)


def _bootstrap_env(env: dict) -> None:
    """Machine-local bootstrap: pull the agent API key from the Hermes .env.

    Lets `fde` run without the user exporting keys by hand on this machine.
    Override the file with the FDE_AGENT_ENV_FILE env var.
    """
    path = os.environ.get("FDE_AGENT_ENV_FILE",
                          str(Path.home() / "AppData" / "Local" / "hermes" / ".env"))
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k in ("OPENCODE_GO_API_KEY", "OPENCODE_GO_BASE_URL",
                         "DEEPSEEK_API_KEY"):
                    env.setdefault(k, v)
    except OSError:
        pass


def _prompt_path(p: Path) -> str:
    return str(p.resolve()).replace("\\", "/")


def _last_message(out: str) -> str:
    """Extract the last assistant text from `codex exec --json` JSONL output.

    Handles two observed shapes: top-level chat/agent_message events with
    payload.message.content, and item.completed events whose item is an
    agent_message with item.text (codex 0.147's actual format).
    """
    last = ""
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        text = None
        t = obj.get("type", "")
        if t == "item.completed" and isinstance(obj.get("item"), dict):
            item = obj["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
        elif t in ("chat_message", "agent_message"):
            payload = obj.get("payload")
            if isinstance(payload, dict):
                msg = (payload.get("message")
                       if isinstance(payload.get("message"), dict) else payload)
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
                elif isinstance(content, str):
                    text = content
        if text and text.strip():
            last = text.strip()
    return last


def codex_exec(prompt: str, cwd: str, timeout: int = ROUND_TIMEOUT) -> dict:
    """Run the configured agent backend non-interactively.

    Returns rc/out/timed_out/summary/duration_ms. Dispatches on
    ``_dispatch_backend()``: codex (default), claude, deepseek, or mock —
    anything else raises a clear error before any binary is touched.

    Sandbox note: codex 0.147's Windows sandbox (read-only / workspace-write)
    blocks its own default shell (powershell) — a known upstream issue. We run
    exec with `danger-full-access`; containment for the pipeline comes from the
    design, not the sandbox: cwd is the run's git worktree, the worktree is
    reset between fix rounds, and the 3-state harness gates every result.

    Watchdog note: `subprocess.run(timeout=)` failed to fire twice on this
    machine (codex's node wrapper outliving its child, process frozen for
    hours). This implementation polls the process itself and tree-kills
    (`taskkill /F /T`) on expiry — the timeout is guaranteed to fire.
    """
    t0 = time.monotonic()
    backend = _dispatch_backend()
    if backend == "mock":
        # deterministic offline stand-in: no codex, no network, no key
        return _mock_exec(prompt, cwd, timeout)
    if backend == "claude":
        return _claude_exec(prompt, cwd, timeout)
    if backend == "deepseek":
        return _deepseek_exec(prompt, cwd, timeout)
    flags = ["codex", "exec", "--json", "-s", "danger-full-access", "-C", cwd, prompt]
    env = os.environ.copy()
    if "OPENCODE_GO_API_KEY" not in env:
        _bootstrap_env(env)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(flags, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            env=env, creationflags=creationflags)
    chunks: list[str] = []

    def _reader():
        try:
            for line in proc.stdout:
                chunks.append(line)
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if time.monotonic() > deadline:
            timed_out = True
            _kill_proc_tree(proc.pid)
            break
        time.sleep(1)
    if proc.poll() is None:
        proc.wait(timeout=30)
    reader.join(timeout=5)
    out = "".join(chunks)
    if proc.returncode != 0 and ("401" in out or "Unauthorized" in out
                                 or "Missing bearer" in out):
        raise AgentAuthError(
            "agent auth failed (401 from the model provider) — set the API "
            "key in the environment or FDE_AGENT_ENV_FILE (see README "
            "'Bring your own key')")
    return {"rc": proc.returncode, "out": out, "timed_out": timed_out,
            "summary": _last_message(out),
            "duration_ms": int((time.monotonic() - t0) * 1000)}


def _kill_proc_tree(pid: int) -> None:
    """Kill the process tree (taskkill /T catches the node wrapper's children)."""
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# claude backend (FDE_AGENT_BACKEND=claude)
# --------------------------------------------------------------------------- #
# Non-interactive Claude Code CLI, same contract as codex_exec:
# {"rc", "out", "timed_out", "summary", "duration_ms"} + AgentAuthError on
# provider 401s (never a harness-shaped rejection).
#
# Invocation: `claude -p --output-format json --dangerously-skip-permissions`
# — the documented headless form (`-p` print mode + `--output-format json`
# machine-readable result). `claude --help` could not be consulted on the
# dev machine (the CLI is not installed there), so this is the documented
# headless invocation; re-verify the flag set against `claude --help` on a
# machine that has it. `--dangerously-skip-permissions` is the analogue of
# codex's `-s danger-full-access`: containment comes from the pipeline
# design (isolated worktree, resets between rounds, 3-state harness), not
# the CLI's permission prompts — headless mode cannot answer them.

AUTH_FAILURES = ("401", "Unauthorized", "authentication failed",
                 "invalid api key", "not authenticated")


def _claude_binary() -> str:
    """Resolve the `claude` CLI. shutil.which honors PATHEXT, so this finds
    the npm-global `claude.cmd` shim on Windows (a bare `claude` name does
    NOT resolve to `claude.cmd` via CreateProcess) as well as a native
    `claude`/`claude.exe`. Falls back to the bare name so a missing CLI
    surfaces as the FileNotFoundError we translate into a clear error."""
    return shutil.which("claude") or "claude"


def _claude_last_message(out: str) -> str:
    """Extract the final reply from `claude -p --output-format json`.

    Prefers the trailing JSON result object ({"result": ...}); falls back
    to the codex JSONL extractor, then to the last non-empty line.
    """
    last = ""
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("result"), str) \
                and obj["result"].strip():
            last = obj["result"].strip()
    if last:
        return last
    return _last_message(out) or next(
        (l.strip() for l in reversed(out.splitlines()) if l.strip()), "")


def _run_cli(argv: list[str], cwd: str, timeout: int, missing_msg: str,
             summarize) -> dict:
    """Shared Popen watchdog for the headless CLI backends (claude, dsh).

    Poll + tree-kill on expiry — `subprocess.run(timeout=)` is not reliable
    with node-wrapper CLIs. Provider 401s surface as AgentAuthError, never a
    harness rejection. `summarize(out)` extracts the final reply.
    """
    t0 = time.monotonic()
    env = os.environ.copy()
    if "OPENCODE_GO_API_KEY" not in env:
        _bootstrap_env(env)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                env=env, cwd=cwd, creationflags=creationflags)
    except FileNotFoundError:
        raise RuntimeError(missing_msg) from None
    chunks: list[str] = []

    def _reader():
        try:
            for line in proc.stdout:
                chunks.append(line)
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if time.monotonic() > deadline:
            timed_out = True
            _kill_proc_tree(proc.pid)
            break
        time.sleep(1)
    if proc.poll() is None:
        proc.wait(timeout=30)
    reader.join(timeout=5)
    out = "".join(chunks)
    if proc.returncode != 0 and any(s in out for s in AUTH_FAILURES):
        raise AgentAuthError(
            "agent auth failed (401 from the model provider) — set the API "
            "key in the environment or FDE_AGENT_ENV_FILE (see README "
            "'Bring your own key')")
    return {"rc": proc.returncode, "out": out, "timed_out": timed_out,
            "summary": summarize(out),
            "duration_ms": int((time.monotonic() - t0) * 1000)}


def _claude_exec(prompt: str, cwd: str, timeout: int = ROUND_TIMEOUT) -> dict:
    """Run the Claude Code CLI headless in the run's worktree (cwd)."""
    argv = [_claude_binary(), "-p", "--output-format", "json",
            "--dangerously-skip-permissions", prompt]
    return _run_cli(
        argv, cwd, timeout,
        "FDE_AGENT_BACKEND=claude but the `claude` CLI was not found on "
        "PATH — install it (npm install -g @anthropic-ai/claude-code) "
        "or set FDE_AGENT_BACKEND=codex|mock",
        _claude_last_message)


def _deepseek_binary() -> str:
    """Resolve the `dsh` CLI (same PATHEXT .cmd logic as claude)."""
    return shutil.which("dsh") or "dsh"


def _deepseek_exec(prompt: str, cwd: str, timeout: int = ROUND_TIMEOUT) -> dict:
    """Run DeepSeek Harness headless in the run's worktree (cwd).

    `dsh --profile headless "<job>"` runs one fresh persisted session,
    prints the final answer, and exits (apps/cli docs). The invoking
    directory is the default workspace root, so cwd=worktree is the analogue
    of codex's `-C cwd`. Developer preview upstream: pin the npm version.
    """
    argv = [_deepseek_binary(), "--profile", "headless", prompt]
    return _run_cli(
        argv, cwd, timeout,
        "FDE_AGENT_BACKEND=deepseek but the `dsh` CLI was not found on "
        "PATH — install it (npm install -g @deepseek-ai/dsh) "
        "or set FDE_AGENT_BACKEND=codex|mock",
        lambda out: next((l.strip() for l in reversed(out.splitlines())
                          if l.strip()), ""))


# --------------------------------------------------------------------------- #
# deterministic mock backend (FDE_AGENT_BACKEND=mock)
# --------------------------------------------------------------------------- #
# The mock makes every agent step deterministic and offline: the repro step
# writes ONE test file that runs the fixture's OWN suite (scoped to its test
# dir so the inner run never re-discovers the repro file itself) and fails
# with the ticket symptom as its message while the bug reproduces — the
# 3-state harness then verifies it exactly like a codex-written test. The fix
# step applies the fixture's gold.patch (the perfect fix). No codex binary is
# ever invoked and no network or key is touched.
#
# Stage detection uses the prompt the loops already build (see _repro_prompt /
# _fix_prompt); everything else (ticket, repo, manifest, repro path) is read
# from the run's own artifacts under runs/<run_id>/, resolved from the
# worktree path the CLI always creates.

def _suite_target(wt: Path) -> str:
    """Directory holding the fixture's own node tests (js repro template)."""
    for name in ("test", "tests"):
        if (wt / name).is_dir() and any((wt / name).glob("*.test.js")):
            return name
    return "test"


def _mock_repro_content(ticket: dict, manifest: dict, wt: Path,
                        repro_name: str) -> str:
    """Deterministic repro test: fails with the symptom while the bug is
    present (harness state A), passes once the fix is in (state B)."""
    symptom = json.dumps(ticket["symptom"])
    if manifest["app_type"] == "py":
        return (
            "# FDE_AGENT_BACKEND=mock: deterministic repro test (offline).\n"
            "# Runs the fixture's own suite; FAILS with the ticket symptom as\n"
            "# its message while the bug reproduces, passes once fixed.\n"
            "import subprocess\n"
            "import sys\n"
            "\n"
            "def test_repro():\n"
            "    r = subprocess.run(\n"
            "        [sys.executable, \"-m\", \"pytest\", \"-q\",\n"
            f"         \"--ignore={repro_name}\"],\n"
            "        capture_output=True, text=True, timeout=120,\n"
            "    )\n"
            "    if r.returncode != 0:\n"
            f"        raise AssertionError({symptom})\n"
        )
    return (
        "// FDE_AGENT_BACKEND=mock: deterministic repro test (offline).\n"
        "// Runs the fixture's own suite; FAILS with the ticket symptom as\n"
        "// its message while the bug reproduces, passes once fixed.\n"
        'const { execFileSync } = require("node:child_process");\n'
        'const assert = require("node:assert");\n'
        'const fs = require("node:fs");\n'
        'const path = require("node:path");\n'
        "\n"
        "const env = { ...process.env };\n"
        "delete env.NODE_TEST_CONTEXT; // else inner runner skips files\n"
        f"        const MY_NAME = {json.dumps(repro_name)};\n"
        "const tests = [];\n"
        "function walk(dir) {\n"
        "  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {\n"
        "    if (e.name === 'node_modules') continue;\n"
        "    const p = path.join(dir, e.name);\n"
        "    if (e.isDirectory()) walk(p);\n"
        "    else if (e.name.endsWith('.test.js') && e.name !== MY_NAME) tests.push(p);\n"
        "  }\n"
        "}\n"
        "walk('.');\n"
        "if (tests.length === 0) { assert.fail('no fixture tests found by mock repro'); }\n"
        "let failed = false;\n"
        "try {\n"
        "  execFileSync(process.execPath, ['--test', ...tests], {\n"
        "    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],\n"
        "    timeout: 120000,\n"
        "    env,\n"
        "  });\n"
        "} catch (e) {\n"
        "  failed = true;\n"
        "}\n"
        "\n"
        "if (failed) {\n"
        f"  assert.fail({symptom});\n"
        "}\n"
    )

def _mock_write_repro(cwd: str) -> None:
    """Write the deterministic repro test to the run dir (harness copies it)."""
    wt = Path(cwd)
    run_id = wt.parent.name
    ticket = parse_ticket(run_dir(run_id) / "ticket.md")
    repo = resolve_repo(ticket["system"])
    manifest = load_repo_manifest(Path(repo))
    repro_name = REPRO_FILES.get(manifest.get("app_type"), "js")
    repro_path = run_dir(run_id) / repro_name
    repro_path.write_text(_mock_repro_content(ticket, manifest, wt, repro_name),
                          encoding="utf-8")


def _mock_apply_gold(cwd: str) -> None:
    """Apply the fixture's gold.patch to the worktree — the perfect fix."""
    wt = Path(cwd)
    run_id = wt.parent.name
    ticket = parse_ticket(run_dir(run_id) / "ticket.md")
    repo = resolve_repo(ticket["system"])
    gold = Path(gold_path_in_sandbox(str(Path(repo) / "gold.patch"), str(wt)))
    if not gold.is_file():
        raise RuntimeError(f"gold.patch not found at {gold}")
    r = subprocess.run(["git", "-C", str(wt), "apply", str(gold)],
                       capture_output=True, text=True)
    if r.returncode != 0:  # Windows CRLF tolerance, same as the harness
        r = subprocess.run(["git", "-C", str(wt), "apply",
                            "--ignore-whitespace", str(gold)],
                           capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gold.patch failed to apply: "
                           f"{r.stderr.strip()[-300:]}")


def _mock_exec(prompt: str, cwd: str, timeout: int = ROUND_TIMEOUT) -> dict:
    """Deterministic offline stand-in for codex (FDE_AGENT_BACKEND=mock).

    Same return shape as codex_exec: rc/out/timed_out/summary/duration_ms.
    """
    t0 = time.monotonic()
    try:
        if "write ONE failing test file" in prompt:
            _mock_write_repro(cwd)
        else:
            _mock_apply_gold(cwd)
    except Exception as e:
        return {"rc": 1, "out": f"mock agent error: {e}", "timed_out": False,
                "summary": "",
                "duration_ms": int((time.monotonic() - t0) * 1000)}
    return {"rc": 0, "out": "[mock] deterministic step complete",
            "timed_out": False,
            "summary": "mock backend: deterministic offline step",
            "duration_ms": int((time.monotonic() - t0) * 1000)}


def _repro_prompt(ticket: dict, manifest: dict, repro_path: Path,
                  feedback: str | None) -> str:
    p = [
        "You are the reproduction agent in a bug-fix pipeline. The working",
        "directory is a git worktree of the buggy repo.",
        f"Repo: {ticket['system']}",
        f"Symptom to reproduce: \"{ticket['symptom']}\"",
        f"Expected: {ticket['expected']}",
        f"Actual: {ticket['actual']}",
        f"Test command: {manifest['test_cmd']}",
        f"Install command (already run): {manifest['install_cmd'] or 'none'}",
        "",
        "Task: write ONE failing test file at exactly:",
        f"  {_prompt_path(repro_path)}",
        "The test must:",
        "1. FAIL on the current (buggy) code,",
        f"2. contain the symptom string \"{ticket['symptom']}\" in its failure",
        "   output (assertion message or diff),",
        "3. PASS when the bug is fixed (do NOT fix the bug yourself).",
        "",
        "Use the repo's existing test framework and conventions. Do not modify",
        "any other file. Write only the test file.",
        "",
        "The verification harness checks that the worktree is UNTOUCHED during",
        "the suite run — never modify, stage, or rewrite existing files,",
        "including other test files.",
    ]
    if feedback:
        p += ["", "Your previous attempt was rejected by the verification harness:",
              feedback, "", "Fix the test file and try again."]
    return "\n".join(p)


def _fix_prompt(ticket: dict, manifest: dict, repro_path: Path,
                feedback: str | None) -> str:
    p = [
        "You are the fix agent in a bug-fix pipeline. The working directory is",
        "a git worktree of the buggy repo. A failing reproduction test exists at:",
        f"  {_prompt_path(repro_path)}",
        "",
        f"Ticket: {ticket['id']} ({ticket['severity']})",
        f"Symptom: \"{ticket['symptom']}\"",
        f"Expected: {ticket['expected']}",
        f"Actual: {ticket['actual']}",
        "",
        f"Test command: {manifest['test_cmd']}",
        f"Full suite command: {manifest['test_cmd']}",
        "",
        "Task: make the MINIMAL source change so the reproduction test PASSES",
        "and the full test suite stays green. Do not touch unrelated code.",
        "Do not modify or delete the reproduction test file.",
        "When done, end your reply with a one-paragraph what/why summary of",
        "the change.",
    ]
    if feedback:
        p += ["", "Your last attempt did not pass. Evidence:",
              feedback, "", "Analyze the failure and try again."]
    return "\n".join(p)


def _verdict_feedback(verdict: dict) -> str:
    lines = []
    for name in ("a", "b", "c"):
        c = verdict["checks"].get(name) or {}
        lines.append(f"check {name}: {'PASS' if c.get('ok') else 'FAIL'}"
                     f" (rc={c.get('rc')}, {c.get('duration_ms')}ms)"
                     + (f" — {c.get('detail')}" if c.get("detail") else ""))
    return "\n".join(lines)


def repro_loop(run_id: str, repo: str, worktree: str, manifest: dict,
               ticket: dict, max_attempts: int = REPRO_ATTEMPTS) -> dict:
    """Agent writes the repro test; the 3-state harness decides acceptance."""
    repro_path = run_dir(run_id) / REPRO_FILES.get(manifest.get("app_type"), "js")
    feedback = None
    for attempt in range(1, max_attempts + 1):
        prompt = _repro_prompt(ticket, manifest, repro_path, feedback)
        res = codex_exec(prompt, cwd=worktree)
        if res["timed_out"]:
            append(run_id, "agent_error",
                   {"stage": "repro", "attempt": attempt, "reason": "timeout"})
            return {"ok": False, "reason": "agent timeout"}
        if not repro_path.exists():
            feedback = (f"you did not create the file "
                        f"{repro_path.name} — create it with the failing test")
            continue
        verdict = verify_repro(repo, worktree, manifest, ticket, str(repro_path))
        append(run_id, "test_result", {
            "stage": "repro", "attempt": attempt, "verdict": verdict["pass"],
            "checks": {k: {"ok": (v or {}).get("ok"), "rc": (v or {}).get("rc")}
                       for k, v in verdict["checks"].items()},
            "duration_ms": verdict["duration_ms"]})
        if verdict["pass"]:
            append(run_id, "repro_test_written", {
                "file": repro_path.name, "attempts": attempt,
                "backend": BACKEND, "backend_version": codex_version(),
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:12]})
            return {"ok": True, "attempts": attempt, "path": str(repro_path)}
        feedback = _verdict_feedback(verdict)
    return {"ok": False, "reason": f"harness rejected repro test after "
                                   f"{max_attempts} attempts"}


def _git_diff(worktree: str) -> str:
    r = subprocess.run(["git", "-C", worktree, "diff"],
                       capture_output=True, text=True)
    return r.stdout


def _head(worktree: str) -> str:
    r = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def _reset_worktree(worktree: str) -> None:
    subprocess.run(["git", "-C", worktree, "checkout", "."],
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", worktree, "clean", "-fd"],
                   capture_output=True, text=True)


def _copy_repro(worktree: str, repro_path: Path) -> None:
    (Path(worktree) / repro_path.name).write_text(
        repro_path.read_text(encoding="utf-8"), encoding="utf-8")


def _worktree_snapshot(worktree: str) -> tuple:
    """(tracked-changes, HEAD, skip-worktree-flag-count) — mutation guard inputs."""
    p = subprocess.run(["git", "-C", worktree, "status", "--porcelain"],
                       capture_output=True, text=True)
    tracked = [l for l in p.stdout.splitlines() if l.strip() and not l.startswith("??")]
    s = subprocess.run(["git", "-C", worktree, "ls-files", "-v"],
                       capture_output=True, text=True)
    flags = sum(1 for l in s.stdout.splitlines() if l and l[0].islower())
    return (tracked, _head(worktree), flags)


def fix_loop(run_id: str, worktree: str, manifest: dict, ticket: dict,
             repro_path: Path, max_rounds: int = FIX_ROUNDS) -> dict:
    """Iterate codex until the repro test + full suite pass. Resets between rounds."""
    feedback = None
    narrowed = False
    baseline_head = _head(worktree)
    for rnd in range(1, max_rounds + 1):
        prompt = _fix_prompt(ticket, manifest, repro_path, feedback)
        res = codex_exec(prompt, cwd=worktree)
        if _head(worktree) != baseline_head:
            # agent committed during the round — reset back to baseline
            _reset_worktree(worktree)
            subprocess.run(["git", "-C", worktree, "reset", "--hard", baseline_head],
                           capture_output=True, text=True)
        if res["timed_out"]:
            append(run_id, "agent_error",
                   {"stage": "fix", "round": rnd, "reason": "timeout"})
            if not narrowed:
                narrowed = True
                feedback = "you ran out of time — start over and make the minimal change"
                continue
            return {"ok": False, "reason": "agent timeout", "rounds": rnd}
        diff = _git_diff(worktree)
        if not diff.strip():
            feedback = "no changes detected — edit the source code to fix the bug"
            continue
        _copy_repro(worktree, repro_path)
        r = run_cmd(f"{manifest['test_cmd']} {shlex.quote(repro_path.name)}",
                    worktree, timeout=120)
        suite = None
        mutated = False
        if r["rc"] == 0:
            before = _worktree_snapshot(worktree)
            suite = run_cmd(manifest["test_cmd"], worktree, timeout=180)
            after = _worktree_snapshot(worktree)
            mutated = before != after
        ok = r["rc"] == 0 and suite is not None and suite["rc"] == 0 and not mutated
        append(run_id, "fix_attempt", {
            "round": rnd, "ok": ok, "rc": r["rc"], "diff_bytes": len(diff),
            "duration_ms": res["duration_ms"], "summary": res["summary"],
            "backend": BACKEND, "backend_version": codex_version(),
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:12]})
        if ok:
            return {"ok": True, "rounds": rnd, "diff": diff,
                    "summary": res["summary"]}
        _reset_worktree(worktree)
        if mutated:
            feedback = ("the suite run mutated the worktree (tracked files "
                        "changed, commits created, or index flags set) — do "
                        "not touch anything except the minimal source fix")
            continue
        evidence = r["out"] if r["rc"] != 0 else (suite["out"] if suite else "")
        feedback = evidence[-2000:]
    return {"ok": False, "reason": "max rounds reached", "rounds": max_rounds}


def commit_fix(repo: str, worktree: str, ticket: dict) -> str:
    """Commit the fix (and repro test) in the worktree; returns the commit hash."""
    ensure_git_identity(repo)
    subprocess.run(["git", "-C", worktree, "add", "-A"], check=True)
    subprocess.run(["git", "-C", worktree, "commit",
                    "-m", f"fix: {ticket['id']} ({ticket['symptom']})"],
                   check=True, capture_output=True, text=True)
    r = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def load_manifest_for_run(run_id: str) -> dict:
    """Ticket + repo + manifest for a run (shared by CLI commands)."""
    from .ticket import parse_ticket
    run = run_dir(run_id)
    ticket = parse_ticket(run / "ticket.md")
    repo = resolve_repo(ticket["system"])
    manifest = load_repo_manifest(Path(repo))
    return ticket, repo, manifest
