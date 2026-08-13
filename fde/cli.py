"""fde CLI — argparse entry point.

Stage 0: submit/status real; diff/approve/rollback/deploy/repro/fix are
stubs that exit 0 with a "not implemented in stage <N>" message.
"""
import argparse
import shutil
import sys
from pathlib import Path

from .runlog import RUNS_DIR, append, events, new_run_id, run_dir, state
from .ticket import TicketError, parse_ticket

# command -> stage where it becomes real (for stub messages)
STUB_STAGES = {"repro": 1, "fix": 2, "diff": 3, "approve": 4, "rollback": 4, "deploy": 4}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fde", description="ticket-to-fix pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_submit = sub.add_parser("submit", help="validate a ticket and start a run")
    p_submit.add_argument("ticket", help="path to ticket.md")

    p_status = sub.add_parser("status", help="show run state and recent events")
    p_status.add_argument("run_id", help="run id, e.g. 20260813-131633-6289")

    for name, stage in STUB_STAGES.items():
        sub.add_parser(name, help=f"{name} (not implemented until stage {stage})")
    return parser


def cmd_submit(args) -> int:
    ticket_path = Path(args.ticket)
    try:
        meta = parse_ticket(ticket_path)
    except TicketError as e:
        print(f"validation error: {e}", file=sys.stderr)
        return 1
    run_id = new_run_id()
    d = run_dir(run_id)
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
    for name in ("ticket.md", "diff.patch", "repro_test"):
        p = RUNS_DIR / run_id / name
        if p.exists():
            print(f"  {name}: {p}")
            found = True
    if not found:
        print("  (none yet)")
    return 0


def cmd_stub(args) -> int:
    print(f"not implemented in stage {STUB_STAGES[args.command]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"submit": cmd_submit, "status": cmd_status}.get(args.command, cmd_stub)
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
