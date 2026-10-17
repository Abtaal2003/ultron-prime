import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { StreamState } from "@/lib/stream";
import { useLayout } from "@/lib/hooks";

export function Header({ conn, onMenu, onStatus }: { conn: StreamState | "idle"; onMenu: () => void; onStatus: () => void }) {
  const layout = useLayout();
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 60_000 });
  const s = status.data;
  const reconnecting = conn === "reconnecting";
  return (
    <header
      className="flex items-center gap-3 border-b border-panel-border bg-bg px-3"
      style={{ paddingTop: "max(0.25rem, env(safe-area-inset-top))" }}
    >
      {layout !== "desktop" && (
        <button className="btn border-transparent" onClick={onMenu} aria-label="Open chats">
          ☰
        </button>
      )}
      <h1 className="font-display text-sm tracking-widest text-neon-soft glow">
        ULTRON PRIME <span className="text-muted">::</span>{" "}
        <span className={status.isError ? "text-muted" : "pulse"}>{status.isError ? "OFFLINE" : "CORE ONLINE"}</span>
      </h1>
      {reconnecting && (
        <span role="status" className="text-xs text-neon-soft">
          reconnecting…
        </span>
      )}
      {layout !== "phone" && s && (
        <p className="ml-auto flex flex-wrap gap-x-4 text-xs text-muted">
          <span>
            main: {s.provider}/{s.model}
          </span>
          <span>vault {s.vault_count}</span>
          <span>guard {s.guard.status === "ok" ? "ok" : "heuristic"}</span>
          {s.remote && <span className="text-neon-soft">remote</span>}
          {s.strict && <span className="text-neon-soft">strict</span>}
        </p>
      )}
      {layout === "phone" && (
        <button className="btn ml-auto border-transparent" onClick={onStatus} aria-label="Status">
          ⋯
        </button>
      )}
    </header>
  );
}
