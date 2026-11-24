import { ArrowDown, ArrowUp, Books, Plus, Sparkle, Table as TableIcon, Trash, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { api, errText } from "../api";
import { trackJob, useJobs } from "../background";
import MicButton from "../shell/MicButton";
import { ErrorNote, PageHeader, Progress, useKept, useNav, usePoll } from "../ui";

type Kind = "table" | "collection";
type Col = { name: string; type: string; description: string };
type Draft = { name: string; columns: Col[]; sections: string[]; rows: number; kind: string };
type Msg = { role: "user" | "assistant"; content: string; model?: string };
type Options = { lake: boolean; libraries: boolean; web: boolean; local_only: boolean };
type Session = {
  id: string; kind: Kind; title: string; messages: Msg[]; draft: Draft | null; ready: boolean; options: Options;
  built: { id: string; kind: Kind; added: number } | null; busy: "" | "thinking" | "filling"; error: string;
};
type Summary = { id: string; kind: Kind; title: string; busy: string; built: Session["built"]; updated: number };

const TYPES = ["text", "number", "date", "url", "boolean"];
const STARTERS: Record<Kind, string[]> = {
  table: [
    "Every Indian state with its capital, population and official language",
    "The 20 most-used open-source databases with licence, language and first release year",
    "Electric cars sold in India with price, range and battery size",
    "Nobel Prize winners in physics since 2000 with country and discovery",
  ],
  collection: [
    "A field guide to common Indian birds, one section per family",
    "First aid basics: burns, cuts, fractures, choking and fever",
    "How a car engine works, from fuel to wheels",
    "A beginner's guide to SQL, one section per concept",
  ],
};
const OPTIONS: [keyof Options, string][] = [["lake", "My collections"], ["libraries", "Offline libraries"], ["web", "Web search"], ["local_only", "Only models on this computer"]];

/** Build a table or an offline library by talking it through. The conversation lives in the engine, so it keeps going
 *  when you switch pages: come back and the reply, or the filled table, is waiting. */
export default function Builder() {
  const { go, params } = useNav();
  const [list, reloadList] = usePoll<Summary[]>("/builder/sessions", 5000);
  const [sid, setSid] = useKept<string>("builder:session", "");
  const [s, setS] = useState<Session | null>(null);
  const [input, setInput] = useState("");
  const [count, setCount] = useKept("builder:count", 20);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const jobs = useJobs();
  const scroller = useRef<HTMLDivElement>(null);
  const id = params.session || sid;

  const load = async (which = id) => {
    if (!which) { setS(null); return; }
    try { setS(await api<Session>(`/builder/sessions/${which}`)); }
    catch { setS(null); if (which === sid) setSid(""); }
  };
  useEffect(() => { if (params.session && params.session !== sid) setSid(params.session); }, [params.session]);
  useEffect(() => { load(); }, [id]);
  useEffect(() => {  // while a step runs, follow it; it finishes in the engine whether or not this page is open
    if (!s?.busy) return;
    const t = window.setInterval(() => load(s.id), 1500);
    return () => window.clearInterval(t);
  }, [s?.id, s?.busy]);
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" }); }, [s?.messages.length, s?.busy]);

  const fillJob = jobs.find((j) => j.title === `Build ${s?.draft?.name}` && (j.status === "running" || j.status === "queued"));

  const start = async (kind: Kind, sheetId?: string) => {
    setStarting(true); setError("");
    try {
      const created = await api<Session>("/builder/sessions", { method: "POST", json: { kind, sheet_id: sheetId ?? null } });
      setSid(created.id); setS(created); reloadList();
      if (params.session) go({ page: "Builder" });
    } catch (e) { setError(errText(e)); }
    setStarting(false);
  };

  const call = async (fn: () => Promise<{ job_id?: string } | Session | void>) => {
    setError("");
    try {
      const out = await fn();
      if (out && "job_id" in out && out.job_id) trackJob(out.job_id);
      await load(s?.id); reloadList();
    } catch (e) { setError(errText(e)); }
  };
  const say = (text = input) => {
    if (!s || !text.trim() || s.busy) return;
    setInput("");
    call(() => api(`/builder/sessions/${s.id}/say`, { method: "POST", json: { text: text.trim() } }));
  };
  const fill = () => s && call(() => api(`/builder/sessions/${s.id}/fill`, { method: "POST", json: { count } }));
  const setOption = (k: keyof Options, v: boolean) => s && (setS({ ...s, options: { ...s.options, [k]: v } }),
    call(() => api(`/builder/sessions/${s.id}/options`, { method: "PUT", json: { [k]: v } })));
  const saveDraft = (d: Draft) => s && (setS({ ...s, draft: d }), call(() => api(`/builder/sessions/${s.id}/draft`, { method: "PUT", json: d })));
  const remove = (x: string) => call(async () => { await api(`/builder/sessions/${x}`, { method: "DELETE" }); if (x === s?.id) { setSid(""); setS(null); } });

  return (
    <div className="space-y-3">
      <PageHeader title="Build with AI">
        <button className="btn-secondary" disabled={starting} onClick={() => start("table")}><TableIcon size={15} /> New table</button>
        <button className="btn-secondary" disabled={starting} onClick={() => start("collection")}><Books size={15} /> New library</button>
        <ExtendTable onPick={(sheetId) => start("table", sheetId)} />
      </PageHeader>
      <ErrorNote error={error} onClose={() => setError("")} />

      <div className="grid gap-4 lg:grid-cols-[13rem_1fr_23rem]">
        <nav aria-label="Your builds" className="card h-fit space-y-1 p-2 max-lg:order-3">
          <p className="px-2 py-1 text-xs font-medium text-text-muted">Your builds</p>
          {!list?.length && <p className="px-2 pb-2 text-sm text-text-muted">Nothing yet.</p>}
          {(list ?? []).map((x) => (
            <div key={x.id} className={`group flex items-center gap-2 rounded-md px-2 py-1.5 text-sm ${x.id === s?.id ? "bg-accent-50 text-accent-600" : "hover:bg-surface-secondary"}`}>
              <button className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={() => { setSid(x.id); if (params.session) go({ page: "Builder" }); }}>
                {x.kind === "table" ? <TableIcon size={14} className="shrink-0" /> : <Books size={14} className="shrink-0" />}
                <span className="truncate">{x.title}</span>
                {x.busy && <span className="status-dot shrink-0 animate-pulse bg-accent-400" aria-label="Working" />}
              </button>
              <button className="icon-btn h-6 w-6 opacity-0 group-hover:opacity-100 focus:opacity-100" aria-label={`Delete ${x.title}`} onClick={() => remove(x.id)}><Trash size={12} /></button>
            </div>
          ))}
        </nav>

        {!s ? (
          <section className="card flex min-h-[460px] flex-col items-center justify-center gap-4 text-center lg:col-span-2">
            <Sparkle size={28} className="text-accent-600" />
            <div>
              <p className="text-base font-semibold">Describe a table or a library</p>
              <p className="mt-1 text-sm text-text-muted">The AI proposes the columns, you shape them, it fills them in. It keeps working if you leave this page.</p>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              <button className="btn-primary" disabled={starting} onClick={() => start("table")}><TableIcon size={15} /> New table</button>
              <button className="btn-secondary" disabled={starting} onClick={() => start("collection")}><Books size={15} /> New library</button>
            </div>
          </section>
        ) : (
          <>
            <section className="card flex min-h-[520px] flex-col p-0">
              <div ref={scroller} className="max-h-[62vh] min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-4">
                {!s.messages.length && (
                  <div className="space-y-3">
                    <div className="flex gap-3"><Sparkle size={20} className="mt-0.5 shrink-0 text-accent-600" />
                      <p className="text-sm">What should this {s.kind === "table" ? "table hold" : "library cover"}? I'll suggest the {s.kind === "table" ? "columns" : "sections"}, you adjust them, then I fill it in.</p></div>
                    <div className="flex flex-wrap gap-2 pl-8">
                      {STARTERS[s.kind].map((t) => <button key={t} className="rounded-full border border-line px-3 py-1.5 text-left text-xs hover:border-accent-400 hover:bg-accent-50" onClick={() => say(t)}>{t}</button>)}
                    </div>
                  </div>
                )}
                {s.messages.map((m, i) => m.role === "user"
                  ? <div key={i} className="ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent-50 px-4 py-2 text-sm">{m.content}</div>
                  : <div key={i} className="flex gap-3"><Sparkle size={20} className="mt-0.5 shrink-0 text-accent-600" />
                      <div className="min-w-0"><p className="whitespace-pre-wrap text-sm">{m.content}</p>{m.model && <p className="mt-1 text-[11px] text-text-muted">{m.model}</p>}</div></div>)}
                {s.busy === "thinking" && <div className="flex gap-3"><Sparkle size={20} className="shrink-0 animate-pulse text-accent-600" /><div className="skeleton h-4 w-1/2" /></div>}
                {s.busy === "filling" && <Progress value={fillJob?.progress ?? 0.03} label={fillJob?.message || "Starting"} />}
                {s.error && <p className="field-error">{s.error}</p>}
              </div>
              <div className="border-t border-line p-3">
                <div className="flex gap-2">
                  <textarea rows={1} aria-label="Message" className="input-field min-h-[40px] flex-1 resize-none" value={input} onChange={(e) => setInput(e.target.value)}
                    placeholder={s.messages.length ? "Reply, or ask for changes" : "Describe what you want"}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); say(); } }} />
                  <MicButton className="h-10 w-10" onText={(t) => setInput((v) => (v.trim() ? `${v.trim()} ${t}` : t))} />
                  <button className="btn-primary h-10 w-10 p-0" aria-label="Send" disabled={!!s.busy || !input.trim()} onClick={() => say()}><ArrowUp size={16} weight="bold" /></button>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
                  {OPTIONS.map(([k, label]) => (
                    <label key={k} className="flex items-center gap-1.5"><input type="checkbox" checked={s.options[k]} onChange={(e) => setOption(k, e.target.checked)} />{label}</label>
                  ))}
                </div>
              </div>
            </section>

            <Plan s={s} count={count} setCount={setCount} onSave={saveDraft} onFill={fill}
              onOpen={() => s.built && go(s.built.kind === "table" ? { page: "Tables", params: { sheet: s.built.id } } : { page: "Collections" })} />
          </>
        )}
      </div>
    </div>
  );
}

