"""fde CLI — ticket-to-fix pipeline.

Commands:
  submit <ticket.md>        start a run from a ticket
  status <run_id>           show run state + recent events
  repro <run_id>            agent writes repro test; 3-state harness verifies
  fix <run_id>              agent fixes until repro test + suite pass; gates run
  diff <run_id>             print the evidence package
  approve <run_id>          human approval gate
  deploy --preview <run_id> serve the fix on the preview port
  deploy --prod <run_id>    fast-forward prod, restart server, verify
  rollback <run_id>         revert the fix on prod, restart, verify
"""
import argparse
import subprocess
import sys
from pathlib import Path

from . import agents, gates
from .config import load_repo_manifest
from .deploy import (DeployError, discard_preview, preview_deploy, prod_deploy,
                     rollback)
from .harness import run_cmd
from .runlog import RUNS_DIR, append, events, new_run_id, run_dir, set_state, state
from .ticket import TicketError, parse_ticket
from .worktree import create_worktree, discard_worktree

FIX_COMMIT_FILE = "fix_commit.txt"


def _need_state(run_id: str, *allowed: str) -> None:
    cur = state(run_id)
    if cur not in allowed:
        raise SystemExit(
            f"run {run_id} is in state '{cur}', expected one of {allowed}")


def _fix_commit(run_id: str) -> str:
    p = run_dir(run_id) / FIX_COMMIT_FILE
    if not p.exists():
        raise SystemExit(f"run {run_id} has no fix commit — run 'fde fix' first")
    return p.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_submit(args) -> int:
    ticket_path = Path(args.ticket)
    try:
        meta = parse_ticket(ticket_path)
    except TicketError as e:
        print(f"validation error: {e}", file=sys.stderr)
        return 1
    run_id = new_run_id()
    d = run_dir(run_id)
    import shutil
    shutil.copyfile(ticket_path, d / "ticket.md")
    append(run_id, "ticket_parsed", {
        "ticket_id": meta["id"], "system": meta["system"], "severity": meta["severity"],
    })
    print(run_id)
    return 0


def cmd_status(args) -> int:
    run_id = args.run_id
    log = RUNS_DIR / run_id / "run.jsonl"
    if not log.exists():
        print(f"run not found: {run_id}", file=sys.stderr)
        return 1
    evs = events(run_id)
    print(f"run: {run_id}")
    print(f"state: {state(run_id)}")
    print(f"events (last {min(5, len(evs))} of {len(evs)}):")
    for e in evs[-5:]:
        print(f"  {e['ts']} {e['event']} {e['data']}")
    print("artifacts:")
    found = False
    for name in ("ticket.md", FIX_COMMIT_FILE, "repro.test.js", "repro_test.py"):
        p = RUNS_DIR / run_id / name
        if p.exists():
            print(f"  {name}: {p}")
            found = True
    if not found:
        print("  (none yet)")
    return 0


def cmd_repro(args) -> int:
    run_id = args.run_id
    _need_state(run_id, "submitted")
    ticket, repo, manifest = agents.load_manifest_for_run(run_id)
    set_state(run_id, "reproducing")
    try:
        worktree = create_worktree(repo, run_id)
        append(run_id, "worktree_created", {"repo": repo, "worktree": worktree})
        result = agents.repro_loop(run_id, repo, worktree, manifest, ticket)
    except Exception as e:
        set_state(run_id, "failed")
        print(f"repro failed: {e}", file=sys.stderr)
        return 1
    if not result["ok"]:
        set_state(run_id, "failed")
        print(f"repro rejected: {result['reason']}", file=sys.stderr)
        return 1
    set_state(run_id, "reproved")
    print(f"repro test accepted after {result['attempts']} attempt(s): "
          f"{result['path']}")
    return 0


def cmd_fix(args) -> int:
    run_id = args.run_id
    _need_state(run_id, "reproved")
    ticket, repo, manifest = agents.load_manifest_for_run(run_id)
    worktree = str(run_dir(run_id) / "worktree")
    if not Path(worktree).exists():
        print("run has no worktree — run 'fde repro' first", file=sys.stderr)
        return 1
    from .agents import REPRO_FILES
    repro_path = run_dir(run_id) / REPRO_FILES.get(manifest.get("app_type"), "js")
    if not repro_path.exists():
        print("run has no repro test — run 'fde repro' first", file=sys.stderr)
        return 1
    set_state(run_id, "fixing")
    result = agents.fix_loop(run_id, worktree, manifest, ticket, repro_path)
    if not result["ok"]:
        set_state(run_id, "failed")
        print(f"fix failed: {result['reason']} (rounds={result.get('rounds')})",
              file=sys.stderr)
        return 1
    commit = agents.commit_fix(repo, worktree, ticket)
    (run_dir(run_id) / FIX_COMMIT_FILE).write_text(commit, encoding="utf-8")
    set_state(run_id, "fixed")
    print(f"fix green in {result['rounds']} round(s); commit {commit[:12]}")
    # ---- automated gates on the fix diff ----
    set_state(run_id, "gating")
    show = subprocess.run(
        ["git", "-C", worktree, "show", "--format=", "--unified=3", commit],
        capture_output=True, text=True)
    findings = gates.scan_diff(show.stdout)
    if findings["secrets"] or findings["lint"]:
        append(run_id, "gates_failed", findings)
        set_state(run_id, "failed")
        print("GATES FAILED — findings:", file=sys.stderr)
        for kind, hits in findings.items():
            for lineno, name, snippet in hits:
                print(f"  [{kind}] line {lineno} {name}: {snippet}", file=sys.stderr)
        return 1
    append(run_id, "gates_passed", {"diff_bytes": len(show.stdout)})
    set_state(run_id, "gated")
    set_state(run_id, "awaiting_approval")
    print("gates passed; run is awaiting human approval")
    return 0


