import { logger } from "./logger";

export type SchedulerState = {
  enabled: boolean;
  running: boolean;
  lastRunAt: string | null;
  lastStatus: "idle" | "running" | "completed" | "failed";
  lastError: string | null;
  nextRunAt: string | null;
};

let timer: NodeJS.Timeout | null = null;
let cycleRunning = false;
const state: SchedulerState = {
  enabled: true,
  running: false,
  lastRunAt: null,
  lastStatus: "idle",
  lastError: null,
  nextRunAt: null,
};

function nextQuarterHour(now = new Date()): Date {
  const next = new Date(now);
  next.setUTCSeconds(5, 0);
  next.setUTCMinutes(Math.floor(now.getUTCMinutes() / 15) * 15 + 15);
  return next;
}

function scheduleNext(runCycle: (source: "scheduled") => Promise<unknown>) {
  if (timer) clearTimeout(timer);
  const next = nextQuarterHour();
  state.nextRunAt = next.toISOString();
  timer = setTimeout(async () => {
    if (state.enabled && !cycleRunning) {
      cycleRunning = true;
      state.running = true;
      state.lastStatus = "running";
      state.lastRunAt = new Date().toISOString();
      state.lastError = null;
      try {
        await runCycle("scheduled");
        state.lastStatus = "completed";
        logger.info({ candleSchedule: state.lastRunAt }, "Scheduled paper scan completed");
      } catch (error) {
        state.lastStatus = "failed";
        state.lastError = error instanceof Error ? error.message : "Scheduled scan failed";
        logger.error({ err: error }, "Scheduled paper scan failed");
      } finally {
        cycleRunning = false;
        state.running = false;
      }
    }
    scheduleNext(runCycle);
  }, Math.max(1000, next.getTime() - Date.now()));
  timer.unref?.();
}

export function startTradingScheduler(
  runCycle: (source: "scheduled") => Promise<unknown>,
  enabled = true,
): void {
  state.enabled = enabled;
  scheduleNext(runCycle);
  logger.info({ nextRunAt: state.nextRunAt }, "Paper trading scheduler started");
}

export function setTradingSchedulerEnabled(
  enabled: boolean,
  runCycle: (source: "scheduled") => Promise<unknown>,
): SchedulerState {
  state.enabled = enabled;
  if (!enabled) {
    state.running = false;
    state.lastStatus = "idle";
  }
  scheduleNext(runCycle);
  return { ...state };
}

export function getTradingSchedulerState(): SchedulerState {
  return { ...state };
}