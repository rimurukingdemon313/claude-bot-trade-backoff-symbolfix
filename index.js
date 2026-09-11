import { execFile } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import { build } from "esbuild";

const execFileAsync = promisify(execFile);
const root = process.cwd();
const frontendEntry = path.join(root, "dist", "index.html");
const runtimeDirectory = path.join(root, ".runtime");
const bundledServer = path.join(runtimeDirectory, "server.mjs");

async function ensureFrontendBuilt() {
  if (existsSync(frontendEntry)) return;
  await execFileAsync("npm", ["run", "build"], {
    cwd: root,
    maxBuffer: 4 * 1024 * 1024,
  });
}

async function start() {
  await ensureFrontendBuilt();
  mkdirSync(runtimeDirectory, { recursive: true });
  await build({
    entryPoints: [path.join(root, "server", "index.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    packages: "external",
    outfile: bundledServer,
    sourcemap: true,
    logLevel: "info",
  });
  await import(`${bundledServer}?startup=${Date.now()}`);
}

start().catch((error) => {
  console.error("Unable to start standalone paper-trading app:", error);
  process.exitCode = 1;
});