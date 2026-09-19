/**
 * Dashboard data layer.
 *
 * Every value rendered by this app comes from one of these calls. There
 * is no local computation of balance, P/L, risk, or position size —
 * those are the bot's answers, and duplicating them in the browser is
 * exactly how two sources of truth drift apart.
 */

export type Status = "LIVE" | "OFFLINE" | "PENDING";

export type Envelope<T> = {
  status: Status;
  data: T | null;
  error?: string;
  hint?: string;
  /** True when the read failed only because FX is shut for the weekend. */
  marketClosed?: boolean;
  /**
   * When the bot last read this from the broker. The page never calls the
   * broker itself — it would queue behind a scan on the shared throttle —
   * so every served value carries its age and a value shown without one
   * would be indistinguishable from a current reading.
   */
  asOf?: string | null;
  ageSeconds?: number | null;
  stale?: boolean;
  /** Set when the last refresh attempt failed but a previous read stands. */
  refreshError?: string | null;
};

export type Account = {
  balance: number;
  equity: number;
  marginUsed: number;
  marginAvailable: number;
  openPnl: number;
  todayPnl: number;
  currency: string;
  dailyRealizedPnl: number;
  dailyPnl: number;
  totalPnl: number;
  peakEquity: number;
  drawdownPct: number;
  tradesToday: number;
  demoVerified: boolean;
  demoReason: string | null;
  environment: string;
  mode: 'paper' | 'demo_live';
  paper: boolean;
};

export type Position = {
  positionId: string;
  symbol: string;
  direction: "BUY" | "SELL";
  quantity: number;
  entryPrice: number;
  stopLoss: number | null;
  takeProfit: number | null;
  unrealizedPnl: number;
  openedAt: string | null;
  currentPrice: number | null;
  /** Why there is no current price: "live", "market closed for the weekend", … */
  priceStatus: string;
  rMultiple: number;
  riskAmount: number | null;
  setupGrade: string | null;
  executionId: string | null;
  durationMinutes: number | null;
  orphaned: boolean;
  /** False when we hold no record of this position — we did not open it. */
  tracked: boolean;
};

export type HistoryRow = {
  executionId: string;
  positionId: string | null;
  symbol: string;
  direction: string;
  openedAt: string | null;
  closedAt: string | null;
  entry: number | null;
  exit: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  quantity: number | null;
  riskAmount: number | null;
  pnl: number | null;
  rMultiple: number | null;
  setupGrade: string | null;
  setupScore: number | null;
  aiConfidence: number | null;
  exitReason: string | null;
  durationMinutes: number | null;
};

export type Performance = {
  trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  winRate: number | null;
  profitFactor: number | null;
  expectancy: number | null;
  averageWin: number | null;
  averageLoss: number | null;
  averageR: number | null;
  totalPnl: number;
  maxDrawdown: number | null;
  currentDrawdown: number | null;
  consecutiveWins: number;
  consecutiveLosses: number;
  dailyPnl: number;
  weeklyPnl: number;
  monthlyPnl: number;
  sample: "insufficient" | "adequate";
  bySymbol: Breakdown[];
  byGrade: Breakdown[];
  bySession: Breakdown[];
  equityCurve: Array<{ balance: number; equity: number; created_at: string }>;
  dailyHistory: Array<Record<string, unknown>>;
};

export type Breakdown = {
  key: string;
  trades: number;
  winRate: number | null;
  totalPnl: number;
  expectancy: number | null;
  profitFactor: number | null;
  sample: string;
};

