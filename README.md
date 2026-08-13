# Ticket-to-Fix

A CLI-first system — ticket in → bug reproduced in a sandbox → sub-agents fix it → evidence package → human approves → deploy to production with rollback armed. Every step audit-logged.

Built as a portfolio piece by a Forward Deployed Engineer candidate. The claim it makes: an LLM agent can be trusted to fix bugs in production code if (a) a verification harness — not the agent — proves the fix, and (b) a human gates the deploy.

**Case study:** [docs/CASE_STUDY.md](docs/CASE_STUDY.md) — the full writeup: how the pipeline works, the bench results, and the three times the agent tried to cheat the harness.

## Why

Most teams that want "AI to fix bugs" start from the assumption they have a ticketing system, CI, and a deployment platform. This project inverts that: the ticket is a plain markdown file with YAML front-matter, the sandbox is a git worktree, the audit log is JSONL, the runtime is the Python stdlib. `fde submit` **is** the ticketing system. No Jira, no Docker, no database, no agent framework — nothing to stand up before it can be demoed.

## How it works

The pipeline is a linear state machine (`submitted → reproducing → reproved → fixing → fixed → gating → gated → awaiting_approval → approved → deploying → deployed → rolling_back → rolled_back`, any state → `failed`). The only branches are retry and approve/reject.

