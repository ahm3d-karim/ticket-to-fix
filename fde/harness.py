"""3-state verification harness + Windows-safe command runner.

Every repo command runs through ``bash -c`` (fixtures declare bash command
strings). Timeout handling on Windows is **best-effort**: subprocess's own
timeout kills the bash process but NOT grandchildren (codex/node children can
outlive it) — we additionally try ``taskkill //F //T`` on the tree; this is
accepted for the demo stage and documented here. A future stage hardens this
with real containers.

verify_repro implements the 3-state contract (the product's proof layer):

  A  repro test FAILS on buggy code with the ticket symptom in its output
  B  repro test PASSES with gold.patch applied
  C  full test suite PASSES with gold.patch applied

After B and C the worktree is restored (``git checkout . && git clean -fd``)
so a previous run's residue never poisons state A evidence. Every verdict is
appended to the run log as a ``test_result`` event (duration_ms + per-check
detail) — the future bench's raw data, recorded now.

The run id is derived from the worktree path (runs/<run_id>/worktree), which
is how the CLI always creates it.
"""
import re
import shlex
import subprocess
import time
from pathlib import Path

from .runlog import append

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
SNIPPET_CHARS = 500


def run_cmd(cmd: str, cwd: str, timeout: int = 60) -> dict:
    """Run ``bash -c <cmd>`` in cwd. Returns {"rc", "out", "timed_out"}."""
    proc = subprocess.Popen(
        ["bash", "-c", cmd], cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=CREATE_NEW_PROCESS_GROUP,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return {"rc": proc.returncode, "out": (out or "") + (err or ""),
                "timed_out": False}
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return {"rc": -1, "out": "[timeout]", "timed_out": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Best-effort Windows process-tree kill (grandchildren may still outlive)."""
    try:
        # resolve bash's direct child pid(s) first, then kill each tree
        kids = subprocess.run(["pgrep", "-P", str(proc.pid)],
                              capture_output=True, text=True, timeout=10)
        pids = kids.stdout.split() or [str(proc.pid)]
        for pid in pids:
            subprocess.run(["taskkill", "//F", "//T", "//PID", pid],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def symptom_in_output(symptom: str, output: str) -> bool:
    return norm(symptom) in norm(output)


def verify_repro(repo: str, worktree: str, manifest: dict, ticket: dict,
                 repro_test: str, timeout: int = 120) -> dict:
    """Run the 3-state verification. Returns {"pass", "checks", "duration_ms"}.

    - ``repo``: fixture repo root (holds gold.patch).
    - ``worktree``: the run's worktree (runs/<run_id>/worktree).
    - ``manifest``: fde.yaml dict (install_cmd/test_cmd/run_cmd/app_type).
    - ``ticket``: parsed ticket dict (needs ["symptom"]).
    - ``repro_test``: path to the agent-written repro test file.
    """
    wt = Path(worktree).resolve()
    run_id = wt.parent.name
    baseline_head = _head(wt)
    # resolve: `git -C <wt> apply <path>` resolves relative paths against the
    # worktree, not the process CWD — absolute paths are immune
    gold = (Path(repo) / "gold.patch").resolve()
    repro_test = Path(repro_test).resolve()
    repro_name = repro_test.name

    # copy the repro test into the worktree (untracked; git clean -fd removes it)
    (wt / repro_name).write_text(Path(repro_test).read_text(encoding="utf-8"),
                                 encoding="utf-8")
    test_one = f"{manifest['test_cmd']} {shlex.quote(repro_name)}"

    checks: dict = {"a": None, "b": None, "c": None}
    started = time.monotonic()

    # --- State A: fails for the right reason --------------------------------
    a_t0 = time.monotonic()
    r = run_cmd(test_one, str(wt), timeout=timeout)
    ok_a = r["rc"] != 0 and symptom_in_output(ticket["symptom"], r["out"])
    checks["a"] = _check(
        ok=ok_a, rc=r["rc"], duration_ms=_ms(a_t0), out=r["out"],
        detail=_a_detail(ok_a, r, ticket),
    )
    if not ok_a:
        return _verdict(checks, started, run_id, ticket, repro_name)

    # --- State B: passes with gold ------------------------------------------
    if not gold.exists():
        checks["b"] = _check(False, None, 0, "",
                             detail=f"gold.patch not found at {gold}")
        _restore_guarded(wt, baseline_head)
        return _verdict(checks, started, run_id, ticket, repro_name)
    b_t0 = time.monotonic()
    apply_r = _apply_gold(wt, gold, timeout)
    r = run_cmd(test_one, str(wt), timeout=timeout)
    ok_b = apply_r["rc"] == 0 and r["rc"] == 0
    detail = ("git apply failed" if apply_r["rc"] != 0
              else f"repro test failed with gold applied (rc={r['rc']})")
    checks["b"] = _check(ok=ok_b, rc=r["rc"], duration_ms=_ms(b_t0), out=r["out"],
                         detail=detail)
    _restore_guarded(wt, baseline_head)
    if not ok_b:
        return _verdict(checks, started, run_id, ticket, repro_name)

    # --- State C: no regression ---------------------------------------------
    # The suite runs WITHOUT the repro test file present (it is removed first):
    # state C answers "does the fix break the repo's OWN tests?" — running the
    # agent-written file here hands it a tampering surface (staging, commits,
    # skip-worktree — all observed in the wild). Repro-test-in-suite
    # integration is verified later by the fix stage.
    c_t0 = time.monotonic()
    try:
        (wt / repro_name).unlink()
    except OSError:
        pass
    # _restore_guarded after state B reverted the gold patch — re-apply it:
    # state C must run the suite against the FIXED tree, not the buggy one
    # (observed in the wild: 3/3 repro attempts spuriously rejected).
    apply_c = _apply_gold(wt, gold, timeout)
    if apply_c["rc"] != 0:
        checks["c"] = _check(False, apply_c["rc"], _ms(c_t0), apply_c["out"],
                             detail="gold.patch failed to apply for state C")
        _restore_guarded(wt, baseline_head)
        return _verdict(checks, started, run_id, ticket, repro_name)
    before = _porcelain(wt)
    r = run_cmd(manifest["test_cmd"], str(wt), timeout=timeout)
    after = _porcelain(wt)
    head_moved = _head(wt) != baseline_head
    ok_c = r["rc"] == 0 and before == after and not head_moved
    checks["c"] = _check(
        ok=ok_c, rc=r["rc"], duration_ms=_ms(c_t0), out=r["out"],
        detail=("full suite green with gold (repro file excluded)" if ok_c
                else (f"full suite failed with gold applied (rc={r['rc']})"
                      if r["rc"] != 0
                      else ("suite mutated the worktree (tracked files changed "
                            "or a commit was created during the run)"))))
    _restore_guarded(wt, baseline_head)

    return _verdict(checks, started, run_id, ticket, repro_name)


def _a_detail(ok: bool, r: dict, ticket: dict) -> str:
    if not ok:
        if r["rc"] == 0:
            return "repro test PASSED on buggy code (rc=0) — must fail"
        return (f"failed (rc={r['rc']}) but symptom "
                f"'{ticket['symptom']}' not found in output")
    return (f"repro test FAILED (rc={r['rc']}) with symptom "
            f"'{ticket['symptom']}' present in output")


def _check(ok: bool, rc: int | None, duration_ms: int, out: str, detail: str) -> dict:
    return {"ok": ok, "rc": rc, "duration_ms": duration_ms,
            "snippet": out[-SNIPPET_CHARS:], "detail": detail}


def _ms(t0: float) -> int:
    return int(round((time.monotonic() - t0) * 1000))


def _head(wt: Path) -> str:
    r = run_cmd("git rev-parse HEAD", str(wt), timeout=30)
    return r["out"].strip()


def _apply_gold(wt: Path, gold: Path, timeout: int = 120) -> dict:
    """git apply gold.patch into the worktree (CRLF-tolerant retry)."""
    apply_r = run_cmd(
        f"git -C {shlex.quote(str(wt))} apply {shlex.quote(str(gold))}",
        str(wt), timeout=timeout)
    if apply_r["rc"] != 0:
        # Windows CRLF tolerance: retry once ignoring whitespace differences
        apply_r = run_cmd(
            f"git -C {shlex.quote(str(wt))} apply --ignore-whitespace "
            f"{shlex.quote(str(gold))}",
            str(wt), timeout=timeout)
    return apply_r


def _porcelain(wt: Path) -> list[str]:
    """git status --porcelain, excluding untracked-only noise (pycache, etc.).

    Flags tracked-file changes (modified/staged/deleted/renamed) — the
    tampering vector — while ignoring fresh untracked files that test runs
    legitimately create (e.g. pytest __pycache__).
    """
    r = run_cmd("git status --porcelain", str(wt), timeout=30)
    out = []
    for line in r["out"].splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("??"):
            continue
        out.append(line)
    return out


def _restore(wt: Path) -> None:
    """Put the worktree back to a pristine checkout (best-effort).

    reset --hard (not checkout .) so STAGED modifications are reverted too —
    a repro test could otherwise stage a rewritten fixture test and survive
    the restore (observed in the wild). Also purges skip-worktree / assume-
    unchanged bits (another observed evasion): those flags make git ignore
    on-disk changes, so they are cleared before the reset.
    """
    run_cmd(
        "git ls-files -v | awk '/^[a-z]/ {print substr($0,3)}' "
        "| xargs -d '\\n' -r git update-index --no-skip-worktree -- "
        "&& git reset --hard HEAD && git clean -fd",
        str(wt), timeout=120)


def _restore_guarded(wt: Path, baseline_head: str) -> bool:
    """Restore; if the run created commits (they survive restore), hard-reset
    back to the baseline and report True. Call after EVERY state."""
    _restore(wt)
    if _head(wt) != baseline_head:
        run_cmd(f"git reset --hard {shlex.quote(baseline_head)}", str(wt), timeout=120)
        return True
    return False


def _verdict(checks: dict, started: float, run_id: str, ticket: dict,
             repro_name: str) -> dict:
    total_ms = _ms(started)
    passed = all(c is not None and c["ok"] for c in checks.values())
    append(run_id, "test_result", {
        "pass": passed,
        "duration_ms": total_ms,
        "checks": checks,  # not-run checks are null in the JSON
        "ticket": ticket.get("id"),
        "symptom": ticket.get("symptom"),
        "repro_test": repro_name,
    })
    return {"pass": passed, "checks": checks, "duration_ms": total_ms}
