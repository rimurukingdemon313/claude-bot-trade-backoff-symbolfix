import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import { Router, type Request, type Response } from "express";
import {
  getTradingSchedulerState,
  setTradingSchedulerEnabled,
} from "../lib/trading-scheduler";

const execFileAsync = promisify(execFile);
const router = Router();

// All symbols the bot scans, analyzes, and can hold live TradeLocker
// positions in.
export const TRADED_SYMBOLS = [
  "EURUSD",
  "USDJPY",
  "USDCHF",
  "AUDJPY",
  "AUDCHF",
  "XAUUSD",
] as const;
export type TradedSymbol = (typeof TRADED_SYMBOLS)[number];

// Must match MAX_TOTAL_OPEN_POSITIONS in risk_engine.py.
const MAX_TOTAL_OPEN_POSITIONS = 6;

type AccountSnapshot = {
  balance: number;
  dailyPnl: number;
  openPositions: number;
};

export type AiDecision = {
  decision: "BUY" | "SELL" | "NO TRADE";
  confidence: number;
  reasoning: string;
  entryPrice: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  riskRewardRatio: number | null;
  riskAmount: number | null;
  aiProvider: "Gemini" | "Groq" | null;
  aiModel: string | null;
};

export type RiskDecision = {
  approved: boolean;
  state: "BUY" | "SELL" | "NO TRADE";
  reasons: string[];
  rules: Record<string, number>;
};

type LiveTrade = {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  status: "OPEN";
  entryPrice: number;
  stopLoss: number;
  takeProfit: number;
  riskAmount: number | null;
  quantity: number;
  openedAt: string | null;
  currentPrice: number | null;
  unrealizedPnl: number;
  unrealizedPnlPct: number | null;
};

type SymbolCycleResult = {
  symbol: TradedSymbol;
  decision: string;
  aiDecision: AiDecision;
  risk: RiskDecision;
  smc: Record<string, unknown>;
  paperTrade: LiveTrade | null;
  duplicate: boolean;
};

type TradingCycleResult = {
  decision: string;
  aiDecision: AiDecision;
  risk: RiskDecision;
  smc: Record<string, unknown>;
  paperTrade: LiveTrade | null;
  account: Record<string, unknown>;
  openTrade: Record<string, unknown> | null;
  trades: Array<Record<string, unknown>>;
  liveTrading: true;
  source: "manual" | "scheduled";
  duplicate: boolean;
  bySymbol: SymbolCycleResult[];
};

type TradeLockerStateResponse = {
  account: Record<string, unknown>;
  openTrade?: Record<string, unknown> | null;
  trades?: Array<Record<string, unknown>>;
  equityCurve?: number[];
  latestAiDecision?: AiDecision | null;
  latestRiskDecision?: RiskDecision | null;
  latestScan?: Record<string, unknown> | null;
  schedulerEnabled?: boolean;
};

const finiteOrNull = (value: unknown): number | null => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const clampConfidence = (value: unknown): number => {
  const parsed = finiteOrNull(value);
  if (parsed === null) return 0;
  return Math.max(0, Math.min(100, Math.round(parsed)));
};

const workspaceFile = (name: string): string => {
  const candidates = [
    path.resolve(process.cwd(), name),
    path.resolve(process.cwd(), "../../", name),
    path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../", name),
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) throw new Error(`Unable to locate ${name}`);
  return found;
};

// -----------------------------------------------------------------------
// TradeLocker bridge — replaces the old SQLite `database()` helper.
// Every call shells out to Python, which hits TradeLocker's live API
// directly. Nothing about account state is stored on disk here or in
// Python, so a Railway restart/redeploy never loses real data: the next
// call simply re-reads current truth from TradeLocker.
// -----------------------------------------------------------------------

// Thrown when a Python helper exits nonzero but still printed a
// structured JSON error to stdout (e.g. tradelocker_state.py's
// {"status": "incomplete", "error": "..."} for missing account data).
// Node's execFile rejects on any nonzero exit code and puts the process's
// stdout on the error object rather than returning it normally — callers
// that need the structured payload (not just "it failed") must catch this
// specifically instead of only reading a successful resolution's stdout.
class PythonScriptError extends Error {
  constructor(
    message: string,
    public readonly stdout: string,
    public readonly stderr: string,
  ) {
    super(message);
    this.name = "PythonScriptError";
  }
}

