import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLayout } from "@/lib/hooks";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface DialogProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** Side placement for drawers; dialogs on a phone become full-height sheets (spec 13.2). */
  placement?: "center" | "left" | "bottom";
  dismissible?: boolean;
}

/** An accessible modal: focus is trapped, Escape closes, focus returns to the opener. */
export function Dialog({ open, title, onClose, children, placement = "center", dismissible = true }: DialogProps) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const layout = useLayout();

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const first = node?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? node)?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape" && dismissible) {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !node) return;
      const items = [...node.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const a = items[0]!;
      const z = items[items.length - 1]!;
      if (e.shiftKey && document.activeElement === a) {
        e.preventDefault();
        z.focus();
      } else if (!e.shiftKey && document.activeElement === z) {
        e.preventDefault();
        a.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      opener?.focus?.();
    };
  }, [open, onClose, dismissible]);

  if (!open) return null;
  const phone = layout === "phone";
  const position =
    placement === "left"
      ? "left-0 top-0 h-dvh w-[min(88vw,22rem)]"
      : placement === "bottom" || phone
        ? "bottom-0 left-0 right-0 h-[min(88dvh,40rem)] sm:left-auto sm:right-auto sm:mx-auto sm:max-w-2xl"
        : "left-1/2 top-1/2 max-h-[85dvh] w-[min(92vw,38rem)] -translate-x-1/2 -translate-y-1/2";
  return createPortal(
    <div className="fixed inset-0 z-40">
      <div className="absolute inset-0 bg-black/70" onClick={dismissible ? onClose : undefined} aria-hidden="true" />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`panel absolute flex flex-col overflow-hidden shadow-[0_0_24px_rgb(var(--neon-red)/0.25)] ${position}`}
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <div className="flex items-center justify-between border-b border-panel-border px-4 py-2">
          <h2 id={titleId} className="font-display text-sm tracking-widest text-neon-soft">
            {title}
          </h2>
          {dismissible && (
            <button className="btn border-transparent" onClick={onClose} aria-label="Close">
              ✕
            </button>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
