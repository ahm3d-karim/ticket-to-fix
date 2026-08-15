import hashlib
import json
import secrets
import datetime
from pathlib import Path

from .gates import redact_event_data

RUNS_DIR = Path("runs")
EVENTS = {"ticket_parsed","worktree_created","repro_test_written","test_result",
          "fix_attempt","gates_passed","gates_failed","evidence_packaged",
          "approved","rejected","deployed","rolled_back","agent_error","resumed",
          "state_changed"}  # NOTE: set_state() appends "state_changed", so it
                            # must be a legal event.
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

def _line_hash(line: str) -> str:
    """sha256 hex digest of a run.jsonl line's canonical bytes.

    Canonical bytes = the line exactly as written, WITHOUT its trailing
    newline: the newline is a file-format artifact (universal-newlines
    reads strip it), so hashing without it keeps the chain stable no
    matter how the file's line endings are normalized. Each line's hash
    covers the FULL line text including its own ``prev`` field, so any
    edit to a line breaks every later link.
    """
    return hashlib.sha256(line.removesuffix("\n").encode("utf-8")).hexdigest()


def _tail_hash(p: Path) -> str | None:
    """Hash of the last line currently on disk; None for an empty log.

    Read from the FILE TAIL, never from memory: runs are appended to by
    multiple processes (bench spawns subprocesses), so the previous hash
    must be whatever is actually last on disk at append time.
    """
    if not p.exists() or p.stat().st_size == 0:
        return None
    lines = p.read_text(encoding="utf-8").splitlines()
    return _line_hash(lines[-1]) if lines else None


def append(run_id: str, event: str, data: dict | None = None):
    if event not in EVENTS:
        raise ValueError(f"unknown event: {event}")
    if data:
        # secrets never reach the log: redact before the line is built (the
        # written line is the hash-chain source of truth)
        data = redact_event_data(data)
    line = {"ts": datetime.datetime.now().isoformat(), "run_id": run_id,
            "event": event, "data": data or {}}
    p = run_dir(run_id) / "run.jsonl"
    line["prev"] = _tail_hash(p)  # chain link to the previous line's hash
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")

def events(run_id: str) -> list[dict]:
    p = run_dir(run_id) / "run.jsonl"
    if not p.exists(): return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]


def verify_chain(run_id: str) -> list[str]:
    """Validate the run.jsonl hash chain; an empty list means it is intact.

    Every line must parse as JSON, line 1 must carry ``prev: null``, and
    every later line's ``prev`` must equal the sha256 of the previous
    line's canonical bytes (see ``_line_hash``). Returns human-readable
    problem descriptions; callers decide how to surface them.
    """
    p = RUNS_DIR / run_id / "run.jsonl"
    if not p.exists():
        return [f"run.jsonl missing for run {run_id}"]
    problems: list[str] = []
    expected: str | None = None  # hash the next line must point back at
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, start=1):
        if not line:
            problems.append(f"line {i}: empty line")
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append(f"line {i}: not valid JSON ({e})")
            continue
        if obj.get("prev") != expected:
            problems.append(
                f"line {i}: prev mismatch (expected {expected!r}, "
                f"found {obj.get('prev')!r})")
        expected = _line_hash(line)
    return problems

def state(run_id: str) -> str:
    p = run_dir(run_id) / "state.json"
    return json.loads(p.read_text(encoding="utf-8"))["state"] if p.exists() else "submitted"

def set_state(run_id: str, new: str):
    old = state(run_id)
    if new not in TRANSITIONS.get(old, []):
        raise ValueError(f"illegal transition {old} -> {new}")
    (run_dir(run_id) / "state.json").write_text(json.dumps({"state": new}), encoding="utf-8")
    append(run_id, "resumed" if new == old else "state_changed", {"from": old, "to": new})

def snapshot(run_id: str) -> dict:
    """Current state + full event log for a run — the resume command's view
    of the last completed step (which loop finished, which didn't)."""
    return {"state": state(run_id), "events": events(run_id)}
