import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { bytes, errorMessage, seconds } from "@/lib/format";
import type { Item } from "@/lib/turnState";
import { Dialog } from "./Dialog";

type Of<K extends Item["kind"]> = Extract<Item, { kind: K }>;

/** A small glass icon marking content that reached the model only through the quarantined reader. */
export function QuarantineBadge({ onShow }: { onShow?: () => void }) {
  if (!onShow) {
    return (
      <span
        role="img"
        aria-label="Read through the quarantined reader"
        className="btn min-h-0 gap-1 px-2 py-1 text-xs"
        title="Read through the quarantined reader."
      >
        <span aria-hidden="true">🔍</span>
        <span>quarantined</span>
      </span>
    );
  }
  return (
    <button
      type="button"
      className="btn min-h-0 gap-1 px-2 py-1 text-xs"
      onClick={onShow}
      aria-label="Content was read through the quarantined reader. Show original."
      title="Read through the quarantined reader. Show original."
    >
      <span aria-hidden="true">🔍</span>
      <span>quarantined</span>
    </button>
  );
}

const STATUS_STYLE: Record<Of<"tool">["status"], string> = {
  running: "border-neon-soft text-neon-soft pulse",
  ok: "border-ok text-ok",
  error: "border-neon text-neon-soft",
  denied: "border-deep text-muted",
};

export function ToolCard({ item }: { item: Of<"tool"> }) {
  const [open, setOpen] = useState(false);
  const panelId = `tool-${item.callId}`;
  return (
    <section className="panel my-2 text-sm" aria-label={`Tool ${item.name}`}>
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <span className="font-display text-xs tracking-widest text-neon-soft">TOOL</span>
        <span className="break-all">{item.name}</span>
        <span className={`rounded border px-2 text-xs ${STATUS_STYLE[item.status]}`}>{item.status}</span>
        <span className="text-xs text-muted">{seconds(item.durationMs)}</span>
        {item.taints && <span className="text-xs text-neon-soft">taints turn</span>}
        {item.quarantined && <QuarantineBadge />}
        <button
          type="button"
          className="btn ml-auto min-h-0 px-2 py-1 text-xs"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "hide" : "details"}
        </button>
      </div>
      {item.themedLine && <p className="px-3 pb-2 text-muted">{item.themedLine}</p>}
      {open && (
        <div id={panelId} className="border-t border-panel-border px-3 py-2">
          {item.reasonLine && <p className="mb-2 text-muted">{item.reasonLine}</p>}
          <p className="label">arguments</p>
          <pre className="scroll-x mb-2 text-xs">{JSON.stringify(item.args, null, 2)}</pre>
          <p className="label">result</p>
          <pre className="scroll-x whitespace-pre-wrap text-xs">{item.error ?? (item.preview || "(none yet)")}</pre>
        </div>
      )}
    </section>
  );
}

