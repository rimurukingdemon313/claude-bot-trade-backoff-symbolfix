/**
 * Mobile-first trading dashboard.
 *
 * Three rules this page obeys:
 *  1. it never computes a trading value — every number comes from the bot;
 *  2. a value that cannot be read renders as OFFLINE/UNKNOWN, never as 0;
 *  3. its controls can only make the system safer (pause, kill switch) or
 *     ask it to look again (scan, reconcile). There is no control here
 *     that can bypass the demo guard, the risk limits or the execution
 *     guards — those live in the bot and are enforced server-side.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Briefcase,
  History,
  Pause,
  Play,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Target,
} from "lucide-react";
import { api, fmt, type Snapshot } from "@/lib/api";
import { Badge, Card, cn } from "@/components/dashboard/primitives";
import {
  AccountPanel,
  ConfigurationPanel,
  DoctorPanel,
  HealthPanel,
  HistoryPanel,
  BlockersPanel,
  StrategyPanel,
  UnlockPanel,
  JournalPanel,
  ModeBanner,
  PerformancePanel,
  PositionsPanel,
  RiskPanel,
  SetupPanel,
} from "@/components/dashboard/panels";

const TABS = [
  { id: "overview", label: "Overview", icon: Activity },
  { id: "setup", label: "Setup", icon: Target },
  { id: "history", label: "History", icon: History },
  { id: "performance", label: "Stats", icon: BarChart3 },
  { id: "system", label: "System", icon: Briefcase },
] as const;

type TabId = (typeof TABS)[number]["id"];

const REFRESH_MS = 15_000;

export default function Dashboard() {
  const [tab, setTab] = useState<TabId>("overview");
  const [notice, setNotice] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const snapshot = useQuery<Snapshot>({
    queryKey: ["snapshot"],
    queryFn: api.snapshot,
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
    retry: 1,
  });

  // Polled slowly: configuration changes on redeploy, not continuously. It is
  // fetched even when the snapshot fails, because an unconfigured deployment
  // is exactly when this is the only screen with anything useful on it.
  const setup = useQuery({
    queryKey: ["setup"],
    queryFn: api.setup,
    refetchInterval: REFRESH_MS * 8,
    retry: 1,
  });

  // Slow poll: the mode changes when a human changes it, never on its own.
  const strategy = useQuery({
    queryKey: ["strategy"],
    queryFn: api.strategy,
    refetchInterval: REFRESH_MS * 8,
    retry: 1,
  });

  const journal = useQuery({
    queryKey: ["journal"],
    queryFn: () => api.journal(60),
    refetchInterval: REFRESH_MS * 4,
    enabled: tab === "system",
    retry: 1,
  });

  // The mode in force, for the header. Never guessed from config: the
  // switch persists its choice, so only the bot can say what is running.
  const activeStrategy = useMemo(() => {
    const option = strategy.data?.data?.options?.find((item) => item.active);
    if (!option) return null;
    return {
      name: option.name,
      shortLabel: option.key === "smc" ? "SMC" : "REVERSION",
    };
  }, [strategy.data]);

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["snapshot"] });
    void queryClient.invalidateQueries({ queryKey: ["journal"] });
  }, [queryClient]);

  const announce = useCallback((message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(null), 6000);
  }, []);

  const switchStrategy = useMutation({
    mutationFn: (key: string) => api.setStrategy(key),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["strategy"] });
      invalidate();
      const active = result?.data?.options?.find((option) => option.active);
      announce(`Strategy switched to ${active?.name ?? result?.data?.active}.`);
    },
    onError: (error: unknown) =>
      announce(error instanceof Error ? error.message : "Could not switch strategy."),
  });

  const scanning = useMutation({
    mutationFn: (enabled: boolean) => api.setScanning(enabled),
    onSuccess: (result) => {
      announce(result.enabled ? "Scanning resumed." : "Scanning paused. Open positions are still managed.");
      invalidate();
    },
    onError: (error: Error) => announce(`Could not change scanning: ${error.message}`),
  });

  const killSwitch = useMutation({
    mutationFn: ({ active, force }: { active: boolean; force?: boolean }) =>
      api.setKillSwitch(active, force),
    onSuccess: () => {
      announce("Kill switch updated.");
      invalidate();
    },
    onError: (error: Error) => announce(`Kill switch change failed: ${error.message}`),
  });

  const manualScan = useMutation({
    mutationFn: api.triggerScan,
    onSuccess: (result) => {
      announce(`Scan finished: ${result.decision ?? "NO TRADE"}`);
      invalidate();
    },
    onError: (error: Error) => announce(`Scan failed: ${error.message}`),
  });

  const reconcile = useMutation({
    mutationFn: api.reconcile,
    onSuccess: () => {
      announce("Reconciled against the broker.");
      invalidate();
    },
    onError: (error: Error) => announce(`Reconcile failed: ${error.message}`),
  });

  const data = snapshot.data;
  const health = data?.health;
  const risk = data?.risk?.data;
  // Tri-state on purpose. Boolean(undefined) is false, and rendering that
  // as PAUSED told the operator the bot was deliberately stopped when the
  // truth was that nobody could see it — a plausible-looking value
  // presented as information, which is the one thing rule 6 forbids. The
  // same applies to the kill switch: "not tripped" and "unreadable" are
  // not the same statement, and only one of them is safe to act on.
  const scannerKnown = health?.components?.scanner?.enabled !== undefined;
  const scannerEnabled = Boolean(health?.components?.scanner?.enabled);
  const killKnown = risk?.killSwitch?.active !== undefined;
  const killActive = Boolean(risk?.killSwitch?.active);
  const stateKnown = scannerKnown && killKnown;
  const demoVerified = Boolean(data?.account?.data?.demoVerified);
  const busy = manualScan.isPending || reconcile.isPending;

  useEffect(() => {
    document.title = demoVerified ? "SMC Bot · DEMO" : "SMC Bot · UNVERIFIED";
  }, [demoVerified]);

  const connectionState = useMemo(() => {
    if (snapshot.isError) return { tone: "bad" as const, label: "BOT OFFLINE" };
    if (snapshot.isLoading) return { tone: "neutral" as const, label: "CONNECTING" };
    if (!health?.ok) return { tone: "warn" as const, label: "DEGRADED" };
    return { tone: "good" as const, label: "HEALTHY" };
  }, [snapshot.isError, snapshot.isLoading, health?.ok]);

  return (
    <div className="min-h-screen bg-slate-950 pb-24 text-slate-100">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            {demoVerified ? (
              <ShieldCheck className="h-5 w-5 shrink-0 text-emerald-400" aria-hidden />
            ) : (
              <ShieldAlert className="h-5 w-5 shrink-0 text-rose-400" aria-hidden />
            )}
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold">
                {activeStrategy?.name ?? "Trading Bot"}
              </h1>
              <p className="truncate text-[11px] text-slate-500">
                {demoVerified
                ? `TradeLocker DEMO · ${health?.paper ? "paper" : "live orders"}`
                : "Environment not verified"}
              </p>
            </div>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {/* Which strategy is looking for trades, at a glance. Buried in
                a tab it was the one thing an operator could not answer
                without hunting for it. */}
            {activeStrategy && <Badge tone="info">{activeStrategy.shortLabel}</Badge>}
            <Badge tone={connectionState.tone}>{connectionState.label}</Badge>
            <Badge
              tone={
                !stateKnown ? "neutral" : killActive ? "bad" : scannerEnabled ? "good" : "warn"
              }
            >
              {!stateKnown ? "UNKNOWN" : killActive ? "STOPPED" : scannerEnabled ? "SCANNING" : "PAUSED"}
            </Badge>
          </div>
        </div>

        <div className="mx-auto flex max-w-5xl flex-wrap gap-2 px-4 pb-3">
          <ControlButton
            onClick={() => scanning.mutate(!scannerEnabled)}
            // Not offered while the state is unknown: the label would be a
            // guess, and pressing it would send the opposite of what the
            // operator sees.
            disabled={scanning.isPending || killActive || !stateKnown}
            icon={scannerEnabled ? Pause : Play}
            label={
              !stateKnown ? "Scanning —" : scannerEnabled ? "Pause scanning" : "Resume scanning"
            }
          />
          <ControlButton
            onClick={() => manualScan.mutate()}
            disabled={busy}
            icon={RefreshCw}
            label={manualScan.isPending ? "Scanning…" : "Scan now"}
          />
          <ControlButton
            onClick={() => reconcile.mutate()}
            disabled={busy}
            icon={Activity}
            label="Reconcile"
          />
          <ControlButton
            onClick={() => killSwitch.mutate({ active: !killActive, force: killActive })}
            // Stopping must always be available. Only the CLEAR direction
            // needs the state to be known, because clearing something that
            // might still be tripped is the unsafe half.
            disabled={killSwitch.isPending || (killActive && !killKnown)}
            icon={AlertTriangle}
            label={
              killActive && killKnown
                ? "Clear kill switch"
                : "Emergency stop"
            }
            tone={killActive && killKnown ? "warn" : "danger"}
          />
        </div>

        {notice && (
          <div className="border-t border-slate-800 bg-slate-900/80 px-4 py-2 text-center text-xs text-slate-300">
            {notice}
          </div>
        )}
      </header>

      <main className="mx-auto max-w-5xl space-y-4 px-4 py-4">
        {snapshot.isError && (
          <Card>
            <div className="text-center">
              <p className="text-sm font-semibold text-rose-400">
                The trading process is not answering.
              </p>
              <p className="mt-1 text-xs text-slate-400">
                {(snapshot.error as Error)?.message}
              </p>
              <p className="mt-2 text-[11px] text-slate-500">
                No values are shown because none could be read. No trading is happening while this
                is true.
              </p>
            </div>
          </Card>
        )}

        <ModeBanner health={health} />

        {setup.data && !setup.data.ready && <ConfigurationPanel setup={setup.data} />}

        {tab === "overview" && (
          <>
            <AccountPanel account={data?.account} />
            <PositionsPanel positions={data?.positions} />
            <RiskPanel risk={data?.risk} />
          </>
        )}

        {tab === "setup" && <SetupPanel scan={data?.scan} />}

        {tab === "history" && <HistoryPanel history={data?.history} />}

        {tab === "performance" && <PerformancePanel performance={data?.performance} />}

        {tab === "system" && (
          <>
            <HealthPanel health={health} />
            {setup.data?.ready && <ConfigurationPanel setup={setup.data} />}
            <DoctorPanel />
            <UnlockPanel />
            <StrategyPanel
              status={strategy.data?.data}
              onSelect={(key) => switchStrategy.mutate(key)}
              pending={switchStrategy.isPending}
            />
            <BlockersPanel blockers={journal.data?.blockers ?? []} />
            <JournalPanel
              rows={journal.data?.data ?? []}
              histogram={journal.data?.histogram ?? []}
            />
          </>
        )}

        <p className="pt-2 text-center text-[11px] text-slate-600">
          Updated {fmt.time(data?.generatedAt)} · This dashboard displays state. It cannot open,
          size or close a trade.
        </p>
      </main>

      <nav
        className="fixed inset-x-0 bottom-0 z-20 border-t border-slate-800 bg-slate-950/95 backdrop-blur"
        aria-label="Sections"
      >
        <div className="mx-auto flex max-w-5xl">
          {TABS.map((item) => {
            const Icon = item.icon;
            const active = tab === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex flex-1 flex-col items-center gap-0.5 px-1 py-2.5 text-[11px] transition-colors",
                  active ? "text-sky-400" : "text-slate-500 hover:text-slate-300",
                )}
              >
                <Icon className="h-4 w-4" aria-hidden />
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      </nav>
    </div>
  );
}

function ControlButton({
  onClick,
  disabled,
  icon: Icon,
  label,
  tone = "default",
}: {
  onClick: () => void;
  disabled?: boolean;
  icon: typeof Play;
  label: string;
  tone?: "default" | "danger" | "warn";
}) {
  const toneClass = {
    default: "border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800",
    danger: "border-rose-800 bg-rose-950 text-rose-300 hover:bg-rose-900",
    warn: "border-amber-800 bg-amber-950 text-amber-300 hover:bg-amber-900",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border px-3 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        toneClass,
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {label}
    </button>
  );
}
