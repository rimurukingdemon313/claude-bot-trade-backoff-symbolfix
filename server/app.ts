import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import path from "node:path";
import router from "./routes";
import { logger } from "./lib/logger";
import { commandAuth } from "./middlewares/command-auth";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return { id: req.id, method: req.method, url: req.url?.split("?")[0] };
      },
      res(res) {
        return { statusCode: res.statusCode };
      },
    },
  }),
);

/**
 * CORS is closed by default. The previous build called bare `cors()`,
 * which sends `Access-Control-Allow-Origin: *` — any website a user
 * visited could have driven the trading controls from their browser.
 * Set DASHBOARD_ORIGINS to a comma-separated allowlist only if the
 * dashboard is genuinely served from a different origin.
 */
const allowedOrigins = (process.env["DASHBOARD_ORIGINS"] ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

app.use(
  cors({
    origin: allowedOrigins.length > 0 ? allowedOrigins : false,
    credentials: false,
  }),
);

app.use(express.json({ limit: "64kb" }));
app.use(express.urlencoded({ extended: false, limit: "64kb" }));

app.use((_req, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "no-referrer");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("Permissions-Policy", "geolocation=(), microphone=(), camera=()");
  next();
});

/**
 * Rate limit on the command surface. The read endpoints are polled by the
 * dashboard and are cheap; the POST endpoints can trigger a full scan, so
 * they are capped per source address.
 */
const commandHits = new Map<string, { count: number; resetAt: number }>();
const COMMAND_WINDOW_MS = 60_000;
const COMMAND_LIMIT = 20;
/**
 * Ceiling on tracked addresses.
 *
 * An expired entry was only ever replaced when the SAME address came back,
 * so the map grew by one per distinct caller and never shrank. On a public
 * URL the distinct callers are the internet — every scanner that finds the
 * host — and the rate limiter meant to protect the process became the way
 * to exhaust its memory.
 */
const COMMAND_TRACKED_MAX = 5_000;

function pruneExpired(now: number): void {
  for (const [address, entry] of commandHits) {
    if (entry.resetAt < now) commandHits.delete(address);
  }
}

app.use((req, res, next) => {
  if (req.method !== "POST") return next();
  const key = req.ip ?? "unknown";
  const now = Date.now();

  if (commandHits.size >= COMMAND_TRACKED_MAX) {
    pruneExpired(now);
    if (commandHits.size >= COMMAND_TRACKED_MAX && !commandHits.has(key)) {
      // Every tracked window is still live: this is a distributed flood,
      // not ordinary traffic. Refusing is correct, and refusing is also
      // what keeps the map from growing past the ceiling.
      return res.status(429).json({ error: "too many commands; slow down" });
    }
  }

  const entry = commandHits.get(key);
  if (!entry || entry.resetAt < now) {
    commandHits.set(key, { count: 1, resetAt: now + COMMAND_WINDOW_MS });
    return next();
  }
  entry.count += 1;
  if (entry.count > COMMAND_LIMIT) {
    return res.status(429).json({ error: "too many commands; slow down" });
  }
  return next();
});

// Ordered after the body parser (it inspects the payload to tell a
// "stop" from a "start") and before the router, so no control runs
// unauthenticated.
app.use(commandAuth);

app.use(router);

const publicDirectory = path.resolve(process.cwd(), "dist");
app.use(express.static(publicDirectory, { maxAge: "1h", index: false }));
app.use((req, res, next) => {
  if (req.method === "GET" && !req.path.startsWith("/api")) {
    return res.sendFile(path.join(publicDirectory, "index.html"), (error) => {
      if (error) next(error);
    });
  }
  return next();
});

export default app;