async function runPython(scriptName: string, action: string, payload: unknown = {}) {
  const script = workspaceFile(scriptName);
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [script, action, JSON.stringify(payload)],
      { cwd: path.dirname(script), maxBuffer: 4 * 1024 * 1024, timeout: 30_000 },
    );
    return JSON.parse(stdout);
  } catch (error) {
    // execFile's rejection shape carries stdout/stderr on the error
    // object itself when the child process ran but exited nonzero.
    const execError = error as NodeJS.ErrnoException & { stdout?: string; stderr?: string };
    if (typeof execError.stdout === "string" && execError.stdout.trim().length > 0) {
      try {
        const parsed = JSON.parse(execError.stdout);
        // A script that deliberately printed structured JSON before
        // exiting nonzero (our "incomplete data" contract) is a known,
        // meaningful failure — surface it as such rather than as an
        // opaque process-exit error.
        throw new PythonScriptError(
          `${scriptName} ${action} exited with a reported error: ${
            parsed.error ?? JSON.stringify(parsed)
          }`,
          execError.stdout,
          execError.stderr ?? "",
        );
      } catch (parseError) {
        if (parseError instanceof PythonScriptError) throw parseError;
        // stdout wasn't JSON either — fall through to the generic error below.
      }
    }
    throw new Error(
      `${scriptName} ${action} failed: ${execError.message}${
        execError.stderr ? ` | stderr: ${execError.stderr.slice(0, 500)}` : ""
      }`,
    );
  }
}

async function tlState(): Promise<TradeLockerStateResponse> {
  return runPython("tradelocker_state.py", "state") as Promise<TradeLockerStateResponse>;
}

async function tlFindInstrument(
  symbol: string,
): Promise<{ tradableInstrumentId: number; routeId: number } | null> {
  const result = await runPython("tradelocker_client.py", "instrument", { symbol });
  if (!result || result.tradableInstrumentId == null || result.routeId == null) return null;
  return result;
}

// TradeLocker's placeOrder does not document any client-supplied
// idempotency/order-id field (only qty, routeId, side, validity, type,
// tradableInstrumentId are documented as accepted). There is deliberately
// no "strategyId" or similar sent here — a previous version of this file
// sent one, but TradeLocker has no documented field by that name, so it
// would have been silently ignored, giving false confidence that orders
// were tagged/deduplicated when they were not. Duplicate prevention is
// instead handled entirely on our side, immediately before this call —
// see the isPositionAlreadyOpenRightNow() check in executeSymbolCycle.
async function tlPlaceOrder(params: {
  tradableInstrumentId: number;
  routeId: number;
  side: "BUY" | "SELL";
  quantity: number;
  stopLoss: number;
  takeProfit: number;
}): Promise<{ orderId?: string }> {
  return runPython("tradelocker_client.py", "place-order", {
    tradableInstrumentId: params.tradableInstrumentId,
    routeId: params.routeId,
    side: params.side,
    quantity: params.quantity,
    stopLoss: params.stopLoss,
    takeProfit: params.takeProfit,
  });
}

export async function getPersistedSchedulerEnabled(): Promise<boolean> {
  try {
    const stored = await tlState();
    return stored.schedulerEnabled !== false;
  } catch {
    return true;
  }
}

