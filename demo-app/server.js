const http = require("http");
const TAX_RATE = 0.15;                       // BUG: ignores ./config.json (0.18)
http.createServer((req, res) => {
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/tax") {
    const amount = Number(u.searchParams.get("amount") || 0);
    const tax = amount * TAX_RATE;
    res.end(JSON.stringify({ amount, tax, total: amount + tax }));
  } else res.end("fde demo app");
}).listen(process.env.PORT || 8124);
