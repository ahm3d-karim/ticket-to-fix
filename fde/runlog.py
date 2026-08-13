import json, secrets, datetime
from pathlib import Path

RUNS_DIR = Path("runs")
EVENTS = {"ticket_parsed","worktree_created","repro_test_written","test_result",
          "fix_attempt","gates_passed","gates_failed","evidence_packaged",
          "approved","rejected","deployed","rolled_back","agent_error","resumed",
          "state_changed"}  # NOTE: "state_changed" added to the PLAN.md vocabulary —
                            # set_state() appends it, so it must be a legal event.
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