async function loadSmcAnalysis(symbol: TradedSymbol): Promise<Record<string, unknown>> {
  const script = workspaceFile("run_smc_demo.py");
  const { stdout } = await execFileAsync(
    "python3",
    [script, "--live", "--symbol", symbol],
    { cwd: path.dirname(script), maxBuffer: 2 * 1024 * 1024, timeout: 90_000 },
  );
  const parsed = JSON.parse(stdout) as Record<string, any>;
  const candles = (parsed.candles ?? []) as Array<{ high: number; low: number }>;
  const recentRanges = candles
    .slice(-20)
    .map((candle) => candle.high - candle.low)
    .filter((range) => Number.isFinite(range) && range > 0);
  const averageRange = recentRanges.length
    ? recentRanges.reduce((sum, range) => sum + range, 0) / recentRanges.length
    : null;
  const latestCandle = parsed.latest_candle as { high?: number; low?: number } | undefined;
  const latestRange =
    latestCandle && Number.isFinite(latestCandle.high) && Number.isFinite(latestCandle.low)
      ? (latestCandle.high as number) - (latestCandle.low as number)
      : null;
  return {
    timeframe: parsed.timeframe,
    symbol: parsed.symbol,
    live: parsed.live,
    dataSource: parsed.data_source,
    latestPrice: parsed.latest_price,
    candleCount: parsed.candle_count,
    latestCandle: parsed.latest_candle,
    marketStructure: parsed.market_structure,
    liquiditySweeps: parsed.liquidity_sweeps?.slice(-5) ?? [],
    structureBreaks: parsed.structure_breaks?.slice(-5) ?? [],
    fairValueGaps: parsed.fair_value_gaps?.slice(-5) ?? [],
    orderBlocks: parsed.order_blocks?.slice(-5) ?? [],
    momentum: parsed.momentum,
    overallContext: parsed.overall_context,
    multiTimeframe: parsed.multiTimeframe ?? null,
    candleRange: latestRange,
    averageRange,
  };
}

function parseGeminiJson(rawText: string): Record<string, unknown> {
  const withoutFence = rawText
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "");
  const parsed = JSON.parse(withoutFence) as unknown;
  if (!parsed || typeof parsed !== "object") {
    throw new Error("Gemini returned a non-object decision");
  }
  return parsed as Record<string, unknown>;
}

// Groq (and other chat-style models) can wrap JSON in reasoning text or
// markdown even when asked not to. Extract the first {...} block instead
// of assuming the whole response is clean JSON.
function extractJsonObject(rawText: string): Record<string, unknown> {
  const withoutFence = rawText
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "");
  try {
    const direct = JSON.parse(withoutFence) as unknown;
    if (direct && typeof direct === "object") return direct as Record<string, unknown>;
  } catch {
    // fall through to brace extraction below
  }
  const start = withoutFence.indexOf("{");
  const end = withoutFence.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) {
    throw new Error("No JSON object found in model response");
  }
  const candidate = withoutFence.slice(start, end + 1);
  const parsed = JSON.parse(candidate) as unknown;
  if (!parsed || typeof parsed !== "object") {
    throw new Error("Extracted JSON is not an object");
  }
  return parsed as Record<string, unknown>;
}

function buildAnalystPrompt(
  symbol: TradedSymbol,
  smc: Record<string, unknown>,
  multiTimeframe: unknown,
): string {
  return [
    `You are a senior multi-timeframe SMC decision analyst for ${symbol}. This is analysis only: never mention broker execution and never assume live market data.`,
    "You are given H4 (bias), H1 (confirmation), and M15 (entry) SMC findings under multiTimeframe, plus the full M15 structure detail.",
    "Require directional alignment: only propose BUY when H4, H1, and M15 bias all agree bullish; only propose SELL when all three agree bearish. If multiTimeframe.aligned is false, or any timeframe disagrees, you must return NO TRADE regardless of how strong the M15 setup looks alone.",
    "Treat the H4 direction as the dominant trend filter: never trade against the H4 bias even if M15 looks tempting.",
    "Only propose BUY or SELL when your genuine confidence is 75 or higher. If your honest confidence is below 75, you must return NO TRADE even if direction and structure look reasonable.",
    "Return JSON only, with no markdown, no reasoning text outside the JSON object. Choose exactly one decision: BUY, SELL, or NO TRADE.",
    "For BUY or SELL, provide entryPrice, stopLoss, takeProfit, and riskRewardRatio. Do not invent a riskAmount; the server computes position size from account balance.",
    "When evidence is mixed, conflicting across timeframes, or insufficient, choose NO TRADE. Confidence must be an integer from 0 to 100.",
    'JSON shape: {"decision":"BUY|SELL|NO TRADE","confidence":0,"reasoning":"detailed evidence-based explanation covering H4/H1/M15 alignment","entryPrice":0,"stopLoss":0,"takeProfit":0,"riskRewardRatio":0}',
    `Multi-timeframe bias summary for ${symbol}:`,
    JSON.stringify(multiTimeframe ?? {}),
    `Full M15 SMC findings for ${symbol}:`,
    JSON.stringify(smc),
  ].join("\n");
}

