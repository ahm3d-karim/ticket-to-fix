from pathlib import Path
import yaml

REQUIRED = ["id", "severity", "system", "expected", "actual", "symptom"]
SEVERITIES = {"low", "med", "high", "critical"}
MIN_SYMPTOM = 6

class TicketError(Exception):
    pass

def parse_ticket(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise TicketError("ticket must start with YAML front-matter '---'")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    problems = []
    for f in REQUIRED:
        if not meta.get(f):
            problems.append(f"missing field: {f}")
    if meta.get("severity") and meta["severity"] not in SEVERITIES:
        problems.append(f"severity must be one of {sorted(SEVERITIES)}")
    if meta.get("symptom") and len(meta["symptom"]) < MIN_SYMPTOM:
        problems.append(f"symptom must be >= {MIN_SYMPTOM} chars")
    if problems:
        raise TicketError("; ".join(problems))
    meta["body"] = body.strip()
    return meta
