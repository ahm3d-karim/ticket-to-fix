# STATUS — full pipeline run (2026-08-13)

> Written by the Discord-session agent after the autonomous chain completed.
> Any agent (CLI/PC, Discord, codex) opening this repo: read this first.
> Source of truth: `runs/<run_id>/run.jsonl` + `runs/_chain2.log`.

## Phase 3: Production security — SHIPPED (2026-08-15)

Five hardening layers, each verified by fresh tests; suite went 118 → **178**
(commits `f2a13e9`, `f5cd2be`, `72e5a82`, `8090a86`, `85930fa`). Threat
model + honest limits: `docs/PRODUCTION_SECURITY.md`.

- **Layer 1+2 (`f2a13e9`) — mandatory sandbox + hardening.** `FDE_SANDBOX`
  now accepts `required` (docker mandatory — daemon down fails fast naming
  the mode, no host fallback) and `host` (explicit, one-time stderr
  warning). Containers get `--memory` (1g), `--cpus` (2),
  `--pids-limit` (256), `--read-only` + `--tmpfs /tmp`; env-overridable via
  `FDE_SANDBOX_MEMORY/CPUS/PIDS`. Real-docker tests run against the hardened
  container.
- **Layer 3 (`72e5a82`) — tamper-evident audit + named approval.** Every
  run.jsonl line carries the sha256 of the previous line (chain survives
  cross-process appends); `fde audit <run>` validates (exit 0/1);
  `fde approve [--approver NAME]` records who (flag → `FDE_APPROVER` → git
  identity).
- **Layer 4 (`f5cd2be` + `8090a86`) — secret scrubbing.** `redact_secrets`
  (API keys, AWS/GitHub tokens, PEM blocks, high-entropy runs),
  `scrub_ticket`, `redact_event_data`. Wired at the run-log write path
  (redaction BEFORE hashing — chain stays valid; `secrets_redacted` marker)
  and into both prompt builders (secrets never reach the model API).
  Pure-hex commit SHAs are exempt — the audit trail's hashes are evidence,
  verified against real run logs (0 false positives).
