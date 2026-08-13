"""Verification summary over harness-observed signals (roadmap S3).

Reads a run's ``run.jsonl`` and aggregates ONLY signals the harness itself
logged — repro acceptance attempts (``test_result`` with ``stage == "repro"``
and ``verdict``), fix rounds (``fix_attempt``), gates (``gates_passed`` /
``gates_failed``), diff size (``gates_passed`` → ``diff_bytes``, falling back
to the last ``fix_attempt``'s ``diff_bytes`` when gates never ran), and the
final state (last ``state_changed`` → ``to``; ``state.json`` as fallback for
runs that never logged a transition).

No probabilities, no model confidence, no agent self-reports: trust is a
property of the machinery, not the model. ``fix_attempt`` carries an agent
``summary`` string — this module deliberately ignores it.

Signals (all keys of :func:`summarize_run`):
- ``repro_first_try``: bool — was the repro test accepted by the harness on
  the first attempt? ``None`` when there is no repro evidence at all.
- ``state_a_rejections``: int — harness rejections of the repro test while in
  state A (``test_result``/repro events whose ``verdict`` is false).
- ``fix_rounds``: int — count of ``fix_attempt`` events.
- ``rounds_vs_budget``: {"used", "budget", "exhausted"} — used vs
  ``agents.FIX_ROUNDS``; exhausted means the budget was reached AND the last
  fix attempt did not pass (the loop ran out without a green fix).
- ``gates``: "passed" | "failed" | "none" — outcome of the last gates event.
- ``diff_bytes``: int | None — ``gates_passed`` diff size, else the last
  ``fix_attempt``'s diff size, else None.
- ``final_state``: str | None — last observed state.
- ``reached_awaiting_approval``: bool — any ``state_changed`` into
  ``awaiting_approval`` (the machinery's green light; the run may have moved
  on to approved/deployed/rolled_back afterwards).

Verdict mapping (:func:`verdict`):
- ``not-verified`` — never reached awaiting_approval, gates failed, final
  state is ``failed``, or the fix budget was exhausted.
- ``verified`` — reached awaiting_approval AND repro accepted first try AND
  exactly 1 fix round AND gates passed AND no state-A rejections AND the run
  was not rejected by the human approval gate afterwards.
- ``verified-with-warnings`` — reached awaiting_approval with any retries,
  rejections, rounds > 1, or a post-verification human rejection.
"""
import json
from pathlib import Path

from .agents import FIX_ROUNDS

VERDICT_VERIFIED = "verified"
VERDICT_WARNINGS = "verified-with-warnings"
VERDICT_NOT = "not-verified"

_REPRO_STAGE = "repro"


def _load_events(run_dir: str) -> list[dict]:
    p = Path(run_dir) / "run.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _read_state_file(run_dir: str) -> str | None:
    p = Path(run_dir) / "state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("state")
    except (json.JSONDecodeError, OSError):
        return None


def summarize_run(run_dir: str) -> dict:
    """Aggregate harness-observed signals from a run's run.jsonl."""
    events = _load_events(run_dir)

    repro_results = [e for e in events
                     if e.get("event") == "test_result"
                     and e.get("data", {}).get("stage") == _REPRO_STAGE]
    repro_written = [e for e in events
                     if e.get("event") == "repro_test_written"]
    fix_attempts = [e for e in events if e.get("event") == "fix_attempt"]
    gates_events = [e for e in events
                    if e.get("event") in ("gates_passed", "gates_failed")]
    state_changes = [e for e in events
                     if e.get("event") == "state_changed"
                     and isinstance(e.get("data"), dict)]

    # --- repro: first-try acceptance + state-A rejections ------------------
    if repro_results:
        repro_first_try = bool(repro_results[0].get("data", {}).get("verdict"))
    elif repro_written:
        # no per-attempt results logged, but the acceptance event says it all
        repro_first_try = repro_written[0].get("data", {}).get("attempts") == 1
    else:
        repro_first_try = None
    state_a_rejections = sum(
        1 for e in repro_results if e.get("data", {}).get("verdict") is False)

    # --- fix rounds vs budget ----------------------------------------------
    fix_rounds = len(fix_attempts)
    last_fix_ok = fix_attempts[-1].get("data", {}).get("ok") if fix_attempts else None
    exhausted = fix_rounds >= FIX_ROUNDS and last_fix_ok is not True

    # --- gates + diff size --------------------------------------------------
    gates = "none"
    if gates_events:
        gates = ("passed" if gates_events[-1]["event"] == "gates_passed"
                 else "failed")
    diff_bytes = None
    passed = [e for e in gates_events if e["event"] == "gates_passed"]
    if passed and isinstance(passed[-1].get("data"), dict):
        diff_bytes = passed[-1]["data"].get("diff_bytes")
    if diff_bytes is None and fix_attempts:
        last = fix_attempts[-1].get("data", {})
        diff_bytes = last.get("diff_bytes")

    # --- final state ---------------------------------------------------------
    final_state = None
    if state_changes:
        final_state = state_changes[-1].get("data", {}).get("to")
    if final_state is None:
        final_state = _read_state_file(run_dir)

    reached_approval = any(
        e.get("data", {}).get("to") == "awaiting_approval"
        for e in state_changes)

    return {
        "repro_first_try": repro_first_try,
        "state_a_rejections": state_a_rejections,
        "fix_rounds": fix_rounds,
        "rounds_vs_budget": {"used": fix_rounds, "budget": FIX_ROUNDS,
                             "exhausted": exhausted},
        "gates": gates,
        "diff_bytes": diff_bytes,
        "final_state": final_state,
        "reached_awaiting_approval": reached_approval,
    }