export type ScanSymbol = {
  symbol: string;
  stage: string;
  outcome: string;
  reason: string | null;
  candidate: Record<string, any> | null;
  score: {
    total: number;
    tier: string;
    components: Record<string, number>;
    maxima: Record<string, number>;
    notes: string[];
  } | null;
  risk: Record<string, any> | null;
  ai: Record<string, any> | null;
  /**
   * The strategy chain, condition by condition, in evaluation order.
   *
   * PENDING is not a quiet FAIL: it means the engine stopped before
   * reaching that condition. A setup that failed at `risk_reward` got
   * everything else right and was priced out; one that failed at
   * `liquidity_sweep` never started. They call for opposite reactions.
   */
  checks?: Array<{ name: string; status: "PASS" | "FAIL" | "PENDING"; detail: string }>;
};

/** NO_TRADE | WATCH | VALID_SETUP | TRADE — see bot/smc/mtf.py. */
export type SignalState = "NO_TRADE" | "WATCH" | "VALID_SETUP" | "TRADE";

export type MtfDecision = {
  direction: string | null;
  setupType: string;
  state: SignalState;
  h1Bias: string;
  m15Bias: string;
  alignment: string;
  scoreFloor: number;
  regime: string;
  rationale: string;
  evidence: string[];
};

export type Scan = {
  scanId: string;
  startedAt: string;
  finishedAt: string | null;
  decision: string;
  skippedReason: string | null;
  errors: string[];
  /** Configured symbols this account cannot trade — a config fault, not a blip. */
  unavailable?: { symbol: string; reason: string; suggestions: string[] }[];
  account: Record<string, unknown>;
  symbols: ScanSymbol[];
  executed: Record<string, any> | null;
  demo: Record<string, any> | null;
};

export type RiskState = {
  balance: number;
  equity: number;
  peakEquity: number;
  drawdownPct: number;
  dailyRealizedPnl: number;
  openPnl: number;
  dailyPnl: number;
  tradesToday: number;
  consecutiveLosses: number;
  openPositions: number;
  killSwitch: { active: boolean; reason: string | null; detail: string | null; trippedAt: string | null };
  limits: Record<string, number>;
  /**
   * The objective, in R. It used to be a pair of dollar figures, and a
   * dollar figure is really a statement about account size: expected
   * profit is `risk x R` and risk is a fixed percentage of equity, so the
   * same setup graded differently on a $1,000 account and a $10,000 one.
   */
  rewardObjective: {
    minimumR: number;
    preferredR: number;
    enabled: boolean;
    /** What one R is worth at current equity. Information, not a gate. */
    riskPerTrade: number;
  };
};

export type Health = {
  ok: boolean;
  tradingPermitted: boolean;
  mode: 'paper' | 'demo_live';
  paper: boolean;
  uptimeSeconds: number;
  symbols: string[];
  versions: Record<string, string>;
  components: Record<string, any>;
  reconciliations: any;
  upcomingNews: any;
};

export type SetupSetting = {
  name: string;
  importance: 'required' | 'recommended' | 'optional';
  purpose: string;
  present: boolean;
  example: string;
};

export type SetupReport = {
  ready: boolean;
  missingRequired: string[];
  missingRecommended: string[];
  settings: SetupSetting[];
  warnings: string[];
  pasteBlock: string;
  nextStep: string;
};

export type DoctorReport = {
  verdict: 'PASS' | 'WARN' | 'FAIL';
  text: string;
  failed: string[];
  checks: Array<{ check: string; status: string; detail: string }>;
};

export type Snapshot = {
  account: Envelope<Account>;
  positions: Envelope<Position[]>;
  history: Envelope<HistoryRow[]>;
  performance: Envelope<Performance>;
  scan: Envelope<Scan>;
  risk: Envelope<RiskState>;
  health: Health;
  generatedAt: string;
};

/**
 * The dashboard token, held only in this browser.
 *
 * It authenticates the controls that can resume or start trading. It is
 * never sent anywhere but this deployment's own origin, and never logged.
 * Storage can throw (private windows, blocked site data), so every access
 * is guarded — a dashboard that crashes because it cannot read a
 * preference is worse than one that simply asks again.
 */
const TOKEN_KEY = "dashboard_token";

