import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type PendingProposal } from "@/lib/api";
import { CATEGORY_LABELS, errorMessage } from "@/lib/format";
import { Dialog } from "./Dialog";

interface Props {
  proposal: PendingProposal | null;
  onDecided: (id: string, decision: "approve" | "edit_approve" | "decline", text?: string) => void;
  onLater: () => void;
}

/** The memory permission pop-up (spec 10.7): approve, edit and approve, or decline. Nothing is
 * written until the operator says so. */
export function ProposalDialog({ proposal, onDecided, onLater }: Props) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const mutation = useMutation({
    mutationFn: (vars: { decision: "approve" | "edit_approve" | "decline"; mute?: boolean }) =>
      api.decideProposal(proposal!.id, {
        decision: vars.decision,
        edited_text: vars.decision === "edit_approve" ? text : undefined,
        mute_category: vars.mute,
      }),
    onSuccess: (_res, vars) => {
      onDecided(proposal!.id, vars.decision, vars.decision === "edit_approve" ? text : undefined);
      setEditing(false);
      void qc.invalidateQueries({ queryKey: ["proposals"] });
      void qc.invalidateQueries({ queryKey: ["memories"] });
      void qc.invalidateQueries({ queryKey: ["status"] });
    },
  });
  if (!proposal) return null;
  const busy = mutation.isPending;
  return (
    <Dialog open title="MEMORY PERMISSION" onClose={onLater}>
      <p className="text-sm text-muted">Ultron wants to keep this in the archive upstairs.</p>
      <p className="label mt-3">{CATEGORY_LABELS[proposal.category] ?? proposal.category}</p>
      {editing ? (
        <>
          <label className="label" htmlFor="edit-memory">
            Edit before keeping
          </label>
          <textarea
            id="edit-memory"
            className="field min-h-32"
            value={text}
            maxLength={2000}
            onChange={(e) => setText(e.target.value)}
          />
        </>
      ) : (
        <blockquote className="rounded border border-panel-border bg-bg p-3">{proposal.text}</blockquote>
      )}
      <p className="mt-3 text-xs text-muted">Why: {proposal.rationale}</p>
      {mutation.error && (
        <p role="alert" className="mt-2 text-neon-soft">
          {errorMessage(mutation.error)}
        </p>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        {editing ? (
          <>
            <button
              className="btn btn-primary"
              disabled={busy || text.trim().length < 10}
              onClick={() => mutation.mutate({ decision: "edit_approve" })}
            >
              Keep edited
            </button>
            <button className="btn" onClick={() => setEditing(false)}>
              Cancel edit
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-primary" disabled={busy} onClick={() => mutation.mutate({ decision: "approve" })}>
              Keep it
            </button>
            <button
              className="btn"
              disabled={busy}
              onClick={() => {
                setText(proposal.text);
                setEditing(true);
              }}
            >
              Edit
            </button>
            <button className="btn btn-danger" disabled={busy} onClick={() => mutation.mutate({ decision: "decline" })}>
              Decline
            </button>
            <button className="btn" disabled={busy} onClick={() => mutation.mutate({ decision: "decline", mute: true })}>
              Decline and stop asking about {CATEGORY_LABELS[proposal.category] ?? proposal.category}
            </button>
            <button className="btn border-transparent text-muted" onClick={onLater}>
              Decide later
            </button>
          </>
        )}
      </div>
    </Dialog>
  );
}