function decisionFromParsed(
  parsed: Record<string, unknown>,
  provider: "Gemini" | "Groq",
  model: string,
): AiDecision {
  const decision = String(parsed.decision ?? "NO TRADE").toUpperCase();
  return {
    decision: decision === "BUY" || decision === "SELL" ? decision : "NO TRADE",
    confidence: clampConfidence(parsed.confidence),
    reasoning: String(parsed.reasoning ?? `${provider} did not provide reasoning.`),
    entryPrice: finiteOrNull(parsed.entryPrice),
    stopLoss: finiteOrNull(parsed.stopLoss),
    takeProfit: finiteOrNull(parsed.takeProfit),
    riskRewardRatio: finiteOrNull(parsed.riskRewardRatio),
    riskAmount: null,
    aiProvider: provider,
    aiModel: model,
  };
}

const GEMINI_MODEL = "gemini-3.6-flash";
const GROQ_MODEL_PRIMARY = "openai/gpt-oss-120b";
const GROQ_MODEL_FALLBACK = "qwen/qwen3.8-27b";

async function askGemini(
  symbol: TradedSymbol,
  smc: Record<string, unknown>,
): Promise<AiDecision> {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) throw new Error("GEMINI_API_KEY is not configured on the API server");

  const multiTimeframe = smc.multiTimeframe as
    | { h4?: { direction?: string }; h1?: { direction?: string }; m15?: { direction?: string }; aligned?: boolean; bias?: string }
    | null
    | undefined;

  const prompt = buildAnalystPrompt(symbol, smc, multiTimeframe);

  const response = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: {
          responseMimeType: "application/json",
          temperature: 0.1,
          maxOutputTokens: 8192,
        },
      }),
    },
  );
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Gemini request failed (${response.status}): ${detail.slice(0, 240)}`);
  }

  const body = (await response.json()) as {
    candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>;
  };
  const rawText = body.candidates?.[0]?.content?.parts
    ?.map((part) => part.text ?? "")
    .join("")
    .trim();
  if (!rawText) throw new Error("Gemini returned an empty decision");

  const parsed = parseGeminiJson(rawText);
  return decisionFromParsed(parsed, "Gemini", GEMINI_MODEL);
}

async function askGroq(
  symbol: TradedSymbol,
  smc: Record<string, unknown>,
  multiTimeframe: unknown,
  model: string,
): Promise<AiDecision> {
  const apiKey = process.env.GROQ_API_KEY;
  if (!apiKey) throw new Error("GROQ_API_KEY is not configured on the API server");

  const prompt = buildAnalystPrompt(symbol, smc, multiTimeframe);

  const response = await fetch("https://api.groq.com/openai/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: "user", content: prompt }],
      temperature: 0.1,
      max_tokens: 4096,
    }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Groq (${model}) request failed (${response.status}): ${detail.slice(0, 240)}`);
  }

  const body = (await response.json()) as {
    choices?: Array<{ message?: { content?: string } }>;
  };
  const rawText = body.choices?.[0]?.message?.content?.trim();
  if (!rawText) throw new Error(`Groq (${model}) returned an empty decision`);

  const parsed = extractJsonObject(rawText);
  return decisionFromParsed(parsed, "Groq", model);
}