export function EgressCard({ item }: { item: Of<"egress"> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const decide = async (decision: "approve" | "reject"): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await api.egressDecision(item.callId, decision);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 404 ? "That hold has already been settled." : errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const pending = item.decision === "pending";
  return (
    <section className="panel my-2 border-neon p-3 text-sm" role="group" aria-label="Egress hold">
      <p className="font-display text-xs tracking-widest text-neon-soft">EGRESS HOLD</p>
      <p className="mt-1 text-muted">
        {item.tool} wants to send this text out of the house. {item.reason}
      </p>
      <p className="label mt-2">exact outbound string</p>
      <pre className="scroll-x whitespace-pre-wrap rounded border border-panel-border bg-bg p-2 text-xs">
        {item.outbound}
      </pre>
      {pending ? (
        <div className="mt-3 flex flex-wrap gap-2">
          <button className="btn btn-primary" disabled={busy} onClick={() => void decide("approve")}>
            Send it
          </button>
          <button className="btn btn-danger" disabled={busy} onClick={() => void decide("reject")}>
            Refuse
          </button>
        </div>
      ) : (
        <p className="mt-2 text-muted">{item.decision === "approve" ? "Approved by you." : "Refused by you."}</p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-neon-soft">
          {error}
        </p>
      )}
    </section>
  );
}

export function FileCard({ item, onOpen }: { item: Of<"file">; onOpen: (fileId: string, version: number) => void }) {
  return (
    <section className="panel my-2 flex flex-wrap items-center gap-2 p-3 text-sm" aria-label={`File ${item.name}`}>
      <span className="font-display text-xs tracking-widest text-neon-soft">FILE</span>
      <span className="break-all">{item.name}</span>
      <span className="text-xs text-muted">
        v{item.version} · {bytes(item.size)}
      </span>
      {item.external && <span className="text-xs text-neon-soft">from external content</span>}
      <span className="ml-auto flex gap-2">
        <button className="btn" onClick={() => onOpen(item.fileId, item.version)}>
          Preview
        </button>
        <a className="btn no-underline" href={api.downloadUrl(item.fileId, item.version)} download aria-label={`Download ${item.name}`}>
          ↓
        </a>
      </span>
      {item.note && <p className="basis-full text-xs text-muted">{item.note}</p>}
    </section>
  );
}

export function AttachmentChip({ item }: { item: Of<"attachment"> }) {
  const rejected = item.status === "rejected";
  return (
    <div
      className={`my-1 inline-flex flex-wrap items-center gap-2 rounded border px-2 py-1 text-xs ${rejected ? "border-neon text-neon-soft" : "border-deep"}`}
    >
      <span aria-hidden="true">📎</span>
      <span className="break-all">{item.name}</span>
      <span className="text-muted">{rejected ? `rejected: ${item.reason ?? "not accepted"}` : "accepted"}</span>
      {item.quarantined && <QuarantineBadge />}
      {item.secretWarning && <span className="text-neon-soft">credentials detected</span>}
    </div>
  );
}

export function NoticeBar({ item }: { item: Of<"notice"> }) {
  const tone = item.level === "error" ? "border-neon text-neon-soft" : item.level === "warning" ? "border-deep" : "border-panel-border text-muted";
  return (
    <p role={item.level === "error" ? "alert" : "status"} className={`my-1 rounded border px-3 py-1 text-xs ${tone}`}>
      {item.message}
    </p>
  );
}

export function QuarantineLine({ item, turnId }: { item: Of<"quarantine">; turnId: string | null }) {
  const [original, setOriginal] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const show = async (): Promise<void> => {
    setFailed(null);
    try {
      if (!turnId) throw new Error("No stored original for this result.");
      setOriginal((await api.quarantineOriginal(turnId, item.handle)).text);
    } catch (err) {
      setFailed(err instanceof ApiError && err.status === 404 ? "That original is no longer held in memory." : errorMessage(err));
      setOriginal("");
    }
  };
  return (
    <div className="my-1 flex flex-wrap items-center gap-2 text-xs text-muted">
      <span aria-hidden="true">🔍</span>
      <span>
        {item.source} read through the reader as {item.handle} ({item.schema})
      </span>
      <QuarantineBadge onShow={() => void show()} />
      <Dialog open={original !== null} title="ORIGINAL (OPERATOR ONLY)" onClose={() => setOriginal(null)}>
        {failed ? (
          <p role="alert">{failed}</p>
        ) : (
          <>
            <p className="mb-2 text-xs text-muted">
              The model never saw this text. It saw only a structured extraction of it.
            </p>
            <pre className="scroll-x whitespace-pre-wrap text-xs">{original}</pre>
          </>
        )}
      </Dialog>
    </div>
  );
}

export function ProposalCard({ item, onReview }: { item: Of<"proposal">; onReview: () => void }) {
  const pending = item.decision === "pending";
  return (
    <section className="panel my-2 p-3 text-sm" aria-label="Memory proposal">
      <p className="font-display text-xs tracking-widest text-neon-soft">MEMORY PROPOSAL</p>
      <p className="mt-1">{item.text}</p>
      {pending ? (
        <button className="btn btn-primary mt-2" onClick={onReview}>
          Review
        </button>
      ) : (
        <p className="mt-1 text-muted">
          {item.decision === "decline" ? "Declined." : "Written to the Vault."}
        </p>
      )}
    </section>
  );
}
