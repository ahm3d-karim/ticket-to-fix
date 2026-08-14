"""S3 wiring: `fde status` prints a one-line verification verdict.

Synthetic run.jsonl logs in tmp dirs (never real runs/); cmd_status is driven
directly with both RUNS_DIR bindings monkeypatched — fde.cli imports the name
from fde.runlog, so both must point at the tmp dir for events()/state() and
the cli's own artifact lookups to agree.
"""
import json
from types import SimpleNamespace

import pytest

from fde.cli import cmd_status


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("fde.runlog.RUNS_DIR", tmp_path)
    monkeypatch.setattr("fde.cli.RUNS_DIR", tmp_path)
    return tmp_path


def write_run(runs_dir, name, events, state=None):
    """Write run.jsonl (+ optional state.json) for a synthetic run."""
    d = runs_dir / name
    d.mkdir()
    lines = [json.dumps({"ts": f"2026-08-13T00:00:{i:02d}", "run_id": name,
                         "event": event, "data": data})
             for i, (event, data) in enumerate(events)]
    (d / "run.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if state is not None:
        (d / "state.json").write_text(json.dumps({"state": state}),
                                      encoding="utf-8")
    return d


def _sc(from_, to):
    return ("state_changed", {"from": from_, "to": to})


def _repro(attempt, verdict_):
    return ("test_result", {"stage": "repro", "attempt": attempt,
                            "verdict": verdict_, "checks": {}})


def _fix(rnd, ok):
    return ("fix_attempt", {"round": rnd, "ok": ok, "rc": 0 if ok else 1,
                            "diff_bytes": 425, "duration_ms": 100})


GREEN_TAIL = [
    _sc("fixing", "fixed"), _sc("fixed", "gating"),
    ("gates_passed", {"diff_bytes": 1126}),
    _sc("gating", "gated"), _sc("gated", "awaiting_approval"),
]


def _status(runs_dir, run_id, capsys):
    rc = cmd_status(SimpleNamespace(run_id=run_id))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_status_verified_line(runs_dir, capsys):
    write_run(runs_dir, "green", [
        ("ticket_parsed", {"ticket_id": "TT-001"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True),
    ] + GREEN_TAIL)
    rc, out, err = _status(runs_dir, "green", capsys)
    assert rc == 0
    assert "verdict: verified (repro 1st try, 1 round, gates passed)" in out
    # the pre-existing status lines are untouched
    assert "run: green" in out
    assert "state: submitted" in out          # no state.json -> default
    assert "events (last" in out
    assert "artifacts:" in out
    assert err == ""


def test_status_retry_warnings_line(runs_dir, capsys):
    write_run(runs_dir, "retry", [
        ("ticket_parsed", {"ticket_id": "TT-002"}),
        _sc("submitted", "reproducing"),
        _repro(1, False), _repro(2, False), _repro(3, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 3}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, False), _fix(2, False), _fix(3, True),
    ] + GREEN_TAIL)
    rc, out, err = _status(runs_dir, "retry", capsys)
    assert rc == 0
    assert "verdict: verified-with-warnings (repro retried, 3 rounds)" in out


def test_status_gates_failed_line(runs_dir, capsys):
    write_run(runs_dir, "gates_failed", [
        ("ticket_parsed", {"ticket_id": "TT-003"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True),
        _sc("fixing", "fixed"), _sc("fixed", "gating"),
        ("gates_failed", {"secrets": [], "lint": []}),
        _sc("gating", "failed"),
    ])
    rc, out, err = _status(runs_dir, "gates_failed", capsys)
    assert rc == 0
    assert "verdict: not-verified (gates failed)" in out


def test_status_budget_exhausted_line(runs_dir, capsys):
    events = [
        ("ticket_parsed", {"ticket_id": "TT-004"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
    ]
    for rnd in range(1, 9):  # 8 rounds, all rejected by the harness
        events.append(_fix(rnd, False))
    events.append(_sc("fixing", "failed"))
    write_run(runs_dir, "exhausted", events)
    rc, out, err = _status(runs_dir, "exhausted", capsys)
    assert rc == 0
    assert "verdict: not-verified (budget exhausted)" in out


def test_status_human_rejected_line(runs_dir, capsys):
    write_run(runs_dir, "rejected", [
        ("ticket_parsed", {"ticket_id": "TT-005"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True),
    ] + GREEN_TAIL + [
        ("rejected", {"by": "human"}), _sc("awaiting_approval", "rejected"),
    ])
    rc, out, err = _status(runs_dir, "rejected", capsys)
    assert rc == 0
    assert "verdict: verified-with-warnings (rejected by human)" in out


def test_status_empty_log_prints_not_verified(runs_dir, capsys):
    write_run(runs_dir, "empty", [])
    rc, out, err = _status(runs_dir, "empty", capsys)
    assert rc == 0
    assert "verdict: not-verified (never reached awaiting approval)" in out


def test_status_missing_run_prints_no_verdict(runs_dir, capsys):
    rc, out, err = _status(runs_dir, "nope", capsys)
    assert rc == 1
    assert "run not found: nope" in err
    assert "verdict:" not in out
