import { useCallback, useEffect, useMemo, useState } from 'react';
import { Pause, Play, RotateCcw } from 'lucide-react';

type BotState = 'running' | 'paused';
type EngineState = 'running' | 'paused';
type TradeSide = 'LONG' | 'SHORT';
type TradeResult = 'WIN' | 'LOSS' | 'OPEN' | 'REJECTED';

type Trade = {
  id: string;
  time: string;
  symbol: string;
  side: TradeSide;
  setup: string;
  result: TradeResult;
  pnl: number;
  pnlPct: number;
  confidence: number;
  entryPrice?: number;
  stopLoss?: number;
  takeProfit?: number;
  openedAt?: string;
  closedAt?: string | null;
};

type AccountModel = {
  balance: number;
  dailyPnl: number;
  monthlyPnl: number;
  pnl30d: number;
  roi30d: number;
  trades: number;
  wins: number;
  losses: number;
  drawdown: number;
  peak: number;
};

type AiDecision = {
  decision: 'BUY' | 'SELL' | 'NO TRADE';
  confidence: number;
  reasoning: string;
  entryPrice: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  riskRewardRatio: number | null;
  riskAmount: number | null;
  aiProvider?: 'OpenRouter' | 'Gemini' | null;
  aiModel?: string | null;
};

type RiskDecision = {
  approved: boolean;
  state: 'BUY' | 'SELL' | 'NO TRADE';
  reasons: string[];
  rules: {
    account_balance?: number;
    risk_per_trade?: number;
    risk_amount?: number;
    max_open_positions_per_symbol?: number;
    max_total_open_positions?: number;
    max_daily_loss?: number;
    min_risk_reward?: number;
    min_confidence?: number;
  };
};

type PaperTrade = {
  id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  status: 'OPEN';
  entryPrice: number;
  stopLoss: number;
  takeProfit: number;
  riskAmount: number;
  quantity?: number;
  openedAt?: string;
  currentPrice?: number;
  unrealizedPnl?: number;
  unrealizedPnlPct?: number;
};

type SchedulerState = {
  enabled: boolean;
  running: boolean;
  lastRunAt: string | null;
  lastStatus: 'idle' | 'running' | 'completed' | 'failed';
  lastError: string | null;
  nextRunAt: string | null;
};

type TradingState = {
  account?: AccountModel & { openPositions?: number };
  openTrade?: PaperTrade | null;
  trades?: Trade[];
  equityCurve?: number[];
  latestAiDecision?: AiDecision | null;
  latestRiskDecision?: RiskDecision | null;
  scheduler?: SchedulerState;
  latestScan?: {
    candleTimestamp: string;
    scannedAt: string;
    latestPrice: number;
    dataSource: string;
    live: boolean;
  } | null;
};

type SmcSnapshot = {
  symbol?: string;
  live?: boolean;
  dataSource?: string;
  latestPrice?: number;
  overallContext?: { direction?: string; score?: number };
  liquiditySweeps?: unknown[];
  structureBreaks?: unknown[];
  fairValueGaps?: unknown[];
  orderBlocks?: unknown[];
  multiTimeframe?: {
    h4?: { direction?: string; score?: number; latestPrice?: number };
    h1?: { direction?: string; score?: number; latestPrice?: number };
    m15?: { direction?: string; score?: number; latestPrice?: number };
    aligned?: boolean;
    bias?: string;
  } | null;
};

type SymbolScanResult = {
  symbol: string;
  decision: 'BUY' | 'SELL' | 'NO TRADE';
  aiDecision: AiDecision;
  risk: RiskDecision;
  smc: SmcSnapshot;
  paperTrade: PaperTrade | null;
  duplicate: boolean;
};

const startingAccount: AccountModel = {
  balance: 1000,
  dailyPnl: 0,
  monthlyPnl: 0,
  pnl30d: 0,
  roi30d: 0,
  trades: 0,
  wins: 0,
  losses: 0,
  drawdown: 0,
  peak: 1000,
};

const startingTrades: Trade[] = [];

