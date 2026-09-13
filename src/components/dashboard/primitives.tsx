/**
 * Small presentational building blocks.
 *
 * The one rule they all enforce: a missing value renders as an explicit
 * placeholder with a status, never as a plausible-looking zero.
 */

import { type ReactNode } from "react";

export function cn(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

export function Card({
  title,
  subtitle,
  action,
  children,
  className,
}: {
  title?: string;
  subtitle?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cn(
        "rounded-2xl border border-slate-800 bg-slate-900/60 p-4 shadow-sm backdrop-blur",
        className,
      )}
    >
      {(title || action) && (
        <header className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-slate-200">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn";
  hint?: string;
}) {
  const toneClass = {
    neutral: "text-slate-100",
    good: "text-emerald-400",
    bad: "text-rose-400",
    warn: "text-amber-400",
  }[tone];
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={cn("truncate text-lg font-semibold tabular-nums", toneClass)}>{value}</div>
      {hint && <div className="truncate text-[11px] text-slate-500">{hint}</div>}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn" | "info";
}) {
  const toneClass = {
    neutral: "bg-slate-800 text-slate-300 border-slate-700",
    good: "bg-emerald-950 text-emerald-300 border-emerald-800",
    bad: "bg-rose-950 text-rose-300 border-rose-800",
    warn: "bg-amber-950 text-amber-300 border-amber-800",
    info: "bg-sky-950 text-sky-300 border-sky-800",
  }[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        toneClass,
      )}
    >
      {children}
    </span>
  );
}

/** Renders an explicit unavailable state instead of a fake value. */
export function Unavailable({ status, error, hint }: { status: string; error?: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-700 bg-slate-950/40 p-4 text-center">
      <div className="text-xs font-semibold uppercase tracking-wide text-amber-400">{status}</div>
      {error && <p className="mt-1 break-words text-xs text-slate-400">{error}</p>}
      <p className="mt-2 text-[11px] text-slate-500">
        {hint ?? "No value is shown because none could be read. Nothing is being invented here."}
      </p>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-800 p-6 text-center text-xs text-slate-500">
      {children}
    </div>
  );
}

export function Row({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-slate-800/60 py-1.5 last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      <span className={cn("text-xs font-medium tabular-nums text-slate-200", tone)}>{value}</span>
    </div>
  );
}

export function Meter({ value, max, tone = "info" }: { value: number; max: number; tone?: string }) {
  const fraction = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const bar = {
    info: "bg-sky-500",
    good: "bg-emerald-500",
    warn: "bg-amber-500",
    bad: "bg-rose-500",
  }[tone] ?? "bg-sky-500";
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800" role="presentation">
      <div className={cn("h-full rounded-full transition-all", bar)} style={{ width: `${fraction * 100}%` }} />
    </div>
  );
}

export function tierTone(tier: string | null | undefined) {
  if (tier === "A+") return "good" as const;
  if (tier === "A") return "info" as const;
  if (tier === "B") return "warn" as const;
  return "neutral" as const;
}

export function pnlTone(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "neutral" as const;
  if (value > 0) return "good" as const;
  if (value < 0) return "bad" as const;
  return "neutral" as const;
}
