"""Layer 3: `fde approve` records the approver's identity.

Precedence: --approver flag -> FDE_APPROVER env -> git config user.name
(via _git_approver) -> "unknown". cmd_approve is driven directly with both
RUNS_DIR bindings monkeypatched to a tmp dir.
"""
import json
from types import SimpleNamespace

import pytest

from fde.cli import build_parser, cmd_approve
from fde.runlog import events, new_run_id, run_dir


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("fde.runlog.RUNS_DIR", tmp_path)
    monkeypatch.setattr("fde.cli.RUNS_DIR", tmp_path)
    return tmp_path


def _awaiting_run(runs_dir):
    """A run sitting at awaiting_approval (approve is only legal there)."""
    rid = new_run_id()
    d = run_dir(rid)
    (d / "state.json").write_text(json.dumps({"state": "awaiting_approval"}),
                                  encoding="utf-8")
    return rid


def _approved_event(runs_dir, rid):
    for e in events(rid):
        if e["event"] == "approved":
            return e
    return None


def _approve(run_id, approver, capsys):
    rc = cmd_approve(SimpleNamespace(run_id=run_id, approver=approver))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_approve_flag_records_approver(runs_dir, monkeypatch, capsys):
    monkeypatch.delenv("FDE_APPROVER", raising=False)
    rid = _awaiting_run(runs_dir)
    rc, out, err = _approve(rid, "Ahmad Karim", capsys)
    assert rc == 0
    ev = _approved_event(runs_dir, rid)
    assert ev is not None
    assert ev["data"]["approver"] == "Ahmad Karim"
    assert ev["data"]["by"] == "human"  # existing fields preserved


def test_approve_env_approver(runs_dir, monkeypatch, capsys):
    monkeypatch.setenv("FDE_APPROVER", "Env Person")
    rid = _awaiting_run(runs_dir)
    rc, out, err = _approve(rid, None, capsys)
    assert rc == 0
    assert _approved_event(runs_dir, rid)["data"]["approver"] == "Env Person"


def test_approve_git_config_approver(runs_dir, monkeypatch, capsys):
    monkeypatch.delenv("FDE_APPROVER", raising=False)
    monkeypatch.setattr("fde.cli._git_approver", lambda: "Git Person")
    rid = _awaiting_run(runs_dir)
    rc, out, err = _approve(rid, None, capsys)
    assert rc == 0
    assert _approved_event(runs_dir, rid)["data"]["approver"] == "Git Person"


def test_approve_unknown_fallback(runs_dir, monkeypatch, capsys):
    monkeypatch.delenv("FDE_APPROVER", raising=False)
    monkeypatch.setattr("fde.cli._git_approver", lambda: None)
    rid = _awaiting_run(runs_dir)
    rc, out, err = _approve(rid, None, capsys)
    assert rc == 0
    assert _approved_event(runs_dir, rid)["data"]["approver"] == "unknown"


def test_approve_flag_beats_env(runs_dir, monkeypatch, capsys):
    monkeypatch.setenv("FDE_APPROVER", "Env Person")
    rid = _awaiting_run(runs_dir)
    rc, out, err = _approve(rid, "Flag Person", capsys)
    assert rc == 0
    assert _approved_event(runs_dir, rid)["data"]["approver"] == "Flag Person"


def test_approve_parser_has_approver_flag():
    args = build_parser().parse_args(
        ["approve", "--approver", "Ahmad Karim", "run-1"])
    assert args.approver == "Ahmad Karim"
    assert args.run_id == "run-1"
