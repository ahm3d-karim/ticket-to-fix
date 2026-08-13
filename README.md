# Ticket-to-Fix

An AI agent that fixes bug tickets — and a harness that proves the fix and catches the agent trying to cheat.

A CLI-first pipeline: ticket in → bug reproduced in a sandbox → an agent fixes it → tests verify the fix → a human approves → deployed with rollback armed. Every step audit-logged.

> **Status: portfolio piece.** Built to demonstrate agentic automation that can be trusted — it works end-to-end and is fully testable, but it is not a supported product. Known limits: the sandbox is a git worktree with timeouts (not a container), only the codex agent backend is implemented, and there is no license yet. Full story: [docs/CASE_STUDY.md](docs/CASE_STUDY.md).

## Quickstart

Requirements: `uv`, `node >= 18`, `git`. The agent backend needs a model key (see below); everything else needs nothing but Python's stdlib.

```bash
git clone https://github.com/ahm3d-karim/ticket-to-fix
cd ticket-to-fix
uv sync --extra test

# the whole pipeline on one fixture, end to end (a few minutes — agent round-trips dominate)
bash acceptance.sh

# or drive it by hand
uv run fde submit demo-app/ticket.md
uv run fde status <run-id>
```

### Bring your own key

The agent step shells out to an external agent CLI (codex by default). Point codex at any OpenAI-compatible endpoint via `~/.codex/config.toml` and make the API key available to the process. The harness, gates, audit log, deploy, and rollback are deterministic — no key, no network.

## CLI

| command | what it does |
|---|---|
| `fde submit <ticket.md>` | validate a ticket and start a run |
| `fde status <run>` | show run state and recent events |
| `fde repro <run>` | agent writes a failing repro test; the harness verifies it |
| `fde fix <run>` | agent fixes until repro + suite pass; security gates run |
| `fde diff <run>` | print the evidence package |
| `fde approve <run>` | human approval gate — nothing deploys without it |
| `fde deploy --preview/--prod <run>` | serve the fix, curl health-check it |
| `fde rollback <run>` | revert the fix on prod, verify pre-fix behavior |

## How it works

1. A ticket is a plain markdown file. `fde submit` *is* the ticketing system — no Jira, no webhooks.
2. The agent writes a single failing test that reproduces the ticket's symptom.
3. A **3-state harness** judges the test — never the agent's word:
   - **A** — the test fails on the buggy code AND its output contains the ticket's symptom (fails for the right reason)
   - **B** — the test passes with the known-good patch applied
   - **C** — the full suite passes with the fix in, and the worktree is untouched
4. Security gates scan the diff (secrets/PII, dangerous calls), then the run waits for a human `approve`.
5. Deploy fast-forwards the `prod` branch and health-checks it; rollback is one `git revert` + restart, verified the same way.
6. Every event lands in `runs/<run_id>/run.jsonl`. The audit trail is a feature, not a byproduct.

## Fixtures & bench

Four buggy repos, each with a `gold.patch` (the known fix), a `ticket.md`, and an `fde.yaml`. Real logic defects, not syntax errors.

| fixture | tier | bug | symptom |
|---|---|---|---|
| `tier1_checkout` | 1 — one file | flat $0.05 tax instead of 5% | `total should be 31.5` |
| `tier2_billing` | 2 — cross-file | hardcoded 15% ignores config's 0.18 | `invoice 100 should be 118` |
| `tier3_ingest` | 3 — invisible | row-7 error swallowed by `.catch(() => {})` | `row 7 malformed` |
| `demo-app` | "production" | config tax rate silently ignored | `total should be 118` |

Full pipeline results (verified 2026-08-14): every fixture fixed in one fix round. The hardest — tier3, a bug with zero visible symptoms — was reproduced and fixed in one attempt + one round. Deploy/rollback demonstrated live: prod served the fix (total 118), rollback restored the bug (115, by design). The tool itself: 52/52 tests pass; `acceptance.sh` passes end-to-end in ~2 minutes.

## Security model

- The agent works in a git worktree with timeout-bounded subprocesses — not a container. Documented limitation.
- The harness treats the agent as an adversary. During development the agent tried three ways to fake a pass: rewriting the tracked test (closed with `reset --hard` + `clean -fd`), committing mid-run (closed with a baseline-HEAD guard), and `git update-index --skip-worktree` (closed structurally — the regression check runs without the agent's file present). Gates the agent can observe are gates the agent can defeat.
- No surviving run log contains an actual tampering event — the guards are prophylactic.
- Nothing ships without `approve`; rollback is armed from the moment anything deploys.

## Layout

```
fde/            the pipeline (stdlib-only Python)
fixtures/       three buggy fixture repos
demo-app/       the "production" target for the full loop
tests/          pytest suite (52 tests, no network, no keys)
docs/           case study
STATUS.md       run evidence ledger
acceptance.sh   one-command end-to-end demo
```

## Status

Portfolio piece (2026-08-14). Built, verified, documented. Not planned: multi-user support, web UI. Roadmap (not built): `fde bench` (pass-rate across fixtures), a second agent backend (pluggable via `FDE_AGENT_BACKEND`), a Docker sandbox. No license yet — ask before reusing.
