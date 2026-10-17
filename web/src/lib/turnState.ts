import type { AgentEvent, EventOf } from "./events";

export type ToolStatus = "running" | "ok" | "error" | "denied";

export type Item =
  | { kind: "text"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      callId: string;
      name: string;
      args: Record<string, unknown>;
      themedLine: string | null;
      reasonLine: string | null;
      status: ToolStatus;
      durationMs: number | null;
      preview: string;
      quarantined: boolean;
      taints: boolean;
      error: string | null;
    }
  | {
      kind: "egress";
      id: string;
      callId: string;
      tool: string;
      outbound: string;
      reason: string;
      decision: "pending" | "approve" | "reject";
    }
  | {
      kind: "file";
      id: string;
      fileId: string;
      name: string;
      version: number;
      mime: string;
      size: number;
      note: string | null;
      external: boolean;
    }
  | {
      kind: "proposal";
      id: string;
      proposalId: string;
      text: string;
      category: string;
      rationale: string;
      themedLine: string | null;
      decision: "pending" | "approve" | "edit_approve" | "decline";
      written: boolean;
    }
  | { kind: "quarantine"; id: string; handle: string; source: string; schema: string; preview: string }
  | {
      kind: "attachment";
      id: string;
      attachmentId: string | null;
      name: string;
      status: "accepted" | "rejected";
      reason: string | null;
      quarantined: boolean;
      secretWarning: boolean;
      themedLine: string | null;
    }
  | { kind: "notice"; id: string; level: "info" | "warning" | "error"; message: string; tag: string | null };

export interface TurnView {
  turnId: string;
  chatId: string | null;
  userText: string;
  items: Item[];
  phase: "running" | "done";
  status: "ok" | "error" | "stopped" | null;
  finalText: string | null;
  replaced: boolean;
  tainted: boolean;
  memoryHits: Record<string, unknown>[];
  historyHits: Record<string, unknown>[];
  guard: { action: string; guardClass: string; attackType: string | null; themedLine: string | null } | null;
  output: { status: string; issues: string[]; regenerated: boolean } | null;
  provider: string;
  model: string;
  latencyMs: number;
  lastSeq: number;
}

export function newTurnView(turnId: string, userText = "", attachments: Item[] = []): TurnView {
  return {
    turnId,
    chatId: null,
    userText,
    items: attachments,
    phase: "running",
    status: null,
    finalText: null,
    replaced: false,
    tainted: false,
    memoryHits: [],
    historyHits: [],
    guard: null,
    output: null,
    provider: "",
    model: "",
    latencyMs: 0,
    lastSeq: 0,
  };
}

const norm = (s: string): string => s.replace(/\s+/g, " ").trim();

function updateItem(items: Item[], pred: (i: Item) => boolean, fn: (i: Item) => Item): Item[] {
  return items.map((i) => (pred(i) ? fn(i) : i));
}

/** Concatenation of every streamed text item. */
export function streamedText(view: TurnView): string {
  return view.items.flatMap((i) => (i.kind === "text" ? [i.text] : [])).join("");
}

/** The text the operator should read: the final text once known, otherwise what has streamed. */
export function visibleText(view: TurnView): string {
  return view.finalText ?? streamedText(view);
}

