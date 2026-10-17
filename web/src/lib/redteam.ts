/** Defensive parsing of the nightly red-team summary (shape owned by redteam/run_nightly.py). */

export interface RateView {
  successes: number;
  trials: number;
  rate: number;
  low: number;
  high: number;
}

export interface ClassRow {
  name: string;
  attempts: number;
  successes: number;
  rate: RateView | null;
  floorBlock: RateView | null;
}

export interface RedTeamView {
  mode: string;
  attempts: number;
  successes: number;
  overall: RateView | null;
  rows: ClassRow[];
  promoted: number;
  spendUsd: number | null;
  stoppedForBudget: boolean;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown, fallback = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : fallback);

export function parseRate(v: unknown): RateView | null {
  if (!isRecord(v)) return null;
  return {
    successes: num(v.successes),
    trials: num(v.trials),
    rate: num(v.rate),
    low: num(v.ci_low),
    high: num(v.ci_high),
  };
}

export function parseSummary(summary: unknown): RedTeamView | null {
  if (!isRecord(summary) || !isRecord(summary.by_class)) return null;
  const floor = isRecord(summary.floor) && isRecord(summary.floor.block_rate_by_class) ? summary.floor.block_rate_by_class : {};
  const names = new Set([...Object.keys(summary.by_class), ...Object.keys(floor)]);
  const rows = [...names].sort().map((name): ClassRow => {
    const cls = isRecord(summary.by_class) && isRecord(summary.by_class[name]) ? summary.by_class[name] : {};
    return {
      name,
      attempts: num((cls as Record<string, unknown>).attempts),
      successes: num((cls as Record<string, unknown>).successes),
      rate: parseRate((cls as Record<string, unknown>).success_rate),
      floorBlock: parseRate(floor[name]),
    };
  });
  return {
    mode: typeof summary.mode === "string" ? summary.mode : "unknown",
    attempts: num(summary.attempts),
    successes: num(summary.successes),
    overall: parseRate(summary.success_rate),
    rows,
    promoted: Array.isArray(summary.promoted) ? summary.promoted.length : 0,
    spendUsd: typeof summary.spend_usd_estimate === "number" ? summary.spend_usd_estimate : null,
    stoppedForBudget: summary.stopped_for_budget === true,
  };
}

export function percent(r: RateView | null): string {
  return r ? `${(r.rate * 100).toFixed(0)}%` : "n/a";
}

export function interval(r: RateView | null): string {
  return r ? `${(r.low * 100).toFixed(0)}% to ${(r.high * 100).toFixed(0)}%` : "";
}
