import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type UploadedAttachment } from "./api";
import { TurnStream, type StreamState } from "./stream";
import { applyEvent, newTurnView, withProposalDecision, type Item, type TurnView } from "./turnState";

export interface ChatTurn {
  view: TurnView | null;
  conn: StreamState | "idle";
  busy: boolean;
  error: string | null;
  send: (text: string, files: File[]) => Promise<boolean>;
  stop: () => Promise<void>;
  markProposal: (id: string, decision: "approve" | "edit_approve" | "decline", text?: string) => void;
  clearError: () => void;
}

/**
 * Owns the live turn of one chat: starts it, follows its event stream, resumes a turn that was
 * already running when the page loaded, and hands over to the stored transcript when it ends.
 */
export function useChatTurn(chatId: string | null, activeTurn: string | null): ChatTurn {
  const qc = useQueryClient();
  const [view, setView] = useState<TurnView | null>(null);
  const [conn, setConn] = useState<StreamState | "idle">("idle");
  const [error, setError] = useState<string | null>(null);
  const streamRef = useRef<TurnStream | null>(null);
  const turnRef = useRef<string | null>(null);

  const closeStream = useCallback(() => {
    streamRef.current?.stop();
    streamRef.current = null;
  }, []);

  const attach = useCallback(
    (turnId: string, userText: string, uploaded: UploadedAttachment[] = []) => {
      closeStream();
      turnRef.current = turnId;
      // The upload response already says what was accepted, so the chips show at once.
      const chips: Item[] = uploaded.map((u, i) => ({
        kind: "attachment",
        id: `u${i}`,
        attachmentId: u.id,
        name: u.name,
        status: "accepted",
        reason: null,
        quarantined: u.quarantine,
        secretWarning: u.warnings.some((w) => /secret|credential|key/i.test(w)),
        themedLine: null,
      }));
      setView(newTurnView(turnId, userText, chips));
      const stream = new TurnStream(turnId, {
        onState: (state) => {
          setConn(state);
          if (state === "closed") {
            void Promise.all([
              qc.invalidateQueries({ queryKey: ["messages", chatId] }),
              qc.invalidateQueries({ queryKey: ["chats"] }),
              qc.invalidateQueries({ queryKey: ["chat", chatId] }),
              qc.invalidateQueries({ queryKey: ["proposals"] }),
              qc.invalidateQueries({ queryKey: ["files"] }),
              qc.invalidateQueries({ queryKey: ["status"] }),
            ]).then(() => {
              // The finished view stays on screen (with its tool, file and quarantine cards)
              // until the next message or a chat switch; the stored copy is hidden meanwhile.
              if (turnRef.current === turnId) setConn("idle");
            });
          }
        },
        onEvent: (event) => {
          setView((prev) => (prev && prev.turnId === turnId ? applyEvent(prev, event) : prev));
          if (event.type === "MemoryProposal") void qc.invalidateQueries({ queryKey: ["proposals"] });
          if (event.type === "FileWritten") void qc.invalidateQueries({ queryKey: ["files"] });
        },
      });
      streamRef.current = stream;
      stream.start();
    },
    [chatId, closeStream, qc],
  );

  // Switching chats drops the old stream (the turn itself keeps running on the server).
  useEffect(() => {
    closeStream();
    turnRef.current = null;
    setView(null);
    setConn("idle");
    setError(null);
    return closeStream;
  }, [chatId, closeStream]);

  // A turn already running when the page loaded (reload, another device): follow it from the start.
  useEffect(() => {
    if (chatId && activeTurn && turnRef.current !== activeTurn) attach(activeTurn, "");
  }, [chatId, activeTurn, attach]);

  const send = useCallback(
    async (text: string, files: File[]): Promise<boolean> => {
      if (!chatId) return false;
      setError(null);
      try {
        const res = await api.startTurn(chatId, text, files);
        attach(res.turn_id, text, res.attachments);
        return true;
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) setError("Ultron is still answering in this chat.");
        else setError(err instanceof Error ? err.message : "The message did not go through.");
        return false;
      }
    },
    [attach, chatId],
  );

  const stop = useCallback(async () => {
    if (turnRef.current) await api.stopTurn(turnRef.current).catch(() => undefined);
  }, []);

  const markProposal = useCallback<ChatTurn["markProposal"]>((id, decision, text) => {
    setView((prev) => (prev ? withProposalDecision(prev, id, decision, text) : prev));
  }, []);

  return {
    view,
    conn,
    busy: view !== null && view.phase === "running",
    error,
    send,
    stop,
    markProposal,
    clearError: () => setError(null),
  };
}
