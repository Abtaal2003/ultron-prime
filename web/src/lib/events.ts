import type { AgentEvent } from "./events.gen";

export type { AgentEvent };
export type EventType = AgentEvent["type"];
export type EventOf<T extends EventType> = Extract<AgentEvent, { type: T }>;

/** Every event type the server can send, for registering SSE listeners. */
export const EVENT_TYPES = [
  "UserMessage",
  "TurnStarted",
  "AttachmentIngested",
  "QuarantineRead",
  "FileWritten",
  "PolicyHold",
  "GuardVerdict",
  "RepeatDetected",
  "MemoryRetrieved",
  "AgentToken",
  "ToolStart",
  "ToolEnd",
  "ToolError",
  "EgressHold",
  "EgressDecision",
  "MemoryProposal",
  "MemoryDecision",
  "MemoryWritten",
  "OutputVerdict",
  "ProviderFailover",
  "AgentDone",
  "SystemNotice",
  "TurnClosed",
] as const satisfies readonly EventType[];

// Compile-time guard: adding an event on the server without handling it here fails the build.
type MissingTypes = Exclude<EventType, (typeof EVENT_TYPES)[number]>;
export const eventTypesAreExhaustive: [MissingTypes] extends [never] ? true : never = true;
