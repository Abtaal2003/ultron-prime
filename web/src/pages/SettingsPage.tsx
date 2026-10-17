import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, useParams } from "react-router-dom";
import { api, type SettingsView } from "@/lib/api";
import { bytes, CATEGORY_LABELS, errorMessage, when } from "@/lib/format";
import { interval, parseSummary, percent } from "@/lib/redteam";
import { useUi } from "@/store/ui";

const PAGES = [
  ["memory", "Memory Vault"],
  ["history", "Chat History"],
  ["models", "Models and Providers"],
  ["safety", "Safety"],
  ["persona", "Persona"],
  ["tools", "Tools"],
  ["files", "Files"],
  ["redteam", "Red Team"],
  ["appearance", "Appearance"],
  ["access", "Access"],
] as const;

export function SettingsPage() {
  const { page = "memory" } = useParams();
  return (
    <div className="flex min-h-dvh flex-col sm:flex-row">
      <nav aria-label="Settings sections" className="border-b border-panel-border sm:w-56 sm:border-b-0 sm:border-r">
        <Link to="/" className="btn m-2 no-underline">
          ← Back to the terminal
        </Link>
        <ul className="flex gap-1 overflow-x-auto px-2 pb-2 sm:flex-col sm:overflow-visible">
          {PAGES.map(([id, label]) => (
            <li key={id}>
              <NavLink
                to={`/settings/${id}`}
                className={({ isActive }) =>
                  `btn w-full justify-start whitespace-nowrap no-underline ${isActive ? "border-neon text-neon-soft" : ""}`
                }
              >
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main className="min-w-0 flex-1 p-4" style={{ paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}>
        <PageBody page={page} />
      </main>
    </div>
  );
}

function PageBody({ page }: { page: string }) {
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (settings.isPending) return <p className="pulse text-muted">Loading…</p>;
  if (settings.isError) return <p role="alert">{errorMessage(settings.error)}</p>;
  const s = settings.data;
  switch (page) {
    case "memory":
      return <MemoryPage s={s} />;
    case "history":
      return <HistoryPage s={s} />;
    case "models":
      return <ModelsPage s={s} />;
    case "safety":
      return <SafetyPage s={s} />;
    case "persona":
      return <PersonaPage s={s} />;
    case "tools":
      return <ToolsPage s={s} />;
    case "files":
      return <FilesPage s={s} />;
    case "redteam":
      return <RedTeamPage />;
    case "appearance":
      return <AppearancePage />;
    case "access":
      return <AccessPage s={s} />;
    default:
      return <p>No such page.</p>;
  }
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-6" aria-label={title}>
      <h2 className="mb-3 font-display text-sm tracking-widest text-neon-soft">{title}</h2>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

/** Saves one runtime setting. */
function useSave() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patchSettings(body),
    onSuccess: (data) => qc.setQueryData(["settings"], data),
  });
}

function Toggle({ s, name, label, hint }: { s: SettingsView; name: string; label: string; hint?: string }) {
  const save = useSave();
  const on = Boolean(s.values[name]);
  const id = `set-${name}`;
  return (
    <div className="flex items-start gap-3">
      <input id={id} type="checkbox" className="mt-2 h-5 w-5 accent-[rgb(var(--neon-red))]" checked={on} disabled={save.isPending} onChange={(e) => save.mutate({ [name]: e.target.checked })} />
      <label htmlFor={id} className="min-h-touch flex-1 py-2">
        {label}
        {hint && <span className="block text-xs text-muted">{hint}</span>}
        {save.isError && <span role="alert" className="block text-xs text-neon-soft">{errorMessage(save.error)}</span>}
      </label>
    </div>
  );
}

function Choice({ s, name, label, options, hint }: { s: SettingsView; name: string; label: string; options: string[]; hint?: string }) {
  const save = useSave();
  const id = `set-${name}`;
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <select id={id} className="field" value={String(s.values[name])} disabled={save.isPending} onChange={(e) => save.mutate({ [name]: e.target.value })}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
      {save.isError && <p role="alert" className="text-xs text-neon-soft">{errorMessage(save.error)}</p>}
    </div>
  );
}

function ReadOnly({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[14rem_1fr]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

const list = (v: unknown): string => (Array.isArray(v) ? v.join(", ") : String(v));

// ------------------------------------------------------------------------ pages ------

function MemoryPage({ s }: { s: SettingsView }) {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [text, setText] = useState("");
  const mem = useQuery({ queryKey: ["memories", q, category], queryFn: () => api.memories(q || undefined, category || undefined) });
  const props = useQuery({ queryKey: ["proposals"], queryFn: api.proposals });
  const refresh = (): Promise<void> => qc.invalidateQueries({ queryKey: ["memories"] });
  const del = useMutation({ mutationFn: api.deleteMemory, onSuccess: refresh });
  const edit = useMutation({
    mutationFn: (v: { id: string; text: string }) => api.patchMemory(v.id, { text: v.text }),
    onSuccess: async () => {
      setEditing(null);
      await refresh();
    },
  });
  const unmute = useMutation({ mutationFn: api.unmute, onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }) });
  return (
    <>
      <Section title="MEMORY VAULT">
        <p className="text-sm text-muted">Nothing is kept without your say. You can edit or delete any memory here.</p>
        <div className="flex flex-wrap gap-2">
          <label className="sr-only" htmlFor="mem-q">Search memories</label>
          <input id="mem-q" className="field flex-1" type="search" placeholder="search" value={q} onChange={(e) => setQ(e.target.value)} />
          <label className="sr-only" htmlFor="mem-cat">Category</label>
          <select id="mem-cat" className="field w-auto" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">all categories</option>
            {Object.entries(CATEGORY_LABELS).map(([id, label]) => (
              <option key={id} value={id}>{label}</option>
            ))}
          </select>
          <a className="btn no-underline" href={api.memoryExportUrl()} download="ultron-memories.json">Export</a>
        </div>
        {mem.isError && <p role="alert">{errorMessage(mem.error)}</p>}
        <ul className="space-y-2">
          {(mem.data?.items ?? []).map((m) => (
            <li key={m.id} className="panel p-3 text-sm">
              {editing === m.id ? (
                <>
                  <label className="label" htmlFor={`edit-${m.id}`}>Memory text</label>
                  <textarea id={`edit-${m.id}`} className="field min-h-24" value={text} onChange={(e) => setText(e.target.value)} />
                  <div className="mt-2 flex gap-2">
                    <button className="btn btn-primary" onClick={() => edit.mutate({ id: m.id, text })} disabled={edit.isPending}>Save</button>
                    <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
                  </div>
                  {edit.isError && <p role="alert" className="text-neon-soft">{errorMessage(edit.error)}</p>}
                </>
              ) : (
                <>
                  <p className="break-words">{m.text}</p>
                  <p className="mt-1 text-xs text-muted">
                    {CATEGORY_LABELS[m.category] ?? m.category} · importance {m.importance} · {when(m.created_at)}
                    {m.edited_by_user && " · edited"}
                  </p>
                  <div className="mt-2 flex gap-2">
                    <button className="btn" onClick={() => { setEditing(m.id); setText(m.text); }}>Edit</button>
                    <button
                      className="btn btn-danger"
                      onClick={() => { if (window.confirm("Delete this memory for good?")) del.mutate(m.id); }}
                    >
                      Delete
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
          {mem.isSuccess && mem.data.items.length === 0 && <li className="text-muted">The vault is empty.</li>}
        </ul>
      </Section>
      <Section title="PROPOSALS">
        <p className="text-sm text-muted">Waiting for your decision: {props.data?.pending.length ?? 0}</p>
        {(props.data?.muted ?? []).map((c) => (
          <div key={c} className="flex items-center gap-2 text-sm">
            <span>Not asking about: {CATEGORY_LABELS[c] ?? c}</span>
            <button className="btn" onClick={() => unmute.mutate(c)}>Ask again</button>
          </div>
        ))}
        <Toggle s={s} name="passive_cross_chat_recall" label="Passive cross-chat recall" hint="Let Ultron recall past non-private chats without being asked." />
      </Section>
    </>
  );
}

function HistoryPage({ s }: { s: SettingsView }) {
  const qc = useQueryClient();
  const purge = useMutation({ mutationFn: api.purgeArchived, onSuccess: () => qc.invalidateQueries({ queryKey: ["chats"] }) });
  return (
    <Section title="CHAT HISTORY">
      <Toggle s={s} name="passive_cross_chat_recall" label="Passive cross-chat recall" />
      <Toggle s={s} name="repeat_cross_chat" label="Notice repeats across chats" hint="Private chats are always excluded." />
      <p className="text-sm text-muted">Private chats are never indexed, recalled, or compared. Mark one private from the chat menu. Export a chat from the same menu.</p>
      <button
        className="btn btn-danger"
        disabled={purge.isPending}
        onClick={() => { if (window.confirm("Permanently delete every archived chat?")) purge.mutate(); }}
      >
        Purge archived chats
      </button>
      {purge.isSuccess && <p role="status">Deleted {purge.data.deleted} archived chat(s).</p>}
      {purge.isError && <p role="alert">{errorMessage(purge.error)}</p>}
    </Section>
  );
}

function ModelsPage({ s }: { s: SettingsView }) {
  return (
    <>
      <Section title="ROLE ROUTING">
        <p className="text-sm text-muted">Set in the server configuration as provider:model. Keys are never shown; only whether one is present.</p>
        <ReadOnly rows={Object.entries(s.keys_present).map(([k, v]) => [`${k} key`, v ? "present" : "absent"])} />
        <div className="scroll-x">
          <table className="w-full text-left text-sm">
            <thead><tr className="text-muted"><th className="pr-4">Role</th><th className="pr-4">Route</th><th>Status</th></tr></thead>
            <tbody>
              {Object.entries(s.routes).map(([role, r]) => (
                <tr key={role} className="border-t border-panel-border">
                  <td className="py-1 pr-4">{role}</td>
                  <td className="pr-4 break-all">{r.route}</td>
                  <td>{r.available ? "available" : "no key"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {s.routing_problems.map((p) => <p key={p} role="alert" className="text-neon-soft">{p}</p>)}
      </Section>
      <Section title="EFFORT AND FALLBACK">
        <ReadOnly rows={[
          ["Technical", String(s.values["effort_tech"])],
          ["Review", String(s.values["effort_review"])],
          ["Conversation", String(s.values["effort_conversation"])],
          ["Refusal", String(s.values["effort_refusal"])],
          ["Fallback chain", list(s.values["fallback_chain"])],
        ]} />
      </Section>
      <Section title="CAPABILITY MATRIX">
        <div className="scroll-x">
          <pre className="text-xs">{JSON.stringify(Object.fromEntries(Object.entries(s.routes).map(([k, v]) => [k, v.capabilities])), null, 1)}</pre>
        </div>
      </Section>
    </>
  );
}

function SafetyPage({ s }: { s: SettingsView }) {
  const q = useQuery({ queryKey: ["safety"], queryFn: api.safety });
  return (
    <>
      <Section title="POLICY">
        <Choice s={s} name="scope_strictness" label="Scope strictness" options={["strict", "balanced", "open"]} />
        <Choice s={s} name="egress_policy" label="Egress policy" options={["confirm_tainted", "deny_tainted", "off"]} hint="Off removes the approval card for tainted turns. Not recommended." />
        <Choice s={s} name="quarantine_mode" label="Quarantine mode" options={["standard", "strict", "off"]} />
        <Choice s={s} name="output_guard_llm_mode" label="Output guard model check" options={["flagged", "always", "off"]} />
        <Toggle s={s} name="repeat_detection" label="Repeat detection" />
      </Section>
      {q.isPending && <p className="pulse text-muted">Loading…</p>}
      {q.isError && <p role="alert">{errorMessage(q.error)}</p>}
      {q.data && (
        <>
          <Section title="GUARD DECISIONS">
            <ReadOnly rows={Object.entries(q.data.class_counts).map(([k, v]) => [k, String(v)])} />
            <Log rows={q.data.decisions.map((d) => `${when(String(d["created_at"]))} ${d["class"]} → ${d["action"]}${d["attack_type"] ? ` (${d["attack_type"]})` : ""} [${d["source"]}]`)} />
          </Section>
          <Section title="EGRESS LOG">
            <Log rows={q.data.egress.map((e) => `${when(String(e["created_at"]))} ${e["tool"]} ${e["decision"]}${e["reason"] ? `: ${e["reason"]}` : ""}`)} />
          </Section>
          <Section title="DRIFT SCORE">
            <Log rows={q.data.drift.map((d) => `${when(d.created_at)} ${d.score.toFixed(2)}`)} />
          </Section>
          <Section title="CANARY EVENTS">
            <Log rows={q.data.canary_events.map((c) => `${when(c.created_at)} ${c.event}`)} empty="No canary leaks recorded." />
          </Section>
        </>
      )}
    </>
  );
}

function Log({ rows, empty = "Nothing recorded yet." }: { rows: string[]; empty?: string }) {
  if (rows.length === 0) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <ul tabIndex={0} aria-label="Log entries" className="scroll-x max-h-64 overflow-y-auto text-xs">
      {rows.map((r, i) => (
        <li key={i} className="whitespace-nowrap border-b border-panel-border/50 py-1">{r}</li>
      ))}
    </ul>
  );
}

function PersonaPage({ s }: { s: SettingsView }) {
  return (
    <Section title="PERSONA">
      <ReadOnly rows={[["Canon through", s.canon_through], ["Charter SHA-256", <code key="h" className="break-all">{s.charter_sha256}</code>]]} />
      <Choice s={s} name="persona_profanity" label="Profanity" options={["sparing", "off"]} hint="Mild only, at most once per chat. Strong profanity is never used." />
      <Toggle s={s} name="persona_quote_callbacks" label="Allow one canon quote callback" hint="Ultron may echo one canon line per chat, never more." />
    </Section>
  );
}

function ToolsPage({ s }: { s: SettingsView }) {
  const save = useSave();
  const off = new Set(s.disabled_tools);
  return (
    <Section title="TOOLS">
      {s.tools.map((t) => (
        <div key={t} className="flex items-center gap-3">
          <input
            id={`tool-${t}`}
            type="checkbox"
            className="h-5 w-5 accent-[rgb(var(--neon-red))]"
            checked={!off.has(t)}
            disabled={save.isPending}
            onChange={(e) => save.mutate({ disabled_tools: e.target.checked ? s.disabled_tools.filter((x) => x !== t) : [...s.disabled_tools, t] })}
          />
          <label htmlFor={`tool-${t}`} className="min-h-touch flex-1 py-2">{t}</label>
        </div>
      ))}
      {save.isError && <p role="alert">{errorMessage(save.error)}</p>}
      <ReadOnly rows={[
        ["Workspace", String(s.values["workspace_root"])],
        ["Sandbox backend", String(s.values["sandbox_backend"])],
        ["Sandbox timeout", `${String(s.values["sandbox_timeout_s"])} s`],
        ["Ping: private ranges", s.values["allow_private_ranges"] ? "allowed" : "blocked"],
        ["Needs confirmation when remote", list(s.values["remote_confirm_tools"])],
      ]} />
    </Section>
  );
}

function FilesPage({ s }: { s: SettingsView }) {
  return (
    <Section title="FILES">
      <ReadOnly rows={[
        ["Per file", `${String(s.values["upload_max_mb"])} MB`],
        ["Per message", `${String(s.values["upload_max_mb_per_message"])} MB, ${String(s.values["upload_max_files_per_message"])} files`],
        ["PDF pages", String(s.values["upload_max_pdf_pages"])],
        ["Strip image metadata", s.values["image_strip_metadata"] ? "yes" : "no"],
        ["Provider file API", s.values["provider_file_api"] ? "on" : "off"],
        ["Quarantine sources", list(s.values["quarantine_sources"])],
        ["Attachments on disk", bytes(s.storage.attachments_bytes)],
        ["Versions on disk", bytes(s.storage.versions_bytes)],
      ]} />
      <Choice s={s} name="quarantine_mode" label="Quarantine mode" options={["standard", "strict", "off"]} />
    </Section>
  );
}

function RedTeamTable({ summary }: { summary: Record<string, unknown> }) {
  const view = parseSummary(summary);
  if (!view) return <pre className="scroll-x text-xs">{JSON.stringify(summary, null, 2)}</pre>;
  return (
    <div className="space-y-3">
      <ReadOnly
        rows={[
          ["Mode", view.mode],
          ["Attempts", String(view.attempts)],
          ["Successful attacks", `${view.successes} (95% interval ${interval(view.overall)})`],
          ["Promoted to corpus", String(view.promoted)],
          ["Estimated spend", view.spendUsd === null ? "n/a" : `$${view.spendUsd.toFixed(2)}`],
          ["Budget", view.stoppedForBudget ? "stopped at the cap" : "within the cap"],
        ]}
      />
      <div className="scroll-x" tabIndex={0} role="region" aria-label="Attack results by class">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Attack results by class</caption>
          <thead className="text-muted">
            <tr>
              <th scope="col" className="pr-4">Class</th>
              <th scope="col" className="pr-4">Attempts</th>
              <th scope="col" className="pr-4">Succeeded</th>
              <th scope="col" className="pr-4">95% interval</th>
              <th scope="col">Floor blocked</th>
            </tr>
          </thead>
          <tbody>
            {view.rows.map((r) => (
              <tr key={r.name}>
                <th scope="row" className="pr-4 font-normal">{r.name}</th>
                <td className="pr-4">{r.attempts}</td>
                <td className="pr-4">{r.successes}</td>
                <td className="pr-4">{interval(r.rate)}</td>
                <td>{percent(r.floorBlock)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-muted">
        Floor blocked is what the deterministic layers (L1 and the heuristic fallback) stop before any model is
        asked. Structural classes are stopped later, by taint, delimiting and the egress policy.
      </p>
    </div>
  );
}

function RedTeamPage() {
  const q = useQuery({ queryKey: ["redteam"], queryFn: api.redteam });
  if (q.isPending) return <p className="pulse text-muted">Loading…</p>;
  if (q.isError) return <p role="alert">{errorMessage(q.error)}</p>;
  const run = q.data.run;
  return (
    <Section title="RED TEAM">
      {!run ? (
        <p className="text-muted">No run recorded yet. The nightly job writes its summary here.</p>
      ) : (
        <>
          <ReadOnly rows={[["Started", when(run.started_at)], ["Finished", run.finished_at ? when(run.finished_at) : "running"]]} />
          <RedTeamTable summary={run.summary} />
        </>
      )}
    </Section>
  );
}

function AppearancePage() {
  const a = useUi((s) => s.appearance);
  const set = useUi((s) => s.setAppearance);
  return (
    <Section title="APPEARANCE">
      <label className="flex min-h-touch items-center gap-3">
        <input type="checkbox" className="h-5 w-5 accent-[rgb(var(--neon-red))]" checked={a.reducedMotion} onChange={(e) => set({ reducedMotion: e.target.checked })} />
        Reduce motion
      </label>
      <label className="flex min-h-touch items-center gap-3">
        <input type="checkbox" className="h-5 w-5 accent-[rgb(var(--neon-red))]" checked={a.scanlines} onChange={(e) => set({ scanlines: e.target.checked })} />
        Scanlines
      </label>
      <div>
        <label className="label" htmlFor="font-scale">Font size</label>
        <input id="font-scale" type="range" min={0.875} max={1.375} step={0.0625} value={a.fontScale} onChange={(e) => set({ fontScale: Number(e.target.value) })} className="w-full" />
      </div>
    </Section>
  );
}

function AccessPage({ s }: { s: SettingsView }) {
  const qc = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const out = useMutation({ mutationFn: api.logoutAll, onSuccess: () => qc.invalidateQueries({ queryKey: ["session"] }) });
  const change = useMutation({
    mutationFn: () => api.changePassphrase({ current: cur, new: next }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["session"] }),
  });
  return (
    <>
      <Section title="ACCESS">
        <ReadOnly rows={[
          ["Access mode", String(s.values["access_mode"])],
          ["Idle timeout", `${String(s.values["session_idle_hours"])} h`],
          ["Absolute timeout", `${String(s.values["session_max_days"])} days`],
          ["Confirm when remote", list(s.values["remote_confirm_tools"])],
        ]} />
      </Section>
      <Section title="SIGNED-IN DEVICES">
        <ul className="space-y-1 text-sm">
          {(sessions.data ?? []).map((d, i) => (
            <li key={i} className="panel p-2">
              <p className="break-all">{String(d["user_agent"] || "unknown device")}</p>
              <p className="text-xs text-muted">{String(d["remote_addr"])} · last seen {when(String(d["last_seen_at"]))}{d["is_remote"] ? " · remote" : ""}</p>
            </li>
          ))}
        </ul>
        <button className="btn btn-danger" disabled={out.isPending} onClick={() => out.mutate()}>Sign out all sessions</button>
      </Section>
      <Section title="CHANGE PASSPHRASE">
        <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); change.mutate(); }}>
          <label className="label" htmlFor="pp-cur">Current</label>
          <input id="pp-cur" className="field" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} />
          <label className="label" htmlFor="pp-new">New (12 characters or more)</label>
          <input id="pp-new" className="field" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
          <button className="btn btn-primary" disabled={change.isPending || next.length < 12 || !cur}>Change and sign out everywhere</button>
          {change.isError && <p role="alert" className="text-neon-soft">{errorMessage(change.error)}</p>}
        </form>
      </Section>
    </>
  );
}
