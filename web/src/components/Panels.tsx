import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type WorkshopFile } from "@/lib/api";
import { bytes, errorMessage, when } from "@/lib/format";
import { usePageVisible } from "@/lib/hooks";
import type { TurnView } from "@/lib/turnState";
import { useUi, type PanelTab } from "@/store/ui";
import { Dialog } from "./Dialog";
import { ErrorBoundary } from "./ErrorBoundary";

export function MemoryUsedPanel({ view }: { view: TurnView | null }) {
  const mem = view?.memoryHits ?? [];
  const hist = view?.historyHits ?? [];
  if (!view) return <p className="text-sm text-muted">Nothing has been recalled yet. Send a message.</p>;
  return (
    <div className="space-y-3 text-sm">
      <p>
        Taint state:{" "}
        <strong className={view.tainted ? "text-neon-soft" : "text-ok"}>
          {view.tainted ? "tainted (external content)" : "clean"}
        </strong>
      </p>
      <section aria-label="Memories used">
        <h3 className="label">Memories used ({mem.length})</h3>
        {mem.length === 0 && <p className="text-muted">None recalled.</p>}
        <ul className="space-y-2">
          {mem.map((m, i) => (
            <li key={String(m["id"] ?? i)} className="rounded border border-panel-border p-2">
              <p className="break-words">{String(m["text"] ?? "")}</p>
              <p className="text-xs text-muted">
                {String(m["id"] ?? "")} · {String(m["category"] ?? "")}
              </p>
            </li>
          ))}
        </ul>
      </section>
      <section aria-label="Past chats used">
        <h3 className="label">Past chats used ({hist.length})</h3>
        <ul className="space-y-2">
          {hist.map((h, i) => (
            <li key={String(h["message_id"] ?? i)} className="rounded border border-panel-border p-2">
              <p className="break-words">{String(h["snippet"] ?? h["text"] ?? "")}</p>
              <p className="text-xs text-muted">{String(h["chat_title"] ?? h["chat_id"] ?? "")}</p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

type Telemetry = Record<string, Record<string, number | string> | number>;

function Gauge({ label, value }: { label: string; value: number | undefined }) {
  const pct = Math.max(0, Math.min(100, value ?? 0));
  return (
    <div>
      <div className="flex justify-between text-xs">
        <span>{label}</span>
        <span>{value === undefined ? "n/a" : `${pct.toFixed(0)}%`}</span>
      </div>
      <div
        className="h-2 rounded bg-bg"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
      >
        <div className="h-2 rounded bg-neon" style={{ width: `${pct}%`, boxShadow: "var(--glow)" }} />
      </div>
    </div>
  );
}

/** Host telemetry gauges. They poll only while the tab is visible (Page Visibility API). */
export function TelevisionsPanel() {
  const visible = usePageVisible();
  const q = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    refetchInterval: visible ? 5000 : false,
    refetchIntervalInBackground: false,
  });
  const host = (q.data?.host ?? null) as Telemetry | null;
  const cpu = host?.["cpu"] as Record<string, number> | undefined;
  const mem = host?.["memory"] as Record<string, number> | undefined;
  return (
    <div className="space-y-3 text-sm">
      <h3 className="label">Televisions</h3>
      {q.isError && <p role="alert">{errorMessage(q.error)}</p>}
      <Gauge label="CPU" value={cpu?.["percent_total"]} />
      <Gauge label="Memory" value={mem?.["percent"]} />
      <p className="text-xs text-muted">{visible ? "Live every 5 s." : "Paused while hidden."}</p>
    </div>
  );
}

export function WorkshopPanel({ chatId, onOpen }: { chatId: string | null; onOpen: (fileId: string, v: number) => void }) {
  const q = useQuery({ queryKey: ["files", chatId], queryFn: () => api.files(chatId ?? undefined) });
  if (q.isPending) return <p className="text-muted">Opening the workshop…</p>;
  if (q.isError) return <p role="alert">{errorMessage(q.error)}</p>;
  if (q.data.length === 0) return <p className="text-sm text-muted">No files yet. Ask me to make something.</p>;
  return (
    <ul className="space-y-2 text-sm" aria-label="Workshop files">
      {q.data.map((f: WorkshopFile) => (
        <li key={f.file_id} className="rounded border border-panel-border p-2">
          <p className="break-all">{f.logical_name}</p>
          <p className="text-xs text-muted">
            v{f.latest_version} of {f.version_count} · {bytes(f.size)} · {when(f.updated_at)}
            {f.external_content && " · external content"}
          </p>
          <div className="mt-1 flex gap-2">
            <button className="btn" onClick={() => onOpen(f.file_id, f.latest_version)}>
              Preview
            </button>
            <a className="btn no-underline" href={api.downloadUrl(f.file_id, f.latest_version)} download>
              Download
            </a>
          </div>
        </li>
      ))}
    </ul>
  );
}

const TABS: { id: PanelTab; label: string }[] = [
  { id: "memory", label: "Memory used" },
  { id: "tvs", label: "Televisions" },
  { id: "workshop", label: "Workshop" },
];

export function PanelTabs({ children }: { children: (tab: PanelTab) => React.ReactNode }) {
  const tab = useUi((s) => s.panelTab);
  const set = useUi((s) => s.setPanelTab);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div role="tablist" aria-label="Side panels" className="flex border-b border-panel-border">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`tabpanel-${t.id}`}
            className={`flex-1 border-b-2 px-2 text-xs ${tab === t.id ? "border-neon text-neon-soft" : "border-transparent text-muted"}`}
            onClick={() => set(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`tabpanel-${tab}`} aria-labelledby={`tab-${tab}`} className="min-h-0 flex-1 overflow-y-auto p-3">
        <ErrorBoundary label="This panel failed.">{children(tab)}</ErrorBoundary>
      </div>
    </div>
  );
}

/** Safe preview: raster images via <img>, text as text, everything else as metadata. */
export function FilePreview({ target, onClose }: { target: { fileId: string; version: number } | null; onClose: () => void }) {
  const q = useQuery({
    enabled: target !== null,
    queryKey: ["preview", target?.fileId, target?.version],
    queryFn: async () => {
      const res = await fetch(api.previewUrl(target!.fileId, target!.version), { credentials: "same-origin" });
      if (!res.ok) throw new Error("Preview unavailable.");
      const type = res.headers.get("content-type") ?? "";
      if (type.startsWith("image/")) return { kind: "image" as const, url: URL.createObjectURL(await res.blob()) };
      return (await res.json()) as { kind: "text" | "binary"; text?: string; note?: string; name: string; truncated?: boolean };
    },
  });
  const [wide, setWide] = useState(false);
  return (
    <Dialog open={target !== null} title="PREVIEW" onClose={onClose}>
      {q.isPending && <p className="pulse text-muted">Loading…</p>}
      {q.isError && <p role="alert">{errorMessage(q.error)}</p>}
      {q.data?.kind === "image" && <img src={q.data.url} alt="Generated file preview" className="max-w-full" />}
      {q.data?.kind === "text" && (
        <>
          <pre className={`scroll-x text-xs ${wide ? "whitespace-pre" : "whitespace-pre-wrap"}`}>{q.data.text}</pre>
          <button className="btn mt-2" onClick={() => setWide((w) => !w)}>
            {wide ? "Wrap lines" : "No wrap"}
          </button>
          {q.data.truncated && <p className="mt-2 text-xs text-muted">Showing the first part. Download for the rest.</p>}
        </>
      )}
      {q.data?.kind === "binary" && <p className="text-muted">{q.data.note}</p>}
      {target && (
        <a className="btn mt-3 no-underline" href={api.downloadUrl(target.fileId, target.version)} download>
          Download
        </a>
      )}
    </Dialog>
  );
}
