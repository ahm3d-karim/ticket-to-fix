"""Bench: run the fixture corpus through the full pipeline and render a report.

``run_corpus`` drives the real CLI (``python -m fde.cli``) for every fixture
(``fixtures/*/fde.yaml`` plus ``demo-app/fde.yaml``), stopping at the first
gate: submit -> repro -> fix (the fix command runs the automated gates and
either lands in ``awaiting_approval`` or ``failed``). Each fixture's outcome is
summarized from its audit log (``runs/<run_id>/run.jsonl``) and
``state.json``: final state, repro attempts, fix rounds, wall time,
harness rejection details, and notes.

Agent failures never abort the corpus: a fixture that fails at any step is
recorded with ``state="failed"`` plus a reason and the bench moves on.

Run it with::

    uv run fde bench --backend mock

The default backend is ``codex`` (the real agent). ``--backend mock`` is the
deterministic, offline stand-in (no codex, no network, no key).
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from .agents import load_manifest_for_run
from .runlog import RUNS_DIR, events, state
from .worktree import discard_worktree

ROOT = Path(__file__).resolve().parent.parent


def discover_fixtures() -> list[Path]:
    """Every fixture repo carrying an fde.yaml: fixtures/* + demo-app/."""
    out = []
    fixtures_dir = ROOT / "fixtures"
    if fixtures_dir.is_dir():
        for d in sorted(fixtures_dir.iterdir()):
            if d.is_dir() and (d / "fde.yaml").is_file():
                out.append(d)
    demo = ROOT / "demo-app"
    if (demo / "fde.yaml").is_file():
        out.append(demo)
    return out


def _select_fixtures(names: list[str] | None) -> list[Path]:
    if not names:
        return discover_fixtures()
    by_name = {d.name: d for d in discover_fixtures()}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise ValueError(f"unknown fixture(s): {', '.join(missing)}")
    return [by_name[n] for n in names]


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    """Run one fde CLI step under the repo's venv python, from the repo root."""
    return subprocess.run(
        [sys.executable, "-m", "fde.cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)


def _summarize_run(run_id: str) -> dict:
    """Parse the run's audit log + state into bench metrics."""
    evs = events(run_id)
    repro_attempts = sum(
        1 for e in evs
        if e["event"] == "test_result" and e["data"].get("stage") == "repro")
    fix_rounds = sum(1 for e in evs if e["event"] == "fix_attempt")

    reasons = []
    for e in evs:
        if e["event"] == "test_result" and e["data"].get("pass") is False \
                and isinstance(e["data"].get("checks"), dict):
            # harness-shaped verdicts carry per-check detail strings
            for c in e["data"]["checks"].values():
                if c and c.get("ok") is False and c.get("detail"):
                    reasons.append(c["detail"])
        elif e["event"] == "agent_error":
            d = e["data"]
            reasons.append(f"agent_error({d.get('stage')}): {d.get('reason')}")

    notes = []
    for e in evs:
        if e["event"] == "gates_failed":
            for kind, hits in e["data"].items():
                if isinstance(hits, list) and hits:
                    notes.append(f"gates_failed: {kind} x{len(hits)}")
    return {"state": state(run_id), "repro_attempts": repro_attempts,
            "fix_rounds": fix_rounds, "rejection_reasons": reasons,
            "notes": notes}


def _discard_worktree(run_id: str) -> None:
    """Best-effort cleanup so repeated bench runs don't pile up worktrees."""
    try:
        _, repo, _ = load_manifest_for_run(run_id)
        discard_worktree(repo, run_id)
    except Exception:
        pass


def _run_fixture(fdir: Path, env: dict) -> dict:
    name = fdir.name
    result = {"fixture": name, "run_id": None, "state": "failed",
              "repro_attempts": 0, "fix_rounds": 0, "wall_time": 0.0,
              "rejection_reasons": [], "notes": []}
    ticket = fdir / "ticket.md"
    if not ticket.is_file():
        result["notes"].append("no ticket.md — fixture skipped")
        return result

    t0 = time.monotonic()
    run_id = None
    try:
        r = _cli(env, "submit", str(ticket))
        if r.returncode != 0:
            result["notes"].append(
                f"submit failed: {r.stderr.strip()[-300:] or r.stdout.strip()[-300:]}")
            return result
        run_id = r.stdout.strip().splitlines()[-1].strip()
        result["run_id"] = run_id

        r = _cli(env, "repro", run_id)
        if r.returncode != 0:
            result["notes"].append(
                f"repro failed: {r.stderr.strip()[-300:]}")
        else:
            r = _cli(env, "fix", run_id)
            if r.returncode != 0:
                result["notes"].append(
                    f"fix failed: {r.stderr.strip()[-300:]}")
    except Exception as e:  # keep the corpus going no matter what
        result["notes"].append(f"bench error: {e}")
    finally:
        result["wall_time"] = round(time.monotonic() - t0, 2)
        if run_id:
            result.update(_summarize_run(run_id))
            _discard_worktree(run_id)
    return result


def run_corpus(backend: str = "codex",
               fixtures: list[str] | None = None) -> list[dict]:
    """Run the pipeline over the fixture corpus; one result dict per fixture.

    Args:
        backend: agent backend for every step (``FDE_AGENT_BACKEND`` env var).
        fixtures: optional subset of fixture dir names; default: all.

    Returns:
        List of dicts with keys: fixture, run_id, state, repro_attempts,
        fix_rounds, wall_time, rejection_reasons, notes.
    """
    env = os.environ.copy()
    env["FDE_AGENT_BACKEND"] = backend
    return [_run_fixture(fdir, env) for fdir in _select_fixtures(fixtures)]


def render_report(results: list[dict]) -> str:
    """Render the bench results as a clean table + one summary line."""
    headers = ["fixture", "state", "repro_attempts", "fix_rounds",
               "wall_time", "notes"]
    rows = []
    for r in results:
        rows.append([
            r["fixture"], r["state"], str(r["repro_attempts"]),
            str(r["fix_rounds"]), f"{r['wall_time']:.1f}s",
            "; ".join(r["notes"]) if r["notes"] else "-",
        ])
    widths = [max(len(h), *(len(row[i]) for row in rows))
              for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    sep = "  ".join("-" * w for w in widths)
    body = [line, sep]
    for row in rows:
        body.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())

    n_ok = sum(1 for r in results if r["state"] == "awaiting_approval")
    n_failed = sum(1 for r in results if r["state"] == "failed")
    total = round(sum(r["wall_time"] for r in results), 1)
    n = len(results)
    summary = (f"summary: {n} fixture(s) — {n_ok} awaiting_approval, "
               f"{n_failed} failed — total {total}s")
    return "\n".join(body + [summary])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fde.bench", description="run the fixture corpus and render a report")
    ap.add_argument("--backend", default=os.environ.get("FDE_AGENT_BACKEND", "codex"),
                    help="agent backend: codex (default), mock, or claude")
    ap.add_argument("--fixture", action="append", dest="fixtures", metavar="NAME",
                    help="restrict to a fixture dir name (repeatable)")
    args = ap.parse_args(argv)
    results = run_corpus(backend=args.backend, fixtures=args.fixtures)
    print(render_report(results))
    # corpus gate: tier5_outofscope is DESIGNED to fail at the security gate
    # (its only correct fix embeds a credential — see README "When it fails");
    # any other failed fixture is a regression the gate must flag.
    bad = [r["fixture"] for r in results
           if r["state"] == "failed" and r["fixture"] != "tier5_outofscope"]
    if bad:
        print(f"bench gate FAILED — unexpected failures: {', '.join(bad)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