- **Layer 5 (`85930fa`) — agent-in-container.** `FDE_AGENT_CONTAINER=1`
  runs the agent CLI itself in the `fde-agent` container
  (`Dockerfile.agent`: node 22-slim + pinned dsh/codex, keys at runtime
  only): worktree mount, explicit env allowlist (API keys + proxy vars),
  same resource policy, egress via `FDE_AGENT_NETWORK` (domain
  allowlisting = operator's job, `FDE_AGENT_PROXY` supported), watchdog
  `docker kill`s the container on timeout. Bare binary names in-container —
  no .cmd shim bug class.

## Phase 2: Docker sandbox — SHIPPED (2026-08-15)

The sandbox is now literal. `FDE_SANDBOX=docker` routes every harness command
through an ephemeral `fde-sandbox:latest` container (built from the repo
`Dockerfile`: node 22 + git + python3 + pytest + bash; commits `183a77f` +
`715b21c`) with `--rm`, `--network none`, `--cap-drop ALL`,
`--security-opt no-new-privileges` (`9e02b8b`). Daemon down or missing docker
CLI → clear `RuntimeError` naming `FDE_SANDBOX` — fail fast, never silent
fallback.

- **Git steering (`0d6477d`)** — the worktree mounts at `/workspace`; the
  fixture's `.git` dir mounts at `/fde/gitdir`, and `GIT_DIR`/`GIT_WORK_TREE`
  env steering makes git work in-container WITHOUT rewriting the worktree's
  `.git` file. `gold.patch` is copied into the worktree as `.fde-gold.patch`
  so `git apply` can see it.
- **tier6_escape (`d57384a`) — the depth proof.** Its test suite asserts
  escape attempts (write outside `/workspace`, network connect) are DENIED:
  green in-container, red on host. The bench skips it in host mode; the CI
  sandbox job proves it on every push.
- **Tests: 116 passed** (was 102 before Phase 2), including real-docker
  integration tests that skip cleanly when no daemon is reachable (`9111b02`).

### Bench — host mode (`FDE_AGENT_BACKEND=mock`) — exit 0

| fixture | repro attempts | fix rounds | outcome |
|---|---|---|---|
| tier1_checkout | 1 | 1 | awaiting_approval |
| tier2_billing | 1 | 1 | awaiting_approval |
| tier3_ingest | 1 | 1 | awaiting_approval |
| tier4_rework | 1 | 1 | awaiting_approval |
| tier5_outofscope | 1 | 1 | **refused at gates (secrets x1 — BY DESIGN)** |
| demo-app | 1 | 1 | awaiting_approval |
| tier6_escape | — | — | skipped (host mode — escape-denial assertions only hold in-container) |

### Bench — docker mode (`FDE_SANDBOX=docker FDE_AGENT_BACKEND=mock`) — exit 0, ~133s

| fixture | outcome |
|---|---|
| tier1_checkout | awaiting_approval — identical to host |
| tier2_billing | awaiting_approval — identical to host |
| tier3_ingest | awaiting_approval — identical to host |
| tier4_rework | awaiting_approval — identical to host |
| tier5_outofscope | refused at gates — same refusal as host; the gate finds the secret **x2** in-container vs x1 (CRLF/mixed-line-endings artifact of the `--ignore-whitespace` apply) |
| demo-app | awaiting_approval — identical to host |
| tier6_escape | awaiting_approval (1 repro attempt, 1 fix round) — escape-denial suite green |

`acceptance.sh` still PASSES in host mode (deploy → rollback → verdict
verified) — the one-command demo needs no Docker.

## Result: ALL GREEN ✅

Full autonomous chain (submit → repro → fix → gates → deploy loop) completed
2026-08-13 ~18:55. Every fixture fixed by codex, verified by the 3-state
harness, gates passed.

| Fixture | Run ID | Repro attempts | Fix rounds | State |
|---|---|---|---|---|
| tier1_checkout (node, one-line bug) | `20260813-174120-2226` | 2 | 1 | `awaiting_approval` |
| tier2_billing (python, cross-file config bypass) | `20260813-183156-c96e` | 1 | 1 | `awaiting_approval` |
| tier3_ingest (node, invisible bug — swallowed error) | `20260813-183354-0bf0` | 1 | 1 | `awaiting_approval` |
| demo-app (node, "production" target) | `20260813-183644-139e` | 2 | 1 | `rolled_back` |

**The headline number: tier3 — the invisible-bug fixture — repro'd and fixed in
1 attempt + 1 round.** The agent found the `.catch(() => {})` that swallowed
row-7 errors with zero visible wrong output. That's the strongest bench result.

## Deploy/rollback loop (demo-app, run 20260813-183644-139e) — verified live

- `fde approve` → approved (auto, to exercise the loop)
- `fde deploy --preview` → http://127.0.0.1:8123 healthy
- `fde deploy --prod` → http://127.0.0.1:8124 healthy, prod_head=`c9ec924b6c64`
- `fde rollback` → healthy, revert_head=`82f413354908`
- demo-app `prod` branch now points at the revert commit; server serves buggy
  code again by design (rollback demonstrated).

## Historical notes (why there are stale runs)

- `20260813-174734-2ea7` (`reproducing`), `20260813-173226-c66c`
  (`reproducing`), several `failed` runs: corpses from the original chain
  killed when its parent CLI session ended (18:0x), plus earlier debugging
  (harness state-C gold-reapply bug — fixed in commits 9e195d0/b5da0d7).
- `20260813-182903-eedd` / `-904-af3c` / `-905-469c` (`failed`): first restart
  attempt failed with `[WinError 2]` — codex not on PATH in the background
  shell. Fixed: codex lives at
  `C:\Users\Ahmad Karim\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe`
  (add to PATH when spawning from scripts; `codex-cli 0.147.0`).
- `20260813-141023-9189` (`awaiting_approval`): older run from earlier
  session, also green-capable.

## Pending / next steps

1. **Human approval gates** — tier1/tier2/tier3 runs sit in
   `awaiting_approval` by design. Approve with `fde approve <run_id>` if you
   want them deployed; or leave as demo state.
2. **GitHub push DONE** — repo is public at
   https://github.com/ahm3d-karim/ticket-to-fix (HEAD `48eb3ff`: README +
   fixtures bench; auth-error handling fix). STATUS.md itself is now committed
   so the repo's own story matches reality.
3. `fde bench` (full-corpus pass-rate command) still roadmap — Stage 5.

## Re-verified 2026-08-13 (post-push)

- `pytest -q -x`: **52 passed** in ~66s — harness suite green at HEAD.
- CLI surface confirmed: `submit status repro fix diff approve rollback deploy`.
- Run evidence intact: tier runs at `awaiting_approval`, demo-app at
  `rolled_back` (see table above).

## Verification commands for any agent

```bash
cd ticket-to-fix
uv run fde status 20260813-183156-c96e   # tier2
uv run fde status 20260813-183354-0bf0   # tier3
uv run fde status 20260813-183644-139e   # demo-app (rolled_back)
cat runs/_chain2.log                     # full chain transcript
cd demo-app && git log --oneline prod -5 # deploy/revert evidence
```

Harness 3-state check semantics: A = repro test fails on buggy code with
ticket symptom, B = passes with gold applied, C = full suite green with gold.
State C re-applies gold (regression fix b5da0d7 — strict suites were spuriously
rejecting good repro tests).
