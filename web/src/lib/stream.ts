import { api } from "./api";
import { EVENT_TYPES, type AgentEvent } from "./events";

export type StreamState = "connecting" | "live" | "reconnecting" | "closed" | "signed-out";

export interface StreamHandlers {
  onEvent: (event: AgentEvent) => void;
  onState: (state: StreamState) => void;
}

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];

/**
 * A resumable view of one turn's event stream (spec 6.4).
 *
 * The turn runs on the server whether or not this is connected. On any drop, and when the tab
 * returns to the foreground, it reconnects asking for events after the last sequence number seen.
 * Events are de-duplicated by sequence number, so a replay is harmless.
 */
export class TurnStream {
  private source: EventSource | null = null;
  private lastSeq = 0;
  private attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private finished = false;

  constructor(
    private readonly turnId: string,
    private readonly handlers: StreamHandlers,
    private readonly makeSource: (url: string) => EventSource = (url) => new EventSource(url),
    afterSeq = 0,
  ) {
    this.lastSeq = afterSeq;
  }

  start(): void {
    document.addEventListener("visibilitychange", this.onVisibility);
    window.addEventListener("online", this.onOnline);
    this.open();
  }

  stop(): void {
    this.finished = true;
    this.teardown();
    document.removeEventListener("visibilitychange", this.onVisibility);
    window.removeEventListener("online", this.onOnline);
  }

  get seq(): number {
    return this.lastSeq;
  }

  private onVisibility = (): void => {
    if (document.visibilityState === "visible" && !this.finished) this.reopenNow();
  };

  private onOnline = (): void => {
    if (!this.finished) this.reopenNow();
  };

  private reopenNow(): void {
    this.clearTimer();
    this.closeSource();
    this.open();
  }

  private open(): void {
    this.handlers.onState(this.attempts === 0 ? "connecting" : "reconnecting");
    const url = `/api/v1/turns/${this.turnId}/events?after=${this.lastSeq}`;
    const source = this.makeSource(url);
    this.source = source;
    source.onopen = () => {
      this.attempts = 0;
      this.handlers.onState("live");
    };
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => this.receive(raw as MessageEvent<string>));
    }
    source.onerror = () => {
      // EventSource would retry on its own, but without our `after` cursor. Take over.
      this.closeSource();
      if (this.finished) return;
      void this.scheduleRetry();
    };
  }

  private receive(raw: MessageEvent<string>): void {
    let event: AgentEvent;
    try {
      event = JSON.parse(raw.data) as AgentEvent;
    } catch {
      return; // a malformed frame is dropped, never rendered
    }
    if (event.seq <= this.lastSeq) return;
    this.lastSeq = event.seq;
    this.handlers.onEvent(event);
    if (event.type === "TurnClosed") {
      this.finished = true;
      this.teardown();
      this.handlers.onState("closed");
    }
  }

  private async scheduleRetry(): Promise<void> {
    this.handlers.onState("reconnecting");
    // A dead session also shows up as a stream error: check before looping forever.
    try {
      const session = await api.session();
      if (!session.authenticated) {
        this.finished = true;
        this.teardown();
        this.handlers.onState("signed-out");
        return;
      }
    } catch {
      /* server unreachable: keep retrying below */
    }
    if (this.finished) return;
    const delay = BACKOFF_MS[Math.min(this.attempts, BACKOFF_MS.length - 1)] ?? 15000;
    this.attempts += 1;
    this.timer = setTimeout(() => this.open(), delay);
  }

  private closeSource(): void {
    this.source?.close();
    this.source = null;
  }

  private clearTimer(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private teardown(): void {
    this.clearTimer();
    this.closeSource();
  }
}
