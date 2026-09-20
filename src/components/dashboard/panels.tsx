/**
 * Dashboard panels. Presentation only — no calculation of trading values.
 */

import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  api,
  type Account,
  type DoctorReport,
  type SetupReport,
  type SetupSetting,
  type Envelope,
  type HistoryRow,
  type Health,
  type Performance,
  type Position,
  type RiskState,
  type Scan,
  type ScanSymbol,
  type StrategyStatus,
  fmt,
  readToken,
  storeToken,
} from "@/lib/api";
import {
  Badge,
  Card,
  Empty,
  Meter,
  Row,
  Stat,
  Unavailable,
  cn,
  pnlTone,
  tierTone,
} from "./primitives";

function guard<T>(envelope: Envelope<T> | undefined, render: (data: T) => ReactNode): ReactNode {
  if (!envelope) return <Unavailable status="LOADING" />;
  if (envelope.status !== "LIVE" || envelope.data === null) {
    // A weekend is a schedule, not an outage. Showing the broker's raw
    // "circuit open after 5 consecutive failures" for two days running
    // teaches the operator to ignore a warning that will one day be real.
    if (envelope.marketClosed) {
      return (
        <Unavailable
          status="MARKET CLOSED"
          error="The forex market is shut for the weekend. It reopens Sunday 22:00 UTC."
          hint="The bot is idle by design. Nothing is wrong and nothing needs doing."
        />
      );
    }
    return <Unavailable status={envelope.status} error={envelope.error} hint={envelope.hint} />;
  }
  return render(envelope.data);
}

/**
 * The execution-mode banner.
 *
 * Paper mode must be impossible to mistake for live: a user who thinks the
 * bot is trading their demo account when it is only simulating would draw
 * the wrong conclusion from every number on this page.
 */
export function ModeBanner({ health }: { health: Health | undefined }) {
  if (!health?.mode) return null;
  const paper = health.paper;
  return (
    <div
      className={cn(
        'rounded-2xl border p-3 text-xs',
        paper
          ? 'border-sky-800 bg-sky-950/50 text-sky-200'
          : 'border-amber-800 bg-amber-950/40 text-amber-200',
      )}
      role="status"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={paper ? 'info' : 'warn'}>
          {paper ? 'PAPER MODE' : 'LIVE ON DEMO ACCOUNT'}
        </Badge>
        <span className="min-w-0">
          {paper
            ? 'Orders are simulated against live broker prices. Nothing is sent to TradeLocker.'
            : 'Real orders are being placed on your TradeLocker DEMO account.'}
        </span>
      </div>
      {paper && (
        <p className="mt-1.5 text-[11px] text-sky-300/80">
          Every other stage — market data, SMC, scoring, risk, execution intents, position
          management — is the real path. Set <code>TRADING_MODE=demo_live</code> to place real
          demo orders.
        </p>
      )}
    </div>
  );
}

/**
 * How old the served reading is.
 *
 * The page is fed by what the bot last read, not by its own broker calls
 * (see bot/broker/cache.py). Under a second old that distinction does not
 * matter and the line would be noise; once a refresh is overdue or has
 * failed, saying so is the difference between a number and a number you
 * can act on.
 */
function Freshness({ envelope }: { envelope: Envelope<unknown> | undefined }) {
  // Undefined is the loading state, and this renders in places `guard()`
  // does not cover. Taking the envelope as possibly-absent is what stops
  // a placement mistake becoming a blank page: React unmounts the whole
  // tree on a throw, so one undefined read here takes down the dashboard.
  if (!envelope) return null;
  const age = envelope.ageSeconds;
  if (age == null) return null;
  if (!envelope.stale && !envelope.refreshError) return null;
  const when =
    age < 90 ? `${Math.round(age)}s ago` : `${Math.round(age / 60)} min ago`;
  return (
    <p className="mt-3 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-xs text-amber-300">
      Last read from the broker {when}.
      {envelope.refreshError ? ` Refresh is failing: ${envelope.refreshError}` : ""}
    </p>
  );
}

// -- account ---------------------------------------------------------------

export function AccountPanel({ account }: { account: Envelope<Account> | undefined }) {
  return (
    <Card
      title="Account"
      action={guard(account, (data) => (
        <Badge tone={data.demoVerified ? "good" : "bad"}>
          {data.demoVerified ? "DEMO VERIFIED" : "UNVERIFIED"}
        </Badge>
      ))}
    >
      {guard(account, (data) => (
        <>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Stat label="Balance" value={fmt.money(data.balance, data.currency)} />
            <Stat label="Equity" value={fmt.money(data.equity, data.currency)} />
            <Stat
              label="Daily P/L"
              value={fmt.money(data.dailyPnl, data.currency)}
              tone={pnlTone(data.dailyPnl)}
              hint={`realised ${fmt.money(data.dailyRealizedPnl, data.currency)}`}
            />
            <Stat
              label="Total P/L"
              value={fmt.money(data.totalPnl, data.currency)}
              tone={data.totalPnlUnpricedTrades ? 'warn' : pnlTone(data.totalPnl)}
              hint={
                data.totalPnlUnpricedTrades
                  ? `partial — ${data.totalPnlUnpricedTrades} closed trade(s) the broker never priced`
                  : undefined
              }
            />
          </div>
          <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Stat label="Margin used" value={fmt.money(data.marginUsed, data.currency)} />
            <Stat label="Margin free" value={fmt.money(data.marginAvailable, data.currency)} />
            <Stat
              label="Drawdown"
              value={fmt.pct(data.drawdownPct)}
              tone={data.drawdownPct > 5 ? "warn" : "neutral"}
              hint={`peak ${fmt.money(data.peakEquity, data.currency)}`}
            />
            <Stat label="Trades today" value={data.tradesToday} />
          </div>
          {!data.demoVerified && (
            <p className="mt-3 rounded-lg border border-rose-900 bg-rose-950/50 p-2 text-xs text-rose-300">
              Trading is blocked: {data.demoReason ?? "the account could not be verified as DEMO."}
            </p>
          )}
          <Freshness envelope={account} />
        </>
      ))}
    </Card>
  );
}

