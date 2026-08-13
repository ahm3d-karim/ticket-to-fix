const { test } = require("node:test");
const assert = require("node:assert");
const { ingest, errors } = require("../ingest.js");

test("ingest surfaces malformed row errors", async () => {
  await ingest();
  assert.deepStrictEqual(errors, ["row 7 malformed"]);
});