const startingTrend = [1000, 1000, 1000, 1000, 1000, 1000];
const initialAiDecision: AiDecision = {
  decision: 'NO TRADE',
  confidence: 0,
  reasoning: 'Run analysis to evaluate the latest multi-timeframe SMC evidence.',
  entryPrice: null,
  stopLoss: null,
  takeProfit: null,
  riskRewardRatio: null,
  riskAmount: null,
  aiProvider: null,
  aiModel: null,
};
const initialRiskDecision: RiskDecision = {
  approved: false,
  state: 'NO TRADE',
  reasons: ['Risk review pending'],
  rules: {
    account_balance: 1000,
    risk_per_trade: 0.02,
    risk_amount: 20,
    max_open_positions_per_symbol: 1,
    max_total_open_positions: 6,
    max_daily_loss: 120,
    min_risk_reward: 2,
    min_confidence: 75,
  },
};

const formatMoney = (value: number) => `${value < 0 ? '-' : ''}$${Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const formatPct = (value: number) => `${value.toFixed(1)}%`;
const formatSignedMoney = (value: number) => `${value >= 0 ? '+' : ''}${formatMoney(value)}`;
const formatDateTime = (value: string | null | undefined) => value
  ? new Date(value).toLocaleString('en-GB', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short', timeZone: 'UTC' })
  : '—';

function EquityChart({ values }: { values: number[] }) {
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const padding = Math.max(8, (rawMax - rawMin) * 0.18);
  const min = rawMin - padding;
  const max = rawMax + padding;
  const points = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 100;
    const y = 100 - ((value - min) / (max - min)) * 78 - 8;
    return `${x},${y}`;
  }).join(' ');
  const areaPoints = `0,100 ${points} 100,100`;
  return (
    <div className="relative mt-4 h-[130px] w-full" data-testid="chart-performance">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-full w-full overflow-visible">
        <polygon points={areaPoints} fill="#00FF41" fillOpacity={0.08} />
        <polyline points={points} fill="none" stroke="#00FF41" strokeWidth="1.4" vectorEffect="non-scaling-stroke" strokeLinecap="square" strokeLinejoin="miter" />
      </svg>
    </div>
  );
}

function Stat({ label, value, tone, testId }: { label: string; value: string; tone: 'positive' | 'negative' | 'neutral'; testId: string }) {
  const toneClass = tone === 'positive' ? 'text-term-bright' : tone === 'negative' ? 'text-term-amber' : 'text-term-dim';
  return (
    <div data-testid={`card-${testId}`}>
      <div className="term-mono text-[9px] text-term-dim">{label}</div>
      <div className={`term-digits mt-1 text-[16px] font-medium ${toneClass}`} data-testid={`text-${testId}`}>{value}</div>
    </div>
  );
}

function LogRow({ trade }: { trade: Trade }) {
  const isWin = trade.result === 'WIN';
  const isOpen = trade.result === 'OPEN';
  const textTone = isWin ? 'text-term-bright' : isOpen ? 'text-term-dim' : 'text-term-amber';
  return (
    <div className="grid grid-cols-[70px_1fr_80px] items-center gap-2 border-b border-term-line/40 py-2.5 last:border-0 sm:grid-cols-[80px_1fr_90px_90px]" data-testid={`row-trade-${trade.id}`}>
      <div className="term-mono text-[9px] text-term-dim">{formatDateTime(trade.time)}</div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="term-mono text-[12px] font-medium text-term-fg">{trade.symbol}</span>
          <span className={`term-mono text-[8px] ${trade.side === 'LONG' ? 'text-term-bright' : 'text-term-amber'}`}>{trade.side === 'LONG' ? '[+]' : '[-]'}</span>
        </div>
        <div className="mt-0.5 truncate term-mono text-[9px] text-term-dim">{trade.setup}</div>
      </div>
      <div className={`text-right term-digits text-[13px] font-medium ${textTone}`}>{isWin ? '+' : ''}{formatMoney(trade.pnl)}</div>
      <div className="hidden text-right term-mono text-[9px] text-term-dim sm:block">{trade.result}</div>
    </div>
  );
}

export default function Dashboard() {
  const [botState, setBotState] = useState<BotState>('running');
  const [scheduler, setScheduler] = useState<SchedulerState | null>(null);
  const [smcState, setSmcState] = useState<EngineState>('running');
  const [account, setAccount] = useState<AccountModel>(startingAccount);
  const [trades, setTrades] = useState<Trade[]>(startingTrades);
  const [trend, setTrend] = useState(startingTrend);
  const [aiDecision, setAiDecision] = useState<AiDecision>(initialAiDecision);
  const [riskDecision, setRiskDecision] = useState<RiskDecision>(initialRiskDecision);
  const [openTrade, setOpenTrade] = useState<PaperTrade | null>(null);
  const [smcSnapshot, setSmcSnapshot] = useState<SmcSnapshot | null>(null);
  const [symbolResults, setSymbolResults] = useState<SymbolScanResult[]>([]);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [showReset, setShowReset] = useState(false);
  const [notice, setNotice] = useState('System nominal');
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const clockLabel = clock.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'UTC' });
  const winRate = useMemo(() => account.trades ? (account.wins / account.trades) * 100 : 0, [account.trades, account.wins]);

  const applyTradingState = useCallback((payload: TradingState) => {
    if (payload.account) setAccount(payload.account);
    if (payload.trades) setTrades(payload.trades);
    if (payload.equityCurve && payload.equityCurve.length > 1) setTrend(payload.equityCurve);
    if ('openTrade' in payload) setOpenTrade(payload.openTrade ?? null);
    if (payload.latestAiDecision) setAiDecision(payload.latestAiDecision);
    if (payload.latestRiskDecision) setRiskDecision(payload.latestRiskDecision);
    if (payload.latestScan) {
      setSmcSnapshot((current) => ({
        ...current,
        live: payload.latestScan?.live,
        latestPrice: payload.latestScan?.latestPrice,
        dataSource: payload.latestScan?.dataSource,
      }));
    }
    if (payload.scheduler) {
      setScheduler(payload.scheduler);
      setBotState(payload.scheduler.enabled ? 'running' : 'paused');
    }
  }, []);

  const syncTradingState = useCallback(async () => {
    const response = await fetch('/api/trading/state');
    if (!response.ok) throw new Error('Unable to load saved trading state');
    const payload = await response.json() as TradingState;
    applyTradingState(payload);
  }, [applyTradingState]);

  useEffect(() => {
    void syncTradingState().catch(() => setNotice('Waiting for the paper trading service'));
    const interval = window.setInterval(() => {
      void syncTradingState().catch(() => undefined);
    }, 20_000);
    return () => window.clearInterval(interval);
  }, [syncTradingState]);

  const toggleBot = async () => {
    const enabled = botState === 'paused';
    setNotice(enabled ? 'Resuming automatic scans…' : 'Pausing automatic scans safely…');
    try {
      const response = await fetch('/api/trading/scheduler', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const payload = await response.json() as { scheduler?: SchedulerState; error?: string };
      if (!response.ok || !payload.scheduler) throw new Error(payload.error ?? 'Scheduler update failed');
      setScheduler(payload.scheduler);
      setBotState(payload.scheduler.enabled ? 'running' : 'paused');
      setNotice(enabled ? 'Automatic scans resumed' : 'Automatic scans paused safely');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Unable to update scheduler');
    }
  };

  const toggleSmc = () => {
    setSmcState((current) => {
      const next = current === 'running' ? 'paused' : 'running';
      setNotice(next === 'running' ? 'Multi-timeframe SMC analysis resumed' : 'SMC analysis paused safely');
      return next;
    });
  };

  const resetAccount = async () => {
    try {
      const response = await fetch('/api/trading/reset', { method: 'POST' });
      const payload = await response.json() as TradingState & { error?: string };
      if (!response.ok) throw new Error(payload.error ?? 'Unable to reset paper account');
      applyTradingState(payload);
      setTrend(startingTrend);
      setAiDecision(initialAiDecision);
      setRiskDecision(initialRiskDecision);
      setSmcSnapshot(null);
      setNotice('Paper account reset to $1,000');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Unable to reset paper account');
    } finally {
      setShowReset(false);
    }
  };

  const runAiDecision = async () => {
    if (botState === 'paused') {
      setNotice('Resume the bot before requesting an AI decision');
      return;
    }
    if (smcState === 'paused') {
      setNotice('Resume SMC analysis before requesting an AI decision');
      return;
    }
    setIsAnalyzing(true);
    setNotice('Running multi-timeframe SMC analysis…');
    try {
      const response = await fetch('/api/trading/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ smcEnabled: true }),
      });
      const payload = await response.json() as TradingState & {
        aiDecision?: AiDecision;
        risk?: RiskDecision;
        paperTrade?: PaperTrade | null;
        smc?: SmcSnapshot;
        bySymbol?: SymbolScanResult[];
      };
      if (payload.aiDecision) setAiDecision(payload.aiDecision);
      if (payload.risk) setRiskDecision(payload.risk);
      if (payload.smc) setSmcSnapshot(payload.smc);
      if (payload.bySymbol) setSymbolResults(payload.bySymbol);
      applyTradingState(payload);

      if (!response.ok || !payload.risk) {
        setNotice(payload.risk?.reasons?.[0] ?? 'No trade: analysis service unavailable');
        return;
      }

      if (payload.paperTrade && payload.risk.approved) {
        setNotice(`Approved ${payload.paperTrade.side}: paper trade logged`);
      } else {
        const reason = payload.risk.reasons[0] ?? 'Risk Engine rejected the proposal';
        setNotice(`NO TRADE — ${reason}`);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unable to reach analysis service';
      setRiskDecision({ ...initialRiskDecision, reasons: [`No trade: ${message}`] });
      setNotice(`NO TRADE — ${message}`);
    } finally {
      setIsAnalyzing(false);
    }
  };

  const biasDirection = smcSnapshot?.multiTimeframe?.bias ?? smcSnapshot?.multiTimeframe?.h4?.direction;
  const timeframesAligned = smcSnapshot?.multiTimeframe?.aligned;
  const providerLabel = aiDecision.aiProvider === 'OpenRouter' ? 'OPENROUTER' : aiDecision.aiProvider === 'Gemini' ? 'GEMINI' : null;

  return (
    <div className="term-bg term-scanlines min-h-[100dvh] text-term-fg">
      <div className="mx-auto flex min-h-[100dvh] max-w-[1200px] flex-col">
        <header className="border-b border-term-line px-5 py-4 sm:px-8">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <span className="term-mono text-[14px] text-term-bright">&gt;</span>
              <div className="term-mono text-[14px] font-medium tracking-tight text-term-bright">SIXPAIR_CONFLUENCE.SYS</div>
              <span className="term-cursor term-mono text-[14px] text-term-bright">_</span>
            </div>
            <div className="term-digits text-[20px] tracking-[.06em] text-term-bright" data-testid="text-clock" aria-label="UTC time">{clockLabel}<span className="text-term-dim"> UTC</span></div>
            <div className="flex items-center gap-3">
              <span className="term-mono flex items-center gap-1.5 border border-term-line px-2 py-1 text-[10px] text-term-dim" data-testid="status-paper-only">
                <span className="term-blink inline-block h-1.5 w-1.5 bg-term-bright" /> PAPER_MODE
              </span>
              <button
                type="button"
                onClick={() => void toggleBot()}
                data-testid="button-toggle-bot"
                className="term-mono flex items-center gap-1.5 border border-term-line px-3 py-1.5 text-[10px] text-term-bright transition-colors hover:bg-term-bright/10"
              >
                {botState === 'running' ? <Pause size={11} /> : <Play size={11} />}
                {botState === 'running' ? 'PAUSE' : 'RESUME'}
              </button>
            </div>
          </div>
          {notice && (
            <div className="term-mono mt-3 text-[11px] text-term-dim" data-testid="text-notice">&gt; {notice}</div>
          )}
        </header>

        <main className="flex-1 px-5 py-6 sm:px-8">
          {/* Bias hero */}
          <section className="border border-term-line bg-term-panel p-6 sm:p-8" data-testid="panel-bias-hero">
            <div className="flex flex-wrap items-start justify-between gap-6">
              <div>
                <div className="term-mono text-[10px] text-term-dim">BIAS// H4_TREND_FILTER</div>
                <div className={`term-mono mt-2 text-[38px] font-bold uppercase leading-none tracking-tight sm:text-[48px] ${biasDirection === 'bullish' ? 'text-term-bright' : biasDirection === 'bearish' ? 'text-term-amber' : 'text-term-dim'}`} data-testid="text-bias-direction">
                  {biasDirection ? biasDirection.toUpperCase() : 'STANDBY'}
                </div>
                <div className="term-mono mt-3 flex items-center gap-2 text-[11px] text-term-dim">
                  {timeframesAligned === true && <span className="text-term-bright">[OK] H4=H1=M15 ALIGNED</span>}
                  {timeframesAligned === false && <span className="text-term-amber">[!!] TIMEFRAME CONFLICT — STANDING ASIDE</span>}
                  {timeframesAligned === undefined && <span>[..] AWAITING SCAN</span>}
                </div>
              </div>
              <div className="flex flex-col items-end gap-1">
                <div className="term-mono text-[10px] text-term-dim">DECISION//</div>
                <div className="flex items-center gap-2">
                  <span className={`term-mono text-[22px] font-bold ${aiDecision.decision === 'BUY' ? 'text-term-bright' : aiDecision.decision === 'SELL' ? 'text-term-amber' : 'text-term-dim'}`} data-testid="text-ai-decision">{aiDecision.decision}</span>
                  <span className="term-digits text-[11px] text-term-amber" data-testid="text-gemini-confidence">{aiDecision.confidence}%</span>
                </div>
                {providerLabel && (
                  <span className="term-mono text-[9px] text-term-dim" data-testid="badge-ai-provider" title={aiDecision.aiModel ?? undefined}>[{providerLabel}]</span>
                )}
              </div>
            </div>

            <div className="mt-6 grid grid-cols-3 gap-px border-t border-term-line bg-term-line pt-px">
              {(['h4', 'h1', 'm15'] as const).map((tf) => {
                const entry = smcSnapshot?.multiTimeframe?.[tf];
                const dir = entry?.direction;
                return (
                  <div key={tf} className="bg-term-panel px-3 py-2.5">
                    <div className="term-mono text-[9px] text-term-dim">{tf.toUpperCase()}</div>
                    <div className={`term-mono mt-1 text-[13px] font-medium uppercase ${dir === 'bullish' ? 'text-term-bright' : dir === 'bearish' ? 'text-term-amber' : 'text-term-dim'}`}>{dir ?? '--'}</div>
                  </div>
                );
              })}
            </div>

            <div className="mt-5 border border-term-line bg-term-bg/60 p-4" data-testid="panel-ai-reasoning">
              <p className="term-mono text-[11px] leading-6 text-term-fg/80">&gt; {aiDecision.reasoning}</p>
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-term-line pt-3 term-mono text-[9px] text-term-dim">
                <span>{smcSnapshot?.dataSource ?? 'AWAITING_LIVE_FEED'}</span>
                <span>{smcSnapshot?.latestPrice ? `${smcSnapshot?.symbol ?? ''} ${smcSnapshot.latestPrice.toFixed(5)}` : '--'}</span>
              </div>
            </div>

            <div className="mt-5 flex flex-wrap gap-3">
              <button
                type="button"
                onClick={() => void runAiDecision()}
                disabled={isAnalyzing}
                data-testid="button-run-ai"
                className="term-mono flex items-center justify-center gap-2 border border-term-bright bg-term-bright/10 px-5 py-2.5 text-[12px] font-medium text-term-bright transition hover:bg-term-bright/20 disabled:cursor-wait disabled:opacity-50"
              >
                {isAnalyzing ? '> SCANNING...' : '> RUN_SCAN'}
              </button>
              <button
                type="button"
                onClick={toggleSmc}
                data-testid="button-toggle-smc"
                className="term-mono flex items-center gap-2 border border-term-line px-4 py-2.5 text-[12px] text-term-dim transition hover:border-term-bright/50 hover:text-term-bright"
              >
                {smcState === 'running' ? <Pause size={13} /> : <Play size={13} />}
                {smcState === 'running' ? 'PAUSE_SMC' : 'RESUME_SMC'}
              </button>
            </div>
          </section>

          {/* Pair grid */}
          <section className="mt-6" data-testid="panel-symbol-scan">
            <div className="mb-3 flex items-center justify-between">
              <div className="term-mono text-[13px] font-medium text-term-bright">PAIR_SCAN//</div>
              <span className="term-mono text-[10px] text-term-dim">{symbolResults.length || 6} TRACKED</span>
            </div>
            {symbolResults.length ? (
              <div className="grid grid-cols-2 gap-px bg-term-line sm:grid-cols-3">
                {symbolResults.map((entry) => {
                  const price = entry.smc?.latestPrice;
                  const decision = entry.decision;
                  const textTone = decision === 'BUY' ? 'text-term-bright' : decision === 'SELL' ? 'text-term-amber' : 'text-term-dim';
                  return (
                    <div key={entry.symbol} className="bg-term-panel px-3.5 py-3" data-testid={`card-symbol-${entry.symbol}`}>
                      <div className="flex items-center justify-between">
                        <span className="term-mono text-[12px] font-medium text-term-fg">{entry.symbol}</span>
                        <span className={`term-mono text-[9px] ${textTone}`}>{decision}</span>
                      </div>
                      <div className="term-digits mt-1.5 text-[16px] font-medium text-term-bright">{typeof price === 'number' ? price.toFixed(price >= 100 ? 2 : 5) : '--'}</div>
                      <div className="term-mono mt-1 flex items-center justify-between text-[9px] text-term-dim">
                        <span>{entry.aiDecision?.confidence ?? 0}%</span>
                        <span>{entry.paperTrade ? 'OPEN' : entry.risk?.approved ? 'OK' : 'HOLD'}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="term-mono flex min-h-[90px] items-center justify-center border border-dashed border-term-line text-[11px] text-term-dim">RUN_SCAN TO POPULATE PAIRS</div>
            )}
          </section>

          {/* Position + performance */}
          <div className="mt-6 grid gap-px bg-term-line lg:grid-cols-[1fr_300px]">
            <section className="bg-term-panel p-5" data-testid="panel-equity">
              <div className="flex items-center justify-between">
                <div className="term-mono text-[13px] font-medium text-term-bright">EQUITY_CURVE//</div>
                <span className="term-mono text-[10px] text-term-dim">SIM_FUNDS</span>
              </div>
              <EquityChart values={trend} />
              <div className="mt-4 grid grid-cols-3 gap-3 border-t border-term-line pt-4">
                <Stat label="TOTAL_PNL" value={formatSignedMoney(account.pnl30d)} tone={account.pnl30d >= 0 ? 'positive' : 'negative'} testId="total-pnl" />
                <Stat label="WIN_RATE" value={formatPct(winRate)} tone="neutral" testId="win-rate" />
                <Stat label="DRAWDOWN" value={formatPct(account.drawdown)} tone={account.drawdown > 5 ? 'negative' : 'neutral'} testId="drawdown" />
              </div>
            </section>

            <section className="bg-term-panel p-5" data-testid="panel-position">
              <div className="flex items-center justify-between">
                <div className="term-mono text-[13px] font-medium text-term-bright">POSITION//</div>
                <span className={`term-mono text-[9px] ${openTrade ? 'text-term-amber' : 'text-term-dim'}`}>{openTrade ? 'OPEN' : 'FLAT'}</span>
              </div>
              {openTrade ? (
                <div className="mt-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="term-mono text-[14px] font-medium text-term-fg">{openTrade.symbol}</span>
                    <span className={`term-digits text-[15px] font-medium ${(openTrade.unrealizedPnl ?? 0) >= 0 ? 'text-term-bright' : 'text-term-amber'}`}>{formatSignedMoney(openTrade.unrealizedPnl ?? 0)}</span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 term-mono text-[10px] text-term-dim">
                    <div><div>ENTRY</div><div className="term-digits mt-0.5 text-[13px] text-term-fg">{openTrade.entryPrice.toFixed(5)}</div></div>
                    <div><div>STOP</div><div className="term-digits mt-0.5 text-[13px] text-term-fg">{openTrade.stopLoss.toFixed(5)}</div></div>
                    <div><div>TARGET</div><div className="term-digits mt-0.5 text-[13px] text-term-fg">{openTrade.takeProfit.toFixed(5)}</div></div>
                  </div>
                </div>
              ) : (
                <div className="term-mono mt-8 flex flex-col items-center gap-2 text-center text-[11px] text-term-dim">
                  <span>[ NO_ACTIVE_TRADE ]</span>
                </div>
              )}
              <div className="mt-5 border-t border-term-line pt-4">
                <div className="term-mono text-[9px] text-term-dim">RISK_GATE//</div>
                <div className="term-mono mt-2 space-y-1.5 text-[10px] text-term-dim">
                  <div className="flex justify-between"><span>RISK/TRADE</span><span className="text-term-fg">{((riskDecision.rules.risk_per_trade ?? 0.02) * 100).toFixed(0)}%</span></div>
                  <div className="flex justify-between"><span>MIN_CONF</span><span className="text-term-fg">{riskDecision.rules.min_confidence ?? 75}%</span></div>
                  <div className="flex justify-between"><span>MIN_R:R</span><span className="text-term-fg">1:{riskDecision.rules.min_risk_reward ?? 2}</span></div>
                  <div className="flex justify-between"><span>MAX_POS</span><span className="text-term-fg">{riskDecision.rules.max_open_positions_per_symbol ?? 1}/PAIR·{riskDecision.rules.max_total_open_positions ?? 6}</span></div>
                </div>
              </div>
            </section>
          </div>

          {/* Trade log */}
          <section className="mt-6 border border-term-line bg-term-panel p-5" data-testid="panel-trade-log">
            <div className="flex items-center justify-between">
              <div className="term-mono text-[13px] font-medium text-term-bright">TRADE_LOG//</div>
              <span className="term-mono text-[10px] text-term-dim">SQLITE_LEDGER</span>
            </div>
            <div className="mt-4">
              {trades.length ? trades.slice(0, 8).map((trade) => <LogRow key={trade.id} trade={trade} />) : (
                <div className="term-mono flex min-h-[90px] items-center justify-center text-[11px] text-term-dim">NO_HISTORY</div>
              )}
            </div>
          </section>

          <div className="term-mono mt-6 flex items-center justify-between text-[10px] text-term-dim">
            <span>BAL={formatMoney(account.balance)} · SANDBOX_ISOLATED · NO_BROKER_CREDS</span>
            <button type="button" onClick={() => setShowReset(true)} data-testid="button-reset-account" className="flex items-center gap-1.5 text-term-amber hover:text-term-amber/80">
              <RotateCcw size={11} /> RESET
            </button>
          </div>
        </main>
      </div>

      {showReset && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4" role="dialog" aria-modal="true">
          <div className="w-full max-w-sm border border-term-bright bg-term-panel p-5">
            <div className="term-mono text-[14px] font-medium text-term-bright">RESET_ACCOUNT?</div>
            <p className="term-mono mt-2 text-[11px] leading-5 text-term-dim">&gt; Balance resets to $1,000. Trade history is NOT deleted.</p>
            <div className="mt-5 flex justify-end gap-2">
              <button type="button" onClick={() => setShowReset(false)} className="term-mono px-4 py-2 text-[11px] text-term-dim hover:text-term-fg">CANCEL</button>
              <button type="button" onClick={() => void resetAccount()} data-testid="button-confirm-reset" className="term-mono border border-term-amber px-4 py-2 text-[11px] font-medium text-term-amber hover:bg-term-amber/10">CONFIRM</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
