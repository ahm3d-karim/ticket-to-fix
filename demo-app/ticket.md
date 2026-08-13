---
id: TT-004
severity: high
system: demo-app
expected: "GET /tax?amount=100 returns total 118 (18% tax from config.json)"
actual: "server returns total 115 (hardcoded 15% tax, config.json ignored)"
symptom: "total should be 118"
---
# Demo app tax rate ignores config.json

The demo server applies a hardcoded 15% tax rate instead of reading
`TAX_RATE` from `config.json` (0.18). A $100 purchase is taxed $15
(total 115) when it should be taxed $18 (total 118).
