---
id: TT-005
severity: high
system: tier4_rework
expected: "total(100) should be 118.0 (18% tax read from config.py)"
actual: "total(100) returns 115.0 (hardcoded 15% rate)"
symptom: "total 100 should be 118"
---
# Pricing total ignores config

pricing.py hardcodes a 15% tax rate instead of reading TAX_RATE from
config.py (0.18). A purchase of 100 should total 118, but the current
code returns 115.
