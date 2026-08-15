# Production Security — five layers, one audit trail

**TL;DR — every layer has a single job, and no layer trusts the one below
it.** Host hardened → agent can't touch it → the agent's code executes only
in a dead-network container → the 3-state referee judges it → a named human
ships it → every step is tamper-evident and secrets never reach the logs or
the model API.

| Layer | What it does | How to turn it on |
|---|---|---|
| 0. Host | dedicated runner, non-root service account, hardened Docker daemon (userns-remap, no privileged containers) | operator setup |
| 1. Agent isolation | the agent CLI itself runs in the `fde-agent` container | `FDE_AGENT_CONTAINER=1` |
| 2. Command isolation | every harness command runs in the sandbox container; no host fallback possible | `FDE_SANDBOX=required` |
| 3. The referee | 3-state harness + gates + worktree resets + tamper closures | always on |
| 4. The human gate | named approval recorded in the audit log | `fde approve --approver <name>` |
| 5. Tamper-evident log | hash-chained run.jsonl + `fde audit` | always on |

## Layer 0 — Host

The pipeline is a CLI: it listens on nothing. The real host risk is the
Docker daemon — whoever owns it owns everything. Run the tool on a
dedicated VM/runner as a non-root account; harden the daemon
(`userns-remap`, no privileged containers, daemon audit logging). Nothing
in the pipeline requires host inbound access.

## Layer 1 — Agent isolation (`FDE_AGENT_CONTAINER=1`)

Without this layer, the agent CLI (codex/dsh/claude) is a host process
that can read the host. With it, the agent runs inside the `fde-agent`
container (build: `docker build -f Dockerfile.agent -t fde-agent:latest .`):

- The worktree is mounted at `/workspace` — the only writable host path.
- **Env allowlist** — only the API keys + base URLs (`OPENCODE_GO_*`,
  `DEEPSEEK_*`, `ANTHROPIC_API_KEY`) and proxy vars (`FDE_AGENT_PROXY` →
  `http_proxy`/`https_proxy`) cross into the container. The host
  environment never does.
- Same resource policy as Layer 2 (`FDE_SANDBOX_MEMORY/CPUS/PIDS`,
  read-only rootfs, tmpfs `/tmp`).
- Egress: `FDE_AGENT_NETWORK` (default `bridge`). Domain-level allowlisting
  is the operator's job — Docker Desktop cannot filter by domain natively;
  run the daemon under host iptables or point `FDE_AGENT_PROXY` at an
  allowlisting egress proxy. This is the honest boundary: the container
  enforces isolation, the operator enforces the domain list.
- Timeout → the container is `docker kill`ed (never left running).
- Keys are injected at runtime only; the image bakes nothing secret.

## Layer 2 — Command isolation (`FDE_SANDBOX=required`)

`FDE_SANDBOX=required` makes docker mandatory: daemon down → fail fast,
never a silent host fallback. `FDE_SANDBOX=host` is the explicit,
loudly-warned opt-out. Every harness command (install/test/git) runs in the
`fde-sandbox` container: `--network none`, `--cap-drop ALL`,
no-new-privileges, read-only rootfs + tmpfs `/tmp`, and
`--memory/--cpus/--pids-limit` (defaults 1g/2/256, overridable via
`FDE_SANDBOX_MEMORY/CPUS/PIDS`). The agent's code executes only here, with
zero network — its blast radius is this container, nothing more.

## Layer 3 — The referee

Unchanged by design: the 3-state harness (fails with the symptom → passes
with the gold patch → full suite green and worktree untouched), the diff
gates (secrets/lint), worktree resets between rounds, and the structural
tampering closures (test rewrite, mid-run commit, skip-worktree). This
decides what is TRUSTED; the containers decide what is CONTAINED.

## Layer 4 — The human gate

`fde approve` records WHO approved: `--approver NAME`, else
`FDE_APPROVER`, else `git config user.name`. No anonymous approvals in the
audit trail. Diff review is the last intent-check the machine cannot do —
a fix can pass every test and still be malicious; the named human reading
the diff is the final gate. There is no auto-approve path.

## Layer 5 — Tamper-evident audit (`fde audit <run_id>`)

Every run.jsonl line carries the sha256 of the previous line — editing any
event breaks every line after it, and `fde audit` proves the chain (exit 0
intact / 1 broken). The chain survives cross-process appends (bench spawns
subprocesses) and is the reason the log can be published as evidence.

## Secrets

- Keys: runtime injection only (Layer 1 allowlist), never in the image,
  never in the worktree.
- Tickets: scrubbed before prompts are built (`scrub_ticket`) — secrets
  never reach the model API.
- Logs: every event is redacted at write time (`redact_event_data`, before
  hashing, so the chain stays valid; `secrets_redacted` marker when
  anything was removed). Pure-hex commit SHAs are exempt by design — the
  audit trail's fix/deploy hashes are evidence, not secrets.
- Residual risk, stated plainly: the model provider sees whatever is in the
  ticket + repo files the agent reads. Containment limits blast radius;
  policy (which repos/tickets are eligible) is the remaining control.

## Honest limits

1. A fix that passes tests can still be malicious — the named human diff
   review is the final gate (Layer 4).
2. Egress domain allowlisting depends on host/proxy configuration — the
   container flags enforce the boundary, the operator enforces the list.
3. Docker daemon compromise = everything compromised. Layer 0 exists for
   this reason.
4. The pipeline's own code is the trust root: 178 tests, CI corpus +
   in-container sandbox gates, and the tampering timeline in
   docs/TAMPERING.md are the evidence for it.

## Secure posture, one line

```bash
FDE_SANDBOX=required FDE_AGENT_CONTAINER=1 uv run fde repro <run_id>
```
