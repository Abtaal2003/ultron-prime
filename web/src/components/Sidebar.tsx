import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, useNavigate } from "react-router-dom";
import { api, type Chat } from "@/lib/api";
import { errorMessage } from "@/lib/format";

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const chats = useQuery({ queryKey: ["chats"], queryFn: () => api.chats() });
  const refresh = (): Promise<void> => qc.invalidateQueries({ queryKey: ["chats"] });

  const create = useMutation({
    mutationFn: (priv: boolean) => api.createChat("New chat", priv),
    onSuccess: async (chat) => {
      await refresh();
      nav(`/chat/${chat.id}`);
      onNavigate?.();
    },
  });
  const patch = useMutation({
    mutationFn: (v: { id: string; body: Parameters<typeof api.patchChat>[1] }) => api.patchChat(v.id, v.body),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteChat(id),
    onSuccess: async () => {
      await refresh();
      nav("/");
    },
  });

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (chats.data ?? []).filter((c) => !f || c.title.toLowerCase().includes(f));
  }, [chats.data, filter]);

  const rename = (c: Chat): void => {
    setEditing(c.id);
    setTitle(c.title);
  };

  return (
    <nav aria-label="Chats" className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 p-3">
        <button className="btn btn-primary w-full" onClick={() => create.mutate(false)} disabled={create.isPending}>
          + New chat
        </button>
        <button className="btn w-full text-xs" onClick={() => create.mutate(true)} disabled={create.isPending}>
          + New private chat
        </button>
        <label className="sr-only" htmlFor="chat-search">
          Search chats
        </label>
        <input
          id="chat-search"
          type="search"
          className="field"
          placeholder="search chats"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      {(chats.isError || create.isError || patch.isError || remove.isError) && (
        <p role="alert" className="px-3 text-xs text-neon-soft">
          {errorMessage(chats.error ?? create.error ?? patch.error ?? remove.error)}
        </p>
      )}
      <ul className="min-h-0 flex-1 overflow-y-auto">
        {shown.map((c) => (
          <li key={c.id} className="group border-b border-panel-border/60">
            {editing === c.id ? (
              <form
                className="flex gap-1 p-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (title.trim()) patch.mutate({ id: c.id, body: { title: title.trim() } });
                  setEditing(null);
                }}
              >
                <label className="sr-only" htmlFor={`rename-${c.id}`}>
                  Chat title
                </label>
                <input id={`rename-${c.id}`} className="field" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus maxLength={120} />
                <button className="btn" type="submit">
                  OK
                </button>
              </form>
            ) : (
              <div className="flex items-center">
                <NavLink
                  to={`/chat/${c.id}`}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    `flex min-h-touch flex-1 items-center gap-2 px-3 py-2 text-sm no-underline ${isActive ? "bg-deep/30 text-text" : "text-muted"}`
                  }
                >
                  <span className="truncate">{c.title}</span>
                  {c.private && <span className="text-xs text-neon-soft">(private)</span>}
                  {c.active_turn && <span className="pulse text-xs text-neon-soft">●</span>}
                </NavLink>
                <details className="relative">
                  <summary className="btn cursor-pointer list-none border-transparent" aria-label={`Actions for ${c.title}`}>
                    ⋯
                  </summary>
                  <div className="panel absolute right-0 z-20 w-44 p-1 text-sm">
                    <button className="btn w-full justify-start" onClick={() => rename(c)}>
                      Rename
                    </button>
                    <button className="btn w-full justify-start" onClick={() => patch.mutate({ id: c.id, body: { private: !c.private } })}>
                      {c.private ? "Make normal" : "Make private"}
                    </button>
                    <button className="btn w-full justify-start" onClick={() => patch.mutate({ id: c.id, body: { archived: true } })}>
                      Archive
                    </button>
                    <a className="btn w-full justify-start no-underline" href={api.exportUrl(c.id)} download>
                      Export Markdown
                    </a>
                    <button
                      className="btn btn-danger w-full justify-start"
                      onClick={() => {
                        if (window.confirm(`Delete "${c.title}" for good?`)) remove.mutate(c.id);
                      }}
                    >
                      Delete
                    </button>
                  </div>
                </details>
              </div>
            )}
          </li>
        ))}
        {chats.isSuccess && shown.length === 0 && <li className="p-4 text-sm text-muted">No chats match.</li>}
      </ul>
      <div className="border-t border-panel-border p-3">
        <NavLink to="/settings" onClick={onNavigate} className="btn w-full no-underline">
          Settings
        </NavLink>
      </div>
    </nav>
  );
}
