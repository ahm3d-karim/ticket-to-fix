"""S1: bench over the fixture corpus with the deterministic mock backend.

The mock backend (FDE_AGENT_BACKEND=mock) makes every agent step deterministic
and offline: the repro step writes a symptom-asserting test, the fix step
applies the fixture's gold.patch. The bench must therefore finish each
fixture in seconds and land in awaiting_approval (gates passed).
"""
import time
from pathlib import Path

import pytest

import fde.bench
from fde.bench import discover_fixtures, render_report, run_corpus

# one js fixture + one py fixture: covers both mock repro templates
FIXTURES = ["tier1_checkout", "tier2_billing"]
RESULT_KEYS = {"fixture", "run_id", "state", "repro_attempts", "fix_rounds",
               "wall_time", "rejection_reasons", "notes"}


@pytest.fixture(scope="module")
def mock_results():
    """Run the corpus once per module (each fixture is a full pipeline run)."""
    return run_corpus(backend="mock", fixtures=FIXTURES)


def test_mock_corpus_runs_end_to_end_in_seconds(mock_results):
    assert len(mock_results) == len(FIXTURES)
    by_name = {r["fixture"]: r for r in mock_results}
    assert set(by_name) == set(FIXTURES)

    for r in mock_results:
        assert RESULT_KEYS.issubset(r.keys()), r
        # the mock is deterministic: first-try repro, first-round fix
        assert r["state"] == "awaiting_approval", r
        assert r["repro_attempts"] == 1, r
        assert r["fix_rounds"] == 1, r
        assert not r["rejection_reasons"], r
        # deterministic + offline: seconds, not minutes
        assert r["wall_time"] < 60, r

    total = sum(r["wall_time"] for r in mock_results)
    assert total < 180, total


def test_render_report_is_a_table_with_summary(mock_results):
    report = render_report(mock_results)
    lines = report.splitlines()
    assert len(lines) >= 4  # header + separator + rows + summary

    header = lines[0].lower()
    for col in ("fixture", "state", "repro_attempts", "fix_rounds", "wall_time"):
        assert col in header, header

    body = "\n".join(lines)
    for r in mock_results:
        assert r["fixture"] in body
    assert "awaiting_approval" in body
    summary = lines[-1].lower()
    assert "fixture" in summary and "awaiting_approval" in summary


def test_discover_fixtures_includes_corpus():
    names = {d.name for d in discover_fixtures()}
    assert {"tier1_checkout", "tier2_billing", "tier3_ingest", "demo-app"} <= names
    for d in discover_fixtures():
        assert (d / "fde.yaml").is_file()


def test_bench_skips_tier6_escape_in_host_mode(monkeypatch):
    # Host mode (sandbox inactive): tier6's escape-denial assertions only hold
    # in-container, so the bench must skip the fixture BEFORE any CLI call —
    # no run_id, no pipeline steps, and the gate stays green.
    monkeypatch.setattr("fde.bench.sandbox_active", lambda: False)
    results = run_corpus(backend="mock", fixtures=["tier6_escape"])
    assert len(results) == 1
    r = results[0]
    assert r["fixture"] == "tier6_escape"
    assert r["state"] == "skipped"
    assert r["run_id"] is None
    assert r["wall_time"] == 0.0
    assert any("requires FDE_SANDBOX=docker" in n for n in r["notes"]), r


def test_bench_gate_carves_out_tier6_escape(monkeypatch):
    # A failed tier6_escape is NOT a corpus failure (host-mode fallout is
    # expected by design — same class as tier5_outofscope); any other failed
    # fixture still trips the gate.
    def _result(fixture: str) -> dict:
        return {"fixture": fixture, "run_id": None, "state": "failed",
                "repro_attempts": 0, "fix_rounds": 0, "wall_time": 0.0,
                "rejection_reasons": [], "notes": []}

    monkeypatch.setattr(
        "fde.bench.run_corpus",
        lambda backend="codex", fixtures=None: [
            _result("tier5_outofscope"), _result("tier6_escape")])
    assert fde.bench.main([]) == 0

    monkeypatch.setattr(
        "fde.bench.run_corpus",
        lambda backend="codex", fixtures=None: [
            _result("tier6_escape"), _result("tier1_checkout")])
    assert fde.bench.main([]) == 1
