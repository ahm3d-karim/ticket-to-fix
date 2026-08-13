"""Agent orchestration: non-interactive Codex wrapper + repro/fix loops.

Contracts (EXECUTION.md S1T6 / S2T1):
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
import subprocess
import threading
import time
from pathlib import Path

from .config import load_repo_manifest
from .harness import run_cmd, verify_repro
from .runlog import append, run_dir

BACKEND = os.environ.get("FDE_AGENT_BACKEND", "codex")
REPRO_ATTEMPTS = 3
FIX_ROUNDS = 8
ROUND_TIMEOUT = 900
REPRO_FILES = {"js": "repro.test.js", "py": "repro_test.py"}


class AgentAuthError(Exception):
    """The model provider rejected the API key (401). Raised so the failure
    surfaces as 'agent auth failed', never as a harness rejection."""


def codex_version() -> str:
    try:
        r = subprocess.run(["codex", "--version"], capture_output=True,
                           text=True, timeout=15)
        return (r.stdout or r.stderr).strip().splitlines()[0] or "unknown"
    except Exception:
        return "unknown"


def resolve_repo(system: str) -> str:
    """Map a ticket's `system` field to a repo dir carrying fde.yaml."""
    for cand in (Path("fixtures") / system, Path(system)):
        if (cand / "fde.yaml").exists():
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
                if k in ("OPENCODE_GO_API_KEY", "OPENCODE_GO_BASE_URL"):
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
    """Run codex non-interactively. Returns rc/out/timed_out/summary/duration_ms.

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
