const { test } = require("node:test");
const assert = require("node:assert");
const { total } = require("../calc.js");

test("total applies 5% tax to the subtotal", () => {
  assert.equal(total(10, 3), 31.5, "total should be 31.5");
});
