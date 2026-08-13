"""Tests for fde.verify — verification summary over harness-observed signals.

Fixtures are synthesized run.jsonl logs (never real runs/): each test writes
a small event stream into a tmp dir and asserts the aggregated signals and
the verdict that fde.verify derives from them.
"""
import json

import pytest

from fde.verify import render_summary, summarize_run, verdict


def write_run(tmp_path, name, events, state=None):
    """Write run.jsonl (+ optional state.json) for a synthetic run."""
    d = tmp_path / name
    d.mkdir()
    lines = []
    for i, (event, data) in enumerate(events):
        lines.append(json.dumps({"ts": f"2026-08-13T00:00:{i:02d}",
                                 "run_id": name, "event": event,
                                 "data": data}))
    (d / "run.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if state is not None:
        (d / "state.json").write_text(json.dumps({"state": state}),
                                      encoding="utf-8")
    return d


def _sc(from_, to):
    return ("state_changed", {"from": from_, "to": to})


def _repro(attempt, verdict_):
    return ("test_result", {"stage": "repro", "attempt": attempt,
                            "verdict": verdict_,
                            "checks": {"a": {"ok": True, "rc": 1},
                                       "b": {"ok": True, "rc": 0},
                                       "c": {"ok": True, "rc": 0}}})


def _fix(rnd, ok, diff_bytes=425):
    return ("fix_attempt", {"round": rnd, "ok": ok, "rc": 0 if ok else 1,
                            "diff_bytes": diff_bytes, "duration_ms": 100,
                            "summary": "", "backend": "codex"})


GREEN_TAIL = [
    _sc("fixing", "fixed"), _sc("fixed", "gating"),
    ("gates_passed", {"diff_bytes": 1126}),
    _sc("gating", "gated"), _sc("gated", "awaiting_approval"),
]


@pytest.fixture
def green_run(tmp_path):
    return write_run(tmp_path, "green", [
        ("ticket_parsed", {"ticket_id": "TT-001"}),
        _sc("submitted", "reproducing"),
        ("worktree_created", {"repo": "x", "worktree": "y"}),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True),
    ] + GREEN_TAIL)


@pytest.fixture
def retry_run(tmp_path):
    return write_run(tmp_path, "retry", [
        ("ticket_parsed", {"ticket_id": "TT-002"}),
        _sc("submitted", "reproducing"),
        _repro(1, False), _repro(2, False), _repro(3, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 3}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, False, 300), _fix(2, False, 310), _fix(3, True, 425),
    ] + GREEN_TAIL)


