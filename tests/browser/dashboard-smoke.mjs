/**
 * Does the dashboard actually render in a browser?
 *
 * This exists because a shipped build with a clean typecheck and a green
 * unit suite still met the operator with "The dashboard hit an error":
 * one component read `envelope.ageSeconds` in a place where the envelope
 * is undefined while the first fetch is in flight. Nothing below the
 * browser can see that — React unmounts the whole tree on a throw, so a
 * single undefined read turns into a blank page.
 *
 * Two states, because the crash was in the one nobody looks at:
 *
 *   1. loaded    — real payloads, recorded from bot.api.DashboardApi
 *                  itself so the shapes cannot drift from the server's.
 *   2. loading   — every /api call hangs, which is the state the page is
 *                  in for the first seconds of every visit.
 *
 * Usage:  npm run build && npm run smoke
 * Requires Playwright and a Chromium at CHROMIUM_PATH (default is the
 * one the container ships).
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(HERE, "../../dist");
const FIXTURES = path.join(HERE, "fixtures");
const PORT = 4173;
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

if (!fs.existsSync(DIST)) {
  console.error("no dist/ — run `npm run build` first");
  process.exit(1);
}

const server = http.createServer((req, res) => {
  const url = req.url.split("?")[0];
  if (url.startsWith("/api/")) {
    const name = url.slice("/api/".length) + ".json";
    const file = path.join(FIXTURES, name);
    const body = fs.existsSync(file)
      ? fs.readFileSync(file, "utf8")
      : JSON.stringify({ status: "LIVE", data: null });
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(body);
  }
  const full = path.join(DIST, url === "/" ? "index.html" : url);
  if (!full.startsWith(DIST) || !fs.existsSync(full)) {
    res.writeHead(404);
    return res.end("not found");
  }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(full)] ?? "application/octet-stream" });
  res.end(fs.readFileSync(full));
});

await new Promise((resolve) => server.listen(PORT, resolve));

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || "/opt/pw-browsers/chromium",
});

/** An aborted fetch is the scenario, not a fault — only page errors count. */
const isPageFault = (line) => !line.includes("Failed to load resource");

async function render(label, { hangApi = false } = {}) {
  const page = await browser.newPage();
  const faults = [];
  page.on("pageerror", (error) => faults.push(`${label}: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text().split("\n")[0];
    if (isPageFault(text)) faults.push(`${label}: ${text}`);
  });
  if (hangApi) {
    await page.route("**/api/**", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 8000));
      await route.abort();
    });
  }
  await page.goto(`http://localhost:${PORT}/`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  const body = await page.innerText("body");
  if (body.includes("dashboard hit an error")) {
    faults.push(`${label}: the error boundary caught a render failure`);
  }
  await page.close();
  return faults;
}

const faults = [
  ...(await render("loaded")),
  ...(await render("loading", { hangApi: true })),
];

await browser.close();
server.close();

if (faults.length) {
  console.error("dashboard smoke FAILED:");
  for (const fault of faults) console.error("  " + fault);
  process.exit(1);
}
console.log("dashboard smoke: both states render with no page errors");
