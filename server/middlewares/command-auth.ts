/**
 * Authentication for the command surface.
 *
 * The bot service behind this proxy has always checked DASHBOARD_TOKEN.
 * This layer did not — and this layer is the one on the internet. The
 * token protected the internal hop and nothing else, so anyone who
 * learned the deployment's hostname could force-clear a SAFETY kill
 * switch or trigger a scan that places a real order. Hostnames are not
 * secrets: every certificate issued for one is published in certificate
 * transparency logs.
 *
 * CORS is closed, which stops a website driving these controls through a
 * visitor's browser. It does nothing about a direct request, which is all
 * this takes.
 *
 * The rule applied here is the dashboard's own (project rule 11): the
 * dashboard may always make the system SAFER. So the split is by
 * direction, not by endpoint:
 *
 *   - commands that can only reduce activity — trip the kill switch,
 *     pause scanning — are never blocked. An operator must always be able
 *     to stop the bot, including from a phone, including having lost the
 *     token.
 *   - everything that can resume, widen, or initiate — clearing the kill
 *     switch, resuming scanning, switching strategy, triggering a scan,
 *     resetting paper state — requires the token.
 *
 * With no token configured the second group is refused outright rather
 * than left open. That is the failure this whole file exists to prevent,
 * and an error message naming the variable is a better outcome than a
 * silently exposed control.
 */

import { timingSafeEqual } from "node:crypto";
import type { NextFunction, Request, Response } from "express";

/** Commands that can only ever reduce what the system is doing. */
function isSafeDirection(path: string, body: unknown): boolean {
  const payload = (body ?? {}) as Record<string, unknown>;
  if (path === "/api/control/kill-switch") {
    // Tripping is safe; clearing is not.
    return payload["active"] === true;
  }
  if (path === "/api/control/scanning") {
    // Pausing is safe; resuming is not.
    return payload["enabled"] === false;
  }
  return false;
}

/** Constant-time compare that does not leak length through early return. */
function matches(supplied: string, expected: string): boolean {
  const a = Buffer.from(supplied);
  const b = Buffer.from(expected);
  if (a.length !== b.length) {
    // Still burn a comparison so a wrong length is not measurably faster.
    timingSafeEqual(b, b);
    return false;
  }
  return timingSafeEqual(a, b);
}

function suppliedToken(req: Request): string | null {
  const header = req.get("authorization");
  if (header?.startsWith("Bearer ")) return header.slice(7).trim();
  const direct = req.get("x-dashboard-token");
  return direct?.trim() || null;
}

export function commandAuth(req: Request, res: Response, next: NextFunction) {
  if (req.method !== "POST") return next();
  if (!req.path.startsWith("/api/control/")) return next();

  if (isSafeDirection(req.path, req.body)) return next();

  const expected = process.env["DASHBOARD_TOKEN"]?.trim();
  if (!expected) {
    return res.status(503).json({
      error:
        "This control is disabled because DASHBOARD_TOKEN is not set. Without it, anyone who " +
        "knows this URL could clear the kill switch or start a scan. Set DASHBOARD_TOKEN in the " +
        "deployment environment, then unlock the dashboard with the same value. Stopping the " +
        "bot works without it.",
      code: "NO_TOKEN_CONFIGURED",
    });
  }

  const supplied = suppliedToken(req);
  if (!supplied || !matches(supplied, expected)) {
    return res.status(401).json({
      error: "Unlock the dashboard to use this control.",
      code: "UNAUTHORIZED",
    });
  }
  return next();
}