@pytest.fixture
def gates_failed_run(tmp_path):
    return write_run(tmp_path, "gates_failed", [
        ("ticket_parsed", {"ticket_id": "TT-003"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True, 900),
        _sc("fixing", "fixed"), _sc("fixed", "gating"),
        ("gates_failed", {"secrets": [(1, "secret_assignment", "x=...")],
                          "lint": []}),
        _sc("gating", "failed"),
    ])


@pytest.fixture
def failed_run(tmp_path):
    events = [
        ("ticket_parsed", {"ticket_id": "TT-004"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
    ]
    for rnd in range(1, 9):  # 8 rounds, all rejected by the harness
        events.append(_fix(rnd, False, 400 + rnd))
    events.append(_sc("fixing", "failed"))
    return write_run(tmp_path, "failed", events)


def test_green_first_try_signals(green_run):
    d = summarize_run(str(green_run))
    assert d["repro_first_try"] is True
    assert d["state_a_rejections"] == 0
    assert d["fix_rounds"] == 1
    assert d["rounds_vs_budget"] == {"used": 1, "budget": 8, "exhausted": False}
    assert d["gates"] == "passed"
    assert d["diff_bytes"] == 1126          # from gates_passed
    assert d["final_state"] == "awaiting_approval"
    assert d["reached_awaiting_approval"] is True
    assert verdict(d) == "verified"


def test_green_first_try_render(green_run):
    out = render_summary(str(green_run))
    assert "verdict: verified" in out
    for label in ("repro first-try", "state-A rejections", "fix rounds",
                  "gates", "diff size", "final state"):
        assert label in out
    assert "1126" in out
    assert "1 of 8" in out


def test_retry_heavy_signals_and_verdict(retry_run):
    d = summarize_run(str(retry_run))
    assert d["repro_first_try"] is False      # attempt 1 was rejected
    assert d["state_a_rejections"] == 2       # attempts 1 and 2 rejected
    assert d["fix_rounds"] == 3
    assert d["rounds_vs_budget"]["exhausted"] is False
    assert d["gates"] == "passed"
    assert d["reached_awaiting_approval"] is True
    assert verdict(d) == "verified-with-warnings"


def test_retry_heavy_render(retry_run):
    out = render_summary(str(retry_run))
    assert "verdict: verified-with-warnings" in out
    assert "2" in out                          # rejection count visible


def test_gates_failed_verdict(gates_failed_run):
    d = summarize_run(str(gates_failed_run))
    assert d["gates"] == "failed"
    assert d["final_state"] == "failed"
    assert d["reached_awaiting_approval"] is False
    assert d["diff_bytes"] == 900              # fallback: last fix_attempt
    assert verdict(d) == "not-verified"


def test_failed_run_rounds_exhausted(failed_run):
    d = summarize_run(str(failed_run))
    assert d["fix_rounds"] == 8
    assert d["rounds_vs_budget"] == {"used": 8, "budget": 8, "exhausted": True}
    assert d["gates"] == "none"
    assert d["final_state"] == "failed"
    assert verdict(d) == "not-verified"


def test_round8_success_is_warning_not_exhausted(tmp_path):
    # Fix succeeds exactly on the last allowed round -> reached awaiting
    # approval, so the budget was NOT exhausted and the verdict warns.
    events = [
        ("ticket_parsed", {"ticket_id": "TT-005"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
    ]
    for rnd in range(1, 8):
        events.append(_fix(rnd, False))
    events.append(_fix(8, True))
    events += GREEN_TAIL
    d = summarize_run(str(write_run(tmp_path, "r8", events)))
    assert d["fix_rounds"] == 8
    assert d["rounds_vs_budget"]["exhausted"] is False
    assert d["reached_awaiting_approval"] is True
    assert verdict(d) == "verified-with-warnings"


def test_deployed_run_still_verified(tmp_path):
    events = [
        ("ticket_parsed", {"ticket_id": "TT-006"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True),
    ] + GREEN_TAIL + [
        ("evidence_packaged", {"commit": "abc"}),
        ("approved", {"by": "human"}), _sc("awaiting_approval", "approved"),
        _sc("approved", "deploying"), _sc("deploying", "deployed"),
        ("deployed", {"commit": "abc"}),
    ]
    d = summarize_run(str(write_run(tmp_path, "deployed", events)))
    assert d["final_state"] == "deployed"
    assert d["reached_awaiting_approval"] is True
    assert verdict(d) == "verified"


def test_human_rejected_run_warns(tmp_path):
    # Machinery verified the fix (reached awaiting_approval) but the human
    # approval gate rejected it afterwards — that is a warning, not a clean
    # verified, and not a machinery failure.
    events = [
        ("ticket_parsed", {"ticket_id": "TT-007"}),
        _sc("submitted", "reproducing"),
        _repro(1, True),
        ("repro_test_written", {"file": "repro.test.js", "attempts": 1}),
        _sc("reproducing", "reproved"), _sc("reproved", "fixing"),
        _fix(1, True),
    ] + GREEN_TAIL + [
        ("rejected", {"by": "human"}), _sc("awaiting_approval", "rejected"),
    ]
    d = summarize_run(str(write_run(tmp_path, "rejected", events)))
    assert d["final_state"] == "rejected"
    assert d["reached_awaiting_approval"] is True
    assert verdict(d) == "verified-with-warnings"


def test_empty_run_defaults(tmp_path):
    d = summarize_run(str(tmp_path / "nowhere"))
    assert d["repro_first_try"] is None
    assert d["state_a_rejections"] == 0
    assert d["fix_rounds"] == 0
    assert d["rounds_vs_budget"]["exhausted"] is False
    assert d["gates"] == "none"
    assert d["diff_bytes"] is None
    assert d["final_state"] is None
    assert d["reached_awaiting_approval"] is False
    assert verdict(d) == "not-verified"
    assert "verdict: not-verified" in render_summary(str(tmp_path / "nowhere"))


def test_state_json_fallback_when_no_state_changed(tmp_path):
    # A run that never logged a state_changed event (e.g. an aborted run)
    # still reports the persisted state.json state.
    d = write_run(tmp_path, "aborted", [
        ("ticket_parsed", {"ticket_id": "TT-008"}),
        ("test_result", {"stage": "repro", "attempt": 1, "verdict": False,
                         "checks": {}}),
    ], state="reproducing")
    summary = summarize_run(str(d))
    assert summary["final_state"] == "reproducing"
    assert summary["repro_first_try"] is False
    assert summary["state_a_rejections"] == 1
