/**
 * The public command surface, exercised over real HTTP.
 *
 * This layer is the one on the internet. Until this test existed, nothing
 * checked it: DASHBOARD_TOKEN guarded the internal hop to the Python
 * service while the Express layer in front of it accepted anything, so
 * knowing the hostname was enough to force-clear a SAFETY kill switch or
 * start a scan that places a real order.
 *
 * Bundled with esbuild (already a dependency) and driven with node:test,
 * so the assertions are about what the server actually answers rather
 * than about the shape of the source.
 */

import assert from "node:assert/strict";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import { build } from "esbuild";

const TOKEN = "test-token-value";
let server;
let origin;
let workdir;

async function loadApp(tag) {
  // Bundled INSIDE the project: the app imports express and friends as
  // externals, and Node resolves those from the nearest node_modules.
  // A temp directory elsewhere on disk has none.
  workdir = path.resolve(".runtime", "test");
  mkdirSync(workdir, { recursive: true });
  const outfile = path.join(workdir, `app-${tag}.mjs`);
  await build({
    entryPoints: [path.resolve("server/app.ts")],
    outfile,
    bundle: true,
    platform: "node",
    format: "esm",
    packages: "external",
    logLevel: "silent",
  });
  return (await import(`file://${outfile}`)).default;
}

async function post(pathname, body, headers = {}) {
  const response = await fetch(`${origin}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body ?? {}),
  });
  const text = await response.text();
  return { status: response.status, body: text ? JSON.parse(text) : {} };
}

describe("command surface authentication", () => {
  before(async () => {
    process.env.DASHBOARD_TOKEN = TOKEN;
    const app = await loadApp("with-token");
    server = app.listen(0);
    await new Promise((resolve) => server.once("listening", resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
  });

  after(() => {
    server?.close();
  });

  it("refuses to clear the kill switch without the token", async () => {
    const { status, body } = await post("/api/control/kill-switch", {
      active: false,
      force: true,
    });
    assert.equal(status, 401, "an unauthenticated force-clear must be refused");
    assert.equal(body.code, "UNAUTHORIZED");
  });

  it("refuses a wrong token, and does not say how it is wrong", async () => {
    const short = await post(
      "/api/control/scan",
      {},
      { "X-Dashboard-Token": "x" },
    );
    const long = await post(
      "/api/control/scan",
      {},
      { "X-Dashboard-Token": `${TOKEN}-extra` },
    );
    assert.equal(short.status, 401);
    assert.equal(long.status, 401);
    assert.equal(short.body.error, long.body.error, "the refusal must not vary with the guess");
  });

  it("refuses to start a scan without the token", async () => {
    const { status } = await post("/api/control/scan", {});
    assert.equal(status, 401, "a scan can place a real order");
  });

  it("refuses to switch strategy without the token", async () => {
    const { status } = await post("/api/control/strategy", { strategy: "reversion" });
    assert.equal(status, 401);
  });

  it("refuses to resume scanning without the token", async () => {
    const { status } = await post("/api/control/scanning", { enabled: true });
    assert.equal(status, 401, "resuming is not a safe direction");
  });

  it("ALWAYS allows stopping the bot, token or not", async () => {
    // An operator must be able to hit the brakes from any device, having
    // lost anything. These reach the router and fail only because no bot
    // process is running behind it — which is the point: they passed auth.
    for (const [pathname, payload] of [
      ["/api/control/kill-switch", { active: true, reason: "test" }],
      ["/api/control/scanning", { enabled: false }],
    ]) {
      const { status } = await post(pathname, payload);
      assert.notEqual(status, 401, `${pathname} must never be locked out`);
    }
  });

  it("accepts the correct token, by either header", async () => {
    const bearer = await post("/api/control/scan", {}, { Authorization: `Bearer ${TOKEN}` });
    const direct = await post("/api/control/scan", {}, { "X-Dashboard-Token": TOKEN });
    assert.notEqual(bearer.status, 401);
    assert.notEqual(direct.status, 401);
  });
});

describe("with no token configured", () => {
  let bareServer;
  let bareOrigin;

  before(async () => {
    delete process.env.DASHBOARD_TOKEN;
    const app = await loadApp("no-token");
    bareServer = app.listen(0);
    await new Promise((resolve) => bareServer.once("listening", resolve));
    bareOrigin = `http://127.0.0.1:${bareServer.address().port}`;
  });

  after(() => {
    bareServer?.close();
    if (workdir) rmSync(workdir, { recursive: true, force: true });
  });

  it("disables the dangerous controls rather than leaving them open", async () => {
    const response = await fetch(`${bareOrigin}/api/control/kill-switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: false, force: true }),
    });
    const body = await response.json();
    assert.equal(response.status, 503);
    assert.equal(body.code, "NO_TOKEN_CONFIGURED");
    assert.match(body.error, /DASHBOARD_TOKEN/, "the message must name the fix");
  });

  it("still lets the operator stop the bot", async () => {
    const response = await fetch(`${bareOrigin}/api/control/kill-switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: true, reason: "test" }),
    });
    const body = await response.json();
    // It reaches the router and reports the bot unreachable, because no
    // Python process is running behind this test. That IS the pass: it
    // was never turned away by the lock. A 503 from the lock carries
    // NO_TOKEN_CONFIGURED; this one does not.
    assert.notEqual(response.status, 401);
    assert.notEqual(body.code, "NO_TOKEN_CONFIGURED", "stopping must never be locked out");
  });
});
