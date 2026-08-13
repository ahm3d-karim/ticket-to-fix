---
id: TT-002
severity: high
system: tier2_billing
expected: "invoice(100) should return 118.0 (18% tax read from config.py)"
actual: "invoice(100) returns 115.0 (hardcoded 15% rate)"
symptom: "invoice 100 should be 118"
---
# Invoice tax rate ignores config

billing.py hardcodes a 15% tax rate instead of reading TAX_RATE from config.py (0.18).
An invoice of 100 should total 118, but the current code returns 115.