export function readToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function storeToken(token: string | null): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* a session-only unlock is still better than refusing to work */
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = readToken();
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Dashboard-Token": token } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const body = payload as { error?: string; code?: string };
    const message = body.error ?? `${path} failed with ${response.status}`;
    const error = new Error(message) as Error & { code?: string; status?: number };
    // Carried so the caller can offer the unlock instead of only showing
    // the refusal.
    error.code = body.code;
    error.status = response.status;
    throw error;
  }
  return payload as T;
}

export type StrategyOption = {
  key: string;
  name: string;
  description: string;
  minRiskReward: number;
  expectedFrequency: string;
  thesis: string;
  active: boolean;
  typicalProfitAtFloorRisk?: number;
  clearsProfitFloor?: boolean;
  note?: string | null;
};

export type StrategyStatus = { active: string; options: StrategyOption[] };

export const api = {
  snapshot: () => request<Snapshot>("/api/snapshot"),
  setup: () => request<SetupReport>("/api/setup"),
  doctor: () => request<DoctorReport>("/api/doctor"),
  journal: (limit = 60) =>
    request<{ status: Status; data: any[]; histogram: any[]; blockers: any[] }>(
      `/api/journal?limit=${limit}`,
    ),
  /**
   * Ask the server whether the token this browser holds is accepted.
   *
   * Resolves to "ok" only on a real 200. "unset" means the deployment has
   * no token configured, which is a different problem with a different
   * fix than "wrong token", and the operator needs to be told which.
   */
  verifyToken: async (): Promise<"ok" | "wrong" | "unset"> => {
    try {
      await request<{ ok: boolean }>("/api/control/verify", { method: "POST", body: "{}" });
      return "ok";
    } catch (error) {
      const code = (error as { code?: string }).code;
      return code === "NO_TOKEN_CONFIGURED" ? "unset" : "wrong";
    }
  },
  strategy: () => request<{ status: Status; data: StrategyStatus }>("/api/strategy"),
  setStrategy: (strategy: string) =>
    request<{ status: Status; data: StrategyStatus }>("/api/control/strategy", {
      method: "POST",
      body: JSON.stringify({ strategy }),
    }),
  setScanning: (enabled: boolean) =>
    request<{ enabled: boolean }>("/api/control/scanning", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  setKillSwitch: (active: boolean, force = false) =>
    request<Record<string, unknown>>("/api/control/kill-switch", {
      method: "POST",
      body: JSON.stringify({ active, force, reason: "dashboard" }),
    }),
  triggerScan: () => request<Scan>("/api/control/scan", { method: "POST" }),
  reconcile: () => request<Record<string, unknown>>("/api/control/reconcile", { method: "POST" }),
  resetPaper: () =>
    request<{ ok: boolean; error?: string }>("/api/control/reset-paper", {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    }),
};

/** Formatting helpers. `null` renders as an explicit dash, never as 0. */
export const fmt = {
  money(value: number | null | undefined, currency = "USD"): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(value);
  },
  number(value: number | null | undefined, digits = 2): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return value.toFixed(digits);
  },
  percent(value: number | null | undefined, digits = 1): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return `${(value * 100).toFixed(digits)}%`;
  },
  pct(value: number | null | undefined, digits = 2): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return `${value.toFixed(digits)}%`;
  },
  price(value: number | null | undefined, digits = 5): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "—";
    return value.toFixed(value > 100 ? 2 : digits);
  },
  duration(minutes: number | null | undefined): string {
    if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) return "—";
    if (minutes < 60) return `${Math.round(minutes)}m`;
    const hours = Math.floor(minutes / 60);
    return hours < 24 ? `${hours}h ${Math.round(minutes % 60)}m` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
  },
  time(value: string | null | undefined): string {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  },
  uptime(seconds: number | null | undefined): string {
    if (!seconds || !Number.isFinite(seconds)) return "—";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  },
};
