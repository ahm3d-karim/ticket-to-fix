#!/usr/bin/env bash
# acceptance.sh — full-loop acceptance for the Ticket-to-Fix pipeline (S4T4).
# demo-app: submit → repro → fix → diff → approve → preview → prod → rollback.
# Run from the repo root. Requires: uv, node >= 18, codex CLI authenticated.
set -euo pipefail
cd "$(dirname "$0")"

echo "== submit =="
RUN=$(uv run fde submit demo-app/ticket.md | tail -1)
echo "run: $RUN"

echo "== repro =="
uv run fde repro "$RUN"

echo "== fix =="
uv run fde fix "$RUN"

echo "== diff (evidence) =="
uv run fde diff "$RUN"

echo "== approve =="
uv run fde approve "$RUN"

echo "== deploy --preview =="
uv run fde deploy --preview "$RUN"

echo "== deploy --prod =="
uv run fde deploy --prod "$RUN"
echo "-- prod health:"
curl -s "http://127.0.0.1:8124/tax?amount=100"
echo

echo "== rollback =="
uv run fde rollback "$RUN"
echo "-- prod after rollback:"
curl -s "http://127.0.0.1:8124/tax?amount=100"
echo

echo "== status =="
uv run fde status "$RUN" | head -12
echo "ACCEPTANCE PASS"
