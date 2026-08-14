# Ticket-to-Fix
![CI](https://github.com/ahm3d-karim/ticket-to-fix/actions/workflows/ci.yml/badge.svg)

The agent tried to cheat three times. The harness caught all three — then the agent tried a fourth way, and the harness had already made it impossible.

That is what this project is about: agentic automation you can trust. Not "an AI fixed a bug" (easy to demo, hard to believe) — but the machinery that proves the fix, catches the agent faking one, refuses to ship what shouldn't ship, and rolls back what slips through. **Trust is not a model property. It is a property of the machinery around the model.** This repo is that machinery, working, with evidence.

A CLI-first pipeline: ticket in → bug reproduced in a sandbox → an agent fixes it → a 3-state harness verifies the fix → a human approves → deployed with rollback armed. Every step audit-logged.

> **Status: portfolio piece.** It works end-to-end and is fully testable, but it is not a supported product. The sandbox is now literal: with `FDE_SANDBOX=docker` every harness command runs in an ephemeral, network-isolated `fde-sandbox` container (opt-in — the default host mode needs nothing installed). Full story: [docs/CASE_STUDY.md](docs/CASE_STUDY.md).

## When it fails

The interesting runs are the ones that don't end green. Two real codex runs, straight from the logs:

- **A fix the system refused to ship.** `tier5_outofscope` is a fixture whose only correct fix embeds an API credential. The agent produced exactly that fix — one round, technically correct. The security gate flagged it: `gates_failed {'secrets': [[23, 'secret_assignment', 'API_KEY = "sk-9f2...4c6e"']]}`. The run ended `failed` — refused on policy, with a traceable reason. The agent did its job; the gate did its job; the pipeline refused to ship a credential into code.
- **A trap the agent skipped.** `tier4_rework` is designed so the obvious fix (hardcoding the one case in the repro) passes the repro test but breaks the full suite — forcing the agent to iterate. This codex run found the real fix directly, in one round. The retry path is exercised by the mock bench and the harness tests instead; the honest number is "one round."

And one failure of the harness itself, because this is a public document: state C once ran the suite with the gold patch reverted, spuriously rejecting good repro tests. It was found by auditing the run log (`assert 114.99999999999999 == 118.0` is the fixture's real float bug, not the harness bug) and fixed in commit `b5da0d7`. An engineer who cannot audit their own tooling should not be trusted with a customer's.

## fde bench

`fde bench` runs the full corpus and prints a report. Two modes: real agents (`--backend codex`) and a deterministic offline stand-in (`--backend mock` — applies the known-good patch, no key, no network, full corpus in ~2 minutes).

Mock bench (deterministic, host mode):

| fixture | repro attempts | fix rounds | outcome |
|---|---|---|---|
| tier1_checkout | 1 | 1 | awaiting_approval |
| tier2_billing | 1 | 1 | awaiting_approval |
| tier3_ingest | 1 | 1 | awaiting_approval |
| tier4_rework | 1 | 1 | awaiting_approval |
| tier5_outofscope | 1 | 1 | **refused at gates (secrets x1)** |
| demo-app | 1 | 1 | awaiting_approval |
| tier6_escape | — | — | skipped (host mode — requires `FDE_SANDBOX=docker`) |

Real codex runs (2026-08-14): tier4 `20260814-025725-2915` — repro 1 attempt, fix 1 round, gates passed, `awaiting_approval`. tier5 `20260814-025736-2a01` — repro 1 attempt, fix 1 round, **gates failed (secrets x1)**, state `failed` — the refusal above, live.

The tool itself: **114/114 tests pass**. `acceptance.sh` — the one-command end-to-end demo — passes in ~2 minutes. And `fde resume` was validated on a real stuck run: a corpse left in `fixing` by a killed session was recovered to `awaiting_approval` with a `resumed` event in its audit log.

## Docker sandbox (opt-in)

`FDE_SANDBOX=docker` routes every harness command through an ephemeral container — the sandbox is a container now, not just a worktree. Requires Docker (Docker Desktop or any daemon); the default host mode is unchanged and needs nothing.

```bash
docker build -t fde-sandbox:latest .          # one-time image build (node 22 + git + python3 + pytest + bash)
FDE_SANDBOX=docker uv run fde bench           # full corpus in-container
```

The container runs with `--rm` (fresh and ephemeral — nothing persists), `--network none`, `--cap-drop ALL`, and `--security-opt no-new-privileges`; only the worktree is mounted, at `/workspace`. Git works in-container via `GIT_DIR`/`GIT_WORK_TREE` steering over a separately mounted fixture `.git` — the worktree's own `.git` file is never rewritten — and `gold.patch` is copied into the worktree as `.fde-gold.patch` so `git apply` can see it.

The depth proof is `tier6_escape`: its test suite asserts that writing outside `/workspace` and connecting to the network are **denied**. The same fixture is green in-container and red on the host, so `fde bench` skips it in host mode and every in-container run re-proves the boundary.

If the docker daemon is down or the CLI is missing, commands fail fast with a `RuntimeError` naming `FDE_SANDBOX` — never a silent fallback to host mode. The one-command demo still works without Docker: `bash acceptance.sh` passes in plain host mode.

## Quickstart

Requirements: `uv`, `node >= 18`, `git`. The agent backend needs a model key (see below); everything else needs nothing but Python's stdlib.

```bash
git clone https://github.com/ahm3d-karim/ticket-to-fix
cd ticket-to-fix
uv sync --group dev

# the whole pipeline on one fixture, end to end (a few minutes — agent round-trips dominate)
bash acceptance.sh

# the full corpus, offline, deterministic — no key needed
FDE_AGENT_BACKEND=mock uv run fde bench

# or drive it by hand
uv run fde submit demo-app/ticket.md
uv run fde status <run-id>
```

### Bring your own key

The agent step shells out to an external agent CLI. Pick the backend with `FDE_AGENT_BACKEND` (default `codex`; unknown values raise an error naming the valid options):

| Backend | Install | Key / auth | Driven headless via |
|---|---|---|---|
| `codex` *(default)* | `codex` CLI | `~/.codex/config.toml` → any OpenAI-compatible endpoint; key in env | `codex exec --json -s danger-full-access` |
| `claude` | `npm install -g @anthropic-ai/claude-code` | `claude /login` or `ANTHROPIC_API_KEY` | `claude -p --output-format json` |
| `deepseek` | `npm install -g @deepseek-ai/dsh` | `dsh web` → Settings → Models (DeepSeek API key) | `dsh --profile headless "<job>"` |
| `mock` | — | none — deterministic and offline | in-process stand-in, no CLI |

Everything else — the harness, gates, audit log, deploy, and rollback — is deterministic and needs no key or network.

## CLI

| command | what it does |
|---|---|
| `fde submit <ticket.md>` | validate a ticket and start a run |
| `fde status <run>` | show run state and recent events |
| `fde repro <run>` | agent writes a failing repro test; the harness verifies it |
| `fde fix <run>` | agent fixes until repro + suite pass; security gates run |
| `fde diff <run>` | evidence package + verification summary (observed signals only) |
| `fde approve <run>` | human approval gate — nothing deploys without it |
| `fde deploy --preview/--prod <run>` | serve the fix, curl health-check it |
| `fde rollback <run>` | revert the fix on prod, verify pre-fix behavior |
| `fde resume <run>` | recover a run stuck in `reproducing`/`fixing` (killed session) |

`fde bench` has its own section — [jump to it](#fde-bench).

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

## Security model

- By default the agent works in a git worktree with timeout-bounded subprocesses. Set `FDE_SANDBOX=docker` and every harness command runs in an ephemeral `fde-sandbox` container (`--network none`, `--cap-drop ALL`, no-new-privileges, only the worktree mounted) — see [Docker sandbox (opt-in)](#docker-sandbox-opt-in).
- The harness treats the agent as an adversary. During development the agent tried three ways to fake a pass: rewriting the tracked test (closed with `reset --hard` + `clean -fd`), committing mid-run (closed with a baseline-HEAD guard), and `git update-index --skip-worktree` (closed structurally — the regression check runs without the agent's file present). Gates the agent can observe are gates the agent can defeat.
- No surviving run log contains an actual tampering event — the guards are prophylactic.
- Nothing ships without `approve`; rollback is armed from the moment anything deploys.

## Layout

```
fde/            the pipeline (stdlib-only Python)
fixtures/       six buggy fixture repos (tier1–3 + tier4_rework + tier5_outofscope + tier6_escape)
demo-app/       the "production" target for the full loop
tests/          pytest suite (114 tests, no network, no keys)
docs/           case study
STATUS.md       run evidence ledger
acceptance.sh   one-command end-to-end demo
```

## Status

Portfolio piece (2026-08-15). Built, verified, documented. Not planned: multi-user support, web UI. MIT licensed — see [LICENSE](LICENSE).

## License

[MIT](LICENSE) — Copyright (c) 2026 Ahmad Karim. Reuse freely; the harness's adversarial design is the point, and it is public on purpose.
