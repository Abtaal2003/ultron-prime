import { describe, expect, it } from "vitest";
import { interval, parseSummary, percent } from "./redteam";

const rate = (s: number, t: number) => ({ successes: s, trials: t, rate: t ? s / t : 0, ci_low: 0, ci_high: 0.5 });

describe("parseSummary", () => {
  it("merges the structural and floor tables by class", () => {
    const view = parseSummary({
      mode: "offline",
      attempts: 9,
      successes: 0,
      success_rate: rate(0, 9),
      by_class: { exfiltration: { attempts: 6, successes: 0, success_rate: rate(0, 6) } },
      floor: { block_rate_by_class: { exfiltration: rate(4, 6), direct_override: rate(8, 8) } },
      promoted: [],
      spend_usd_estimate: 0,
      stopped_for_budget: false,
    });
    expect(view?.rows.map((r) => r.name)).toEqual(["direct_override", "exfiltration"]);
    const exf = view?.rows.find((r) => r.name === "exfiltration");
    expect(exf?.attempts).toBe(6);
    expect(percent(exf?.floorBlock ?? null)).toBe("67%");
    expect(view?.rows[0]?.attempts).toBe(0);
  });

  it("returns null for shapes it does not recognise", () => {
    expect(parseSummary(null)).toBeNull();
    expect(parseSummary({ by_class: [] })).toBeNull();
    expect(parseSummary("x")).toBeNull();
  });

  it("tolerates missing numbers", () => {
    const view = parseSummary({ by_class: { x: { success_rate: { successes: "a" } } } });
    expect(view?.rows[0]?.rate?.successes).toBe(0);
    expect(view?.spendUsd).toBeNull();
  });

  it("formats absent rates", () => {
    expect(percent(null)).toBe("n/a");
    expect(interval(null)).toBe("");
    expect(interval({ successes: 0, trials: 6, rate: 0, low: 0, high: 0.39 })).toBe("0% to 39%");
  });
});
