# Ticket-to-Fix Pipeline — Build Plan (staged, incremental)

> Product in one line: **a CLI-first system — ticket in → bug reproduced in a sandbox → sub-agents fix it → evidence package → human approves → deploy to production with rollback armed.** Every step audit-logged.

## Design constraints (decided now, cheap to honor later)

1. **Tickets are plain artifacts** (markdown + JSON front-matter) from day 1. Jira / webhooks / Slack are *ingestion adapters*, never the core.
   → This is the answer to **"what if the customer has no ticketing system"**: `fde submit` IS their system. No Jira required, ever. The file-based ticket is the lowest-common-denominator entry point by design.
2. **Agent backend is pluggable** (`FDE_AGENT_BACKEND=codex|claude|openai`). Start with Codex CLI (installed, proven). Swap without rewiring.
3. **Every stage appends to a run log** (JSONL). The audit trail is a feature, not a byproduct.
4. **Sandbox honesty**: no Docker on the dev machine today → Stages 1–4 sandbox = git worktree + timeout-limited subprocess + restricted env. Real container isolation is a later stage, clearly labeled.
5. **No databases until Stage 6.** Run state lives in files (JSONL + git worktrees). Simple, inspectable, zero ops.
6. **The pipeline is a LINEAR state machine, not a DAG.** ticket → repro → fix → gates → approve → deploy → rollback, with retries at the repro/fix points. The only branches are retry and approve/reject. Don't build a DAG engine.
7. **Toolchain runs in git-bash on Windows.** Paths handed to agent prompts must be MSYS-safe (`C:/...` form, not `/c/...`); subprocess env is restricted and explicit.

## Run log schema (defined now, used from Stage 0)

- One JSONL file per run: `runs/<run_id>/run.jsonl`. Every stage appends lines.
- Line shape: `{"ts": "...", "run_id": "...", "event": "<type>", "data": {...}}`
- Event vocabulary (fixed; extend by adding, never renaming): `ticket_parsed`, `worktree_created`, `repro_test_written`, `test_result`, `fix_attempt`, `gates_passed`, `gates_failed`, `evidence_packaged`, `approved`, `rejected`, `deployed`, `rolled_back`, `agent_error`, `resumed`.
- Agent invocations additionally record: backend name + CLI version, prompt hash, rounds used, exit status. Audit-relevant, free to capture.

## Stages

### Stage 0 — Skeleton & ticket model (0.5 session)
- `fde` CLI scaffold: `submit`, `status`, `diff`, `approve`, `rollback` (stubs that exit 0)
- Ticket format `ticket.md` (front-matter: id, severity, system, expected/actual, **symptom**) + validation. `symptom` is required — a short string the repro test's failure output must match; it anchors the Stage 1 guard.
- Config file (repo path, agent backend, deploy targets). Per-repo manifest spec drafted: `fde.yaml` (`install_cmd`, `test_cmd`, `run_cmd`, `app_type`).
- Run log schema (above) implemented for `ticket_parsed`.
- **Done when:** `fde submit sample.md` prints a run ID; `fde status <id>` shows a persisted run file.
- **Not doing:** any agent logic.

### Stage 1 — Repro agent + verification harness (1–1.5 sessions)
- Role: ticket + repo → git worktree → run app/tests → capture failing behavior → write a failing test + repro notes.
- **Build the verification harness FIRST** — it is the product's proof of correctness. Given a repro test, evaluate it in 3 states:
  (a) on buggy code: must FAIL, and failure output must match the ticket's `symptom` (normalized substring match)
  (b) on fixed code (gold patch applied): must PASS
  (c) regression: full test suite passes with the fix in
  All three must hold, or the repro test is rejected and the agent retries (max 3). Harness verdict, not agent judgment, decides "fails for the right reason".
- Fixture corpus: 3 tiered buggy repos, each with: buggy version, gold patch, ticket (with symptom), `fde.yaml`.
  - Tier 1: bug in one file, direct symptom
  - Tier 2: bug requires cross-file trace
  - Tier 3: bug only visible through test-suite output
  - Syntax errors are not bugs (trivial for LLMs).
