from pathlib import Path

import pytest

from fde.ticket import TicketError, parse_ticket

SAMPLE = Path(__file__).parent / "fixtures" / "sample.md"


def test_valid_ticket_parses():
    meta = parse_ticket(SAMPLE)
    assert meta["id"] == "TT-001"
    assert meta["severity"] == "high"
    assert meta["system"] == "tier1_checkout"
    assert meta["expected"] == "3 items at $10 should total $31.50 (5% tax)"
    assert meta["actual"] == "total comes to $30.05"
    assert meta["symptom"] == "total should be 31.5"


def test_missing_field_raises_with_field_name(tmp_path):
    p = tmp_path / "t.md"
    p.write_text("---\nid: TT-002\nseverity: med\nsystem: x\n---\nbody", encoding="utf-8")
    with pytest.raises(TicketError) as ei:
        parse_ticket(p)
    assert "expected" in str(ei.value)
    assert "actual" in str(ei.value)
    assert "symptom" in str(ei.value)


def test_short_symptom_raises(tmp_path):
    p = tmp_path / "t.md"
    p.write_text(
        "---\nid: TT-003\nseverity: low\nsystem: x\n"
        "expected: e\nactual: a\nsymptom: short\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(TicketError) as ei:
        parse_ticket(p)
    assert "symptom" in str(ei.value)


def test_bad_severity_raises(tmp_path):
    p = tmp_path / "t.md"
    p.write_text(
        "---\nid: TT-004\nseverity: catastrophic\nsystem: x\n"
        "expected: e\nactual: a\nsymptom: distinctive token\n---\nbody",
        encoding="utf-8",
    )
    with pytest.raises(TicketError) as ei:
        parse_ticket(p)
    assert "severity" in str(ei.value)


def test_body_captured(tmp_path):
    p = tmp_path / "t.md"
    p.write_text(
        "---\nid: TT-005\nseverity: critical\nsystem: x\n"
        "expected: e\nactual: a\nsymptom: distinctive token\n---\n# Title\n\nSome body text.",
        encoding="utf-8",
    )
    meta = parse_ticket(p)
    assert meta["body"].startswith("# Title")


def test_all_problems_reported_not_just_first(tmp_path):
    p = tmp_path / "t.md"
    p.write_text("---\nid: TT-006\n---\nbody", encoding="utf-8")
    with pytest.raises(TicketError) as ei:
        parse_ticket(p)
    msg = str(ei.value)
    for field in ("severity", "system", "expected", "actual", "symptom"):
        assert f"missing field: {field}" in msg