def cmd_diff(args) -> int:
    run_id = args.run_id
    log = RUNS_DIR / run_id / "run.jsonl"
    if not log.exists():
        print(f"run not found: {run_id}", file=sys.stderr)
        return 1
    evs = events(run_id)
    ticket, repo, manifest = agents.load_manifest_for_run(run_id)
    print(f"run: {run_id}   state: {state(run_id)}")
    print(f"ticket: {ticket['id']} ({ticket['severity']}) — {ticket['system']}")
    print(f"symptom: {ticket['symptom']}")
    commit = _fix_commit(run_id)
    stat = subprocess.run(["git", "-C", repo, "show", "--stat", "--format=", commit],
                          capture_output=True, text=True)
    print("\nchanged files:")
    print(stat.stdout.strip() or "  (none)")
    print("\ntest before/after:")
    for e in evs:
        if e["event"] == "test_result" and e["data"].get("stage") == "repro":
            d = e["data"]
            print(f"  repro attempt {d['attempt']}: verdict={d['verdict']} "
                  f"{d['duration_ms']}ms")
    for e in evs:
        if e["event"] == "fix_attempt":
            d = e["data"]
            print(f"  fix round {d['round']}: ok={d['ok']} rc={d['rc']} "
                  f"{d['duration_ms']}ms diff={d['diff_bytes']}B")
    summary = ""
    for e in reversed(evs):
        if e["event"] == "fix_attempt" and e["data"].get("summary"):
            summary = e["data"]["summary"]
            break
    print("\nagent what/why:")
    print(f"  {summary or '(none captured)'}")
    print("\ngates:")
    gated = [e for e in evs if e["event"] in ("gates_passed", "gates_failed")]
    if gated:
        g = gated[-1]
        print(f"  {g['event']} {g['data']}")
    else:
        print("  (not run)")
    append(run_id, "evidence_packaged", {"commit": commit})
    return 0


def cmd_approve(args) -> int:
    run_id = args.run_id
    _need_state(run_id, "awaiting_approval")
    set_state(run_id, "approved")
    append(run_id, "approved", {"by": "human"})
    print(f"run {run_id} approved — ready for 'fde deploy --prod'")
    return 0


def cmd_deploy(args) -> int:
    run_id = args.run_id
    ticket, repo, manifest = agents.load_manifest_for_run(run_id)
    commit = _fix_commit(run_id)
    if args.mode == "preview":
        _need_state(run_id, "gated", "awaiting_approval", "approved")
        try:
            res = preview_deploy(run_id, repo, commit)
        except DeployError as e:
            print(f"preview failed: {e}", file=sys.stderr)
            return 1
        print(f"preview: {res['url']}  healthy={res['healthy']}")
        return 0 if res["healthy"] else 1
    _need_state(run_id, "approved")
    try:
        res = prod_deploy(run_id, repo, commit)
    except DeployError as e:
        print(f"prod deploy failed: {e}", file=sys.stderr)
        return 1
    print(f"prod: {res['url']}  healthy={res['healthy']}  prod_head={res['prod_head'][:12]}")
    return 0 if res["healthy"] else 1


def cmd_rollback(args) -> int:
    run_id = args.run_id
    ticket, repo, manifest = agents.load_manifest_for_run(run_id)
    commit = _fix_commit(run_id)
    try:
        res = rollback(run_id, repo, commit)
    except DeployError as e:
        print(f"rollback failed: {e}", file=sys.stderr)
        return 1
    print(f"rolled back: healthy={res['healthy']}  revert_head={res['revert_head'][:12]}")
    return 0 if res["healthy"] else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fde", description="ticket-to-fix pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_submit = sub.add_parser("submit", help="validate a ticket and start a run")
    p_submit.add_argument("ticket", help="path to ticket.md")

    p_status = sub.add_parser("status", help="show run state and recent events")
    p_status.add_argument("run_id")

    for name, help_ in (
        ("repro", "agent writes repro test; 3-state harness verifies"),
        ("fix", "agent fixes until repro test + suite pass; gates run"),
        ("diff", "print the evidence package"),
        ("approve", "human approval gate"),
        ("rollback", "revert the fix on prod and restart"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("run_id")

    p_deploy = sub.add_parser("deploy", help="preview or production deploy")
    p_deploy.add_argument("--preview", dest="mode", action="store_const", const="preview",
                          help="serve the fix on the preview port")
    p_deploy.add_argument("--prod", dest="mode", action="store_const", const="prod",
                          help="fast-forward prod, restart server, verify")
    p_deploy.add_argument("run_id")
    p_deploy.set_defaults(mode=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "submit": cmd_submit, "status": cmd_status, "repro": cmd_repro,
        "fix": cmd_fix, "diff": cmd_diff, "approve": cmd_approve,
        "deploy": cmd_deploy, "rollback": cmd_rollback,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