/** The agreed plan, editable by hand: rename, retype, reorder or remove columns (or sections), then fill. */
function Plan({ s, count, setCount, onSave, onFill, onOpen }: {
  s: Session; count: number; setCount: (n: number) => void; onSave: (d: Draft) => void; onFill: () => void; onOpen: () => void;
}) {
  const d: Draft = s.draft ?? { name: "", columns: [], sections: [], rows: 20, kind: s.kind };
  const [name, setName] = useState(d.name);
  useEffect(() => setName(d.name), [d.name]);
  const busy = !!s.busy;
  const cols = d.columns;
  const setCols = (c: Col[]) => onSave({ ...d, columns: c });
  const move = (i: number, by: number) => { const c = [...cols]; const [x] = c.splice(i, 1); c.splice(i + by, 0, x); setCols(c); };
  const items = s.kind === "table" ? cols.length : d.sections.length;

  return (
    <aside className="card h-fit space-y-3">
      <input aria-label="Name" className="input-field font-semibold" placeholder={s.kind === "table" ? "Table name" : "Library name"} value={name} disabled={busy}
        onChange={(e) => setName(e.target.value)} onBlur={() => name !== d.name && onSave({ ...d, name })} />
      {s.kind === "table" ? (
        <div className="space-y-2">
          {!cols.length && <p className="text-sm text-text-muted">Columns appear here as you talk.</p>}
          {cols.map((c, i) => (
            <div key={`${i}:${c.name}`} className="group rounded-md border border-line p-2">
              <div className="flex items-center gap-1.5">
                <input aria-label={`Column ${i + 1} name`} className="input-field mono h-8 min-w-0 flex-1 py-0 text-xs" defaultValue={c.name} disabled={busy}
                  onBlur={(e) => e.target.value !== c.name && setCols(cols.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                <select aria-label={`Column ${i + 1} type`} className="input-field h-8 w-[5.5rem] shrink-0 py-0 text-xs" value={c.type} disabled={busy}
                  onChange={(e) => setCols(cols.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)))}>
                  {TYPES.map((t) => <option key={t}>{t}</option>)}
                </select>
              </div>
              <div className="mt-1 flex items-center gap-1">
                {i === 0 && <span className="badge bg-accent-50 text-accent-600" title="Each row is told apart by this column">key</span>}
                <input aria-label={`Column ${i + 1} description`} className="min-w-0 flex-1 bg-transparent px-1 text-xs text-text-muted outline-none" placeholder="What goes in this column"
                  defaultValue={c.description} disabled={busy} onBlur={(e) => e.target.value !== c.description && setCols(cols.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))} />
                <button className="icon-btn h-6 w-6" aria-label={`Move ${c.name} up`} disabled={busy || i === 0} onClick={() => move(i, -1)}><ArrowUp size={12} /></button>
                <button className="icon-btn h-6 w-6" aria-label={`Move ${c.name} down`} disabled={busy || i === cols.length - 1} onClick={() => move(i, 1)}><ArrowDown size={12} /></button>
                <button className="icon-btn h-6 w-6" aria-label={`Remove ${c.name}`} disabled={busy || !!s.built} onClick={() => setCols(cols.filter((_, j) => j !== i))}><X size={12} /></button>
              </div>
            </div>
          ))}
          <button className="btn-ghost w-full" disabled={busy} onClick={() => setCols([...cols, { name: `column_${cols.length + 1}`, type: "text", description: "" }])}><Plus size={14} /> Add column</button>
        </div>
      ) : (
        <div className="space-y-1.5">
          {!d.sections.length && <p className="text-sm text-text-muted">Sections appear here as you talk.</p>}
          {d.sections.map((t, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <span className="w-5 text-right text-xs text-text-muted">{i + 1}</span>
              <input aria-label={`Section ${i + 1}`} className="input-field h-8 min-w-0 flex-1 py-0 text-sm" defaultValue={t} disabled={busy}
                onBlur={(e) => e.target.value !== t && onSave({ ...d, sections: d.sections.map((x, j) => (j === i ? e.target.value : x)) })} />
              <button className="icon-btn h-7 w-7" aria-label={`Remove section ${i + 1}`} disabled={busy} onClick={() => onSave({ ...d, sections: d.sections.filter((_, j) => j !== i) })}><X size={12} /></button>
            </div>
          ))}
          <button className="btn-ghost w-full" disabled={busy} onClick={() => onSave({ ...d, sections: [...d.sections, "New section"] })}><Plus size={14} /> Add section</button>
        </div>
      )}

      <div className="space-y-2 border-t border-line pt-3">
        {s.kind === "table" && (
          <label className="flex items-center justify-between gap-2 text-sm">Rows to write
            <input type="number" min={1} max={200} className="input-field w-20 py-1" value={count} onChange={(e) => setCount(Math.max(1, Math.min(200, +e.target.value || 1)))} /></label>
        )}
        <button className="btn-primary w-full" disabled={busy || !d.name || !items} onClick={onFill}>
          {s.busy === "filling" ? "Filling…" : s.built ? (s.kind === "table" ? `Add ${count} more rows` : "Write these sections") : s.kind === "table" ? `Create and fill ${count} rows` : "Create the library"}</button>
        {s.built && (
          <button className="btn-secondary w-full" onClick={onOpen}>
            {s.built.kind === "table" ? <><TableIcon size={14} /> Open the table ({s.built.added} added)</> : <><Books size={14} /> Open collections</>}</button>
        )}
        {s.built?.kind === "table" && <Preview sheetId={s.built.id} cols={cols.map((c) => c.name)} refresh={s.built.added} />}
        <p className="text-xs text-text-muted">Cells from the model's own knowledge are marked low confidence. Turn on your collections or web search for sourced values.</p>
      </div>
    </aside>
  );
}

