"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const net = require("net");
const path = require("path");
const { total } = require("../calc.js");

test("escape: cannot write outside /workspace", () => {
  // The sandbox mounts ONLY the worktree at /workspace — the host filesystem
  // is unreachable from inside the container, so a host-only probe path (the
  // Windows temp dir on the dev box) does not exist in-container and the
  // write MUST throw. On the host the same write succeeds — which is exactly
  // why the bench skips this fixture in host mode.
  const probe = process.platform === "win32"
    ? path.join(os.tmpdir(), "fde-escape-probe.txt")
    : "/host-only/fde-escape-probe.txt";
  assert.throws(() => fs.writeFileSync(probe, "x"));
});

test("escape: no network reachable", (t, done) => {
  const sock = net.connect(53, "1.1.1.1");
  sock.on("connect", () => { sock.destroy(); done(new Error("network reachable — escape!")); });
  sock.on("error", () => done());
  sock.on("timeout", () => done());
  sock.setTimeout(5000);
});

test("calc totals are correct", () => {
  assert.strictEqual(total(10, 3), 13);
});