- **Done when:** on all 3 fixtures the harness accepts the repro test (fails for the right reason pre-fix, passes post-fix, no regressions).

### Stage 2 — Fix agent (2 sessions)
- Role: repro evidence → edit code until repro test passes → iterate (run tests, read failures, re-patch, max N rounds).
- **Agent invocation contract (fixed now):**
  - Non-interactive: `codex exec` (backend via `FDE_AGENT_BACKEND`), cwd = the worktree, never the main checkout.
  - Budgets: max 8 fix rounds, 10-min per-round timeout, overall token cap.
  - On timeout/error: log `agent_error`, retry once with a narrowed prompt (include failing test output), then fail the run.
  - Every attempt appends `fix_attempt` + `test_result` lines; the loop is resumable from the log.
  - Prompt includes: ticket, repro test, harness verdict, `fde.yaml` commands, repo tree.
- **Done when:** all 3 fixtures: harness 3-state green (repro red → green, no other tests broken), diff produced.
- **Not doing:** preview deploy or approval — next stage.

### Stage 3 — Evidence package + automated gates (1 session)
- `fde diff <run>`: changed files, test before/after (harness output), agent's what/why summary.
- Gates before human: (a) harness 3-state green, (b) secret/PII scan on diff — shell out to gitleaks (or rg patterns) for keys/tokens/emails; don't write a scanner, (c) security lint (no eval, no shell-injection, no sudo).
- **Done when:** planted secret in a fixture diff gets flagged; clean diff passes.

### Stage 4 — Preview deploy, approval, production + rollback (1–2 sessions)
- `fde deploy --preview`, `fde approve <run>`, `fde deploy --prod`, `fde rollback <run>`
- "Production" = a `prod` git branch + running demo server; rollback = revert commit + redeploy.
- **Done when:** full loop on a fixture: submit → status → diff → approve → prod → break something → rollback → green.

### Stage 5 — Real orchestration + bench (2 sessions)
- Role agents as separate stages, durable run state (JSONL), checkpoint/resume (`fde resume`), retries with backoff on agent failures.
- **`fde bench`**: run the full pipeline over all fixtures; report pass rate, agent rounds, wall time. This is the interview-numbers generator AND the regression test for prompt changes. The demo opener.
- **Done when:** kill the process mid-run → `fde resume` completes; `fde bench` reports a pass rate; run log is a complete audit trail.

### Stage 6 — Ingestion adapters + web control plane (2 sessions)
- Webhook endpoint (Express/Fastify) accepting tickets; Jira adapter (mock first, real later).
- Thin web UI — deliberately minimal: reads JSONL + polls + ONE approve endpoint. No auth, no real backend, no DAG visualization (it's a linear state machine; render a step list).
- **Done when:** ticket created in mock Jira → full pipeline → approve from web UI.

### Stage 7 — Customer-readiness (later, keep in mind)
- Docker sandbox isolation, teams/SSO, Slack ingestion, hardening. The no-ticketing path already works by design (file/CLI submit).

## Deliberately skipped (until a stage demands it)
- Agent frameworks (LangChain etc.) — build the runtime ourselves; that's the interview value
- Distributed systems, message queues, real databases
- Multi-tenant / multi-customer concerns

## Sequencing logic
Stages 1–4 are the "small tool": a CLI that fixes bugs with proof and a human gate. Stages 5–6 are the expansion: resilience (resume, bench) + enterprise surfaces (webhooks, Jira, web UI). Stage 7 is where "customer has no system" gets productized — but the architecture answers it from day 1.

Budget note: Stages 1–2 carry ~95% of the project's risk (LLM nondeterminism + harness debugging); they are budgeted at ~3.5 sessions total. Everything after is plumbing. If a deadline looms, cut Stage 6's web UI first — the CLI is the product.
