const { test, after } = require("node:test");
const assert = require("node:assert");
const { spawn } = require("node:child_process");
const path = require("node:path");

const PORT = 8124;
const BASE = `http://127.0.0.1:${PORT}`;
const APP_DIR = path.join(__dirname, "..");

let server = null;

// Poll the server until it accepts connections (up to ~5s), so the test is
// robust against process startup latency.
function waitForServer(url, timeoutMs = 5000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const res = await fetch(url);
        if (res.ok) return resolve();
      } catch {
        // not up yet — keep polling
      }
      if (Date.now() - start > timeoutMs) {
        return reject(new Error(`server did not start on port ${PORT} within ${timeoutMs}ms`));
      }
      setTimeout(tick, 100);
    };
    tick();
  });
}

after(() => {
  if (server) {
    server.kill();
    server = null;
  }
});

test("GET /tax?amount=100 totals 118 (18% tax from config.json)", async () => {
  server = spawn(process.execPath, ["server.js"], {
    cwd: APP_DIR,
    env: { ...process.env, PORT: String(PORT) },
    stdio: "ignore",
  });

  await waitForServer(`${BASE}/tax?amount=100`);

  const res = await fetch(`${BASE}/tax?amount=100`);
  assert.strictEqual(res.status, 200);
  const body = await res.json();
  assert.strictEqual(body.total, 118, "total should be 118");
});
