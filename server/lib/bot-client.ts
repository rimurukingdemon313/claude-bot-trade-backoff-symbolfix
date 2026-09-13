/**
 * Client for the Python bot service.
 *
 * The Node layer deliberately holds NO trading state and performs no
 * trading logic. It serves the dashboard and forwards requests to the bot
 * process, which owns the broker session, the database, the scheduler and
 * every safety guard. That separation is what makes "the dashboard is
 * presentation only" structurally true rather than a convention: there is
 * no code path here that could open, size, or close a position.
 */

import { logger } from "./logger";

const BOT_HOST = process.env["BOT_HOST"] ?? "127.0.0.1";
const BOT_PORT = process.env["BOT_PORT"] ?? "8787";
const BOT_BASE = `http://${BOT_HOST}:${BOT_PORT}`;
const DEFAULT_TIMEOUT_MS = 30_000;

export class BotUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BotUnavailableError";
  }
}

async function call(
  path: string,
  init: RequestInit = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...((init.headers as Record<string, string>) ?? {}),
    };
    // The bot service's command surface is token-protected. The token is
    // read from the server's own environment and never sent to the
    // browser, so a dashboard user cannot forge a command by hand.
    const token = process.env["DASHBOARD_TOKEN"];
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const response = await fetch(`${BOT_BASE}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : {};
    if (!response.ok && response.status >= 500 && response.status !== 503) {
      throw new Error(
        `bot service ${path} returned ${response.status}: ${text.slice(0, 200)}`,
      );
    }
    return payload;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new BotUnavailableError(`bot service timed out after ${timeoutMs}ms on ${path}`);
    }
    if (error instanceof TypeError) {
      // fetch throws TypeError when the socket cannot be opened at all,
      // which is the normal state while the Python process is still booting.
      throw new BotUnavailableError(`bot service is not reachable on ${BOT_BASE}`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const botClient = {
  get: (path: string, timeoutMs?: number) => call(path, {}, timeoutMs),
  post: (path: string, body: unknown = {}, timeoutMs?: number) =>
    call(path, { method: "POST", body: JSON.stringify(body) }, timeoutMs),
};

/** True once the bot service answers its health endpoint. */
export async function waitForBot(attempts = 30, delayMs = 1000): Promise<boolean> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      await call("/healthz", {}, 4000);
      logger.info("Bot service is reachable");
      return true;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  logger.error("Bot service did not become reachable; the dashboard will report it as offline");
  return false;
}
