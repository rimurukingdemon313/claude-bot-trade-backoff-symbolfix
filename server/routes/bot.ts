/**
 * Dashboard API — a thin, read-mostly proxy onto the bot service.
 *
 * Every response passes through unchanged. When the bot process is not
 * reachable this returns an explicit OFFLINE status rather than a cached
 * or placeholder value: a dashboard that invents numbers is worse than one
 * that admits it cannot see the system.
 */

import { Router, type IRouter, type Request, type Response } from "express";
import { BotUnavailableError, botClient } from "../lib/bot-client";

const router: IRouter = Router();

const READ_ROUTES = [
  "/health",
  "/account",
  "/positions",
  "/history",
  "/performance",
  "/scan",
  "/journal",
  "/risk",
  "/snapshot",
] as const;

function offlinePayload(path: string, error: unknown) {
  const message = error instanceof Error ? error.message : "bot service unavailable";
  return {
    status: "OFFLINE" as const,
    error: message,
    path,
    data: null,
    hint: "The trading process is not answering. No trading is happening while this is true.",
  };
}

for (const path of READ_ROUTES) {
  router.get(`/api${path}`, async (req: Request, res: Response) => {
    const query = req.originalUrl.includes("?")
      ? `?${req.originalUrl.split("?").slice(1).join("?")}`
      : "";
    try {
      return res.json(await botClient.get(`/api${path}${query}`));
    } catch (error) {
      const status = error instanceof BotUnavailableError ? 503 : 502;
      req.log?.error?.({ err: error }, `bot proxy failed for ${path}`);
      return res.status(status).json(offlinePayload(path, error));
    }
  });
}

const COMMAND_ROUTES: Record<string, { path: string; timeoutMs?: number }> = {
  "/api/control/scanning": { path: "/api/control/scanning" },
  "/api/control/kill-switch": { path: "/api/control/kill-switch" },
  // A manual scan analyses every configured symbol and may place an
  // order, so it needs a far longer budget than a status read.
  "/api/control/scan": { path: "/api/control/scan", timeoutMs: 240_000 },
  "/api/control/reconcile": { path: "/api/control/reconcile", timeoutMs: 60_000 },
};

for (const [route, target] of Object.entries(COMMAND_ROUTES)) {
  router.post(route, async (req: Request, res: Response) => {
    try {
      return res.json(await botClient.post(target.path, req.body ?? {}, target.timeoutMs));
    } catch (error) {
      const status = error instanceof BotUnavailableError ? 503 : 502;
      return res.status(status).json(offlinePayload(route, error));
    }
  });
}

/**
 * Container health. Reports the BOT's health, not merely "Express is up" —
 * a green check on a process that cannot trade would be worse than no
 * check at all.
 */
router.get("/healthz", async (_req: Request, res: Response) => {
  try {
    const payload = (await botClient.get("/healthz", 5000)) as { status?: string };
    const healthy = payload?.status === "ok";
    return res.status(healthy ? 200 : 503).json({
      status: healthy ? "ok" : "degraded",
      bot: payload,
    });
  } catch (error) {
    return res.status(503).json({
      status: "unavailable",
      error: error instanceof Error ? error.message : "unknown",
    });
  }
});

export default router;
