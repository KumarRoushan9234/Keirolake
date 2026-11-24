import { ArrowClockwise, ArrowLeft, CaretDown, DownloadSimple, FloppyDisk, Sparkle, Trash } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import Markdown from "react-markdown";
import { api, base64Download, errText, isDesktop, shell } from "../../api";
import { clearTask, runTask, useTask } from "../../background";
import { Chart, chartable } from "../../Chart";
import { Card, ErrorNote, Progress, useJobRunner, useKept, useNav } from "../../ui";
import Grid from "./Grid";
import CleanTab from "./CleanTab";
import SheetCharts from "./SheetCharts";
import { Column, INTERVALS, Sheet, SOURCE_LABEL } from "./types";

const TABS = [["data", "Data"], ["chart", "Chart"], ["clean", "Clean"], ["schema", "Schema"], ["build", "Build"], ["history", "History"], ["api", "API"]] as const;
const FORMATS = [["csv", "CSV"], ["xlsx", "Excel"], ["json", "JSON"], ["jsonl", "JSON Lines"], ["pdf", "PDF"]] as const;

/** One sheet, with tabs like a Docker container's detail page. */
export default function SheetView({ id, onClose }: { id: string; onClose: () => void }) {
  const { setCrumb } = useNav();
  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [tab, setTab] = useKept(`sheet:${id}:tab`, "data");
  const [error, setError] = useState("");
  const [menu, setMenu] = useState(false);
  const [note, setNote] = useState("");
  const load = useCallback(() => api<Sheet>(`/sheets/${id}`).then(setSheet).catch((e) => setError(errText(e))), [id]);
  const runner = useJobRunner(load, `sheet-run:${id}`);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (sheet) setCrumb(["Sheets", sheet.name]); return () => setCrumb([]); }, [sheet?.name]);

  const act = async (fn: () => Promise<unknown>) => { setError(""); setNote(""); try { await fn(); } catch (e) { setError(errText(e)); } load(); };
  const saveTo = async (fmt: string) => {
    const folder = await shell<string | null>("pick_folder");
    if (folder) await act(async () => { const r = await api<{ path: string }>(`/sheets/${id}/save`, { method: "POST", json: { format: fmt, folder } }); setNote(`Saved ${r.path}`); });
  };

  if (!sheet) return <div className="skeleton h-40" />;
  const tabs = TABS.filter(([t]) => t !== "build" || sheet.build);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button className="icon-btn" aria-label="Back to sheets" onClick={onClose}><ArrowLeft size={16} /></button>
        <h2 className="min-w-0 truncate text-base font-semibold">{sheet.name}</h2>
        <span className={sheet.source === "build" ? "badge-ok" : "badge-muted"}>{SOURCE_LABEL[sheet.source] ?? sheet.source}</span>
        <span className="mono text-text-muted">{(sheet.rows ?? 0).toLocaleString()} rows</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {sheet.build && (
            <button className="btn-secondary" disabled={!!runner.job} onClick={() => runner.run(() => api(`/sheets/${id}/refresh`, { method: "POST" }))}>
              <ArrowClockwise size={15} /> {runner.job ? "Refreshing…" : "Refresh"}
            </button>
          )}
          <div className="relative">
            <button className="btn-secondary" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu(!menu)}><DownloadSimple size={15} /> Export <CaretDown size={12} /></button>
            {menu && (
              <div role="menu" className="absolute right-0 top-10 z-30 w-52 rounded-md border border-line bg-surface-primary p-1 shadow-lg" onMouseLeave={() => setMenu(false)}>
                {FORMATS.map(([f, label]) => (
                  <div key={f} className="flex items-center">
                    <button role="menuitem" className="nav-item flex-1" onClick={() => { setMenu(false); act(() => base64Download(`/sheets/${id}/export?format=${f}`)); }}>{label}</button>
                    {isDesktop && <button className="icon-btn" aria-label={`Save ${label} to a folder`} title="Save to a folder" onClick={() => { setMenu(false); saveTo(f); }}><FloppyDisk size={15} /></button>}
                  </div>
                ))}
              </div>
            )}
          </div>
          <button className="icon-btn" aria-label="Delete sheet" onClick={() => confirm(`Delete ${sheet.name} and its rows?`) &&
            act(async () => { await api(`/sheets/${id}`, { method: "DELETE" }); onClose(); })}><Trash size={16} /></button>
        </div>
      </div>
      {runner.job && <Progress value={runner.job.progress} label={runner.job.message || "Queued"} />}
      <ErrorNote error={error || runner.error} onClose={() => setError("")} />
      {note && <p className="field-ok">{note}</p>}

      <div className="flex gap-4 border-b border-line" role="tablist" aria-label="Sheet">
        {tabs.map(([t, label]) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-1 pb-2 text-sm ${tab === t ? "border-accent-400 font-medium text-text-primary" : "border-transparent text-text-muted hover:text-text-primary"}`}>{label}</button>
        ))}
      </div>

      {tab === "data" && <><Ask id={id} /><Grid sheet={sheet} onChange={load} /></>}
      {tab === "chart" && <SheetCharts sheet={sheet} />}
      {tab === "clean" && <CleanTab sheet={sheet} onChange={load} />}
      {tab === "schema" && <Schema sheet={sheet} act={act} />}
      {tab === "build" && sheet.build && <BuildTab sheet={sheet} act={act} />}
      {tab === "history" && <History id={id} />}
      {tab === "api" && <ApiTab sheet={sheet} />}
    </div>
  );
}

function Ask({ id }: { id: string }) {
  const [q, setQ] = useKept(`sheet:${id}:ask`, "");
  const task = useTask<any>(`sheet:${id}:ask`);  // the question keeps being answered if you leave the page
  const out = task.result ?? null;
  const busy = task.status === "running";
  const error = task.error ?? "";
  const setError = (_: string) => clearTask(`sheet:${id}:ask`);
  const ask = () => runTask(`sheet:${id}:ask`, `Ask a sheet: ${q.slice(0, 40)}`, () => api(`/sheets/${id}/ask`, { method: "POST", json: { question: q } }),
    { page: "Tables", params: { sheet: id } });
  const chart = out?.result ? chartable(out.result.columns, out.result.rows) : null;
  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <input className="input-field flex-1" placeholder="Ask this sheet, e.g. which rows cost the most?" aria-label="Ask this sheet" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q.trim().length > 1 && ask()} />
        <button className="btn-secondary" disabled={busy || q.trim().length < 2} onClick={ask}><Sparkle size={15} /> {busy ? "Thinking…" : "Ask"}</button>
      </div>
      <ErrorNote error={error} onClose={() => setError("")} />
      {out && (
        <div className="space-y-2">
          {out.answer && <p className="text-sm">{out.answer}</p>}
          {out.error && <p className="field-error">{out.error}</p>}
          {chart && <Chart {...chart} />}
          <details className="text-xs text-text-muted"><summary className="cursor-pointer">SQL</summary><pre className="mono mt-1 whitespace-pre-wrap">{out.sql}</pre></details>
          <button className="text-xs text-text-muted underline" onClick={() => clearTask(`sheet:${id}:ask`)}>Clear</button>
        </div>
      )}
    </div>
  );
}

function Schema({ sheet, act }: { sheet: Sheet; act: (fn: () => Promise<unknown>) => void }) {
  const [edits, setEdits] = useState<Record<string, Partial<Column>>>({});
  const val = (c: Column, k: keyof Column) => (edits[c.name]?.[k] ?? c[k]) as string;
  const set = (c: Column, k: keyof Column, v: string) => setEdits({ ...edits, [c.name]: { ...edits[c.name], [k]: v } });
  const save = (c: Column) => act(async () => {
    const e = edits[c.name] ?? {};
    await api(`/sheets/${sheet.id}/columns/${encodeURIComponent(c.name)}`, { method: "PATCH", json: { name: e.name !== c.name ? e.name : undefined, type: e.type, kind: e.kind, prompt: e.prompt } });
    setEdits({ ...edits, [c.name]: {} });
  });
  return (
    <Card className="overflow-hidden p-0">
      <div className="overflow-x-auto">
        <table className="table">
          <thead><tr><th>Column</th><th>Type</th><th>Fill</th><th>Instruction</th><th /></tr></thead>
          <tbody>
            {sheet.columns.map((c) => (
              <tr key={c.name}>
                <td><input className="input-field mono py-1" aria-label={`Name of ${c.name}`} value={val(c, "name")} onChange={(e) => set(c, "name", e.target.value)} /></td>
                <td><select className="input-field py-1" aria-label={`Type of ${c.name}`} value={val(c, "type")} onChange={(e) => set(c, "type", e.target.value)}>
                  {["text", "number", "date", "boolean", "url"].map((t) => <option key={t}>{t}</option>)}</select></td>
                <td><select className="input-field py-1" aria-label={`How ${c.name} is filled`} value={val(c, "kind")} onChange={(e) => set(c, "kind", e.target.value)}>
                  <option value="plain">By hand</option><option value="ai">AI (from the row)</option><option value="web">Web (KeiroLabs)</option></select></td>
                <td className="min-w-[16rem]"><input className="input-field py-1" aria-label={`Instruction for ${c.name}`} value={val(c, "prompt")}
                  disabled={val(c, "kind") === "plain"} onChange={(e) => set(c, "prompt", e.target.value)} /></td>
                <td className="whitespace-nowrap text-right">
                  <button className="btn-secondary py-1" disabled={!edits[c.name] || !Object.keys(edits[c.name]).length} onClick={() => save(c)}>Save</button>
                  <button className="icon-btn" aria-label={`Delete column ${c.name}`} onClick={() => confirm(`Delete column ${c.name}?`) &&
                    act(() => api(`/sheets/${sheet.id}/columns/${encodeURIComponent(c.name)}`, { method: "DELETE" }))}><Trash size={15} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function BuildTab({ sheet, act }: { sheet: Sheet; act: (fn: () => Promise<unknown>) => void }) {
  const b = sheet.build!;
  const r = b.last_result ?? {};
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card title="How it fills">
        <p className="text-sm text-text-secondary">{b.goal}</p>
        <label className="mt-3 flex items-center gap-2 text-sm">Refresh
          <select className="input-field py-1" value={b.interval_minutes} onChange={(e) => act(() => api(`/sheets/${sheet.id}/schedule`, { method: "PUT", json: { interval_minutes: +e.target.value } }))}>
            {INTERVALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <p className="mt-2 text-xs text-text-muted">Collected with KeiroLabs, zero data retention.</p>
      </Card>
      <Card title="Last run">
        {!b.last_run_at ? <p className="text-sm text-text-muted">Not run yet.</p> : (
          <div className="space-y-2 text-sm">
            <p className="text-text-secondary">{new Date(b.last_run_at).toLocaleString()} · {r.pages ?? 0} pages · {r.rows_found ?? 0} rows found
              {r.credits_used != null && ` · ${r.credits_used} credits`}</p>
            {r.replanned && <p className="text-xs text-text-muted">Steps were re-planned: {r.replanned.why}</p>}
            {r.review && <div className="md text-sm"><Markdown>{r.review}</Markdown></div>}
            {r.warnings?.length > 0 && <p className="text-xs text-coral-500">{r.warnings.join(" · ")}</p>}
          </div>
        )}
      </Card>
    </div>
  );
}

function History({ id }: { id: string }) {
  const [items, setItems] = useState<{ name: string; bytes: number; rows: number }[] | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api(`/sheets/${id}/history`).then(setItems).catch((e) => setError(errText(e))), [id]);
  useEffect(() => { load(); }, [load]);
  const when = (n: string) => { const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(n); return m ? new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`).toLocaleString() : n; };
  return (
    <Card title="Copies" actions={<button className="btn-secondary" onClick={() => api(`/sheets/${id}/history`, { method: "POST" }).then(load).catch((e) => setError(errText(e)))}><FloppyDisk size={15} /> Save a copy now</button>}>
      <ErrorNote error={error} />
      <div className="divide-y divide-line text-sm">
        {items?.map((h) => (
          <div key={h.name} className="flex items-center gap-2 py-1.5">
            <span className="flex-1">{when(h.name)}</span>
            <span className="mono text-text-muted">{h.rows} rows</span>
            <button className="btn-ghost py-1" onClick={() => base64Download(`/sheets/${id}/history/${h.name}`)}>CSV</button>
          </div>
        ))}
        {items && !items.length && <p className="text-text-muted">No copies yet. One is kept after every refresh and AI fill.</p>}
      </div>
    </Card>
  );
}

function ApiTab({ sheet }: { sheet: Sheet }) {
  const base = "http://127.0.0.1:8443";
  const lines = [
    `curl -H "Authorization: Bearer KEY" ${base}/v1/sheets/${sheet.id}/rows`,
    `curl -H "Authorization: Bearer KEY" -H "Content-Type: application/json" -X POST ${base}/v1/sheets/${sheet.id}/rows -d '{"rows":[{${sheet.columns.slice(0, 2).map((c) => `"${c.name}":"…"`).join(",")}}]}'`,
    `curl -H "Authorization: Bearer KEY" -X POST ${base}/v1/sql -d '{"sql":"select * from ${sheet.table} limit 10"}'`,
  ];
  return (
    <Card title="Use this sheet from your apps">
      <p className="mb-2 text-sm text-text-secondary">Turn on the data API in Settings → Network and make a key in Connect.</p>
      <pre className="mono overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-surface-secondary p-3">{lines.join("\n\n")}</pre>
    </Card>
  );
}
