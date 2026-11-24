import {
  ArrowDown, ArrowRight, ArrowUp, Books, BookOpen, ChatCircleText, CheckCircle, Database, FileArrowUp, Lightbulb, Plug, ShareNetwork, SlidersHorizontal,
  Table as TableIcon, Toolbox, WarningCircle, X,
} from "@phosphor-icons/react";
import { ReactNode, useState } from "react";
import { Job } from "../api";
import { Progress, useNav, usePoll } from "../ui";

type Bar = { engine: { ok: boolean }; vectors: { ok: boolean }; local_model: { running: boolean; model: string }; keiro: { configured: boolean };
  network: string; disk: { lake_bytes: number; free_bytes: number }; usage: { local_calls: number; cloud_calls: number } };
type Sheet = { id: string; name: string; rows: number | null; columns: unknown[]; source: string; updated_at: string };
type KB = { id: string; name: string; documents: number; chunks: number };
type Topic = { id: string; name: string; status: string; summary: { rows?: number; pages?: number } };
type Lib = { id: string; title: string; name: string; status: string; articles: number | null; size_bytes: number | null };
type Conn = { id: string; name: string; kind: string; sheet_id: string | null; last_run_at: string | null; interval_minutes: number };
type Conv = { id: string; title: string; updated_at: string };

