import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";
import app from "./app";
import { logger } from "./lib/logger";
import { runTradingCycle } from "./routes/trading";
import { startTradingScheduler } from "./lib/trading-scheduler";
import { getPersistedSchedulerEnabled } from "./routes/trading";

const execFileAsync = promisify(execFile);

const rawPort = process.env["PORT"] ?? "5000";
const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

// One-time TradeLocker connectivity diagnostic, run automatically on every
// boot. This exists purely to answer "which HTTP approach gets past
// TradeLocker's Cloudflare protection" from Railway's own logs, with zero
// manual steps — no shell access needed. It never blocks or fails
// startup: the trading scheduler starts regardless of what this prints.
// Safe to remove once the underlying Cloudflare-block issue is confirmed
// fixed; it adds a few seconds to boot and nothing else.
async function runStartupDiagnosticOnce(): Promise<void> {
  const script = path.resolve(process.cwd(), "diagnose_tradelocker_connection.py");
  logger.info("Running one-time TradeLocker connectivity diagnostic...");
  try {
    const { stdout, stderr } = await execFileAsync("python3", [script], {
      timeout: 60_000,
      maxBuffer: 2 * 1024 * 1024,
      env: process.env,
    });
    // Logged as plain text (not JSON) on purpose: this is meant to be read
    // directly in the Railway log viewer, line by line, not parsed.
    logger.info("===== TradeLocker connectivity diagnostic output (start) =====");
    for (const line of stdout.split("\n")) logger.info(line);
    if (stderr.trim()) {
      logger.info("----- diagnostic stderr -----");
      for (const line of stderr.split("\n")) logger.info(line);
    }
    logger.info("===== TradeLocker connectivity diagnostic output (end) =====");
  } catch (error) {
    const execError = error as NodeJS.ErrnoException & { stdout?: string; stderr?: string };
    logger.info("===== TradeLocker connectivity diagnostic output (start) =====");
    if (execError.stdout) {
      for (const line of execError.stdout.split("\n")) logger.info(line);
    }
    logger.info(
      `Diagnostic script exited with an error (this is fine, it still printed useful output above): ${execError.message}`,
    );
    logger.info("===== TradeLocker connectivity diagnostic output (end) =====");
  }
}

app.listen(port, "0.0.0.0", (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");

  runStartupDiagnosticOnce().catch((error) => {
    logger.error({ err: error }, "Startup diagnostic itself crashed (non-fatal, continuing)");
  });

  getPersistedSchedulerEnabled()
    .then((enabled) => {
      startTradingScheduler(async (source) => {
        await runTradingCycle(source);
      }, enabled);
    })
    .catch((error) => {
      logger.error({ err: error }, "Unable to load scheduler state; starting enabled");
      startTradingScheduler(async (source) => {
        await runTradingCycle(source);
      });
    });
});
