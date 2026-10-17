import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TurnStream, type StreamState } from "./stream";
import { EVENT_TYPES } from "./events";

class FakeSource {
  static instances: FakeSource[] = [];
  listeners = new Map<string, ((e: MessageEvent<string>) => void)[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSource.instances.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent<string>) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, seq: number, extra: Record<string, unknown> = {}): void {
    const data = JSON.stringify({ type, seq, ...extra });
    for (const fn of this.listeners.get(type) ?? []) fn({ data } as MessageEvent<string>);
  }
}

function setup() {
  const events: { type: string; seq: number }[] = [];
  const states: StreamState[] = [];
  const stream = new TurnStream(
    "turn_1",
    { onEvent: (e) => events.push(e), onState: (s) => states.push(s) },
    (url) => new FakeSource(url) as unknown as EventSource,
  );
  return { stream, events, states };
}

describe("TurnStream", () => {
  beforeEach(() => {
    FakeSource.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ authenticated: true }), { status: 200 })));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("listens for every event type", () => {
    const { stream } = setup();
    stream.start();
    expect([...FakeSource.instances[0]!.listeners.keys()].sort()).toEqual([...EVENT_TYPES].sort());
    stream.stop();
  });

  it("de-duplicates by sequence and finishes on TurnClosed", () => {
    const { stream, events, states } = setup();
    stream.start();
    const src = FakeSource.instances[0]!;
    src.onopen?.();
    src.emit("AgentToken", 1, { delta: "a" });
    src.emit("AgentToken", 1, { delta: "a" });
    src.emit("AgentToken", 2, { delta: "b" });
    src.emit("TurnClosed", 3);
    expect(events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(states).toContain("live");
    expect(states.at(-1)).toBe("closed");
    expect(src.closed).toBe(true);
  });

  it("reconnects after an error resuming after the last sequence seen", async () => {
    const { stream, states } = setup();
    stream.start();
    const first = FakeSource.instances[0]!;
    first.emit("AgentToken", 7, { delta: "x" });
    first.onerror?.();
    await vi.advanceTimersByTimeAsync(600);
    expect(states).toContain("reconnecting");
    expect(FakeSource.instances).toHaveLength(2);
    expect(FakeSource.instances[1]!.url).toContain("after=7");
    stream.stop();
  });

  it("stops retrying and reports signed-out when the session is gone", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ authenticated: false }), { status: 200 })));
    const { stream, states } = setup();
    stream.start();
    FakeSource.instances[0]!.onerror?.();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(states.at(-1)).toBe("signed-out");
    expect(FakeSource.instances).toHaveLength(1);
  });

  it("catches up immediately when the tab returns to the foreground", () => {
    const { stream } = setup();
    stream.start();
    FakeSource.instances[0]!.emit("AgentToken", 4, { delta: "x" });
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(FakeSource.instances[0]!.closed).toBe(true);
    expect(FakeSource.instances).toHaveLength(2);
    expect(FakeSource.instances[1]!.url).toContain("after=4");
    stream.stop();
  });

  it("drops malformed frames instead of rendering them", () => {
    const { stream, events } = setup();
    stream.start();
    for (const fn of FakeSource.instances[0]!.listeners.get("AgentToken") ?? []) fn({ data: "{not json" } as MessageEvent<string>);
    expect(events).toHaveLength(0);
    stream.stop();
  });
});