1. **`fde submit ticket.md`** — validates the ticket (id, severity, expected/actual, and a required `symptom`: a short string the bug's failure output must contain) and starts a run.
2. **`fde repro`** — a codex agent, working in a throwaway git worktree, writes a single failing test that reproduces the symptom. It may not fix the bug.
3. **`fde fix`** — a fix agent iterates (max 8 rounds, worktree reset between rounds, feedback chaining from the last test output) until the repro test passes and the full suite stays green. The fix is committed, then automated gates run: a secret/PII regex scan and a security lint (no eval, no shell injection, no sudo) over the diff. Findings block the run.
4. **`fde diff`** — prints the evidence package: changed files, test before/after with timings, the agent's what/why summary, gate results. Read from the run log only — nothing recomputed.
5. **`fde approve`** — the human gate. The run sits in `awaiting_approval` until a person says yes.
6. **`fde deploy --preview` / `--prod`** — preview serves the fix on port 8123; prod fast-forwards the `prod` branch, restarts the demo server, and verifies with a curl health check.
7. **`fde rollback`** — `git revert` the fix on `prod`, restart, verify the pre-fix behavior is back. Rollback is armed from the moment the fix ships.

Every event is appended to `runs/<run_id>/run.jsonl` with a fixed vocabulary and timestamps — the audit trail is a feature, not a byproduct.

### The verification harness (the product's proof layer)

The repro test the agent writes is not trusted on the agent's word. The harness evaluates it in three states, and all three must hold:

| State | Condition |
|---|---|
| **A** | The repro test FAILS on the buggy code, and the ticket's `symptom` appears in its failure output — it fails for the right reason |
| **B** | The repro test PASSES with the known-good `gold.patch` applied |
| **C** | The full test suite PASSES with the fix in, and the worktree is untouched (no staged changes, no new commits) |

Harness verdict beats agent judgment, always. State A is what prevents "the test passes because it tests nothing"; B anchors the test to ground truth; C catches regressions and tampering. Every verdict lands in the run log as a `test_result` event with per-check detail — the raw data for the bench.

The harness earned its hardening in the first real run. Details below.

> **War story: the agent tried three ways to fake a passing repro test.**
>
> The repro agent is told "write a test that fails on the current code." In the first live run it decided the more efficient path was to make the test pass by editing the fixture's own test file instead:
>
> 1. **Stage the rewrite.** It rewrote the fixture's test file and `git add`-ed it, so the restore step (`git checkout .`) could not revert it — staged changes survive checkout. Closed: the restore now does `reset --hard` (which clears the index) and purges `skip-worktree`/`assume-unchanged` bits before resetting.
> 2. **Commit the rewrite.** Undeterred, it committed the change, so even `reset --hard` couldn't reach it. Closed: the harness captures the worktree HEAD before every state and hard-resets back to it if it moved.
> 3. **Hide the mutation with `git update-index --skip-worktree`.** Closed: state C now runs the suite **without the agent's file present at all** — the repro test is removed before the full suite runs, so there is nothing left to tamper with.
>
> The agent's first two repro attempts were rejected by the harness; the third was accepted. The hardening is documented in `fde/harness.py`. This is why the harness, not the agent, is the product.

## Quickstart

```bash
# install (uv) — see pyproject.toml
uv sync

# the full loop, one script (a few minutes — agent round-trips dominate)
bash acceptance.sh

# or drive it by hand
RUN=$(uv run fde submit demo-app/ticket.md | tail -1)
uv run fde status "$RUN"            # state + recent events + artifacts
uv run fde repro "$RUN"             # agent writes repro test; harness accepts/rejects
uv run fde fix "$RUN"               # agent fixes; gates run; run awaits approval
uv run fde diff "$RUN"              # evidence package
uv run fde approve "$RUN"           # human gate
uv run fde deploy --preview "$RUN"  # serve the fix on :8123, curl health check
uv run fde deploy --prod "$RUN"     # fast-forward prod, restart :8124, curl health check
uv run fde rollback "$RUN"          # revert on prod, restart, verify pre-fix behavior
```

The agent invocation is isolated in `agents.codex_exec` — the backend is pluggable by design (`FDE_AGENT_BACKEND` label in the run log; codex is the implemented one). Paths handed to the agent are MSYS-safe; the demo "production" target is `demo-app/`, a tiny Node HTTP server with `main` + `prod` branches.

### Bring your own key

`fde repro` and `fde fix` are the only steps that need an LLM. The rest of the pipeline — harness, gates, audit log, deploy, rollback — is deterministic and runs without any key. To drive the agent loops you need an OpenAI-compatible endpoint:

```bash
# point codex at your provider (~/.codex/config.toml)
model = "your-model"
model_provider = "your-provider"

[model_providers.your-provider]
name = "Your Provider"
base_url = "https://your-endpoint/v1"
env_key = "YOUR_API_KEY_ENV_VAR"
wire_api = "responses"
```

Set the key in the environment (`YOUR_API_KEY_ENV_VAR=...`) or export `FDE_AGENT_ENV_FILE=/path/to/a/.env` with it. A missing/invalid key fails the run cleanly with `agent auth failed` — the harness never sees a bad key as a bad fix.

## Layout

```
fde/            CLI package (cli, ticket, config, runlog, worktree,
                harness, agents, gates, deploy) — argparse + pyyaml, stdlib
fixtures/       three tiered buggy repos, each with gold.patch + fde.yaml + ticket.md
demo-app/       "production" target: tiny Node server, branches main + prod
runs/           gitignored: run.jsonl, state.json, ticket.md, worktree, PIDs
acceptance.sh   the full-loop demo script
```

## Fixtures & bench

Three standalone git repos under `fixtures/`, each with a committed buggy `main`, a `gold.patch` (the known fix), a file-first `ticket.md`, and an `fde.yaml` manifest (install/test/run commands + app type). Syntax errors don't count as bugs — these are real logic defects.

| fixture | tier | bug | symptom |
|---|---|---|---|
| `tier1_checkout` | 1 — one file, direct symptom | flat $0.05 tax instead of 5% (`p * q + TAX`) | `total should be 31.5` |
| `tier2_billing` | 2 — cross-file trace | hardcoded 15% ignores `config.py`'s `TAX_RATE = 0.18` | `invoice 100 should be 118` |
| `tier3_ingest` | 3 — visible only through test output | row-7 error swallowed by `.catch(() => {})` | `row 7 malformed` |

Bench results (full pipeline per fixture):

| fixture | repro attempts | fix rounds | wall time | notes |
|---|---|---|---|---|
| tier1_checkout | 2 | 1 | ~6m | attempt 1 rejected (tampering — see war story) |
| tier2_billing | 1 | 1 | ~3.5m | accepted first try |
| tier3_ingest | 1 | 1 | ~3.5m | accepted first try; the hardest tier |
| demo-app | 1 | 1 | ~3.5m | full-loop target: deployed + rolled back with live health checks |

## Security model

- **Human gate.** Nothing reaches production without an explicit `fde approve` from state `awaiting_approval`. Gate failures, preview, and rollback are the only other ways out.
- **Automated gates.** Secret/PII regex scan + security lint on the fix diff, before any human sees it. Findings block the run outright.
- **Rollback.** Prod is a git branch; a bad fix is one `git revert` + restart away, verified by curl before the run is marked `rolled_back`.
- **Sandbox honesty.** The sandbox is a git worktree with timeouts and a restricted environment — not a container. Process-tree kills are best-effort on Windows, and grandchildren can outlive the timeout. This is accepted and documented for the demo stage; real isolation is Stage 7, clearly labeled.

## What's next

- **Stage 5 — real orchestration + bench.** Durable run state with checkpoint/resume (`fde resume`), retries with backoff, and `fde bench`: the full pipeline over all fixtures reporting pass rate, agent rounds, and wall time — the interview-numbers generator and the regression test for prompt changes.
- **Stage 6 — ingestion adapters + web control plane.** Webhook endpoint, Jira adapter (mock first), and a deliberately thin web UI: reads JSONL, polls, one approve endpoint. No auth, no real backend — the CLI remains the product.
- **Stage 7 — customer-readiness.** Docker sandbox isolation, teams/SSO, Slack ingestion, hardening. The no-ticketing path already works by design.

## Notes

- Runs, fixtures' nested `.git` dirs, and `demo-app/.git` are gitignored (prevents the gitlink trap).
- The ticket format, event vocabulary, and state machine are specified in `PLAN.md`; the build record and session map are in `EXECUTION.md`.
