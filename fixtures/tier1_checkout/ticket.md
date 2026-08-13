---
id: TT-001
severity: high
system: tier1_checkout
expected: "3 items at $10 should total $31.50 (5% tax on the subtotal)"
actual: "total comes to $30.05 (flat $0.05 tax added)"
symptom: "total should be 31.5"
---
# Checkout total miscalculated

The checkout calculator applies tax as a flat fee instead of 5% of the subtotal.
3 items at $10 each should total $31.50, but the current code returns $30.05.