// Chained fallback across three models, tried in order:
//   1. Gemini (GEMINI_API_KEY)                       — primary
//   2. Groq openai/gpt-oss-120b (GROQ_API_KEY)        — fallback 1
//   3. Groq qwen/qwen3.8-27b (GROQ_API_KEY)           — fallback 2
// Each stage is wrapped in its own try/except so a rejection, timeout, or
// empty/unparseable response from one model falls through to the next
// without ever stopping the scan cycle. If all three fail, the error from
// the final attempt propagates and the symbol cycle records "NO TRADE".
async function askAi(
  symbol: TradedSymbol,
  smc: Record<string, unknown>,
): Promise<AiDecision> {
  const multiTimeframe = smc.multiTimeframe;

  try {
    const decision = await askGemini(symbol, smc);
    console.log(`[AI] ${symbol}: analyzed by Gemini (${GEMINI_MODEL}) -> ${decision.decision}`);
    return decision;
  } catch (geminiError) {
    const geminiMessage = geminiError instanceof Error ? geminiError.message : "Unknown Gemini error";
    console.log(`[AI] ${symbol}: Gemini failed (${geminiMessage}); falling back to Groq (${GROQ_MODEL_PRIMARY})`);

    try {
      const decision = await askGroq(symbol, smc, multiTimeframe, GROQ_MODEL_PRIMARY);
      console.log(`[AI] ${symbol}: analyzed by Groq (${GROQ_MODEL_PRIMARY}) -> ${decision.decision}`);
      return decision;
    } catch (groqPrimaryError) {
      const groqPrimaryMessage =
        groqPrimaryError instanceof Error ? groqPrimaryError.message : "Unknown Groq error";
      console.log(
        `[AI] ${symbol}: Groq (${GROQ_MODEL_PRIMARY}) failed (${groqPrimaryMessage}); falling back to Groq (${GROQ_MODEL_FALLBACK})`,
      );

      // Final stage — let this one raise all the way up if it also fails,
      // so the caller records a clear "Scan failed" reason.
      const decision = await askGroq(symbol, smc, multiTimeframe, GROQ_MODEL_FALLBACK);
      console.log(`[AI] ${symbol}: analyzed by Groq (${GROQ_MODEL_FALLBACK}) -> ${decision.decision}`);
      return decision;
    }
  }
}

async function runRiskEngine(
  proposal: AiDecision,
  account: AccountSnapshot,
  volatility: { candleRange: number | null; averageRange: number | null },
  symbol: TradedSymbol,
): Promise<RiskDecision> {
  const script = workspaceFile("risk_engine.py");
  const payload = JSON.stringify({
    proposal: {
      decision: proposal.decision,
      entry_price: proposal.entryPrice,
      stop_loss: proposal.stopLoss,
      take_profit: proposal.takeProfit,
      risk_reward_ratio: proposal.riskRewardRatio,
      risk_amount: proposal.riskAmount,
      confidence: proposal.confidence,
      candle_range: volatility.candleRange,
      average_range: volatility.averageRange,
      symbol,
    },
    account: {
      balance: account.balance,
      daily_pnl: account.dailyPnl,
      open_positions: account.openPositions,
    },
  });
  const { stdout } = await execFileAsync("python3", [script, payload], {
    cwd: path.dirname(script),
    maxBuffer: 64 * 1024,
    timeout: 20_000,
  });
  return JSON.parse(stdout) as RiskDecision;
}

async function symbolHasOpenPosition(
  symbol: TradedSymbol,
  state?: TradeLockerStateResponse,
): Promise<boolean> {
  const resolved = state ?? (await tlState());
  const trades = (resolved.trades ?? []) as Array<Record<string, unknown>>;
  return trades.some((trade) => trade.symbol === symbol && trade.result === "OPEN");
}

function totalOpenPositionsFrom(state: TradeLockerStateResponse): number {
  const trades = (state.trades ?? []) as Array<Record<string, unknown>>;
  return trades.filter((trade) => trade.result === "OPEN").length;
}