const size = (b: number) => (b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : b >= 2 ** 20 ? `${Math.round(b / 2 ** 20)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
const ago = (iso: string | null) => {
  if (!iso) return "never";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

const WIDGETS = [
  ["data", "What's in the lake"], ["tables", "Tables"], ["collections", "Collections"], ["topics", "Topics"], ["sources", "Connected sources"],
  ["libraries", "Libraries"], ["chats", "Recent chats"], ["status", "Status"], ["running", "Running now"], ["start", "Start here"],
] as const;
type WidgetId = (typeof WIDGETS)[number][0];
const DEFAULT_ORDER: WidgetId[] = ["data", "tables", "collections", "topics", "start", "status", "sources", "chats", "running", "libraries"];
const WIDE: WidgetId[] = ["data", "tables"];

function loadLayout(): { order: WidgetId[]; hidden: WidgetId[] } {
  try {
    const raw = JSON.parse(localStorage.getItem("home:layout") ?? "{}");
    const known = new Set<string>(DEFAULT_ORDER);
    const order = (raw.order ?? []).filter((w: string) => known.has(w));
    return { order: [...order, ...DEFAULT_ORDER.filter((w) => !order.includes(w))], hidden: (raw.hidden ?? []).filter((w: string) => known.has(w)) };
  } catch { return { order: DEFAULT_ORDER, hidden: [] }; }
}

/** The lake at a glance: what it holds, what is running, and where to go next. Pick and order the panels with Customise. */
export default function Home() {
  const { go } = useNav();
  const [bar] = usePoll<Bar>("/statusbar", 5000);
  const [sheets] = usePoll<Sheet[]>("/sheets", 15000);
  const [kbs] = usePoll<KB[]>("/kbs", 30000);
  const [topics] = usePoll<Topic[]>("/topics", 15000);
  const [libs] = usePoll<Lib[]>("/libraries", 60000);
  const [conns] = usePoll<Conn[]>("/connectors", 30000);
  const [convs] = usePoll<Conv[]>("/conversations", 30000);
  const [jobs] = usePoll<Job[]>("/jobs?limit=8", 3000);
  const [layout, setLayout] = useState(loadLayout);
  const [editing, setEditing] = useState(false);
  const save = (next: typeof layout) => { setLayout(next); try { localStorage.setItem("home:layout", JSON.stringify(next)); } catch { /* private mode */ } };
  const move = (w: WidgetId, dir: -1 | 1) => {
    const order = [...layout.order];
    const i = order.indexOf(w), j = i + dir;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    save({ ...layout, order });
  };
  const toggle = (w: WidgetId) => save({ ...layout, hidden: layout.hidden.includes(w) ? layout.hidden.filter((x) => x !== w) : [...layout.hidden, w] });

  const rows = (sheets ?? []).reduce((n, s) => n + (s.rows ?? 0), 0);
  const docs = (kbs ?? []).reduce((n, k) => n + (k.documents ?? 0), 0);
  const running = (jobs ?? []).filter((j) => j.status === "running" || j.status === "queued");
  const hour = new Date().getHours();
  const hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  const panels: Record<WidgetId, ReactNode> = {
    data: (
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-3 lg:grid-cols-6">
        {([[TableIcon, "Tables", sheets?.length, "Tables"], [Database, "Rows", rows.toLocaleString(), "Tables"], [Books, "Collections", kbs?.length, "Collections"],
          [FileArrowUp, "Documents", docs.toLocaleString(), "Collections"], [Lightbulb, "Topics", topics?.length, "Topics"],
          [Database, "On disk", bar ? size(bar.disk.lake_bytes) : undefined, "Files"]] as const).map(([I, label, value, page]) => (
          <button key={label} className="bg-surface-primary p-4 text-left transition hover:bg-accent-50" onClick={() => go({ page })}>
            <span className="flex items-center gap-1.5 text-xs text-text-muted"><I size={14} />{label}</span>
            <span className="mt-1 block text-2xl font-semibold tabular-nums">{value ?? "–"}</span>
          </button>
        ))}
      </div>
    ),
    tables: (
      <Panel title="Tables" icon={TableIcon} more={() => go({ page: "Tables" })}>
        {sheets && !sheets.length ? <Empty text="No tables yet." action="Add a file" onAction={() => go({ page: "Tables", params: { new: "import" } })} /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="text-left text-xs text-text-muted"><th className="pb-2 font-medium">Name</th><th className="pb-2 text-right font-medium">Rows</th>
                <th className="hidden pb-2 text-right font-medium sm:table-cell">Columns</th><th className="hidden pb-2 pl-4 font-medium md:table-cell">Source</th><th className="pb-2 text-right font-medium">Updated</th></tr></thead>
              <tbody className="divide-y divide-line">{(sheets ?? []).slice(0, 8).map((s) => (
                <tr key={s.id} className="cursor-pointer hover:bg-surface-secondary" onClick={() => go({ page: "Tables", params: { sheet: s.id } })}>
                  <td className="max-w-0 truncate py-2 pr-3 font-medium">{s.name}</td><td className="py-2 text-right tabular-nums">{(s.rows ?? 0).toLocaleString()}</td>
                  <td className="hidden py-2 text-right tabular-nums sm:table-cell">{s.columns.length}</td><td className="hidden py-2 pl-4 text-text-muted md:table-cell">{s.source}</td>
                  <td className="whitespace-nowrap py-2 text-right text-text-muted">{ago(s.updated_at)}</td></tr>))}</tbody>
            </table>
          </div>
        )}
      </Panel>
    ),
    collections: (
      <Panel title="Collections" icon={Books} more={() => go({ page: "Collections" })}>
        {kbs && !kbs.length ? <Empty text="No collections yet." action="Create one" onAction={() => go({ page: "Collections" })} /> : (
          <ul className="divide-y divide-line text-sm">{(kbs ?? []).slice(0, 6).map((k) => (
            <li key={k.id} className="flex items-center gap-2 py-2"><Books size={15} className="text-text-muted" /><span className="min-w-0 flex-1 truncate">{k.name}</span>
              <span className="text-xs text-text-muted tabular-nums">{k.documents} docs · {k.chunks} passages</span></li>))}</ul>
        )}
      </Panel>
    ),
    topics: (
      <Panel title="Topics" icon={Lightbulb} more={() => go({ page: "Topics" })}>
        {topics && !topics.length ? <Empty text="No topics yet." action="Build a topic" onAction={() => go({ page: "Topics" })} /> : (
          <ul className="divide-y divide-line text-sm">{(topics ?? []).slice(0, 6).map((t) => (
            <li key={t.id} className="flex items-center gap-2 py-2"><Lightbulb size={15} className="text-text-muted" /><span className="min-w-0 flex-1 truncate">{t.name}</span>
              {t.status === "ready" ? <span className="text-xs text-text-muted">{t.summary.rows ?? 0} rows · {t.summary.pages ?? 0} pages</span>
                : <span className={t.status === "error" ? "badge-bad" : "badge-muted"}>{t.status}</span>}</li>))}</ul>
        )}
      </Panel>
    ),
    sources: (
      <Panel title="Connected sources" icon={Plug} more={() => go({ page: "DataTools" })}>
        {conns && !conns.length ? <Empty text="Nothing refreshes on its own yet." action="Open data tools" onAction={() => go({ page: "DataTools" })} /> : (
          <ul className="divide-y divide-line text-sm">{(conns ?? []).slice(0, 6).map((c) => (
            <li key={c.id} className="flex items-center gap-2 py-2"><Toolbox size={15} className="text-text-muted" /><span className="min-w-0 flex-1 truncate">{c.name}</span>
              <span className="text-xs text-text-muted">{c.interval_minutes ? `every ${c.interval_minutes >= 1440 ? `${c.interval_minutes / 1440} d` : c.interval_minutes >= 60 ? `${c.interval_minutes / 60} h` : `${c.interval_minutes} min`}` : "by hand"} · {ago(c.last_run_at)}</span></li>))}</ul>
        )}
      </Panel>
    ),
    libraries: (
      <Panel title="Libraries" icon={BookOpen} more={() => go({ page: "Libraries" })}>
        {libs && !libs.length ? <Empty text="No offline libraries." action="Browse libraries" onAction={() => go({ page: "Libraries" })} /> : (
          <ul className="divide-y divide-line text-sm">{(libs ?? []).slice(0, 5).map((l) => (
            <li key={l.id} className="flex items-center gap-2 py-2"><BookOpen size={15} className="text-text-muted" /><span className="min-w-0 flex-1 truncate">{l.title || l.name}</span>
              <span className="text-xs text-text-muted">{l.status === "ready" ? `${(l.articles ?? 0).toLocaleString()} articles` : l.status}</span></li>))}</ul>
        )}
      </Panel>
    ),
    chats: (
      <Panel title="Recent chats" icon={ChatCircleText} more={() => go({ page: "Ask" })}>
        {convs && !convs.length ? <Empty text="No chats yet." action="Ask a question" onAction={() => go({ page: "Ask" })} /> : (
          <ul className="divide-y divide-line text-sm">{(convs ?? []).slice(0, 6).map((c) => (
            <li key={c.id}><button className="flex w-full items-center gap-2 py-2 text-left hover:text-accent-600"
              onClick={() => { try { localStorage.setItem("conv", c.id); } catch { /* ignore */ } go({ page: "Ask" }); }}>
              <ChatCircleText size={15} className="text-text-muted" /><span className="min-w-0 flex-1 truncate">{c.title}</span><span className="text-xs text-text-muted">{ago(c.updated_at)}</span></button></li>))}</ul>
        )}
      </Panel>
    ),
    status: (
      <Panel title="Status" icon={CheckCircle}>
        {!bar ? <div className="skeleton h-24" /> : (
          <ul className="space-y-2 text-sm">
            {([["Engine", bar.engine.ok, "Running"], ["Vector search", bar.vectors.ok, bar.vectors.ok ? "Ready" : "Not answering"],
              ["Local model", bar.local_model.running, bar.local_model.running ? bar.local_model.model || "Ready" : "Stopped"],
              ["Web access", bar.keiro.configured, bar.keiro.configured ? "Zero data retention" : "No key"]] as const).map(([n, ok, d]) => (
              <li key={n} className="flex items-center gap-2">{ok ? <CheckCircle size={16} className="text-sage-500" /> : <WarningCircle size={16} className="text-cream-500" />}
                <span className="flex-1">{n}</span><span className="truncate text-text-muted">{d}</span></li>))}
            <li className="flex items-center gap-2"><ShareNetwork size={16} className="text-text-muted" /><span className="flex-1">Sharing</span>
              <button className="text-accent-600 hover:underline" onClick={() => go({ page: "Sharing" })}>{bar.network === "off" ? "Off" : bar.network}</button></li>
          </ul>
        )}
      </Panel>
    ),
    running: (
      <Panel title="Running now" icon={Database} more={() => go({ page: "Jobs" })}>
        {running.length ? <div className="space-y-2">{running.map((j) => <Progress key={j.id} value={j.progress} label={`${j.title} · ${j.message || "queued"}`} />)}</div>
          : <p className="text-sm text-text-muted">Nothing is running.</p>}
      </Panel>
    ),
    start: (
      <Panel title="Start here" icon={ArrowRight}>
        <div className="grid grid-cols-2 gap-2">
          {([[Lightbulb, "Build a topic", "Topics"], [FileArrowUp, "Add a file", "Tables"], [Toolbox, "Collect data", "DataTools"], [Plug, "Connect an app", "Gateway"]] as const).map(([I, label, page]) => (
            <button key={label} className="rounded-md border border-line p-3 text-left text-sm transition hover:border-accent-400 hover:bg-accent-50" onClick={() => go({ page })}>
              <I size={18} className="text-accent-600" /><span className="mt-1.5 block font-medium">{label}</span></button>))}
        </div>
      </Panel>
    ),
  };

  const shown = layout.order.filter((w) => !layout.hidden.includes(w));
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{hello}</h1>
          <p className="mt-0.5 text-sm text-text-secondary">
            {sheets && kbs ? `${sheets.length} tables, ${kbs.length} collections and ${topics?.length ?? 0} topics in your lake.` : "Loading your lake…"}
          </p>
        </div>
        <button className={editing ? "btn-primary" : "btn-secondary"} onClick={() => setEditing(!editing)}>
          {editing ? <><X size={14} /> Done</> : <><SlidersHorizontal size={14} /> Customise</>}</button>
      </div>

      {editing && (
        <div className="card">
          <p className="mb-2 text-sm font-medium">Show and order the panels</p>
          <ul className="grid gap-1 sm:grid-cols-2">
            {layout.order.map((w, i) => (
              <li key={w} className="flex items-center gap-2 rounded-md border border-line px-2 py-1.5 text-sm">
                <label className="flex flex-1 items-center gap-2"><input type="checkbox" checked={!layout.hidden.includes(w)} onChange={() => toggle(w)} />
                  {WIDGETS.find(([id]) => id === w)?.[1]}</label>
                <button className="icon-btn h-7 w-7" aria-label="Move up" disabled={i === 0} onClick={() => move(w, -1)}><ArrowUp size={13} /></button>
                <button className="icon-btn h-7 w-7" aria-label="Move down" disabled={i === layout.order.length - 1} onClick={() => move(w, 1)}><ArrowDown size={13} /></button>
              </li>))}
          </ul>
          <button className="btn-ghost mt-2" onClick={() => save({ order: DEFAULT_ORDER, hidden: [] })}>Reset to default</button>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {shown.map((w) => <div key={w} className={WIDE.includes(w) ? "lg:col-span-2" : ""}>{panels[w]}</div>)}
      </div>
    </div>
  );
}

function Panel({ title, icon: I, more, children }: { title: string; icon: typeof TableIcon; more?: () => void; children: ReactNode }) {
  return (
    <section className="card h-full">
      <div className="mb-3 flex items-center gap-2">
        <I size={16} className="text-accent-600" /><h2 className="flex-1 text-sm font-semibold">{title}</h2>
        {more && <button className="btn-ghost py-0.5 text-xs" onClick={more}>All <ArrowRight size={12} /></button>}
      </div>
      {children}
    </section>
  );
}

function Empty({ text, action, onAction }: { text: string; action: string; onAction: () => void }) {
  return <div className="flex items-center justify-between gap-3 rounded-md bg-surface-secondary px-3 py-3 text-sm"><span className="text-text-muted">{text}</span>
    <button className="btn-secondary py-1" onClick={onAction}>{action}</button></div>;
}
