"""Layer 3: `fde audit <run_id>` — tamper-evident audit-log verification.

Synthetic runs in tmp dirs (never real runs/); cmd_audit is driven directly
with both RUNS_DIR bindings monkeypatched (fde.cli imports the name from
fde.runlog, so both must point at the tmp dir).
"""
import json
from types import SimpleNamespace

import pytest

from fde.cli import build_parser, cmd_audit
from fde.runlog import append, new_run_id


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("fde.runlog.RUNS_DIR", tmp_path)
    monkeypatch.setattr("fde.cli.RUNS_DIR", tmp_path)
    return tmp_path


def _make_run(runs_dir, n=3):
    rid = new_run_id()
    for i in range(n):
        append(rid, "ticket_parsed", {"ticket_id": f"TT-{i}"})
    return rid


def _audit(run_id, capsys):
    rc = cmd_audit(SimpleNamespace(run_id=run_id))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_audit_intact_prints_count(runs_dir, capsys):
    rid = _make_run(runs_dir, 3)
    rc, out, err = _audit(rid, capsys)
    assert rc == 0
    assert "audit: chain intact (3 events)" in out
    assert err == ""


def test_audit_tampered_run_exits_1(runs_dir, capsys):
    rid = _make_run(runs_dir, 3)
    p = runs_dir / rid / "run.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    bad = json.loads(lines[1])
    bad["data"] = {"ticket_id": "EVIL"}
    lines[1] = json.dumps(bad)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc, out, err = _audit(rid, capsys)
    assert rc == 1
    assert "audit: CHAIN BROKEN" in out


def test_audit_missing_run_exits_1(runs_dir, capsys):
    rc, out, err = _audit("nope", capsys)
    assert rc == 1
    assert "audit: CHAIN BROKEN" in out


def test_audit_subcommand_parses():
    args = build_parser().parse_args(["audit", "some-run"])
    assert args.command == "audit"
    assert args.run_id == "some-run"


def test_audit_listed_in_help(capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--help"])
    assert e.value.code == 0
    assert "audit" in capsys.readouterr().out