// Converts a dollar risk amount + entry/stop distance into a TradeLocker
// lot/unit quantity. For FX pairs quoted with USD as the quote currency,
// 1 standard lot = 100,000 units and each pip is ~$10/lot; this uses the
// direct distance-in-price-units approach so it holds for XAUUSD too
// (where "pip" conventions differ), by working in raw price distance.
function quantityForRisk(riskAmount: number, entry: number, stop: number, symbol: TradedSymbol): number {
  const distance = Math.abs(entry - stop);
  if (distance <= 0) return 0;
  // Units such that distance * units ≈ riskAmount in quote currency.
  // This matches the old paper-trading sizing formula (risk / distance)
  // so position sizing behavior is unchanged from before.
  const rawUnits = riskAmount / distance;
  // TradeLocker expects quantity in lots for FX/metals on most brokers
  // (1 lot = 100,000 units for FX, 100 oz for XAUUSD on many feeds).
  // Convert conservatively and round to 2 decimals; GATESFX's exact lot
  // step should be verified against /trade/accounts/{id}/instruments
  // (lotStep field) once live, and this divisor adjusted if needed.
  const lotDivisor = symbol === "XAUUSD" ? 100 : 100_000;
  const lots = rawUnits / lotDivisor;
  return Math.max(0.01, Math.round(lots * 100) / 100);
}

async function executeSymbolCycle(
  symbol: TradedSymbol,
  planningState: TradeLockerStateResponse,
): Promise<{
  result: SymbolCycleResult;
}> {
  const smc = await loadSmcAnalysis(symbol);
  // Uses the single state snapshot fetched once per trading cycle (see
  // executeTradingCycle) rather than fetching fresh here — this is
  // planning data (roughly-current is fine); the actual duplicate-order
  // safety check happens fresh, right before order submission below.
  const hasOpen = await symbolHasOpenPosition(symbol, planningState);
  const totalOpen = totalOpenPositionsFrom(planningState);
  const account = planningState.account as unknown as AccountSnapshot;
  const accountForSymbol: AccountSnapshot = {
    ...account,
    openPositions: hasOpen ? 1 : 0,
  };
  const aiDecision = await askAi(symbol, smc);
  // 2% of the current live account balance, matching risk_engine.py's
  // risk_amount_for_balance(). Computed here (not by the AI) so position
  // size always reflects the true current TradeLocker balance.
  aiDecision.riskAmount = Math.round(account.balance * 0.02 * 100) / 100;
  const risk = await runRiskEngine(
    aiDecision,
    accountForSymbol,
    {
      candleRange: (smc.candleRange as number | null) ?? null,
      averageRange: (smc.averageRange as number | null) ?? null,
    },
    symbol,
  );
  if (risk.approved && totalOpen >= MAX_TOTAL_OPEN_POSITIONS) {
    risk.approved = false;
    risk.state = "NO TRADE";
    risk.reasons = [
      `Total open position limit reached (${MAX_TOTAL_OPEN_POSITIONS} across all pairs)`,
    ];
  }

  let paperTrade: LiveTrade | null = null;
  // Explicit null/NaN checks — not truthiness — because a stop-loss or
  // take-profit of exactly 0 is a distinct (and here invalid) value, not
  // an absent one. `0 && ...` would short-circuit and silently skip
  // execution without ever explaining why, which is exactly the kind of
  // swallowed failure this review was asked to eliminate.
  const hasCompleteOrderPlan =
    risk.approved &&
    finiteOrNull(aiDecision.entryPrice) !== null &&
    finiteOrNull(aiDecision.stopLoss) !== null &&
    finiteOrNull(aiDecision.takeProfit) !== null &&
    (aiDecision.entryPrice as number) > 0 &&
    (aiDecision.stopLoss as number) > 0 &&
    (aiDecision.takeProfit as number) > 0;

  if (risk.approved && !hasCompleteOrderPlan) {
    risk.approved = false;
    risk.state = "NO TRADE";
    risk.reasons = [
      "Order plan incomplete: entryPrice, stopLoss, and takeProfit must all be positive numbers",
    ];
  }

  if (hasCompleteOrderPlan) {
    try {
      // Re-check for an open position on this symbol immediately before
      // submitting — deliberately a FRESH call (no planningState passed),
      // unlike the top-of-function checks above which reuse the
      // once-per-cycle snapshot. The AI call and risk-engine call above
      // can each take seconds; without a live re-check here, a position
      // opened by a concurrent cycle (e.g. a manual trigger overlapping
      // with the scheduler) during that window would go undetected and a
      // duplicate order could be submitted for the same symbol. This is
      // the one call in this function that must never be a cached/shared
      // snapshot.
      const stillFlat = !(await symbolHasOpenPosition(symbol));
      if (!stillFlat) {
        throw new Error(
          `Aborted: a position on ${symbol} was opened by another cycle while this decision was in progress`,
        );
      }

      const instrument = await tlFindInstrument(symbol);
      if (!instrument) {
        throw new Error(`Instrument ${symbol} not found on TradeLocker account`);
      }
      const quantity = quantityForRisk(
        aiDecision.riskAmount ?? 0,
        aiDecision.entryPrice as number,
        aiDecision.stopLoss as number,
        symbol,
      );
      if (quantity <= 0) {
        throw new Error(
          `Computed order quantity was ${quantity} (must be positive) — refusing to submit`,
        );
      }

      const order = await tlPlaceOrder({
        tradableInstrumentId: instrument.tradableInstrumentId,
        routeId: instrument.routeId,
        side: aiDecision.decision as "BUY" | "SELL",
        quantity,
        stopLoss: aiDecision.stopLoss as number,
        takeProfit: aiDecision.takeProfit as number,
      });
      paperTrade = {
        id: String(order.orderId ?? `${symbol}-${Date.now()}`),
        symbol,
        side: aiDecision.decision as "BUY" | "SELL",
        status: "OPEN",
        entryPrice: aiDecision.entryPrice as number,
        stopLoss: aiDecision.stopLoss as number,
        takeProfit: aiDecision.takeProfit as number,
        riskAmount: aiDecision.riskAmount,
        quantity,
        openedAt: new Date().toISOString(),
        currentPrice: null,
        unrealizedPnl: 0,
        unrealizedPnlPct: null,
      };
    } catch (executionError) {
      const message =
        executionError instanceof Error ? executionError.message : "Unknown execution error";
      risk.approved = false;
      risk.state = "NO TRADE";
      // "outcome unknown" is the marker string surfaced by
      // tradelocker_client.OrderSubmissionAmbiguous via the Python CLI's
      // stderr/exception text. Surface it distinctly rather than as a
      // generic failure: this specific case means we genuinely do not
      // know whether an order was placed, and the next scan cycle's
      // symbolHasOpenPosition() re-check (not an automatic retry here)
      // is what determines whether to try again.
      const isAmbiguous = message.toLowerCase().includes("outcome unknown");
      risk.reasons = [
        isAmbiguous
          ? `Order submission outcome unknown — will not auto-retry; next scan re-checks live positions before deciding: ${message}`
          : `Order execution failed: ${message}`,
      ];
    }
  }

  return {
    result: {
      symbol,
      decision: risk.state,
      aiDecision,
      risk,
      smc,
      paperTrade,
      duplicate: false,
    },
  };
}

