# Ticket-to-Fix — Execution Plan (Stages 0–4, small tool)

> **For Hermes:** implement this plan task-by-task (TDD, commit after every task, run tests before moving on).

**Goal:** Build the `fde` CLI through Stage 4 — ticket in → repro with proof → agent fix → gates → human approve → prod deploy + rollback — demo-ready on 3 fixtures + 1 demo app. No Docker, no DB, no agent frameworks.

**Architecture:** Python 3.11 CLI (stdlib + pyyaml) that orchestrates git worktrees, a 3-state verification harness, and non-interactive Codex CLI. Linear state machine (submitted → … → deployed → rolled_back). Every event appended to `runs/<run_id>/run.jsonl` (vocabulary fixed in PLAN.md).

**Tech Stack:** Python 3.11 + uv (env), pyyaml, pytest; Node ≥18 (fixtures use `node:test`); Codex CLI (agent backend); git worktrees; curl (health checks). All repo commands run through `bash -c` (toolchain is git-bash on Windows).

**Constraints recap (from PLAN.md):** file-first tickets; linear state machine — never a DAG; MSYS-safe paths (`C:/...` form) in anything handed to codex; harness verdict beats agent judgment; `symptom` field anchors all repro verification.

---

## Layout

```
C:\Users\Ahmad Karim\Documents\fde\ticket-to-fix\
├── PLAN.md
├── EXECUTION.md              (this plan)
├── pyproject.toml            # fde package + console script + dev deps
├── fde\                      # CLI package
│   ├── __init__.py
│   ├── cli.py                # argparse: submit/status/diff/approve/rollback/deploy/repro/fix
│   ├── ticket.py             # parse + validate ticket.md
│   ├── config.py             # ~/.fde.yaml + per-repo fde.yaml
│   ├── runlog.py             # JSONL events + state.json transitions
│   ├── worktree.py           # git worktree create/discard
│   ├── harness.py            # 3-state verification + run_cmd (Windows-safe)
│   ├── agents.py             # codex exec wrapper, repro + fix loops
│   ├── gates.py              # secret scan + security lint (regex, no deps)
│   └── deploy.py             # preview/prod/rollback + server process mgmt
├── fixtures\
│   ├── tier1_checkout\       # git repo (buggy main) + gold.patch + ticket.md + fde.yaml
│   ├── tier2_billing\
│   └── tier3_ingest\
├── demo-app\                 # "production" target: tiny Node server, branches main+prod
├── runs\                     # gitignored: per-run worktree, run.jsonl, state.json, PIDs
├── tests\
│   ├── test_ticket.py, test_runlog.py, test_config.py, test_worktree.py,
│   ├── test_harness.py, test_gates.py
│   ├── fixtures\sample.md    # sample ticket for Stage 0
│   └── scratch_repos\        # tiny git repos created by tests (tmp)
└── acceptance.sh             # full-loop script (S4)
```

Gitignore: `runs/`, `fixtures/*/.git/` (prevents gitlink trap — nested repos stay plain dirs), `demo-app/.git/`, `__pycache__/`.

## Session map

| Session | Stage | Done when |
|---|---|---|
| 0 (0.5) | S0 — skeleton & ticket model | `uv run fde submit tests/fixtures/sample.md` prints run ID; `status` shows run |
| 1 (1–1.5) | S1 — harness + fixtures | on all 3 fixtures, harness ACCEPTS the agent-written repro test |
| 2 (2) | S2 — fix agent | on all 3 fixtures, harness 3-state green using the agent's own diff |
| 3 (1) | S3 — gates + diff | planted secret flagged; clean diff passes; `fde diff` readable |
| 4 (1–2) | S4 — deploy/approve/rollback | acceptance.sh green on demo-app |

