import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, type StoredMessage } from "@/lib/api";
import { useThrottled } from "@/lib/hooks";
import { streamedText, visibleText, type Item, type TurnView } from "@/lib/turnState";
import { AttachmentChip, EgressCard, FileCard, NoticeBar, ProposalCard, QuarantineLine, ToolCard } from "./Cards";
import { Markdown } from "./Markdown";

interface Props {
  chatId: string;
  live: TurnView | null;
  onOpenFile: (fileId: string, version: number) => void;
  onReviewProposal: () => void;
}

const PAGE = 40;

function StoredAttachment({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["attachment", id], queryFn: () => api.attachment(id), staleTime: Infinity });
  if (!q.data) return <p className="mt-1 text-xs text-muted">📎 attachment</p>;
  const a = q.data;
  const image = a.mime.startsWith("image/");
  return (
    <p className="mt-1 flex items-center gap-2 text-xs text-muted">
      {image ? (
        <img src={api.thumbnailUrl(id)} alt={`Thumbnail of ${a.name}`} className="h-12 w-12 rounded object-cover" loading="lazy" />
      ) : (
        <span aria-hidden="true">📎</span>
      )}
      <a href={api.attachmentUrl(id)} download>
        {a.name}
      </a>
      <span>{a.trust === "external" ? "external" : "trusted"}</span>
    </p>
  );
}

function StoredRow({ m }: { m: StoredMessage }) {
  if (m.role === "user") {
    return (
      <div className="px-4 py-2">
        <p className="label">operator</p>
        <p className="whitespace-pre-wrap break-words">
          <span className="text-neon-soft">&gt; </span>
          {m.content}
        </p>
        {m.attachments.map((id) => (
          <StoredAttachment key={id} id={id} />
        ))}
      </div>
    );
  }
  return (
    <div className="px-4 py-2">
      <p className="label glow text-neon-soft">ULTRON</p>
      <Markdown text={m.content} />
      {m.tool_calls.map((t, i) => (
        <p key={String(t["id"] ?? i)} className="my-1 text-xs text-muted">
          <span className="font-display tracking-widest text-neon-soft">TOOL</span> {String(t["name"])} ·{" "}
          {t["ok"] ? "ok" : "failed"}
          {t["receipt"] ? ` · receipt ${String(t["receipt"])}` : ""}
        </p>
      ))}
    </div>
  );
}

export function Terminal({ chatId, live, onOpenFile, onReviewProposal }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const query = useInfiniteQuery({
    queryKey: ["messages", chatId],
    queryFn: ({ pageParam }) => api.messages(chatId, pageParam, PAGE),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.has_more ? last.messages[0]?.id : undefined),
  });

  // Pages arrive newest first; each page is chronological. Flatten to oldest -> newest.
  const liveTurnId = live?.turnId ?? null;
  const messages = useMemo(
    () =>
      [...(query.data?.pages ?? [])]
        .reverse()
        .flatMap((p) => p.messages)
        .filter((m) => liveTurnId === null || m.turn_id !== liveTurnId),
    [query.data, liveTurnId],
  );

  const virtual = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => 96,
    overscan: 8,
    getItemKey: (i) => messages[i]?.id ?? i,
  });

  // Keep the newest content in view unless the operator scrolled up.
  const liveLength = live ? streamedText(live).length + live.items.length : 0;
  const totalSize = virtual.getTotalSize();
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, liveLength, totalSize]);

  const onScroll = (): void => {
    const el = scroller.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (el.scrollTop < 120 && query.hasNextPage && !query.isFetchingNextPage) void query.fetchNextPage();
  };

  useEffect(() => {
    stick.current = true;
  }, [chatId]);

  // Streamed text is announced through a throttled live region, never token by token.
  const announce = useThrottled(live ? visibleText(live) : "", 1500);

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scroller}
        onScroll={onScroll}
        className="absolute inset-0 overflow-y-auto overflow-x-hidden"
        role="log"
        aria-label="Conversation"
        tabIndex={0}
      >
        {query.isPending && <p className="p-4 text-muted pulse">Reading the archive…</p>}
        {query.isFetchingNextPage && <p className="p-2 text-center text-xs text-muted">older messages…</p>}
        {!query.isPending && messages.length === 0 && !live && (
          <p className="p-6 text-muted">The house is quiet. Give me something to do.</p>
        )}
        <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
          {virtual.getVirtualItems().map((v) => {
            const m = messages[v.index];
            if (!m) return null;
            return (
              <div
                key={v.key}
                data-index={v.index}
                ref={virtual.measureElement}
                style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${v.start}px)` }}
              >
                <StoredRow m={m} />
              </div>
            );
          })}
        </div>
        {live && <LiveTurn view={live} onOpenFile={onOpenFile} onReviewProposal={onReviewProposal} />}
        <div className="h-4" />
      </div>
      <div className="sr-only" aria-live="polite" aria-atomic="false">
        {announce}
      </div>
    </div>
  );
}

function LiveTurn({
  view,
  onOpenFile,
  onReviewProposal,
}: {
  view: TurnView;
  onOpenFile: (fileId: string, version: number) => void;
  onReviewProposal: () => void;
}) {
  const running = view.phase === "running";
  const lastTextIndex = view.items.reduce((acc, it, i) => (it.kind === "text" ? i : acc), -1);
  return (
    <div>
      {view.userText && (
        <div className="px-4 py-2">
          <p className="label">operator</p>
          <p className="whitespace-pre-wrap break-words">
            <span className="text-neon-soft">&gt; </span>
            {view.userText}
          </p>
        </div>
      )}
      <div className="px-4 py-2">
        <p className="label glow text-neon-soft">ULTRON</p>
        {view.tainted && (
          <p className="mb-1 text-xs text-neon-soft" title="External content reached this turn">
            ⚠ tainted turn: outbound actions need your approval
          </p>
        )}
        {view.items.length === 0 && running && <p className="cursor text-muted" aria-label="Ultron is thinking" />}
        {view.items.map((item, i) => (
          <ItemView
            key={item.id}
            item={item}
            turnId={view.turnId}
            cursor={running && i === lastTextIndex && i === view.items.length - 1}
            onOpenFile={onOpenFile}
            onReviewProposal={onReviewProposal}
          />
        ))}
        {view.replaced && (
          <p className="text-xs text-muted">The reply was checked and replaced before it was shown in full.</p>
        )}
        {view.phase === "done" && view.status === "stopped" && <p className="text-xs text-muted">Stopped.</p>}
      </div>
    </div>
  );
}

function ItemView({
  item,
  turnId,
  cursor,
  onOpenFile,
  onReviewProposal,
}: {
  item: Item;
  turnId: string;
  cursor: boolean;
  onOpenFile: (fileId: string, version: number) => void;
  onReviewProposal: () => void;
}) {
  switch (item.kind) {
    case "text":
      return cursor ? (
        <div className="cursor">
          <Markdown text={item.text} />
        </div>
      ) : (
        <Markdown text={item.text} />
      );
    case "tool":
      return <ToolCard item={item} />;
    case "egress":
      return <EgressCard item={item} />;
    case "file":
      return <FileCard item={item} onOpen={onOpenFile} />;
    case "attachment":
      return <AttachmentChip item={item} />;
    case "quarantine":
      return <QuarantineLine item={item} turnId={turnId} />;
    case "proposal":
      return <ProposalCard item={item} onReview={onReviewProposal} />;
    case "notice":
      return <NoticeBar item={item} />;
  }
}
