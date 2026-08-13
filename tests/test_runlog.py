import json

import pytest

from fde.runlog import (
    RUNS_DIR,
    append,
    events,
    new_run_id,
    run_dir,
    set_state,
    state,
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
