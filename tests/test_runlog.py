import json

import pytest

from fde.runlog import (
    RUNS_DIR,
    _line_hash,
    append,
    events,
    new_run_id,
    run_dir,
    set_state,
    state,
    verify_chain,
)


@pytest.fixture(autouse=True)
def clean_runs(tmp_path, monkeypatch):
    """Point RUNS_DIR at a tmp dir so tests never touch the repo's runs/."""
    monkeypatch.setattr("fde.runlog.RUNS_DIR", tmp_path)
    yield tmp_path


def test_append_read_roundtrip(clean_runs):
    rid = new_run_id()
    append(rid, "ticket_parsed", {"ticket_id": "TT-001"})
    lines = events(rid)
    assert len(lines) == 1
    assert lines[0]["event"] == "ticket_parsed"
    assert lines[0]["run_id"] == rid
    assert lines[0]["data"] == {"ticket_id": "TT-001"}
    assert "ts" in lines[0]


def test_unknown_event_raises(clean_runs):
    rid = new_run_id()
    with pytest.raises(ValueError, match="unknown event"):
        append(rid, "not_an_event")


def test_illegal_transition_raises(clean_runs):
    rid = new_run_id()
    with pytest.raises(ValueError, match="illegal transition"):
        set_state(rid, "deployed")  # submitted -> deployed is not allowed


def test_new_run_id_unique_twice():
    assert new_run_id() != new_run_id()


def test_state_defaults_to_submitted(clean_runs):
    rid = new_run_id()
    assert state(rid) == "submitted"


def test_state_machine_forward(clean_runs):
    rid = new_run_id()
    for s in ["reproducing", "reproved", "fixing", "fixed", "gating", "gated",
              "awaiting_approval", "approved", "deploying", "deployed",
              "rolling_back", "rolled_back"]:
        set_state(rid, s)
        assert state(rid) == s


def test_any_state_to_failed(clean_runs):
    rid = new_run_id()
    set_state(rid, "reproducing")
    set_state(rid, "failed")
    assert state(rid) == "failed"
    # failed has no forward moves
    with pytest.raises(ValueError, match="illegal transition"):
        set_state(rid, "submitted")


def test_set_state_appends_state_changed_event(clean_runs):
    rid = new_run_id()
    set_state(rid, "reproducing")
    evs = events(rid)
    assert evs[-1]["event"] == "state_changed"
    assert evs[-1]["data"] == {"from": "submitted", "to": "reproducing"}


def test_run_dir_created(clean_runs):
    import fde.runlog as rl
    rid = new_run_id()
    d = run_dir(rid)
    assert d.exists()
    assert (rl.RUNS_DIR / rid) == d


# --------------------------------------------------------------------------- #
# Layer 3: tamper-evident hash-chained run.jsonl
# --------------------------------------------------------------------------- #

def test_first_line_prev_is_null(clean_runs):
    rid = new_run_id()
    append(rid, "ticket_parsed", {"ticket_id": "TT-001"})
    lines = events(rid)
    assert len(lines) == 1
    assert lines[0]["prev"] is None


def test_each_line_prev_is_previous_line_hash(clean_runs):
    rid = new_run_id()
    append(rid, "ticket_parsed", {"ticket_id": "TT-001"})
    append(rid, "worktree_created", {"repo": "demo"})
    append(rid, "test_result", {"stage": "repro", "attempt": 1, "verdict": True})
    raw = (clean_runs / rid / "run.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = events(rid)
    assert len(raw) == len(parsed) == 3
    # each line's prev is the sha256 of the previous line's canonical bytes
    assert parsed[1]["prev"] == _line_hash(raw[0])
    assert parsed[2]["prev"] == _line_hash(raw[1])
    assert len(parsed[1]["prev"]) == 64  # hex sha256


def test_verify_chain_intact_after_appends(clean_runs):
    rid = new_run_id()
    for event, data in [
        ("ticket_parsed", {"ticket_id": "TT-001"}),
        ("worktree_created", {"repo": "demo"}),
        ("test_result", {"stage": "repro", "attempt": 1, "verdict": True}),
    ]:
        append(rid, event, data)
    assert verify_chain(rid) == []


def test_verify_chain_detects_tampered_middle_line(clean_runs):
    rid = new_run_id()
    append(rid, "ticket_parsed", {"ticket_id": "TT-001"})
    append(rid, "worktree_created", {"repo": "demo"})
    append(rid, "test_result", {"stage": "repro", "attempt": 1, "verdict": True})
    p = clean_runs / rid / "run.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["data"] = {"repo": "EVIL"}
    lines[1] = json.dumps(tampered)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problems = verify_chain(rid)
    assert problems, "a tampered middle line must break the chain"
    assert any("line 2" in pr or "prev" in pr for pr in problems)


def test_verify_chain_detects_forged_first_prev(clean_runs):
    rid = new_run_id()
    append(rid, "ticket_parsed", {"ticket_id": "TT-001"})
    append(rid, "worktree_created", {"repo": "demo"})
    p = clean_runs / rid / "run.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[0])
    forged["prev"] = "0" * 64
    lines[0] = json.dumps(forged)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(rid)  # first line must have prev=null


def test_verify_chain_detects_removed_line(clean_runs):
    rid = new_run_id()
    for event, data in [
        ("ticket_parsed", {"ticket_id": "TT-001"}),
        ("worktree_created", {"repo": "demo"}),
        ("test_result", {"stage": "repro", "attempt": 1, "verdict": True}),
    ]:
        append(rid, event, data)
    p = clean_runs / rid / "run.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")  # drop line 1
    assert verify_chain(rid)  # line 2's prev no longer matches line 1


def test_verify_chain_missing_file_reports_problem(clean_runs):
    rid = new_run_id()
    assert verify_chain(rid)  # no run.jsonl at all


def test_append_chains_through_foreign_tail(clean_runs):
    """append() links to whatever line is last ON DISK, even one it did not
    write itself — the cross-process case (bench spawns subprocesses)."""
    rid = new_run_id()
    append(rid, "ticket_parsed", {"ticket_id": "TT-001"})
    raw0 = (clean_runs / rid / "run.jsonl").read_text(encoding="utf-8").splitlines()[0]
    foreign = {"ts": "2026-08-15T00:00:00", "run_id": rid,
               "event": "worktree_created", "data": {"repo": "other-proc"},
               "prev": _line_hash(raw0)}
    with open(clean_runs / rid / "run.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(foreign) + "\n")  # a second, independent handle
    append(rid, "test_result", {"stage": "repro", "attempt": 1, "verdict": True})
    raw = (clean_runs / rid / "run.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = events(rid)
    assert parsed[-1]["prev"] == _line_hash(raw[1])
    assert verify_chain(rid) == []
