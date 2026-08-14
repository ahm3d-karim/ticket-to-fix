"use strict";
// BUG (by design): off-by-one — the repro symptom.
module.exports = {
  total(a, b) { return a + b - 1; }
};
