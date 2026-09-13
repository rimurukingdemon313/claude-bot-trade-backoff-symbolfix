/**
 * Container entry point.
 *
 * Starts the Python bot process as a supervised child, then serves the
 * dashboard. The bot is the system; Express is its window. If the bot
 * exits unexpectedly the whole container exits so Railway restarts it —
 * a dashboard that keeps serving a dead trading process is a lie.
 */

import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import app from "./app";
import { logger } from "./lib/logger";
import { waitForBot } from "./lib/bot-client";

const rawPort = process.env["PORT"] ?? "5000";
const port = Number(rawPort);
if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

let botProcess: ChildProcess | null = null;
let shuttingDown = false;

function startBot(): ChildProcess {
  const python = process.env["PYTHON_BIN"] ?? "python3";
  const child = spawn(python, ["-u", "-m", "bot.service"], {
    cwd: path.resolve(process.cwd()),
    env: process.env,
    stdio: ["ignore", "inherit", "inherit"],
  });

  child.on("exit", (code, signal) => {
    if (shuttingDown) return;
    logger.error({ code, signal }, "Bot process exited unexpectedly; exiting so the platform restarts us");
    process.exit(code ?? 1);
  });
  child.on("error", (error) => {
    logger.error({ err: error }, "Failed to start the bot process");
    process.exit(1);
  });
  return child;
}

if (process.env["BOT_EXTERNAL"] !== "true") {
  botProcess = startBot();
}

const server = app.listen(port, "0.0.0.0", (error?: Error) => {
  if (error) {
    logger.error({ err: error }, "Error listening on port");
    process.exit(1);
  }
  logger.info({ port }, "Dashboard server listening");
  void waitForBot();
});

/**
 * Graceful shutdown. The bot is signalled first and given time to stop
 * its scheduler and close the database cleanly; it deliberately never
 * closes broker positions on the way out.
 */
function shutdown(signal: string): void {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info({ signal }, "Shutting down");

  server.close(() => logger.info("HTTP server closed"));

  if (botProcess) {
    botProcess.kill("SIGTERM");
    const forceTimer = setTimeout(() => {
      logger.warn("Bot process did not stop in time; sending SIGKILL");
      botProcess?.kill("SIGKILL");
      process.exit(0);
    }, 15_000);
    botProcess.on("exit", () => {
      clearTimeout(forceTimer);
      logger.info("Bot process stopped cleanly");
      process.exit(0);
    });
  } else {
    process.exit(0);
  }
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("unhandledRejection", (reason) => {
  logger.error({ err: reason }, "Unhandled promise rejection");
});
