# Ticket-to-Fix — Fixture Corpus

Three standalone git repos under `fixtures/`, each with a buggy `main` branch
committed, a `gold.patch` (the known fix), a `ticket.md` (file-first ticket), and
an `fde.yaml` (repo manifest: install/test/run commands + app type).

## tier1_checkout — Tier 1 (node, no deps)

- **Bug:** `calc.js` adds a flat $0.05 tax instead of 5% of the subtotal (`p * q + TAX` instead of `p * q * (1 + TAX)`).
- **Symptom:** `total should be 31.5`
- **Repro:** `node --test` → `test/total.test.js` fails (30.05 ≠ 31.5).
- **Gold:** `p * q * (1 + TAX)`

## tier2_billing — Tier 2 (python/pytest)

- **Bug:** `billing.py` hardcodes 15% (`amount * (1 + 0.15)`), ignoring `config.py`'s `TAX_RATE = 0.18` (cross-file trace).
- **Symptom:** `invoice 100 should be 118`
- **Repro:** `python -m pytest -q` → `test_billing.py` fails (115.0 ≠ 118.0).
- **Gold:** `from config import TAX_RATE` + `return amount * (1 + TAX_RATE)`

## tier3_ingest — Tier 3 (node)

- **Bug:** `ingest.js` swallows the row-7 malformed error (`.catch(() => {})`); the error never surfaces anywhere — invisible without a test asserting error capture.
- **Symptom:** `row 7 malformed`
- **Repro:** `node --test` → `test/ingest.test.js` fails (`deepStrictEqual(errors, ["row 7 malformed"])`; errors is empty).
- **Gold:** `.catch(err => errors.push("row 7 malformed"))`

## Bench stats

| fixture | repro attempts | fix rounds | wall time | notes |
|---|---|---|---|---|
| tier1_checkout | 2 | 1 | ~6m | attempt 1 rejected (tampering — the 3-evasion war story); clean accept on 2 |
| tier2_billing | 1 | 1 | ~3.5m | first attempt accepted; earlier 3/3 rejections were a harness bug (gold reverted before state C) — fixed + regression-tested |
| tier3_ingest | 1 | 1 | ~3.5m | accepted first try; bug invisible without a test, so this is the difficulty tier |
| demo-app | 1 | 1 | ~3.5m | full-loop target; deployed + rolled back with live curl health checks |
