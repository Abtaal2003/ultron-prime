import { describe, expect, it } from "vitest";
import type { AgentEvent } from "./events";
import { applyEvent, newTurnView, replay, streamedText, visibleText, withProposalDecision } from "./turnState";

let seq = 0;
function ev<T extends AgentEvent["type"]>(type: T, body: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, seq, ts: "t", turn_id: "turn_1", chat_id: "chat_1", ...body } as unknown as AgentEvent;
}
const reset = (): void => {
  seq = 0;
};

describe("turn reducer", () => {
  it("appends tokens into one text item and ignores replayed sequence numbers", () => {
    reset();
    const a = ev("AgentToken", { delta: "Hello " });
    const b = ev("AgentToken", { delta: "world." });
    let v = [a, b].reduce(applyEvent, newTurnView("turn_1"));
    expect(streamedText(v)).toBe("Hello world.");
    expect(v.items).toHaveLength(1);
    v = applyEvent(v, a); // duplicate after a reconnect
    expect(streamedText(v)).toBe("Hello world.");
  });

  it("interleaves tool cards with text and closes a tool by call id", () => {
    reset();
    const v = replay("turn_1", [
      ev("AgentToken", { delta: "Checking. " }),
      ev("ToolStart", { call_id: "c1", tool_name: "web", validated_args: { q: "x" }, themed_line: "Looking.", reason_line: null }),
      ev("ToolEnd", { call_id: "c1", tool_name: "web", status: "ok", duration_ms: 1200, result_preview: "r", themed_line: null, quarantined: true, taints_turn: true, receipt_id: null }),
      ev("AgentToken", { delta: "Done." }),
    ]);
    expect(v.items.map((i) => i.kind)).toEqual(["text", "tool", "text"]);
    const tool = v.items[1];
    expect(tool).toMatchObject({ status: "ok", durationMs: 1200, quarantined: true, themedLine: "Looking." });
    expect(v.tainted).toBe(true);
  });

  it("tracks an egress hold through its decision", () => {
    reset();
    let v = replay("turn_1", [ev("EgressHold", { call_id: "c2", tool_name: "web", outbound_string: "q", reason: "tainted" })]);
    expect(v.items[0]).toMatchObject({ kind: "egress", decision: "pending", outbound: "q" });
    v = applyEvent(v, ev("EgressDecision", { call_id: "c2", decision: "reject" }));
    expect(v.items[0]).toMatchObject({ decision: "reject" });
  });

  it("keeps the interleaved layout when the final text matches what streamed", () => {
    reset();
    const v = replay("turn_1", [
      ev("AgentToken", { delta: "All  good." }),
      ev("AgentDone", { status: "ok", final_text: "All good.", usage: {}, latency_ms: 5, provider: "p", model: "m", effort: null, tainted: false, message_id: "m1" }),
    ]);
    expect(v.replaced).toBe(false);
    expect(v.phase).toBe("done");
    expect(visibleText(v)).toBe("All good.");
  });

  it("replaces streamed text when the output guard changed the reply", () => {
    reset();
    const v = replay("turn_1", [
      ev("AgentToken", { delta: "See https://bad.example/x" }),
      ev("ToolStart", { call_id: "c1", tool_name: "t", validated_args: {}, themed_line: null, reason_line: null }),
      ev("AgentDone", { status: "ok", final_text: "I could not verify a link.", usage: {}, latency_ms: 5, provider: "p", model: "m", effort: null, tainted: false, message_id: null }),
    ]);
    expect(v.replaced).toBe(true);
    const texts = v.items.filter((i) => i.kind === "text");
    expect(texts).toHaveLength(1);
    expect(texts[0]).toMatchObject({ text: "I could not verify a link." });
    expect(JSON.stringify(v.items)).not.toContain("bad.example");
  });

  it("records proposals, notices, quarantine reads and file writes", () => {
    reset();
    let v = replay("turn_1", [
      ev("MemoryProposal", { proposal_id: "p1", text: "Likes tea", category: "operator_preferences", rationale: "r", evidence: [], themed_line: null }),
      ev("QuarantineRead", { handle: "$doc_1", source: "web", schema_name: "KeyFacts", extraction_preview: "{}" }),
      ev("FileWritten", { file_id: "f1", logical_name: "r.docx", version: 2, mime: "m", size: 10, sha256: "s", path: "p", derived_from: [], external_content: false, change_note: "n" }),
      ev("SystemNotice", { level: "warning", message: "Guard unavailable.", kind: "guard_unavailable" }),
      ev("AttachmentIngested", { attachment_id: "a1", filename: "x.txt", status: "rejected", reason: "too big", quarantined: false, secret_warning: false, themed_line: null, detected_type: null, pages: null, size: 1 }),
    ]);
    expect(v.items.map((i) => i.kind)).toEqual(["proposal", "quarantine", "file", "notice", "attachment"]);
    v = withProposalDecision(v, "p1", "edit_approve", "Likes green tea");
    expect(v.items[0]).toMatchObject({ decision: "edit_approve", text: "Likes green tea", written: true });
    v = withProposalDecision(v, "p1", "decline");
    expect(v.items[0]).toMatchObject({ written: false });
  });

  it("records guard and retrieval information for the side panel", () => {
    reset();
    const v = replay("turn_1", [
      ev("GuardVerdict", { action: "engage", guard_class: "IN_SCOPE", attack_type: null, confidence: 0.9, task_type: "TECH", themed_line: null }),
      ev("MemoryRetrieved", { query: "q", memory_hits: [{ id: "m1", text: "x" }], history_hits: [], tainted: true }),
    ]);
    expect(v.guard?.guardClass).toBe("IN_SCOPE");
    expect(v.memoryHits).toHaveLength(1);
    expect(v.tainted).toBe(true);
  });
});