/** Apply one server event. Pure: returns a new view and ignores replayed sequence numbers. */
export function applyEvent(view: TurnView, ev: AgentEvent): TurnView {
  if (ev.seq <= view.lastSeq) return view;
  const base: TurnView = { ...view, lastSeq: ev.seq, chatId: ev.chat_id ?? view.chatId };
  switch (ev.type) {
    case "UserMessage":
      return { ...base, userText: ev.text };
    case "AgentToken": {
      const last = base.items[base.items.length - 1];
      if (last?.kind === "text") {
        const items = base.items.slice(0, -1);
        items.push({ ...last, text: last.text + ev.delta });
        return { ...base, items };
      }
      return { ...base, items: [...base.items, { kind: "text", id: `t${ev.seq}`, text: ev.delta }] };
    }
    case "GuardVerdict":
      return {
        ...base,
        guard: {
          action: ev.action,
          guardClass: ev.guard_class,
          attackType: ev.attack_type,
          themedLine: ev.themed_line,
        },
      };
    case "MemoryRetrieved":
      return {
        ...base,
        memoryHits: ev.memory_hits as Record<string, unknown>[],
        historyHits: ev.history_hits as Record<string, unknown>[],
        tainted: base.tainted || ev.tainted,
      };
    case "AttachmentIngested":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "attachment",
            id: `a${ev.seq}`,
            attachmentId: ev.attachment_id,
            name: ev.filename,
            status: ev.status,
            reason: ev.reason,
            quarantined: ev.quarantined,
            secretWarning: ev.secret_warning,
            themedLine: ev.themed_line,
          },
        ],
      };
    case "ToolStart":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "tool",
            id: `c${ev.call_id}`,
            callId: ev.call_id,
            name: ev.tool_name,
            args: ev.validated_args,
            themedLine: ev.themed_line,
            reasonLine: ev.reason_line,
            status: "running",
            durationMs: null,
            preview: "",
            quarantined: false,
            taints: false,
            error: null,
          },
        ],
      };
    case "ToolEnd": {
      const known = base.items.some((i) => i.kind === "tool" && i.callId === ev.call_id);
      const patch = (i: Item): Item =>
        i.kind === "tool"
          ? {
              ...i,
              status: ev.status,
              durationMs: ev.duration_ms,
              preview: ev.result_preview,
              quarantined: ev.quarantined,
              taints: ev.taints_turn,
              themedLine: ev.themed_line ?? i.themedLine,
            }
          : i;
      const items = known
        ? updateItem(base.items, (i) => i.kind === "tool" && i.callId === ev.call_id, patch)
        : [
            ...base.items,
            patch({
              kind: "tool",
              id: `c${ev.call_id}`,
              callId: ev.call_id,
              name: ev.tool_name,
              args: {},
              themedLine: null,
              reasonLine: null,
              status: "running",
              durationMs: null,
              preview: "",
              quarantined: false,
              taints: false,
              error: null,
            }),
          ];
      return { ...base, items, tainted: base.tainted || ev.taints_turn };
    }
    case "ToolError":
      return {
        ...base,
        items: updateItem(
          base.items,
          (i) => i.kind === "tool" && i.callId === ev.call_id,
          (i) => (i.kind === "tool" ? { ...i, status: "error", error: ev.message } : i),
        ),
      };
    case "QuarantineRead":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "quarantine",
            id: `q${ev.seq}`,
            handle: ev.handle,
            source: ev.source,
            schema: ev.schema_name,
            preview: ev.extraction_preview,
          },
        ],
      };
    case "EgressHold":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "egress",
            id: `e${ev.call_id}`,
            callId: ev.call_id,
            tool: ev.tool_name,
            outbound: ev.outbound_string,
            reason: ev.reason,
            decision: "pending",
          },
        ],
      };
    case "EgressDecision":
      return {
        ...base,
        items: updateItem(
          base.items,
          (i) => i.kind === "egress" && i.callId === ev.call_id,
          (i) => (i.kind === "egress" ? { ...i, decision: ev.decision } : i),
        ),
      };
    case "FileWritten":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "file",
            id: `f${ev.file_id}v${ev.version}`,
            fileId: ev.file_id,
            name: ev.logical_name,
            version: ev.version,
            mime: ev.mime,
            size: ev.size,
            note: ev.change_note,
            external: ev.external_content,
          },
        ],
      };
    case "MemoryProposal":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "proposal",
            id: `m${ev.proposal_id}`,
            proposalId: ev.proposal_id,
            text: ev.text,
            category: ev.category,
            rationale: ev.rationale,
            themedLine: ev.themed_line,
            decision: "pending",
            written: false,
          },
        ],
      };
    case "MemoryDecision":
      return {
        ...base,
        items: updateItem(
          base.items,
          (i) => i.kind === "proposal" && i.proposalId === ev.proposal_id,
          (i) => (i.kind === "proposal" ? { ...i, decision: ev.decision, text: ev.final_text ?? i.text } : i),
        ),
      };
    case "MemoryWritten":
      return base;
    case "OutputVerdict":
      return { ...base, output: { status: ev.status, issues: ev.issues, regenerated: ev.regenerated } };
    case "SystemNotice":
    case "PolicyHold":
    case "ProviderFailover":
    case "RepeatDetected": {
      const notice = noticeFor(ev);
      return notice ? { ...base, items: [...base.items, { ...notice, id: `n${ev.seq}` }] } : base;
    }
    case "AgentDone": {
      // The output guard may replace the streamed text. Keep the interleaved layout only when
      // the final text matches what streamed; otherwise show the final text alone.
      const streamed = streamedText(base);
      const same = norm(streamed) === norm(ev.final_text);
      const items = same
        ? base.items
        : [
            ...base.items.filter((i) => i.kind !== "text"),
            { kind: "text" as const, id: `final${ev.seq}`, text: ev.final_text },
          ];
      return {
        ...base,
        items,
        phase: "done",
        status: ev.status,
        finalText: ev.final_text,
        replaced: !same && streamed.trim().length > 0,
        tainted: base.tainted || ev.tainted,
        provider: ev.provider,
        model: ev.model,
        latencyMs: ev.latency_ms,
      };
    }
    case "TurnStarted":
    case "TurnClosed":
      return ev.type === "TurnClosed" ? { ...base, phase: "done" } : base;
    default:
      return base;
  }
}

type Notice = Extract<Item, { kind: "notice" }>;

function noticeFor(
  ev: EventOf<"SystemNotice"> | EventOf<"PolicyHold"> | EventOf<"ProviderFailover"> | EventOf<"RepeatDetected">,
): Omit<Notice, "id"> | null {
  switch (ev.type) {
    case "SystemNotice":
      return { kind: "notice", level: ev.level, message: ev.message, tag: ev.kind };
    case "ProviderFailover":
      return {
        kind: "notice",
        level: "warning",
        message: ev.themed_line ?? `Switched from ${ev.from_route} to ${ev.to_route}.`,
        tag: "failover",
      };
    case "PolicyHold":
      return { kind: "notice", level: "warning", message: "A tool call was held by policy.", tag: "policy" };
    case "RepeatDetected":
      return null; // surfaced in the Safety page; the reply itself carries the dry remark
  }
}

export function replay(turnId: string, events: AgentEvent[], userText = ""): TurnView {
  return events.reduce(applyEvent, newTurnView(turnId, userText));
}

/** Record a local proposal decision (the REST decision does not echo an event). */
export function withProposalDecision(
  view: TurnView,
  proposalId: string,
  decision: "approve" | "edit_approve" | "decline",
  text?: string,
): TurnView {
  return {
    ...view,
    items: view.items.map((i) =>
      i.kind === "proposal" && i.proposalId === proposalId
        ? { ...i, decision, text: text ?? i.text, written: decision !== "decline" }
        : i,
    ),
  };
}