// -- positions -------------------------------------------------------------

export function PositionsPanel({ positions }: { positions: Envelope<Position[]> | undefined }) {
  return (
    <Card
      title="Open positions"
      subtitle="As the bot last read them from the broker"
      action={guard(positions, (rows) => <Badge>{rows.length} open</Badge>)}
    >
      {guard(positions, (rows) =>
        rows.length === 0 ? (
          <Empty>
            No open positions. Standing aside is a valid result — this system is built to wait.
          </Empty>
        ) : (
          <ul className="space-y-3">
            {rows.map((position) => (
              <li
                key={position.positionId}
                className="rounded-xl border border-slate-800 bg-slate-950/50 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-slate-100">{position.symbol}</span>
                  <Badge tone={position.direction === "BUY" ? "good" : "bad"}>
                    {position.direction}
                  </Badge>
                  {position.setupGrade && (
                    <Badge tone={tierTone(position.setupGrade)}>{position.setupGrade}</Badge>
                  )}
                  {position.orphaned && <Badge tone="warn">ADOPTED</Badge>}
                  {position.tracked === false && !position.orphaned && (
                    <Badge tone="neutral">UNTRACKED</Badge>
                  )}
                  <span
                    className={cn(
                      "ml-auto text-sm font-semibold tabular-nums",
                      position.unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400",
                    )}
                  >
                    {fmt.money(position.unrealizedPnl)}
                  </span>
                </div>
                <div className="mt-3 grid grid-cols-3 gap-2 text-xs sm:grid-cols-6">
                  <Field label="Entry" value={fmt.price(position.entryPrice)} />
                  <Field label="Now" value={fmt.price(position.currentPrice)} />
                  <Field label="Stop" value={fmt.price(position.stopLoss)} />
                  <Field label="Target" value={fmt.price(position.takeProfit)} />
                  <Field label="Lots" value={fmt.number(position.quantity, 2)} />
                  <Field label="R" value={fmt.number(position.rMultiple, 2)} />
                </div>
                {position.currentPrice == null && position.priceStatus && (
                  // A bare "—" over a weekend reads as a broken feed. It is
                  // not: rule 6 forbids inventing a price, so the gap is
                  // correct — it just has to say why it is there.
                  <p className="mt-2 text-[11px] text-slate-500">
                    No live price · {position.priceStatus}
                  </p>
                )}
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
                  <span>Risk {fmt.money(position.riskAmount)}</span>
                  <span>Open {fmt.duration(position.durationMinutes)}</span>
                  <span className="truncate">Position {position.positionId}</span>
                  {position.executionId && (
                    <span className="truncate">Exec {position.executionId.slice(0, 12)}</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        ),
      )}
      <Freshness envelope={positions} />
    </Card>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="tabular-nums text-slate-200">{value}</div>
    </div>
  );
}

// -- setup / SMC -----------------------------------------------------------

export function SetupPanel({ scan }: { scan: Envelope<Scan> | undefined }) {
  return (
    <Card
      title="Current analysis"
      subtitle={guard(scan, (data) => `Scan ${data.scanId} · ${fmt.time(data.finishedAt)}`)}
      action={guard(scan, (data) => (
        <Badge tone={data.decision === "NO TRADE" ? "neutral" : "good"}>{data.decision}</Badge>
      ))}
    >
      {guard(scan, (data) => (
        <>
          {data.skippedReason && (
            <p className="mb-3 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-xs text-amber-300">
              {data.skippedReason}
            </p>
          )}
          {data.unavailable && data.unavailable.length > 0 && (
            <div className="mb-3 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-xs text-amber-300">
              <p className="font-semibold">
                {data.unavailable.length} configured symbol
                {data.unavailable.length === 1 ? "" : "s"} this account cannot trade
              </p>
              <ul className="mt-1 space-y-0.5">
                {data.unavailable.map((entry) => (
                  <li key={entry.symbol}>
                    <span className="font-mono">{entry.symbol}</span>
                    {entry.suggestions.length > 0 ? (
                      <>
                        {" — try "}
                        <span className="font-mono">{entry.suggestions.join(", ")}</span>
                      </>
                    ) : (
                      " — nothing on this account resembles it"
                    )}
                  </li>
                ))}
              </ul>
              <p className="mt-1 opacity-80">
                They fail identically on every scan until TRADED_SYMBOLS changes.
              </p>
            </div>
          )}
          {data.errors.length > 0 && (
            <div className="mb-3 rounded-lg border border-red-900 bg-red-950/40 p-2 text-xs text-red-300">
              <p className="font-semibold">
                {data.errors.length} symbol{data.errors.length === 1 ? "" : "s"} failed this scan
              </p>
              <ul className="mt-1 space-y-0.5">
                {data.errors.map((line) => (
                  <li key={line} className="break-words">
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {data.symbols.length === 0 ? (
            <Empty>No symbols were analysed in this scan.</Empty>
          ) : (
            <div className="space-y-3">
              {data.symbols.map((entry) => (
                <SymbolCard key={entry.symbol} entry={entry} />
              ))}
            </div>
          )}
        </>
      ))}
    </Card>
  );
}

/** How much the setup is fighting: green with the bias, amber against it. */
function setupTone(setupType: string | undefined): "good" | "warn" | "neutral" {
  switch (setupType) {
    case "CONTINUATION":
      return "good";
    case "RANGE_ROTATION":
      return "neutral";
    case "REVERSAL":
      return "warn";
    default:
      return "neutral";
  }
}

/**
 * The strategy chain for one symbol, condition by condition.
 *
 * The reason string says what went wrong. This says what went RIGHT
 * first, which is the half it cannot carry: "no live fair value gap"
 * leaves an operator guessing whether the sweep was found and the break
 * confirmed, or whether the whole thing fell over at the first hurdle.
 *
 * PENDING is deliberately its own colour rather than a dimmer FAIL. A
 * setup priced out at `risk_reward` got everything else right; one that
 * failed at `liquidity_sweep` never started. Same row, opposite meaning.
 */
function SetupChain({ checks }: { checks?: ScanSymbol["checks"] }) {
  if (!checks?.length) return null;
  const tone = { PASS: "bg-emerald-500", FAIL: "bg-rose-500", PENDING: "bg-slate-700" } as const;
  const failed = checks.find((check) => check.status === "FAIL");
  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-1">
        {checks.map((check) => (
          <span
            key={check.name}
            title={`${check.name}: ${check.status}${check.detail ? ` — ${check.detail}` : ""}`}
            className="flex items-center gap-1 rounded-md bg-slate-900/70 px-1.5 py-0.5 text-[10px] text-slate-400"
          >
            <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", tone[check.status])} />
            {check.name.replace(/_/g, " ")}
          </span>
        ))}
      </div>
      {failed?.detail && (
        <p className="mt-1 text-[10px] text-rose-300">
          {failed.name.replace(/_/g, " ")}: {failed.detail}
        </p>
      )}
    </div>
  );
}

function SymbolCard({ entry }: { entry: ScanSymbol }) {
  const candidate = entry.candidate;
  const score = entry.score;
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-slate-100">{entry.symbol}</span>
        <Badge tone={entry.outcome === "CANDIDATE" ? "good" : "neutral"}>{entry.outcome}</Badge>
        <span className="text-[11px] text-slate-500">stage {entry.stage}</span>
        {score && (
          <span className="ml-auto flex items-center gap-2">
            <Badge tone={tierTone(score.tier)}>{score.tier}</Badge>
            <span className="text-xs tabular-nums text-slate-300">{score.total.toFixed(1)}/100</span>
          </span>
        )}
      </div>

      {entry.reason && <p className="mt-2 text-xs text-slate-400">{entry.reason}</p>}

      <SetupChain checks={entry.checks} />

      {candidate && (
        <>
          <div className="mt-3 grid grid-cols-3 gap-2 text-xs sm:grid-cols-6">
            <Field label="Direction" value={candidate.direction} />
            <Field label="Entry" value={fmt.price(candidate.entry)} />
            <Field label="Stop" value={fmt.price(candidate.stopLoss)} />
            <Field label="Target" value={fmt.price(candidate.takeProfit)} />
            <Field label="R:R" value={`1:${fmt.number(candidate.riskReward, 2)}`} />
            <Field label="Session" value={candidate.session?.name ?? "—"} />
          </div>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <Badge tone="info">H1 {candidate.h1Bias}</Badge>
            <Badge tone="info">M15 {candidate.m15Bias}</Badge>
            {/*
              The classification, not an agreement score. H1 and M15
              pointing the same way is not what makes a setup good - a
              reversal disagrees with the trend by definition - so what is
              shown is how the setup stands to the trend, and the score it
              therefore had to clear.
            */}
            <Badge tone={setupTone(candidate.setupType)}>
              {String(candidate.setupType ?? candidate.alignment).replace(/_/g, " ")}
            </Badge>
            {Number(candidate.scoreFloor) > 0 && (
              <Badge tone="neutral">needs {Math.round(Number(candidate.scoreFloor))}</Badge>
            )}
            {candidate.sweep && (
              <Badge tone="good">sweep {Number(candidate.sweep.quality).toFixed(2)}</Badge>
            )}
            {candidate.structureEvent && (
              <Badge tone="good">
                {candidate.structureEvent.type} {candidate.structureEvent.direction}
              </Badge>
            )}
            {candidate.displacement && (
              <Badge tone="good">
                disp {Number(candidate.displacement.atrMultiple).toFixed(1)}×
              </Badge>
            )}
            {candidate.pointOfInterest && (
              <Badge tone="info">{candidate.pointOfInterest.kind}</Badge>
            )}
            {candidate.dealingRange && (
              <Badge tone="neutral">{candidate.dealingRange.zone}</Badge>
            )}
            {candidate.regime && (
              <Badge tone="neutral">
                {candidate.regime.trend}/{candidate.regime.volatility}
              </Badge>
            )}
          </div>
        </>
      )}

      {score && (
        <div className="mt-3 space-y-1.5">
          {Object.entries(score.components).map(([name, value]) => (
            <div key={name} className="flex items-center gap-2">
              <span className="w-28 shrink-0 text-[11px] capitalize text-slate-500">
                {name.replace(/_/g, " ")}
              </span>
              <Meter
                value={value}
                max={score.maxima[name] ?? 1}
                tone={value / (score.maxima[name] || 1) > 0.6 ? "good" : "warn"}
              />
              <span className="w-14 shrink-0 text-right text-[11px] tabular-nums text-slate-400">
                {value.toFixed(1)}/{(score.maxima[name] ?? 0).toFixed(0)}
              </span>
            </div>
          ))}
        </div>
      )}

      {entry.ai && (
        <p className="mt-3 rounded-lg border border-slate-800 bg-slate-900/60 p-2 text-[11px] text-slate-400">
          <span className="font-semibold text-slate-300">AI review:</span>{" "}
          {(entry.ai.reasons ?? []).join(" · ")}
        </p>
      )}

      {entry.risk && (
        <p className="mt-2 text-[11px] text-slate-500">
          <span className="font-semibold text-slate-400">Risk:</span>{" "}
          {(entry.risk.reasons ?? []).join(" · ")}
        </p>
      )}
    </div>
  );
}

// -- risk ------------------------------------------------------------------

export function RiskPanel({ risk }: { risk: Envelope<RiskState> | undefined }) {
  return (
    <Card
      title="Risk"
      subtitle="One engine, one authority — nothing else sizes a trade"
      action={guard(risk, (data) => (
        <Badge tone={data.killSwitch.active ? "bad" : "good"}>
          {data.killSwitch.active ? "KILL SWITCH ACTIVE" : "ARMED"}
        </Badge>
      ))}
    >
      {guard(risk, (data) => (
        <>
          {data.killSwitch.active && (
            <p className="mb-3 rounded-lg border border-rose-900 bg-rose-950/50 p-2 text-xs text-rose-300">
              No new trades: {data.killSwitch.reason}
              {data.killSwitch.detail ? ` — ${data.killSwitch.detail}` : ""}
            </p>
          )}
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Stat
              label="Daily P/L"
              value={fmt.money(data.dailyPnl)}
              tone={pnlTone(data.dailyPnl)}
              hint={`limit ${fmt.money(-data.balance * (data.limits.maxDailyLossPct ?? 0))}`}
            />
            <Stat
              label="Drawdown"
              value={fmt.pct(data.drawdownPct)}
              tone={data.drawdownPct > 5 ? "warn" : "neutral"}
              hint={`limit ${fmt.percent(data.limits.maxDrawdownPct)}`}
            />
            <Stat
              label="Loss streak"
              value={data.consecutiveLosses}
              tone={data.consecutiveLosses >= 2 ? "warn" : "neutral"}
              hint={`stop at ${data.limits.maxConsecutiveLosses}`}
            />
            <Stat
              label="Open positions"
              value={`${data.openPositions}/${data.limits.maxOpenPositions}`}
            />
          </div>
          <div className="mt-4 space-y-1">
            <Row
              label="Risk per trade"
              value={`${fmt.percent(data.limits.baseRiskPct)} base · ${fmt.percent(
                data.limits.maxRiskPct,
              )} hard cap`}
            />
            <Row label="Portfolio cap" value={fmt.percent(data.limits.maxPortfolioRiskPct)} />
            <Row
              label="Trades today"
              value={`${data.tradesToday}/${data.limits.maxTradesPerDay}`}
            />
            <Row label="Minimum R:R" value={`1:${fmt.number(data.limits.minRiskReward, 1)}`} />
            {data.rewardObjective && (
              <>
                <Row
                  label="Risk per trade"
                  value={`${fmt.money(data.rewardObjective.riskPerTrade)} (1R)`}
                />
                <Row
                  label="Reward objective"
                  value={`1:${fmt.number(data.rewardObjective.minimumR, 1)} min · 1:${fmt.number(
                    data.rewardObjective.preferredR,
                    1,
                  )} strong`}
                />
              </>
            )}
          </div>
          {data.rewardObjective && (
            <p className="mt-3 text-[11px] leading-snug text-slate-500">
              A setup is judged on its ratio, never on a dollar figure. At{" "}
              {fmt.money(data.rewardObjective.riskPerTrade)} per R the minimum target is worth about{" "}
              {fmt.money(data.rewardObjective.riskPerTrade * data.rewardObjective.minimumR)} — but
              the structure sets the stop and the target, so the ratio is what is checked. Size is
              never raised, and the stop is never tightened, to make a trade worth more.
            </p>
          )}
          <p className="mt-3 text-[11px] text-slate-500">
            Risk is reduced by drawdown and losing streaks, never increased.
          </p>
          <Freshness envelope={risk} />
        </>
      ))}
    </Card>
  );
}

// -- performance -----------------------------------------------------------

export function PerformancePanel({ performance }: { performance: Envelope<Performance> | undefined }) {
  return (
    <Card
      title="Performance"
      action={guard(performance, (data) => (
        <Badge tone={data.sample === "adequate" ? "good" : "warn"}>
          {data.sample === "adequate" ? "sample adequate" : "small sample"}
        </Badge>
      ))}
    >
      {guard(performance, (data) => (
        <>
          {data.trades === 0 ? (
            <Empty>
              No closed trades yet. Statistics stay empty rather than being computed from nothing.
            </Empty>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Stat label="Trades" value={data.trades} hint={`${data.wins}W / ${data.losses}L`} />
                <Stat label="Win rate" value={fmt.percent(data.winRate)} />
                <Stat
                  label="Profit factor"
                  value={data.profitFactor === null ? "—" : fmt.number(data.profitFactor, 2)}
                  tone={(data.profitFactor ?? 0) > 1 ? "good" : "bad"}
                />
                <Stat
                  label="Expectancy"
                  value={fmt.money(data.expectancy)}
                  tone={pnlTone(data.expectancy)}
                />
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Stat label="Avg win" value={fmt.money(data.averageWin)} tone="good" />
                <Stat label="Avg loss" value={fmt.money(data.averageLoss)} tone="bad" />
                <Stat label="Avg R" value={fmt.number(data.averageR, 2)} />
                <Stat label="Max drawdown" value={fmt.money(data.maxDrawdown)} tone="warn" />
              </div>
              <div className="mt-4 grid grid-cols-3 gap-4">
                <Stat label="Today" value={fmt.money(data.dailyPnl)} tone={pnlTone(data.dailyPnl)} />
                <Stat label="Week" value={fmt.money(data.weeklyPnl)} tone={pnlTone(data.weeklyPnl)} />
                <Stat label="Month" value={fmt.money(data.monthlyPnl)} tone={pnlTone(data.monthlyPnl)} />
              </div>
              {data.sample === "insufficient" && (
                <p className="mt-3 text-[11px] text-amber-400">
                  Fewer than 20 closed trades. These numbers describe what happened; they are not
                  yet evidence of an edge.
                </p>
              )}
              <BreakdownTable title="By symbol" rows={data.bySymbol} />
              <BreakdownTable title="By setup grade" rows={data.byGrade} />
            </>
          )}
        </>
      ))}
    </Card>
  );
}

function BreakdownTable({ title, rows }: { title: string; rows: Performance["bySymbol"] }) {
  if (!rows?.length) return null;
  return (
    <div className="mt-4">
      <h3 className="mb-1.5 text-xs font-semibold text-slate-400">{title}</h3>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="pb-1">Key</th>
              <th className="pb-1 text-right">Trades</th>
              <th className="pb-1 text-right">Win</th>
              <th className="pb-1 text-right">P/L</th>
              <th className="pb-1 text-right">Expectancy</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {rows.map((row) => (
              <tr key={row.key} className="border-t border-slate-800/60">
                <td className="py-1.5">{row.key}</td>
                <td className="py-1.5 text-right tabular-nums">{row.trades}</td>
                <td className="py-1.5 text-right tabular-nums">{fmt.percent(row.winRate)}</td>
                <td
                  className={cn(
                    "py-1.5 text-right tabular-nums",
                    row.totalPnl >= 0 ? "text-emerald-400" : "text-rose-400",
                  )}
                >
                  {fmt.money(row.totalPnl)}
                </td>
                <td className="py-1.5 text-right tabular-nums">{fmt.money(row.expectancy)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// -- history ---------------------------------------------------------------

export function HistoryPanel({ history }: { history: Envelope<HistoryRow[]> | undefined }) {
  return (
    <Card title="Trade history">
      {guard(history, (rows) =>
        rows.length === 0 ? (
          <Empty>No closed trades yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="pb-1">Closed</th>
                  <th className="pb-1">Symbol</th>
                  <th className="pb-1">Side</th>
                  <th className="pb-1 text-right">Entry</th>
                  <th className="pb-1 text-right">Exit</th>
                  <th className="pb-1 text-right">Lots</th>
                  <th className="pb-1 text-right">Risk</th>
                  <th className="pb-1 text-right">P/L</th>
                  <th className="pb-1 text-right">R</th>
                  <th className="pb-1">Grade</th>
                  <th className="pb-1">Reason</th>
                  <th className="pb-1 text-right">Held</th>
                </tr>
              </thead>
              <tbody className="text-slate-300">
                {rows.map((row) => (
                  <tr key={row.executionId} className="border-t border-slate-800/60">
                    <td className="py-1.5 whitespace-nowrap">{fmt.time(row.closedAt)}</td>
                    <td className="py-1.5">{row.symbol}</td>
                    <td className="py-1.5">{row.direction}</td>
                    <td className="py-1.5 text-right tabular-nums">{fmt.price(row.entry)}</td>
                    <td className="py-1.5 text-right tabular-nums">{fmt.price(row.exit)}</td>
                    <td className="py-1.5 text-right tabular-nums">{fmt.number(row.quantity, 2)}</td>
                    <td className="py-1.5 text-right tabular-nums">{fmt.money(row.riskAmount)}</td>
                    <td
                      className={cn(
                        "py-1.5 text-right font-medium tabular-nums",
                        (row.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400",
                      )}
                    >
                      {fmt.money(row.pnl)}
                    </td>
                    <td className="py-1.5 text-right tabular-nums">{fmt.number(row.rMultiple, 2)}</td>
                    <td className="py-1.5">{row.setupGrade ?? "—"}</td>
                    <td className="py-1.5 whitespace-nowrap">{row.exitReason ?? "—"}</td>
                    <td className="py-1.5 text-right">{fmt.duration(row.durationMinutes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ),
      )}
    </Card>
  );
}

// -- health ----------------------------------------------------------------

const COMPONENT_LABELS: Record<string, string> = {
  database: "Database",
  broker: "TradeLocker",
  demo: "DEMO verification",
  marketData: "Market data",
  ai: "AI",
  news: "News filter",
  startup: "Startup sequence",
  scanner: "Scanner",
};

/** The first human-readable explanation a health component offers. */
function componentDetail(component: Record<string, any>): string | null {
  for (const key of ["note", "reason", "error", "hint"]) {
    const value = component?.[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

export function HealthPanel({ health }: { health: Health | undefined }) {
  if (!health) return <Card title="System health"><Unavailable status="LOADING" /></Card>;
  const components = health.components ?? {};
  return (
    <Card
      title="System health"
      subtitle={`Uptime ${fmt.uptime(health.uptimeSeconds)} · ${health.symbols?.length ?? 0} symbols watched`}
      action={<Badge tone={health.ok ? "good" : "bad"}>{health.ok ? "HEALTHY" : "DEGRADED"}</Badge>}
    >
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {Object.entries(COMPONENT_LABELS).map(([key, label]) => {
          const component = components[key] ?? {};
          const ok = component.ok ?? component.enabled ?? false;
          // A component switched off on purpose is not a fault. Reporting
          // AI as DOWN when AI_ENABLED=false sent the last operator
          // hunting for a broken provider that was never configured.
          const off = key === "ai" && component.enabled === false;
          // A bare DOWN badge says something is wrong without saying what,
          // which is the least useful thing a health panel can do. Every
          // component that reports a reason shows it here.
          const detail = componentDetail(component);
          return (
            <div
              key={key}
              className="rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-300">{label}</span>
                <Badge tone={off ? "neutral" : ok ? "good" : "bad"}>
                  {off ? "OFF" : ok ? "OK" : "DOWN"}
                </Badge>
              </div>
              {!ok && detail && (
                <p className="mt-1.5 text-[11px] leading-snug text-rose-300/90">{detail}</p>
              )}
            </div>
          );
        })}
      </div>

      {components.killSwitch?.active && (
        <p className="mt-3 rounded-lg border border-rose-900 bg-rose-950/50 p-2 text-xs text-rose-300">
          Kill switch active: {components.killSwitch.reason}
        </p>
      )}
      {components.startup?.error && (
        <p className="mt-3 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-xs text-amber-300">
          {components.startup.error}
        </p>
      )}

      <div className="mt-4 space-y-1">
        <Row label="Last scan" value={fmt.time(components.scanner?.lastScan?.at)} />
        <Row label="Scan decision" value={components.scanner?.lastScan?.decision ?? "—"} />
        <Row label="Broker circuit" value={components.broker?.circuit ?? "—"} />
        <Row label="AI calls" value={components.ai?.calls ?? 0} />
        <Row label="Database" value={components.database?.backend ?? "—"} />
        <Row label="Strategy version" value={health.versions?.smc ?? "—"} />
        <Row label="Risk version" value={health.versions?.risk ?? "—"} />
      </div>

      {Array.isArray(health.upcomingNews) && health.upcomingNews.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-1.5 text-xs font-semibold text-slate-400">Upcoming high-impact news</h3>
          <ul className="space-y-1 text-[11px] text-slate-400">
            {health.upcomingNews.slice(0, 5).map((event: any, index: number) => (
              <li key={index} className="flex justify-between gap-2">
                <span className="truncate">
                  {event.country} · {event.title}
                </span>
                <span className="shrink-0 tabular-nums">in {event.minutesAway}m</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

/**
 * Configuration checklist.
 *
 * Shown whenever something required is missing, because the failure that
 * actually strands people is a deployed service with one absent variable and
 * no indication of which. Presence only — never a value.
 */
export function ConfigurationPanel({ setup }: { setup: SetupReport | undefined }) {
  const [copied, setCopied] = useState(false);
  if (!setup) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(setup.pasteBlock);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 4000);
    } catch {
      /* selection fallback: the block is rendered below */
    }
  };

  const byImportance = (level: SetupSetting['importance']) =>
    setup.settings.filter((item) => item.importance === level);

  return (
    <Card
      title="Configuration"
      subtitle="What this deployment has, and what it still needs. Values are never shown."
      action={
        <Badge tone={setup.ready ? 'good' : 'bad'}>
          {setup.ready ? 'READY' : `${setup.missingRequired.length} MISSING`}
        </Badge>
      }
    >
      <p className="rounded-lg border border-slate-800 bg-slate-950/40 p-2 text-xs text-slate-300">
        {setup.nextStep}
      </p>

      {setup.warnings.map((warning, index) => (
        <p
          key={index}
          className="mt-2 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-[11px] text-amber-300"
        >
          {warning}
        </p>
      ))}

      {(['required', 'recommended', 'optional'] as const).map((level) => {
        const items = byImportance(level);
        if (!items.length) return null;
        return (
          <div key={level} className="mt-4">
            <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              {level}
            </h3>
            <ul className="space-y-1">
              {items.map((item) => (
                <li
                  key={item.name}
                  className="flex items-start gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-2 py-1.5"
                >
                  <span
                    className={cn(
                      'mt-0.5 shrink-0 text-xs',
                      item.present ? 'text-emerald-400' : level === 'required' ? 'text-rose-400' : 'text-amber-400',
                    )}
                    aria-hidden
                  >
                    {item.present ? '✓' : '✗'}
                  </span>
                  <span className="min-w-0">
                    <span className="block font-mono text-[11px] text-slate-200">{item.name}</span>
                    <span className="block text-[11px] text-slate-500">{item.purpose}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        );
      })}

      {setup.pasteBlock && (
        <div className="mt-4">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              Paste into your host's environment variables
            </h3>
            <button
              type="button"
              onClick={copy}
              className="inline-flex min-h-[32px] items-center rounded-lg border border-sky-800 bg-sky-950 px-2.5 text-[11px] font-medium text-sky-200 hover:bg-sky-900"
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-950 p-3 text-[10px] leading-relaxed text-slate-300">
            {setup.pasteBlock}
          </pre>
          <p className="mt-1.5 text-[11px] text-slate-500">
            Fill the blanks in your host's settings — never in a chat, a file, or this page.
          </p>
        </div>
      )}
    </Card>
  );
}

/**
 * Broker verification, in the UI.
 *
 * The same read-only check as `python3 -m bot.doctor`, reachable without a
 * terminal — which is the only way some operators can reach it at all. The
 * report is masked server-side, so Copy produces something safe to share.
 */
export function DoctorPanel() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const runCheck = async () => {
    setRunning(true);
    setError(null);
    setCopied(false);
    try {
      setReport(await api.doctor());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'verification failed');
    } finally {
      setRunning(false);
    }
  };

  const copy = async () => {
    if (!report?.text) return;
    try {
      await navigator.clipboard.writeText(report.text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 4000);
    } catch {
      setError('Could not copy automatically — select the text below manually.');
    }
  };

  const tone =
    report?.verdict === 'PASS' ? 'good' : report?.verdict === 'WARN' ? 'warn' : 'bad';

  return (
    <Card
      title="Verify broker account"
      subtitle="Read-only. Never places an order. Balances are masked, so the report is safe to share."
      action={report ? <Badge tone={tone}>{report.verdict}</Badge> : undefined}
    >
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={runCheck}
          disabled={running}
          className="inline-flex min-h-[38px] items-center rounded-lg border border-slate-700 bg-slate-900 px-3 text-xs font-medium text-slate-200 hover:bg-slate-800 disabled:opacity-50"
        >
          {running ? 'Checking…' : 'Run verification'}
        </button>
        {report?.text && (
          <button
            type="button"
            onClick={copy}
            className="inline-flex min-h-[38px] items-center rounded-lg border border-sky-800 bg-sky-950 px-3 text-xs font-medium text-sky-200 hover:bg-sky-900"
          >
            {copied ? 'Copied' : 'Copy report'}
          </button>
        )}
      </div>

      {running && (
        <p className="mt-3 text-[11px] text-slate-500">
          This makes a few dozen read calls to the broker and can take up to a minute.
        </p>
      )}
      {error && (
        <p className="mt-3 rounded-lg border border-rose-900 bg-rose-950/50 p-2 text-xs text-rose-300">
          {error}
        </p>
      )}
      {report && report.failed.length > 0 && (
        <p className="mt-3 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-xs text-amber-300">
          <span className="font-semibold">Failing: </span>
          {report.failed.join(', ')}
        </p>
      )}
      {report?.text && (
        <pre className="mt-3 max-h-96 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-3 text-[10px] leading-relaxed text-slate-300">
          {report.text}
        </pre>
      )}
    </Card>
  );
}

// -- journal ---------------------------------------------------------------

/**
 * The ranked causes of NO TRADE.
 *
 * "Why is it not trading?" is the question every operator of a selective
 * system eventually asks, and the honest answer is a measurement, not an
 * opinion. A stage histogram says SMC/NO_SETUP and explains nothing; the
 * top row here names the binding constraint, which is usually one thing
 * and is usually fixable.
 */
export function BlockersPanel({ blockers }: { blockers: any[] }) {
  return (
    <Card
      title="Why no trade"
      subtitle="The last 7 days of rejections, ranked by cause"
    >
      {!blockers?.length ? (
        <Empty>
          Nothing rejected yet. Once the bot has scanned a few sessions, the reasons it stood
          aside are counted here.
        </Empty>
      ) : (
        <ul className="space-y-2">
          {blockers.map((entry, index) => (
            <li key={index}>
              <div className="flex items-baseline justify-between gap-2 text-[11px]">
                <span className="truncate text-slate-300">{entry.reason}</span>
                <span className="shrink-0 tabular-nums text-slate-500">
                  {entry.count} · {Math.round((entry.share ?? 0) * 100)}%
                </span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-800">
                <div
                  className={cn("h-full rounded-full", index === 0 ? "bg-amber-500" : "bg-slate-600")}
                  style={{ width: `${Math.max(2, Math.round((entry.share ?? 0) * 100))}%` }}
                />
              </div>
              <p className="mt-0.5 text-[10px] uppercase tracking-wide text-slate-600">
                {entry.stage}
              </p>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-4 text-[11px] text-slate-500">
        A high count is not a fault. NO TRADE is the expected result — this only shows which
        filter is doing the work, so any change is made against evidence rather than a hunch.
      </p>
    </Card>
  );
}

export function JournalPanel({ rows, histogram }: { rows: any[]; histogram: any[] }) {
  return (
    <Card
      title="Decision journal"
      subtitle="Why the system stood aside — the record that makes tuning evidence-based"
    >
      {histogram?.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5">
          {histogram.slice(0, 10).map((entry, index) => (
            <Badge key={index} tone="neutral">
              {entry.stage}/{entry.outcome}: {entry.count}
            </Badge>
          ))}
        </div>
      )}
      {!rows?.length ? (
        <Empty>No decisions recorded yet.</Empty>
      ) : (
        <ul className="space-y-2">
          {rows.slice(0, 40).map((row, index) => (
            <li key={index} className="rounded-lg border border-slate-800 bg-slate-950/40 p-2">
              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                <span className="font-semibold text-slate-200">{row.symbol}</span>
                <Badge tone={row.outcome === "CANDIDATE" ? "good" : "neutral"}>{row.outcome}</Badge>
                <span className="text-slate-500">{row.stage}</span>
                <span className="ml-auto text-slate-600">{fmt.time(row.created_at)}</span>
              </div>
              {row.reason && <p className="mt-1 text-[11px] text-slate-400">{row.reason}</p>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// -- strategy --------------------------------------------------------------

/**
 * The mode switch.
 *
 * Selecting a strategy changes which analysis runs — nothing else. The
 * demo guard, the risk engine and the execution guards are the same code
 * either way (project rule 11: presentation may make the system safer or
 * ask it to look again; it may never open, size or close a trade).
 *
 * Each option states plainly whether it can actually trade at the current
 * equity. A mode whose target is smaller than the profit floor will find
 * setups and watch every one of them rejected, and that belongs at the
 * switch rather than at the end of a silent week.
 */
export function StrategyPanel({
  status,
  onSelect,
  pending,
  lock,
}: {
  status: StrategyStatus | undefined;
  onSelect: (key: string) => void;
  pending: boolean;
  /**
   * Whether the command surface will accept a switch at all.
   *
   * Switching modes can widen what the bot does, so the proxy requires
   * DASHBOARD_TOKEN for it (project rule 15). That refusal used to arrive
   * only as a toast that cleared itself after six seconds, which on a
   * phone reads as "the button does nothing" — and the operator concludes
   * the switch is broken rather than locked. The state belongs on the
   * control itself.
   */
  lock?: "ok" | "wrong" | "unset" | "checking";
}) {
  const locked = lock !== undefined && lock !== "ok";
  if (!status) {
    return (
      <Card title="Strategy">
        <Unavailable status="LOADING" />
      </Card>
    );
  }
  return (
    <Card
      title="Strategy"
      subtitle="Which analysis looks for trades. Risk, sizing and the demo guard never change."
    >
      {locked && (
        <p className="mb-3 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-[11px] leading-snug text-amber-300">
          {lock === "unset" ? (
            <>
              Switching is disabled because this deployment has no{" "}
              <code>DASHBOARD_TOKEN</code> set. Add it to the environment and redeploy, then
              unlock the dashboard below with the same value.
            </>
          ) : (
            <>
              Unlock the dashboard below to switch modes. Switching can widen what the bot does,
              so it needs the deployment token; stopping the bot never does.
            </>
          )}
        </p>
      )}
      <div className="space-y-2">
        {status.options.map((option) => {
          const active = option.key === status.active;
          return (
            <button
              key={option.key}
              type="button"
              disabled={pending || active || locked}
              onClick={() => onSelect(option.key)}
              className={cn(
                "block w-full rounded-xl border p-3 text-left transition",
                "min-h-[38px] disabled:cursor-default",
                active
                  ? "border-sky-700 bg-sky-950/40"
                  : "border-slate-800 bg-slate-950/40 hover:border-slate-700",
              )}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-slate-100">{option.name}</span>
                {active ? (
                  <Badge tone="good">ACTIVE</Badge>
                ) : (
                  <Badge tone={locked ? "warn" : "neutral"}>
                    {locked ? "LOCKED" : pending ? "…" : "SWITCH"}
                  </Badge>
                )}
                <span className="ml-auto shrink-0 text-[11px] tabular-nums text-slate-400">
                  min 1:{option.minRiskReward}
                </span>
              </div>
              <p className="mt-1.5 text-[11px] leading-snug text-slate-400">{option.description}</p>
              <p className="mt-1 text-[11px] text-slate-500">
                Expect {option.expectedFrequency}.
              </p>
              {option.note && (
                <p className="mt-2 rounded-lg border border-amber-900 bg-amber-950/40 p-2 text-[11px] leading-snug text-amber-300">
                  {option.note}
                </p>
              )}
            </button>
          );
        })}
      </div>
      <p className="mt-4 text-[11px] leading-snug text-slate-500">
        Switching takes effect on the next scan and survives a restart. Open positions are not
        touched — they keep the stop and target they were opened with. No win rate is claimed for
        either mode; the trade history is the only thing that can say.
      </p>
    </Card>
  );
}

// -- unlock ----------------------------------------------------------------

/**
 * Unlocks the controls that can resume or start trading.
 *
 * The deployment sits on a public URL, so those controls need a secret.
 * Stopping the bot deliberately does NOT — an operator must always be
 * able to hit the brakes, from any device, having lost anything.
 */
export function UnlockPanel({ onChange }: { onChange?: (state: string) => void } = {}) {
  const [value, setValue] = useState("");
  const [state, setState] = useState<"checking" | "ok" | "wrong" | "unset" | "empty">(
    () => (readToken() ? "checking" : "empty"),
  );
  const [message, setMessage] = useState<string | null>(null);

  // Asked of the server, never assumed. Holding a string is not the same
  // as holding the right string, and the previous version could not tell
  // the difference — it reported UNLOCKED over a deployment that had no
  // token at all, and the operator found out from a failed scan.
  const check = useCallback(async () => {
    if (!readToken()) {
      setState("empty");
      onChange?.("empty");
      return;
    }
    setState("checking");
    const result = await api.verifyToken();
    setState(result);
    // Other controls read the same lock. Telling them here is what makes
    // unlocking take effect on the Strategy card without a page reload.
    onChange?.(result);
  }, [onChange]);

  useEffect(() => {
    void check();
  }, [check]);

  const badge =
    state === "ok"
      ? { tone: "good" as const, label: "UNLOCKED" }
      : state === "unset"
        ? { tone: "bad" as const, label: "NO SERVER TOKEN" }
        : state === "wrong"
          ? { tone: "bad" as const, label: "WRONG TOKEN" }
          : state === "checking"
            ? { tone: "neutral" as const, label: "CHECKING" }
            : { tone: "warn" as const, label: "LOCKED" };

  return (
    <Card
      title="Dashboard lock"
      subtitle="Controls that can start or resume trading need the deployment token"
      action={<Badge tone={badge.tone}>{badge.label}</Badge>}
    >
      {state === "unset" && (
        <p className="mb-3 rounded-lg border border-rose-900 bg-rose-950/40 p-2 text-[11px] leading-snug text-rose-300">
          This deployment has no <code>DASHBOARD_TOKEN</code> set, so no token can unlock it.
          Add <code>DASHBOARD_TOKEN</code> to the environment and redeploy, then come back and
          paste the same value here. Until then, every control that could start trading stays
          disabled — which is the safe state, not a fault.
        </p>
      )}
      {state === "wrong" && (
        <p className="mb-3 rounded-lg border border-rose-900 bg-rose-950/40 p-2 text-[11px] leading-snug text-rose-300">
          The server did not accept this token. Check it matches{" "}
          <code>DASHBOARD_TOKEN</code> exactly, with no stray spaces.
        </p>
      )}

      {state === "ok" ? (
        <>
          <p className="text-[11px] leading-snug text-slate-400">
            The server accepts this browser&rsquo;s token. Every control is available.
          </p>
          <button
            type="button"
            onClick={() => {
              storeToken(null);
              setState("empty");
              onChange?.("empty");
              setMessage("Token removed from this browser.");
            }}
            className="mt-3 min-h-[38px] w-full rounded-xl border border-slate-700 bg-slate-900 px-3 text-xs font-medium text-slate-200"
          >
            Forget token on this device
          </button>
        </>
      ) : (
        <>
          <p className="text-[11px] leading-snug text-slate-400">
            Paste the value of <code className="text-slate-300">DASHBOARD_TOKEN</code> from the
            deployment environment. It is stored in this browser only and never leaves this site.
          </p>
          <form
            className="mt-3 space-y-2"
            onSubmit={(event) => {
              event.preventDefault();
              const trimmed = value.trim();
              if (!trimmed) return;
              storeToken(trimmed);
              setValue("");
              setMessage(null);
              void check();
            }}
          >
            <input
              type="password"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              autoComplete="off"
              placeholder="DASHBOARD_TOKEN"
              aria-label="Dashboard token"
              className="min-h-[38px] w-full rounded-xl border border-slate-700 bg-slate-950 px-3 text-xs text-slate-100 placeholder:text-slate-600"
            />
            <button
              type="submit"
              disabled={state === "checking"}
              className="min-h-[38px] w-full rounded-xl border border-sky-700 bg-sky-950/60 px-3 text-xs font-semibold text-sky-200 disabled:opacity-60"
            >
              {state === "checking" ? "Checking…" : "Unlock"}
            </button>
          </form>
        </>
      )}
      {message && <p className="mt-2 text-[11px] text-slate-500">{message}</p>}
      <p className="mt-4 text-[11px] leading-snug text-slate-500">
        Emergency stop and pause never require this. Stopping the bot must work even when nothing
        else does.
      </p>
    </Card>
  );
}
