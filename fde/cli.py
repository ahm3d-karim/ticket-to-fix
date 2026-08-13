"""fde CLI — argparse entry point.

Stage 0: submit/status real; diff/approve/rollback/deploy/repro/fix stubs.
"""
import argparse

COMMANDS = ["submit", "status", "diff", "approve", "rollback", "deploy", "repro", "fix"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fde", description="ticket-to-fix pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name in COMMANDS:
        sub.add_parser(name, help=f"{name} (not implemented yet)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"command '{args.command}' not implemented yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
