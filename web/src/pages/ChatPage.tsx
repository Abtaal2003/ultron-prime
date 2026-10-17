import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { useLayout } from "@/lib/hooks";
import { useChatTurn } from "@/lib/useChatTurn";
import { useUi } from "@/store/ui";
import { Composer } from "@/components/Composer";
import { Dialog } from "@/components/Dialog";
import { FilePreview, MemoryUsedPanel, PanelTabs, TelevisionsPanel, WorkshopPanel } from "@/components/Panels";
import { ProposalDialog } from "@/components/ProposalDialog";
import { Terminal } from "@/components/Terminal";
import { Header } from "@/components/Header";
import { Sidebar } from "@/components/Sidebar";
import type { StreamState } from "@/lib/stream";
import type { TurnView } from "@/lib/turnState";

const NO_PROPOSALS: never[] = [];

export function ChatPage() {
  const { chatId = null } = useParams();
  const layout = useLayout();
  const drawer = useUi((s) => s.drawerOpen);
  const setDrawer = useUi((s) => s.setDrawer);
  const sheet = useUi((s) => s.sheetOpen);
  const setSheet = useUi((s) => s.setSheet);
  const [statusOpen, setStatusOpen] = useState(false);
  const [preview, setPreview] = useState<{ fileId: string; version: number } | null>(null);
  const [skipped, setSkipped] = useState<Set<string>>(new Set());

  const chat = useQuery({ queryKey: ["chat", chatId], queryFn: () => api.getChat(chatId!), enabled: !!chatId });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const turn = useChatTurn(chatId, chat.data?.active_turn ?? null);
  const proposals = useQuery({ queryKey: ["proposals"], queryFn: api.proposals, enabled: !!chatId });
  const allPending = proposals.data?.pending ?? NO_PROPOSALS;
  const pending = useMemo(() => allPending.filter((p) => !skipped.has(p.id)), [allPending, skipped]);

  useEffect(() => {
    document.title = chat.data ? `${chat.data.title} · Ultron Prime` : "Ultron Prime";
  }, [chat.data]);

  const values = settings.data?.values ?? {};
  const maxFiles = Number(values["upload_max_files_per_message"] ?? 5);
  const maxMb = Number(values["upload_max_mb"] ?? 20);

  const sideContent = (view: TurnView | null) => (
    <PanelTabs>
      {(tab) =>
        tab === "memory" ? (
          <MemoryUsedPanel view={view} />
        ) : tab === "tvs" ? (
          <TelevisionsPanel />
        ) : (
          <WorkshopPanel chatId={chatId} onOpen={(fileId, version) => setPreview({ fileId, version })} />
        )
      }
    </PanelTabs>
  );

  const body = !chatId ? (
    <div className="flex flex-1 items-center justify-center p-6 text-center text-muted">
      <p>Pick a chat, or start a new one.</p>
    </div>
  ) : (
    <>
      {allPending.length > 0 && pending.length === 0 && (
        <button className="btn m-2 border-neon text-sm" onClick={() => setSkipped(new Set())}>
          {allPending.length} memor{allPending.length === 1 ? "y awaits" : "ies await"} your decision
        </button>
      )}
      <Terminal
        chatId={chatId}
        live={turn.view}
        onOpenFile={(fileId, version) => setPreview({ fileId, version })}
        onReviewProposal={() => setSkipped(new Set())}
      />
      {turn.error && (
        <p role="alert" className="mx-3 mb-1 rounded border border-neon px-3 py-1 text-sm text-neon-soft">
          {turn.error}
        </p>
      )}
      <Composer
        chatId={chatId}
        busy={turn.busy}
        onSend={turn.send}
        onStop={() => void turn.stop()}
        maxFiles={maxFiles}
        maxMb={maxMb}
      />
      {layout === "phone" && (
        <div role="tablist" aria-label="Panels" className="flex border-t border-panel-border text-xs" style={{ paddingBottom: "env(safe-area-inset-bottom)" }}>
          {(["memory", "tvs", "workshop"] as const).map((t) => (
            <button key={t} role="tab" aria-selected={false} className="flex-1 text-muted" onClick={() => useUi.getState().setPanelTab(t)}>
              {t === "memory" ? "Memory used" : t === "tvs" ? "TVs" : "Workshop"}
            </button>
          ))}
        </div>
      )}
    </>
  );

  return (
    <div className="flex h-dvh flex-col">
      <Header conn={turn.conn as StreamState | "idle"} onMenu={() => setDrawer(true)} onStatus={() => setStatusOpen(true)} />
      <div className="flex min-h-0 flex-1">
        {layout === "desktop" && (
          <aside className="w-64 shrink-0 border-r border-panel-border">
            <Sidebar />
          </aside>
        )}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">{body}</main>
        {layout === "desktop" && chatId && (
          <aside className="w-80 shrink-0 border-l border-panel-border" aria-label="Side panels">
            {sideContent(turn.view)}
          </aside>
        )}
      </div>

      <Dialog open={drawer && layout !== "desktop"} title="CHATS" onClose={() => setDrawer(false)} placement="left">
        <div className="-m-4 h-[calc(100dvh-3.5rem)]">
          <Sidebar onNavigate={() => setDrawer(false)} />
        </div>
      </Dialog>
      <Dialog open={sheet && layout !== "desktop"} title="PANELS" onClose={() => setSheet(false)} placement="bottom">
        <div className="-m-4 h-[70dvh]">{sideContent(turn.view)}</div>
      </Dialog>
      <Dialog open={statusOpen} title="STATUS" onClose={() => setStatusOpen(false)}>
        <StatusSheet />
      </Dialog>
      <ProposalDialog
        proposal={pending[0] ?? null}
        onDecided={(id, decision, text) => turn.markProposal(id, decision, text)}
        onLater={() => pending[0] && setSkipped((s) => new Set(s).add(pending[0]!.id))}
      />
      <FilePreview target={preview} onClose={() => setPreview(null)} />
    </div>
  );
}

function StatusSheet() {
  const q = useQuery({ queryKey: ["status"], queryFn: api.status });
  const s = q.data;
  if (!s) return <p className="text-muted">Checking…</p>;
  return (
    <dl className="grid grid-cols-2 gap-2 text-sm">
      <dt className="text-muted">Main model</dt>
      <dd>
        {s.provider}/{s.model}
      </dd>
      <dt className="text-muted">Vault</dt>
      <dd>{s.vault_count} memories</dd>
      <dt className="text-muted">Guard</dt>
      <dd>{s.guard.status === "ok" ? "ok" : "heuristic only"}</dd>
      <dt className="text-muted">Access</dt>
      <dd>
        {s.access_mode}
        {s.remote ? " (remote)" : ""}
      </dd>
    </dl>
  );
}
