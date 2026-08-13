---
id: TT-003
severity: med
system: tier3_ingest
expected: "malformed row 7 error is captured in the errors list"
actual: "row 7 error is swallowed; errors list stays empty"
symptom: "row 7 malformed"
---
# Ingest swallows malformed row errors

ingest.js catches the row 7 error with an empty handler, so the malformed-row
error is never surfaced anywhere. The errors list ends up empty instead of
containing "row 7 malformed".
