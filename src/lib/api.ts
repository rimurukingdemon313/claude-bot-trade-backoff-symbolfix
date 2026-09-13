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
};

export type Scan = {
  scanId: string;
  startedAt: string;
  finishedAt: string | null;
  decision: string;
  skippedReason: string | null;
  errors: string[];
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
  opportunityTarget: number;
  opportunityMinimum: number;
  profitObjective: {
    feasible: boolean;
    /** Reachable, but only by setups above the configured minimum R:R. */
    demanding?: boolean;
    reason: string;
    comfortableProfit?: number;
    bestCaseProfit?: number;
    minimumProfit?: number;
    requiredRiskReward?: number | null;
    requiredEquity?: number | null;
    comfortableEquity?: number | null;
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const message =
      (payload as { error?: string }).error ?? `${path} failed with ${response.status}`;
    throw new Error(message);
  }
  return payload as T;
}

export const api = {
  snapshot: () => request<Snapshot>("/api/snapshot"),
  setup: () => request<SetupReport>("/api/setup"),
  doctor: () => request<DoctorReport>("/api/doctor"),
  journal: (limit = 60) =>
    request<{ status: Status; data: any[]; histogram: any[] }>(`/api/journal?limit=${limit}`),
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
