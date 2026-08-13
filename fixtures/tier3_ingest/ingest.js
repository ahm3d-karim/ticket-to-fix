// ingest.js — batch ingest pipeline. Row 7 is malformed; its error is swallowed.
const errors = [];

function processRow(row) {
  return new Promise((resolve, reject) => {
    if (row === 7) reject(new Error("row 7 malformed"));
    else resolve({ row, ok: true });
  });
}

async function ingest() {
  const seen = [];
  for (let row = 1; row <= 10; row++) {
    await processRow(row).catch(() => {}); // BUG: error swallowed, never surfaced
    seen.push(row);
  }
  return seen;
}

module.exports = { ingest, errors };

if (require.main === module) {
  ingest().then((rows) => console.log(`ingested ${rows.length} rows`));
}