/** The first rows of the built table, with how sure each one is. */
function Preview({ sheetId, cols, refresh }: { sheetId: string; cols: string[]; refresh: number }) {
  const [rows, setRows] = useState<Record<string, any>[] | null>(null);
  useEffect(() => { api<{ rows: Record<string, any>[] }>(`/sheets/${sheetId}/rows?offset=0&limit=5&sort=_id&desc=true`).then((r) => setRows(r.rows)).catch(() => setRows(null)); }, [sheetId, refresh]);
  if (!rows?.length) return null;
  const shown = cols.slice(0, 3);
  return (
    <div className="overflow-hidden rounded-md border border-line">
      <p className="bg-surface-secondary px-2 py-1 text-[11px] font-medium text-text-muted">Latest rows</p>
      <table className="w-full table-fixed text-xs">
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-line">
              {shown.map((c) => <td key={c} className="truncate px-2 py-1" title={String(r[c] ?? "")}>{r[c] ?? <span className="text-text-muted">—</span>}</td>)}
              <td className="w-10 px-1 text-right"><Confidence meta={r._meta?.[shown[0]]} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Confidence({ meta }: { meta?: { confidence: number | null; sources: string[] } }) {
  if (!meta || meta.confidence == null) return null;
  const sure = meta.confidence >= 0.7;
  return <span className={`badge ${sure ? "badge-ok" : "bg-surface-tertiary text-text-muted"}`} title={meta.sources.join("\n")}>{sure ? "src" : "est"}</span>;
}

/** Start a conversation from a table you already have, to add rows or columns to it. */
function ExtendTable({ onPick }: { onPick: (sheetId: string) => void }) {
  const [sheets] = usePoll<{ id: string; name: string }[]>("/sheets", 30000);
  if (!sheets?.length) return null;
  return (
    <select aria-label="Add to a table you have" className="input-field h-9 w-auto py-0 text-sm" value="" onChange={(e) => e.target.value && onPick(e.target.value)}>
      <option value="">Add to a table…</option>
      {sheets.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
    </select>
  );
}
