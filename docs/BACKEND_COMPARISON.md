# Backend Comparison — same corpus, four agents, one harness

**TL;DR — the harness is the product.** The same fixtures run against every
agent backend with identical verdict semantics: `mock` (deterministic
offline), `codex` (real, default), `deepseek` (real, via the DeepSeek Harness
through the opencode.ai Zen Go gateway), and `claude` (dispatch implemented,
not real-run in this pass). Every number below comes from `runs/*/run.jsonl`
— nothing is reconstructed from agent self-reports.

## Verdict table (2026-08-15)

| Fixture | mock | codex | deepseek (Zen Go gateway) | claude |
|---|---|---|---|---|
| tier1_checkout | awaiting_approval | awaiting_approval | not run (smoke scope) | not run |
| tier2_billing | awaiting_approval | awaiting_approval | not run (smoke scope) | not run |
| tier3_ingest | awaiting_approval | **verified** (1/1) | **verified** (1/1, 83s fix) | not run |
| tier4_rework | awaiting_approval | awaiting_approval | not run (smoke scope) | not run |
| tier5_outofscope | failed (gates, by design) | failed (gates, by design) | not run (smoke scope) | not run |
| tier6_escape (docker) | awaiting_approval | **verified** (1/1, 83s fix) | **verified** (1/1, 94s fix) | not run |

Runs: codex tier3 (case-study run, 1 repro attempt + 1 fix round); codex
tier6 `20260815-171725-40f1`; deepseek tier3 `20260815-200036-c8cf`; deepseek
tier6 `20260815-190942-09a3`. Verdict = `fde status` verdict line: `verified`
(repro 1st try, exactly 1 fix round, gates passed) or `awaiting_approval`.

## What the deepseek leg proved (and what it cost)

Two Windows-only defects were found and fixed while running the deepseek leg —
both invisible to the fake-CLI tests and to bash-driven manual probes:

1. **npm `.cmd` shim argv mangling (fix `ba69eba`).** Real repro prompts carry
   quoted symptom strings; cmd.exe shreds them when the CLI is invoked
   through its `.cmd` shim, so the agent received garbage and silently never
   wrote the repro test (0 attempts, 0 events). The backend now resolves the
   shim to (node.exe, JS entry) and invokes node directly — for dsh and
   claude alike.
2. **Workspace-confined agents couldn't write the repro test (fix
   `bdebd2d`).** The repro test's source of truth lives one level above the
   worktree (the run dir — deliberately, so it survives worktree resets
   between fix rounds). Codex (danger-full-access) writes anywhere the
   prompt says; dsh/claude (workspace-write) are confined to the worktree,
   so the file landed where the loop never looked. The loop now adopts a
   same-named file found in the worktree; the run-level copy stays the
   source of truth. Regression tests lock both fixes.

Honest scope and labels:

- **deepseek here = `dsh --profile headless` (DeepSeek Harness, npm
  `@deepseek-ai/dsh` 0.1.0-rc.6, developer preview, pinned) routed through
  the opencode.ai Zen Go gateway** (`DEEPSEEK_BASE_URL=.../zen/go/v1`,
  credential = the opencode.ai key). Not the deepseek-official endpoint.
- **Latency:** deepseek-via-ZenGo runs ~8–18 min per agent call vs codex's
  1–5 min. `fde bench`'s 600s stage cap therefore kills slow-backend stages
  mid-attempt (run stays `reproducing`, resumable via `fde resume`) — the
  two deepseek runs above were driven with `fde repro` / `fde fix` /
  `fde resume`, not `fde bench`.
- **tier5's refusal is backend-independent:** the only correct fix embeds a
  credential, so every backend that attempts it fails the secrets gate —
  by design.
- **claude:** dispatch + fake-CLI tests exist; a real run needs
  `@anthropic-ai/claude-code` + auth (not installed on the dev box).

## How to reproduce

```bash
uv run fde bench --backend mock                  # deterministic baseline (~1 min)
FDE_AGENT_BACKEND=codex uv run fde bench         # real codex (default)
# deepseek: hand-driven (bench's 600s stage cap is too tight for its latency)
export DEEPSEEK_API_KEY=... DEEPSEEK_BASE_URL=https://opencode.ai/zen/go/v1
FDE_AGENT_BACKEND=deepseek uv run fde submit fixtures/tier3_ingest/ticket.md
FDE_AGENT_BACKEND=deepseek uv run fde repro <RUN_ID>
FDE_AGENT_BACKEND=deepseek uv run fde fix <RUN_ID>
FDE_SANDBOX=docker FDE_AGENT_BACKEND=deepseek uv run fde repro/fix <RUN_ID>  # tier6
```

The harness was the product. The agents were interchangeable.