let activeCycle: Promise<TradingCycleResult> | null = null;

async function executeTradingCycle(
  source: "manual" | "scheduled",
): Promise<TradingCycleResult> {
  const bySymbol: SymbolCycleResult[] = [];

  // Fetch ONE state snapshot for the whole cycle's planning needs
  // (open-position checks, current balance for risk sizing), instead of
  // each symbol's cycle fetching its own. Each tlState() call shells out
  // to a fresh python3 process that logs in from scratch and makes ~8
  // HTTP calls to TradeLocker; the previous version called it 3+ times
  // per symbol (18+ times across 6 symbols per scan), which was enough
  // request volume in a short window to trip TradeLocker's Cloudflare
  // rate limiting (HTTP 429 / error 1015). The one safety-critical
  // exception is the immediate pre-order-submission re-check inside
  // executeSymbolCycle, which deliberately still makes its own fresh
  // call — that one must never be stale.
  let planningState: TradeLockerStateResponse;
  try {
    planningState = await tlState();
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    throw new Error(`Unable to fetch TradeLocker state for this scan cycle: ${message}`);
  }

  // Scan every symbol sequentially. Sequential (not parallel) keeps each
  // python3 subprocess call simple and avoids hammering TradeLocker's
  // rate limits further.
  for (const symbol of TRADED_SYMBOLS) {
    try {
      const { result } = await executeSymbolCycle(symbol, planningState);
      bySymbol.push(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      bySymbol.push({
        symbol,
        decision: "NO TRADE",
        aiDecision: {
          decision: "NO TRADE",
          confidence: 0,
          reasoning: `Scan failed: ${message}`,
          entryPrice: null,
          stopLoss: null,
          takeProfit: null,
          riskRewardRatio: null,
          riskAmount: null,
          aiProvider: null,
          aiModel: null,
        },
        risk: {
          approved: false,
          state: "NO TRADE",
          reasons: [`No trade: ${message}`],
          rules: {},
        },
        smc: {},
        paperTrade: null,
        duplicate: false,
      });
    }
  }

  // Read the final combined state straight from TradeLocker after all
  // symbols have been processed — this one is deliberately fresh (not
  // planningState) since orders may have been placed during the loop
  // above and the response needs to reflect that.
  const finalState = await tlState();

  const primary = bySymbol.find((entry) => entry.paperTrade) ?? bySymbol[0];

  return {
    decision: primary.decision,
    aiDecision: primary.aiDecision,
    risk: primary.risk,
    smc: primary.smc,
    paperTrade: primary.paperTrade,
    account: finalState.account ?? {},
    openTrade: finalState.openTrade ?? null,
    trades: finalState.trades ?? [],
    liveTrading: true,
    source,
    duplicate: false,
    bySymbol,
  };
}

export async function runTradingCycle(
  source: "manual" | "scheduled" = "manual",
): Promise<TradingCycleResult> {
  if (activeCycle) return activeCycle;
  activeCycle = executeTradingCycle(source);
  try {
    return await activeCycle;
  } finally {
    activeCycle = null;
  }
}

function schedulerResponse() {
  return { ...getTradingSchedulerState(), liveTrading: true };
}

router.get("/trading/state", async (_req: Request, res: Response) => {
  try {
    const stored = await tlState();
    return res.json({ ...stored, scheduler: schedulerResponse(), liveTrading: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to load TradeLocker state";
    return res.status(502).json({ error: message, liveTrading: true });
  }
});

router.post("/trading/analyze", async (req: Request, res: Response) => {
  try {
    if (req.body?.smcEnabled === false) {
      return res.status(409).json({
        decision: "NO TRADE",
        risk: { approved: false, state: "NO TRADE", reasons: ["SMC engine is paused"], rules: {} },
        liveTrading: true,
      });
    }
    return res.json({ ...(await runTradingCycle("manual")), scheduler: schedulerResponse() });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Trading analysis failed";
    req.log?.error?.({ err: error }, "Trading analysis failed");
    return res.status(502).json({
      decision: "NO TRADE",
      risk: {
        approved: false,
        state: "NO TRADE",
        reasons: [`No trade: ${message}`],
        rules: {},
      },
      liveTrading: true,
    });
  }
});

router.post("/trading/scheduler", (req: Request, res: Response) => {
  const enabled = req.body?.enabled;
  if (typeof enabled !== "boolean") {
    return res.status(400).json({ error: "enabled must be a boolean" });
  }
  return Promise.resolve()
    .then(() => ({
      scheduler: setTradingSchedulerEnabled(enabled, async (source) => runTradingCycle(source)),
      liveTrading: true,
    }))
    .then((payload) => res.json(payload))
    .catch((error) => {
      const message = error instanceof Error ? error.message : "Unable to update scheduler";
      return res.status(502).json({ error: message, liveTrading: true });
    });
});

// Reset is a no-op against a live broker account — there is no local
// ledger to clear anymore. Kept as a route (rather than removed) so the
// existing dashboard reset button doesn't 404; it simply re-reads current
// TradeLocker state.
router.post("/trading/reset", async (_req: Request, res: Response) => {
  try {
    return res.json({
      ...(await tlState()),
      scheduler: schedulerResponse(),
      liveTrading: true,
      note: "Reset has no effect on a live TradeLocker account; balance reflects your real account.",
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to load TradeLocker state";
    return res.status(502).json({ error: message, liveTrading: true });
  }
});

export default router;
