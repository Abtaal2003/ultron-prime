export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function seconds(ms: number | null): string {
  return ms === null ? "…" : `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

export function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export const CATEGORY_LABELS: Record<string, string> = {
  operator_profile: "Operator profile",
  operator_preferences: "Operator preferences",
  project_context: "Project context",
  decisions_and_outcomes: "Decisions and outcomes",
  ultron_observations: "Ultron observations",
};

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}