Fixtures 1–3 are the repro/fix evals. **demo-app is the full-loop target** (it's the only one with a server).

---

## Session 0 — Skeleton & ticket model (0.5 session)

### S0T1: Scaffold package + console script

**Files:** Create `pyproject.toml`, `fde/__init__.py`, `fde/cli.py` (stub `main()` with `--help` listing all subcommands), `tests/__init__.py`, `.gitignore`; run `git init -b main`.

**pyproject.toml:**
```toml
[project]
name = "fde"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pyyaml"]

[project.scripts]
fde = "fde.cli:main"

[dependency-groups]
dev = ["pytest"]
```

**Steps:** `uv sync` → `uv run fde --help` lists `submit, status, diff, approve, rollback, deploy` (stubs exit 0). Commit `chore: scaffold fde package`.

### S0T2: ticket.py — parse + validate

**Files:** Create `fde/ticket.py`, `tests/test_ticket.py`, `tests/fixtures/sample.md`.

**ticket.md front-matter (the contract):**
```yaml
---
id: TT-001
severity: high
system: tier1_checkout
expected: "3 items at $10 should total $31.50 (5% tax)"
actual: "total comes to $30.05"
symptom: "total should be 31.5"
---
# Checkout total miscalculated
...
```

**Rules:** required fields `id, severity, system, expected, actual, symptom`; severity ∈ {low, med, high, critical}; symptom length ≥ 6 (distinctive token — prevents fuzzy-match false positives later). Raise `TicketError` with all problems, not just the first.

**fde/ticket.py (complete):**
```python
from pathlib import Path
import yaml

REQUIRED = ["id", "severity", "system", "expected", "actual", "symptom"]
SEVERITIES = {"low", "med", "high", "critical"}
MIN_SYMPTOM = 6

class TicketError(Exception):
    pass

def parse_ticket(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise TicketError("ticket must start with YAML front-matter '---'")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    problems = []
    for f in REQUIRED:
        if not meta.get(f):
            problems.append(f"missing field: {f}")
    if meta.get("severity") and meta["severity"] not in SEVERITIES:
        problems.append(f"severity must be one of {sorted(SEVERITIES)}")
    if meta.get("symptom") and len(meta["symptom"]) < MIN_SYMPTOM:
        problems.append(f"symptom must be >= {MIN_SYMPTOM} chars")
    if problems:
        raise TicketError("; ".join(problems))
    meta["body"] = body.strip()
    return meta
```

**Tests (TDD — write first, watch fail):** valid ticket parses; missing field raises with field name; short symptom raises; bad severity raises; body captured.

**Verify:** `uv run pytest tests/test_ticket.py -v` → all pass. Commit `feat: ticket model + validation`.

### S0T3: runlog.py — JSONL + state machine

**Files:** Create `fde/runlog.py`, `tests/test_runlog.py`.

**Contract:** `runs/<run_id>/run.jsonl` + `runs/<run_id>/state.json`. Event names are the PLAN.md vocabulary — append() raises on unknown event (typo protection). State enum and transitions are the linear machine:

```
submitted → reproducing → reproved → fixing → fixed → gating → gated →
awaiting_approval → approved → deploying → deployed → rolling_back → rolled_back
```
Any state → `failed` allowed (agent_error). `set_state` raises on illegal transition.

**fde/runlog.py (complete):**
```python
import json, secrets, datetime
from pathlib import Path

RUNS_DIR = Path("runs")
EVENTS = {"ticket_parsed","worktree_created","repro_test_written","test_result",
          "fix_attempt","gates_passed","gates_failed","evidence_packaged",
          "approved","rejected","deployed","rolled_back","agent_error","resumed"}
STATES = ["submitted","reproducing","reproved","fixing","fixed","gating","gated",
          "awaiting_approval","approved","deploying","deployed","rolling_back",
          "rolled_back","failed"]
TRANSITIONS = {  # from -> allowed next states (missing key = no forward moves)
    "submitted": ["reproducing"], "reproducing": ["reproved", "failed"],
    "reproved": ["fixing"], "fixing": ["fixed", "failed"], "fixed": ["gating"],
    "gating": ["gated", "failed"], "gated": ["awaiting_approval"],
    "awaiting_approval": ["approved", "rejected"], "approved": ["deploying"],
    "deploying": ["deployed", "failed"], "deployed": ["rolling_back"],
    "rolling_back": ["rolled_back", "failed"],
}

def new_run_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

def run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def append(run_id: str, event: str, data: dict | None = None):
    if event not in EVENTS:
        raise ValueError(f"unknown event: {event}")
    line = {"ts": datetime.datetime.now().isoformat(), "run_id": run_id,
            "event": event, "data": data or {}}
    with open(run_dir(run_id) / "run.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")

def events(run_id: str) -> list[dict]:
    p = run_dir(run_id) / "run.jsonl"
    if not p.exists(): return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]

def state(run_id: str) -> str:
    p = run_dir(run_id) / "state.json"
    return json.loads(p.read_text(encoding="utf-8"))["state"] if p.exists() else "submitted"

def set_state(run_id: str, new: str):
    old = state(run_id)
    if new not in TRANSITIONS.get(old, []):
        raise ValueError(f"illegal transition {old} -> {new}")
    (run_dir(run_id) / "state.json").write_text(json.dumps({"state": new}), encoding="utf-8")
    append(run_id, "resumed" if new == old else "state_changed", {"from": old, "to": new})
```

**Tests:** append→read roundtrip; unknown event raises; illegal transition raises; `new_run_id` unique twice. Commit `feat: run log + linear state machine`.

### S0T4: config.py

**Files:** Create `fde/config.py`, `tests/test_config.py`.

**`~/.fde.yaml` (user config, defaults if missing):**
```yaml
agent_backend: codex
deploy:
  prod_branch: prod
  port: 8124
  preview_port: 8123
```
`load_user_config()` merges over defaults. `load_repo_manifest(repo_dir)` reads the repo's `fde.yaml` — **required** fields: `install_cmd, test_cmd, run_cmd, app_type` (js|py|node); missing → error listing the missing keys.

**Tests:** defaults when no file; overrides honored; repo manifest missing field error. Commit `feat: config + repo manifest`.

### S0T5: cli.py submit/status (real), others stub

**Files:** Modify `fde/cli.py`.

**submit:** parse ticket (TicketError → print problems, exit 1) → `new_run_id()` → copy ticket.md into run dir → `append(ticket_parsed)` → print run ID. No worktree, no agent.
**status:** print state + last 5 events + artifact paths (ticket, diff, test file if present).
**diff/approve/rollback/deploy:** exit 0 with `"not implemented in stage <N>"`.

**Verify (acceptance for S0):**
```bash
uv run fde submit tests/fixtures/sample.md     # prints run ID
uv run fde status <RUN_ID>                      # shows submitted + ticket_parsed
uv run fde submit tests/fixtures/bad.md         # (create one) → clear error, exit 1
```
Commit `feat: submit + status commands`.

---

## Session 1 — Repro agent + verification harness (1–1.5 sessions)

> Build the harness FIRST. It is the product's proof layer and everything else tests against it.

### S1T1: worktree.py

**Files:** Create `fde/worktree.py`, `tests/test_worktree.py`.

```python
import subprocess
from .runlog import run_dir

def create_worktree(repo: str, run_id: str) -> str:
    wt = str(run_dir(run_id) / "worktree")
    subprocess.run(["git", "-C", repo, "worktree", "add", wt, "main"],
                   check=True, capture_output=True, text=True)
    return wt

def discard_worktree(repo: str, run_id: str):
    wt = str(run_dir(run_id) / "worktree")
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                   check=True, capture_output=True, text=True)
```

**Tests:** create scratch repo (tmpdir: `git init -b main`, one commit) → add worktree → file exists → discard → gone; `git worktree list` clean. Commit `feat: worktree management`.

### S1T2: harness.py — run_cmd + 3-state verification

**Files:** Create `fde/harness.py`, `tests/test_harness.py`.

**run_cmd (Windows-safe):** run through `bash -c` (fixtures declare bash command strings). Timeout kills the whole tree:
```python
import subprocess, os

def run_cmd(cmd: str, cwd: str, timeout: int = 60) -> dict:
    """Returns {"rc": int, "out": str, "timed_out": bool}. Windows-safe kill."""
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        return {"rc": r.returncode, "out": r.stdout + r.stderr, "timed_out": False}
    except subprocess.TimeoutExpired:
        # kill process tree: taskkill /F /T /PID <pid> — child of bash, not the shell itself
        # (implementer: resolve bash's child pid via `pgrep -P` or start bash in its own group;
        #  fallback `taskkill /F /T /IM node.exe` is acceptable for demo stage)
        return {"rc": -1, "out": "[timeout]", "timed_out": True}
```
*(Pitfall note: `subprocess.run(timeout=)` does not kill grandchildren — codex/node children outlive bash. Acceptable for the demo stage; document it in the module docstring. Stage 7 hardens this with real containers.)*

**3-state verification** — `verify_repro(repo, worktree, manifest, ticket, repro_test)`:
1. **State A (fails for the right reason):** copy repro test into worktree → `run_cmd(f"{manifest.test_cmd} {repro_test_path}")` → rc ≠ 0 AND `symptom_in_output(ticket["symptom"], out)`.
2. **State B (passes with gold):** `git apply gold.patch` → run repro test → rc == 0 → `git checkout . && git clean -fd` (restore).
3. **State C (no regression):** with gold applied → full `manifest.test_cmd` → rc == 0 → restore.
Return `{"pass": bool, "checks": {"a": {...}, "b": {...}, "c": {...}}}`. Append `test_result` event with `duration_ms` and per-check detail — **this is the future bench's raw data, record it now.**

**Symptom match (normalization):**
```python
import re
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()
def symptom_in_output(symptom: str, output: str) -> bool:
    return norm(symptom) in norm(output)
```

**Tests (scratch buggy repo in tests/scratch_repos/, mini tier-1):** good repro test → PASS; trivial test (`assert(true)`) → FAIL on state A; test failing for wrong reason (different symptom) → FAIL on state A; gold patch broken → FAIL on B/C. Commit `feat: 3-state verification harness`.

### S1T3–S1T5: Fixtures (one task each, same ritual)

Ritual per fixture: create dir + files → `git init -b main` → commit buggy code → `git diff > gold.patch` after writing fixed version, then restore buggy (`git checkout .`) → add `fde.yaml` + `ticket.md`.

**tier1_checkout** (node, no deps) — bug in one file, direct symptom.
- `calc.js`: `const TAX = 0.05; module.exports = { total: (p, q) => p * q + TAX };`  ← bug: adds flat tax, should be 5% of subtotal
- `test/total.test.js`: `assert.equal(total(10, 3), 31.5)` → fails with 30.05
- gold.patch: `p * q * (1 + TAX)`
- `fde.yaml`: `install_cmd: ""`, `test_cmd: "node --test"`, `run_cmd: "node -e \"console.log(require('./calc.js').total(10,3))\""`, `app_type: js`
- ticket symptom: `total should be 31.5`

**tier2_billing** (python/pytest) — bug requires cross-file trace.
- `config.py`: `TAX_RATE = 0.18`
- `billing.py`: `def invoice(amount): return amount * (1 + 0.15)` ← bug: hardcoded, ignores config
- `test_billing.py`: `assert invoice(100) == 118.0` → fails with 115.0
- gold: `from config import TAX_RATE` + `return amount * (1 + TAX_RATE)`
- `fde.yaml`: `install_cmd: "pip install -q pytest"`, `test_cmd: "python -m pytest -q"`, `run_cmd: "python app.py"`, `app_type: py`
- ticket symptom: `invoice 100 should be 118`

**tier3_ingest** (node) — bug only visible through test-suite output.
- `ingest.js`: processes rows; malformed row 7 hits `.catch(() => {})` (swallowed); error is never surfaced anywhere — the bug is *invisible* without a test asserting error capture
- `test/ingest.test.js`: `assert.deepStrictEqual(errors, ["row 7 malformed"])` → fails (errors == []) and node's diff output contains `row 7 malformed`
- gold: `.catch(err => errors.push("row 7 malformed"))` (error surfaced into the observable list)
- `fde.yaml`: `install_cmd: ""`, `test_cmd: "node --test"`, `run_cmd: "node ingest.js"`, `app_type: js`
- ticket symptom: `row 7 malformed`

**Verify per fixture:** manually run the repro test → fails with symptom in output; `git apply gold.patch` → passes; full suite green. Then `git status` clean, gold.patch committed in the tool repo. Commit `feat: fixture tier1_checkout` (etc.).

### S1T6: agents.py — repro loop (minimal codex wrapper)

**Files:** Create `fde/agents.py`.

**codex exec contract (verify actual flags at build time — `codex exec --help`):**
```python
def codex_exec(prompt: str, cwd: str, timeout: int = 600) -> dict:
    # expected invocation (adjust to installed version):
    #   codex exec --json -C <cwd> "<prompt>"
    # record in the run log: backend, `codex --version` output, prompt hash, exit status
```
- cwd = worktree **only**; paths inside prompts in `C:/...` form, never `/c/...`.
- Restricted env: pass through `PATH, HOME, USERPROFILE, APPDATA, LOCALAPPDATA` (codex needs HOME for auth) — nothing else.

**repro prompt:** ticket (expected/actual/symptom), repo tree, fde.yaml commands, instruction: "Write a single failing test that reproduces the symptom. The test must fail on the current code with the symptom visible in its failure output. Do not fix the bug." + the harness's rejection feedback on retry (max 3 attempts, feedback = which check failed + why).

**Verify (S1 acceptance):** for each fixture run the repro flow; harness must ACCEPT the agent's test on all 3. Expect to iterate on the prompt here — that's the point of the session. If the agent writes a fix instead of a test, the harness doesn't care (state A still runs); but prompt should forbid it anyway. Commit `feat: repro agent loop`.

---

## Session 2 — Fix agent (2 sessions)

### S2T1: agents.py — fix loop

```python
def run_fix_loop(worktree, manifest, ticket, repro_test, harness_verdict,
                 max_rounds=8, round_timeout=600) -> dict:
    feedback = None
    for r in range(1, max_rounds + 1):
        prompt = build_fix_prompt(ticket, repro_test, harness_verdict, feedback)
        res = codex_exec(prompt, cwd=worktree, timeout=round_timeout)
        if res["timed_out"]:
            append("agent_error", {...})                     # narrowed retry once, then fail
        diff = git_diff(worktree)                            # codex edits in place; read git diff
        if not diff: feedback = "no changes made — edit the code"; continue
        r = run_cmd(manifest.test_cmd + " " + repro_test, worktree)
        append("fix_attempt", {"round": r, "rc": r.rc, "diff_bytes": len(diff), "duration_ms": ...})
        if r.rc == 0:                                       # repro test green
            full = run_cmd(manifest.test_cmd, worktree)
            if full.rc == 0: return {"ok": True, "diff": diff, "rounds": r}
            feedback = "repro test passes but suite fails: " + full.out[-2000:]
        else:
            feedback = "repro test still failing: " + r.out[-2000:]
    return {"ok": False, "diff": git_diff(worktree), "rounds": max_rounds}
```

**Fix prompt:** ticket + repro test source + harness verdict (state A evidence: "test fails with symptom X on buggy code") + instruction: "Make the minimal change so the repro test passes and the full suite stays green. Do not touch unrelated code. When done, output a one-paragraph what/why summary." Capture the summary from the final codex output into the `fix_attempt` event — it feeds `fde diff`.

### S2T2: `fde fix <run>` orchestration

Repro → `set_state(reproved)` → worktree + repro test persisted (`repro_test_written`) → `set_state(fixing)` → run_fix_loop → on ok: harness 3-state **using the agent's diff as oracle** (state A evidence already in the log; B = repro test passes with agent diff, C = full suite green) → `set_state(fixed)`. On loop failure: `set_state(failed)` + `agent_error` event with the last feedback.

### S2T3: Tuning (the real work of this session)

Run `repro` + `fix` on all 3 fixtures. Iterate prompts until 3/3 pass. Keep a table in the repo (`FIXTURES.md`): per fixture — rounds used, wall time, prompt changes that mattered. **This table is the interview story** (eval-driven prompt engineering, not vibes).

**Verify (S2 acceptance):** all 3 fixtures: harness 3-state green with agent diff; no other tests broken; `git diff` non-empty. Commit `feat: fix agent loop` + any prompt tuning commits.

---

## Session 3 — Gates + evidence (1 session)

### S3T1: gates.py

**Files:** Create `fde/gates.py`, `tests/test_gates.py`. Pure python regex — no gitleaks dependency (optional: if `gitleaks` is on PATH, prefer it via subprocess; regex fallback is the default).

```python
import re
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][A-Za-z0-9_\-/+=]{16,}[\"']"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]
LINT_PATTERNS = [
    re.compile(r"\beval\s*\("), re.compile(r"\bexec\s*\("),
    re.compile(r"shell\s*=\s*True"), re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsudo\b"),
]
def scan_diff(diff_text: str) -> dict:
    return {"secrets": [...findings], "lint": [...findings]}
```
Scan the agent's `git diff` output only (not the whole tree). Findings = (line, pattern, snippet).

**Tests:** diff containing `api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"` → flagged; email in diff → flagged; clean diff → no findings; `eval(` and `shell=True` → flagged; benign code (variable named `token_count`) → not flagged. Commit `feat: secret + lint gates`.

### S3T2: `fde diff <run>` — the evidence package

Read from run log only (no recompute): changed files (`git diff --stat` — recompute this one, it's cheap), test before/after (state A output + final suite output from events), agent's what/why summary (last fix_attempt data). Print human-readable. `append("evidence_packaged")`.

### S3T3: wire gates into pipeline + integration test

After fix: `set_state(gating)` → run gates → all pass: `gates_passed` + `set_state(gated)`; any finding: `gates_failed` + print findings + stop (human can `fde approve` only from `awaiting_approval`... which requires gated — so findings block approval entirely; `rejected` state available for manual close).

**Integration test (planted secret):** craft a fixture scenario where the agent's diff contains a fake key (e.g. tier1 ticket "also add config support" — simplest: unit-level test that runs `scan_diff` on a crafted diff, plus one manual run where you append a secret line to the worktree diff and watch gates block). The PLAN.md "done when" is satisfied by: planted secret → flagged; clean → passes. Commit `feat: gates wired into pipeline`.

**Verify (S3 acceptance):** `fde diff` on a completed fixture run reads like evidence; planted secret blocks approval; clean run reaches `awaiting_approval`.

---

## Session 4 — Preview, approve, prod, rollback (1–2 sessions)

### S4T1: demo-app — the production target

**Files:** `demo-app/server.js`, `demo-app/config.json`, `demo-app/test/tax.test.js`, `demo-app/fde.yaml`, `demo-app/ticket.md`. Own git repo: branches `main` (buggy) + `prod` (same commit initially).

**server.js** (buggy: hardcoded rate, ignores config.json):
```js
const http = require("http");
const TAX_RATE = 0.15;                       // BUG: ignores ./config.json (0.18)
http.createServer((req, res) => {
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/tax") {
    const amount = Number(u.searchParams.get("amount") || 0);
    const tax = amount * TAX_RATE;
    res.end(JSON.stringify({ amount, tax, total: amount + tax }));
  } else res.end("fde demo app");
}).listen(process.env.PORT || 8124);
```
**config.json:** `{"TAX_RATE": 0.18}` · **gold:** `const { TAX_RATE } = require("./config.json");`
**test/tax.test.js:** `node:test` + `fetch` against the running server; assert `total === 118`. Test spawns the server itself (child_process spawn with `PORT=8124`, retry-wait for listen up to 5s, kill in `test.after`). fde.yaml: `install_cmd: ""`, `test_cmd: "node --test"`, `run_cmd: "node server.js"`, `app_type: js`. Ticket symptom: `total should be 118`.

### S4T2: deploy.py

**Commands table (all verified with curl health checks):**

| op | git | process | verify |
|---|---|---|---|
| `deploy --preview` | worktree at fix commit (from run) | `node server.js` on preview_port, PID → `runs/<id>/preview.pid` | `curl -s "http://127.0.0.1:8123/tax?amount=100"` shows 118 |
| `deploy --prod` | fast-forward `prod` ← fix commit (require `approved` state) | kill old PID, start on port 8124, PID → `runs/<id>/server.pid` | curl shows 118 |
| `rollback` | `git revert` fix commit on `prod` | kill + restart on 8124 | curl shows 115 (pre-fix) |

PID kill on Windows: `taskkill /F /T /PID <pid>` (tree). Server logs → `runs/<id>/server.log`. State moves: approved → deploying → deployed → rolling_back → rolled_back. Append `deployed` / `rolled_back` events with the curl results.

### S4T3: wire approve + gates to deploy

`fde approve <run>`: require state `awaiting_approval` → `approved` event + state. `deploy --prod` requires `approved`; `deploy --preview` requires `gated` or later. `rollback` requires `deployed`. Rejections with clear messages (this is the human-gate story — make the messages good, they're demo surface).

### S4T4: acceptance.sh — the full loop (repeatable)

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
RUN=$(uv run fde submit demo-app/ticket.md | tail -1)
uv run fde repro "$RUN"             # harness accepts repro test (prints PASS)
uv run fde fix "$RUN"               # harness 3-state green
uv run fde diff "$RUN"              # human-readable evidence
uv run fde approve "$RUN"
uv run fde deploy --preview "$RUN"  # curl preview: total 118
uv run fde deploy --prod "$RUN"     # curl prod: total 118
uv run fde rollback "$RUN"          # curl prod: total 115  ← green after rollback
echo "ACCEPTANCE PASS"
```
Run it 3× to prove repeatability. **This script is the demo** — a 90-second live walkthrough. Commit `feat: full deploy/rollback loop`.

**Verify (S4 acceptance):** acceptance.sh green; `git -C demo-app log --oneline prod` shows fix → revert; run log is a complete audit trail of the whole loop.

---

## Portfolio capture (do this as you go, not after)

- Keep every fixture's `FIXTURES.md` row (rounds, wall time) — bench fuel.
- One screenshot per stage: `fde diff` output, gates blocking a planted secret, acceptance.sh final screen.
- Record `duration_ms` on every `test_result`/`fix_attempt` (already in the plan — don't skip it).
- The 90-second demo arc: submit a real ticket file → status → diff (the evidence) → approve → curl before/after → rollback → curl back to green. Talking points: harness = proof, human gate = safety, file-first tickets = "no ticketing system needed".

## Risks & pitfalls (read before each session)

1. **Windows child-process kill:** `subprocess.run(timeout=)` leaves grandchildren alive (codex/node). taskkill tree-kill is the accepted demo-stage answer; document it.
2. **MSYS path mangling:** anything inside codex prompts must be `C:/...`. `/c/...` will confuse the agent.
3. **codex exec flags:** verify with `codex exec --help` at S1 start; record `codex --version` in run log.
4. **gitlink trap:** without `fixtures/*/.git/` in .gitignore, `git add` turns fixture repos into submodule links. Check `git status` after first fixture commit.
5. **Symptom match false positives:** symptom ≥ 6 chars with a distinctive token (a number like `31.5` alone is too short). This is why the length rule exists.
6. **Trivial repro tests** (`assert(true)`): caught by harness state A (they pass on buggy code). Trust the harness, not the agent.
7. **Node ≥ 18** required (`node:test`, global `fetch`). Check `node -v` in Session 0.
8. **codex needs HOME** for auth — pass it through in the env; restrict cwd and everything else.
9. **Agent edits outside worktree:** codex runs with cwd=worktree; if the diff touches files outside it, reject the round ("changes outside worktree").
10. **Harness gold-patch restore:** always `git checkout . && git clean -fd` after states B/C, or state A gets poisoned on the next run.

## Tests / validation summary

- pytest: ~25 tests across ticket, runlog, config, worktree, harness, gates — run `uv run pytest -q` after every task.
- S0: submit/status acceptance. S1: harness accepts 3/3 fixtures. S2: 3/3 fix loops green. S3: planted secret blocked. S4: acceptance.sh × 3.
- Final: `uv run pytest -q && bash acceptance.sh` from clean clone of the repo.

## Open questions (resolve at build time, none block the start)

- Exact `codex exec` flags for this installed version (Session 1 kickoff).
- gitleaks on PATH? (Default is pure-regex fallback — no dependency either way.)
- Node version on machine (Session 0 check).