def verdict(d: dict) -> str:
    """Map aggregated signals to a verdict. See the module docstring."""
    if not d.get("reached_awaiting_approval"):
        return VERDICT_NOT
    if d.get("final_state") == "failed" or d.get("gates") == "failed":
        return VERDICT_NOT
    if d.get("rounds_vs_budget", {}).get("exhausted"):
        return VERDICT_NOT
    if (d.get("repro_first_try") is True
            and d.get("state_a_rejections") == 0
            and d.get("fix_rounds") == 1
            and d.get("gates") == "passed"
            and d.get("final_state") != "rejected"):
        return VERDICT_VERIFIED
    return VERDICT_WARNINGS


# --- rendering ---------------------------------------------------------------

def _row(check: str, status: str, evidence: str) -> str:
    return f"{check:<20} | {status:<5} | {evidence}"


def render_summary(run_dir: str) -> str:
    """Per-check status block + verdict line for a run directory."""
    d = summarize_run(run_dir)
    out = [_row("check", "status", "evidence"),
           _row("-" * 20, "-" * 5, "-" * 40)]

    # repro first-try
    if d["repro_first_try"] is True:
        out.append(_row("repro first-try", "pass",
                        "accepted on attempt 1"))
    elif d["repro_first_try"] is False:
        out.append(_row("repro first-try", "fail",
                        f"accepted after {d['state_a_rejections'] + 1} attempts"))
    else:
        out.append(_row("repro first-try", "n/a", "no repro evidence"))

    # state-A rejections
    if d["state_a_rejections"] > 0:
        out.append(_row("state-A rejections", "warn",
                        f"{d['state_a_rejections']} harness rejection(s)"))
    else:
        out.append(_row("state-A rejections", "ok",
                        "0" if d["repro_first_try"] is not None else "n/a"))

    # fix rounds
    budget = d["rounds_vs_budget"]
    if budget["exhausted"]:
        out.append(_row("fix rounds", "fail",
                        f"{budget['used']} of {budget['budget']} (budget exhausted)"))
    elif budget["used"] > 1:
        out.append(_row("fix rounds", "warn",
                        f"{budget['used']} of {budget['budget']}"))
    else:
        out.append(_row("fix rounds", "ok",
                        f"{budget['used']} of {budget['budget']}"))

    # gates
    if d["gates"] == "passed":
        out.append(_row("gates", "pass", "gates_passed"))
    elif d["gates"] == "failed":
        out.append(_row("gates", "fail", "gates_failed"))
    else:
        out.append(_row("gates", "warn", "no gates run"))

    # diff size
    out.append(_row("diff size", "info",
                    f"{d['diff_bytes']} bytes" if d["diff_bytes"] is not None
                    else "n/a"))

    # final state
    out.append(_row("final state", "info", d["final_state"] or "n/a"))

    v = verdict(d)
    note = {
        VERDICT_VERIFIED: "all harness checks green on first try",
        VERDICT_WARNINGS: "harness verified, but with retries/rejections",
        VERDICT_NOT: "harness did not verify this run",
    }[v]
    out.append(f"verdict: {v} — {note}")
    return "\n".join(out)
