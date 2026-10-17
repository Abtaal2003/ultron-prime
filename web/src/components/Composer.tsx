import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { bytes } from "@/lib/format";
import { useKeyboardInset } from "@/lib/hooks";
import { useUi } from "@/store/ui";

interface Props {
  chatId: string;
  busy: boolean;
  disabled?: boolean;
  onSend: (text: string, files: File[]) => Promise<boolean>;
  onStop: () => void;
  maxFiles: number;
  maxMb: number;
}

/** Fixed input bar: attach (camera, library, files), drag and drop, send/stop. Tracks the
 * on-screen keyboard through the visual viewport so it never hides behind it (spec 13.3). */
export function Composer({ chatId, busy, disabled, onSend, onStop, maxFiles, maxMb }: Props) {
  const draft = useUi((s) => s.drafts[chatId] ?? "");
  const setDraft = useUi((s) => s.setDraft);
  const [files, setFiles] = useState<File[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const inset = useKeyboardInset();

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [draft]);

  const add = (incoming: File[]): void => {
    const next = [...files];
    for (const f of incoming) {
      if (next.length >= maxFiles) return setProblem(`At most ${maxFiles} files per message.`);
      if (f.size > maxMb * 1024 * 1024) {
        setProblem(`${f.name} is ${bytes(f.size)}; the limit is ${maxMb} MB.`);
        continue;
      }
      next.push(f);
    }
    setFiles(next);
  };

  const submit = async (): Promise<void> => {
    if (busy || disabled || (!draft.trim() && files.length === 0)) return;
    setProblem(null);
    const ok = await onSend(draft, files);
    if (ok) {
      setDraft(chatId, "");
      setFiles([]);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  };

  const onDrop = (e: DragEvent): void => {
    e.preventDefault();
    setDragging(false);
    add([...e.dataTransfer.files]);
  };

  return (
    <form
      className={`border-t border-panel-border bg-bg px-3 pt-2 ${dragging ? "outline outline-2 outline-neon" : ""}`}
      style={{ paddingBottom: `max(0.5rem, env(safe-area-inset-bottom))`, marginBottom: inset }}
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      aria-label="Message composer"
    >
      {(files.length > 0 || problem) && (
        <ul className="mb-2 flex flex-wrap gap-2" aria-label="Attached files">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`} className="flex items-center gap-2 rounded border border-deep px-2 py-1 text-xs">
              {f.type.startsWith("image/") && <Thumb file={f} />}
              <span className="break-all">{f.name}</span>
              <span className="text-muted">{bytes(f.size)}</span>
              <button
                type="button"
                className="btn min-h-0 border-transparent px-2 py-0"
                aria-label={`Remove ${f.name}`}
                onClick={() => setFiles(files.filter((_, j) => j !== i))}
              >
                ✕
              </button>
            </li>
          ))}
          {problem && (
            <li role="alert" className="text-xs text-neon-soft">
              {problem}
            </li>
          )}
        </ul>
      )}
      <div className="flex items-end gap-2">
        <input
          ref={input}
          type="file"
          multiple
          hidden
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            add([...(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <button type="button" className="btn" onClick={() => input.current?.click()} aria-label="Attach files, photos, or camera" disabled={disabled}>
          +
        </button>
        <label htmlFor="composer-text" className="sr-only">
          Message
        </label>
        <textarea
          id="composer-text"
          ref={area}
          rows={1}
          className="field max-h-40 flex-1 resize-none"
          placeholder="> type your command..."
          value={draft}
          disabled={disabled}
          onChange={(e) => setDraft(chatId, e.target.value)}
          onKeyDown={onKey}
          enterKeyHint="send"
          autoComplete="off"
          maxLength={20000}
        />
        {busy ? (
          <button type="button" className="btn btn-danger" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="submit" className="btn btn-primary" disabled={disabled || (!draft.trim() && files.length === 0)}>
            Send
          </button>
        )}
      </div>
    </form>
  );
}

function Thumb({ file }: { file: File }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const u = URL.createObjectURL(file);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [file]);
  return url ? <img src={url} alt="" className="h-8 w-8 rounded object-cover" /> : null;
}
