import { ArrowDown, ArrowUp, Binoculars, Globe, MagnifyingGlass, Plus, Sparkle, Trash } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, errText, safeUrl } from "../../api";
import { ErrorNote, Progress, useJobRunner, useNav } from "../../ui";
import { CellMeta, Column, Row, Sheet } from "./types";

const PAGE = 100;
type Page = { columns: string[]; rows: Row[]; total: number; offset: number };

const dotClass = (m: CellMeta) => (m.confidence == null ? "bg-text-muted" : m.confidence >= 0.75 ? "bg-sage-400" : m.confidence >= 0.4 ? "bg-cream-500" : "bg-coral-400");
const show = (v: any) => (v == null ? "" : typeof v === "number" && !Number.isInteger(v) ? String(Math.round(v * 1000) / 1000) : String(v));

/** A spreadsheet grid over one sheet: edit in place, sort, search, page, add rows and columns, fill AI and web columns. */
export default function Grid({ sheet, onChange }: { sheet: Sheet; onChange: () => void }) {
  const [page, setPage] = useState<Page | null>(null);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<{ col: string; desc: boolean } | null>(null);
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const [edit, setEdit] = useState<{ row: number; col: string; value: string } | null>(null);
  const [sources, setSources] = useState<{ row: number; col: string } | null>(null);
  const [addCol, setAddCol] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const cols: Column[] = sheet.columns;
  const { go } = useNav();

  /** Research one row; the result is a saved conversation in Ask. */
  const dive = (r: Row) => act(async () => {
    const out = await api<{ conversation_id: string }>(`/sheets/${sheet.id}/rows/${r._id}/dive`, { method: "POST", json: { depth: "standard" } });
    try { localStorage.setItem("conv", out.conversation_id); } catch { /* ignore */ }
    go({ page: "Ask" });
  });

  const load = useCallback(async () => {
    const qs = new URLSearchParams({ offset: String(offset), limit: String(PAGE), q, ...(sort ? { sort: sort.col, desc: String(sort.desc) } : {}) });
    try { setPage(await api<Page>(`/sheets/${sheet.id}/rows?${qs}`)); } catch (e) { setError(errText(e)); }
  }, [sheet.id, offset, q, sort]);
  const filler = useJobRunner(() => { load(); onChange(); }, `fill:${sheet.id}`);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (edit) input.current?.focus(); }, [edit?.row, edit?.col]);

  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } load(); };
  const save = async (next?: { row: number; col: string }) => {
    if (!edit) return;
    const row = page?.rows.find((r) => r._id === edit.row);
    if (row && show(row[edit.col]) !== edit.value) {
      try { await api(`/sheets/${sheet.id}/rows/${edit.row}`, { method: "PATCH", json: { column: edit.col, value: edit.value } }); }
      catch (e) { setError(errText(e)); return; }
      await load();
    }
    setEdit(null);
    if (next && page) {
      const r = page.rows.find((x) => x._id === next.row);
      if (r) setEdit({ row: next.row, col: next.col, value: show(r[next.col]) });
    }
  };
  const startEdit = (r: Row, col: string) => setEdit({ row: r._id, col, value: show(r[col]) });
  const toggleSort = (col: string) => setSort(sort?.col === col ? (sort.desc ? null : { col, desc: true }) : { col, desc: false });

  async function fillColumn(c: Column) {
    try {
      const est = await api<{ rows: number; keirolabs_calls: number }>(`/sheets/${sheet.id}/columns/${encodeURIComponent(c.name)}/estimate`);
      if (!est.rows) { setError(`Every row already has a ${c.name}.`); return; }
      const cost = c.kind === "web" ? ` It makes ${est.keirolabs_calls} KeiroLabs searches (zero data retention).` : " It uses the model on this computer first.";
      if (!confirm(`Fill ${c.name} for ${est.rows} rows?${cost}`)) return;
      filler.run(() => api(`/sheets/${sheet.id}/columns/${encodeURIComponent(c.name)}/fill`, { method: "POST", json: { only_empty: true } }));
    } catch (e) { setError(errText(e)); }
  }

  const rows = page?.rows ?? [];
  const meta = sources && rows.find((r) => r._id === sources.row)?._meta?.[sources.col];
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative min-w-0 flex-1 sm:max-w-xs">
          <MagnifyingGlass size={15} className="pointer-events-none absolute left-2.5 top-2.5 text-text-muted" />
          <input className="input-field pl-8" placeholder="Filter rows" aria-label="Filter rows" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        </label>
        <button className="btn-secondary" onClick={() => act(async () => {
          const { ids } = await api<{ ids: number[] }>(`/sheets/${sheet.id}/rows`, { method: "POST", json: { rows: [{}] } });
          setSort(null); setQ("");
          const total = (page?.total ?? 0) + 1;
          setOffset(Math.floor((total - 1) / PAGE) * PAGE);
          onChange();
          setTimeout(() => setEdit({ row: ids[0], col: cols[0].name, value: "" }), 300);
        })}><Plus size={15} /> Row</button>
        <button className="btn-secondary" onClick={() => setAddCol(!addCol)}><Plus size={15} /> Column</button>
        {picked.length > 0 && <button className="btn-danger" onClick={() => confirm(`Delete ${picked.length} row(s)?`) &&
          act(async () => { await api(`/sheets/${sheet.id}/rows/delete`, { method: "POST", json: { ids: picked } }); setPicked([]); onChange(); })}>
          <Trash size={15} /> Delete {picked.length}</button>}
        <span className="mono ml-auto text-text-muted">{page ? `${page.total ? page.offset + 1 : 0}–${page.offset + rows.length} of ${page.total.toLocaleString()}` : ""}</span>
        <button className="btn-ghost px-2" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))} aria-label="Previous page">‹</button>
        <button className="btn-ghost px-2" disabled={!page || offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)} aria-label="Next page">›</button>
      </div>
      {addCol && <AddColumn sheetId={sheet.id} onDone={() => { setAddCol(false); onChange(); load(); }} />}
      {filler.job && <Progress value={filler.job.progress} label={filler.job.message || "Starting"} />}
      <ErrorNote error={error || filler.error} onClose={() => setError("")} />

      <div className="max-h-[calc(100vh-22rem)] min-h-[12rem] overflow-auto rounded-md border border-line bg-surface-primary">
        <table className="table min-w-max">
          <thead>
            <tr>
              <th className="w-8"><input type="checkbox" aria-label="Select all rows" checked={rows.length > 0 && picked.length === rows.length}
                onChange={(e) => setPicked(e.target.checked ? rows.map((r) => r._id) : [])} /></th>
              {cols.map((c) => (
                <th key={c.name} className="whitespace-nowrap">
                  <span className="flex items-center gap-1">
                    <button className="flex items-center gap-1 hover:text-text-primary" onClick={() => toggleSort(c.name)} title={c.prompt || c.type}>
                      {c.kind === "ai" && <Sparkle size={12} className="text-accent-600" />}
                      {c.kind === "web" && <Globe size={12} className="text-accent-600" />}
                      <span className="mono">{c.name}</span>
                      {sort?.col === c.name && (sort.desc ? <ArrowDown size={11} /> : <ArrowUp size={11} />)}
                    </button>
                    {c.kind !== "plain" && <button className="rounded px-1 text-[11px] text-accent-600 hover:bg-accent-50" disabled={!!filler.job}
                      onClick={() => fillColumn(c)}>Fill</button>}
                  </span>
                </th>
              ))}
              <th className="w-10"><span className="sr-only">Row actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r._id}>
                <td><input type="checkbox" aria-label={`Select row ${r._id}`} checked={picked.includes(r._id)}
                  onChange={(e) => setPicked(e.target.checked ? [...picked, r._id] : picked.filter((x) => x !== r._id))} /></td>
                {cols.map((c, ci) => {
                  const m = r._meta?.[c.name];
                  const editing = edit?.row === r._id && edit.col === c.name;
                  return (
                    <td key={c.name} className={`max-w-[22rem] p-0 ${c.type === "number" ? "text-right" : ""}`}>
                      {editing ? (
                        <input ref={input} className="mono w-full min-w-[8rem] border-0 bg-accent-50 px-3 py-1.5 outline-none ring-2 ring-inset ring-accent-400"
                          aria-label={`${c.name}, row ${r._id}`} value={edit.value} onChange={(e) => setEdit({ ...edit, value: e.target.value })}
                          onBlur={() => save()}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") { e.preventDefault(); save(); }
                            else if (e.key === "Escape") setEdit(null);
                            else if (e.key === "Tab") { e.preventDefault(); const nc = cols[ci + (e.shiftKey ? -1 : 1)]; save(nc ? { row: r._id, col: nc.name } : undefined); }
                          }} />
                      ) : (
                        <div role="gridcell" tabIndex={0} className="flex min-h-[2rem] cursor-text items-center gap-1.5 px-3 py-1.5 focus:outline focus:outline-2 focus:outline-accent-400"
                          onDoubleClick={() => startEdit(r, c.name)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === "F2") { e.preventDefault(); startEdit(r, c.name); } }}
                          title={show(r[c.name])}>
                          <span className={`min-w-0 flex-1 truncate ${c.type === "number" ? "mono" : ""}`}>
                            {c.type === "url" && safeUrl(r[c.name]) ? <a className="text-accent-600 hover:underline" href={safeUrl(r[c.name])} target="_blank" rel="noreferrer">{show(r[c.name])}</a> : show(r[c.name])}
                          </span>
                          {m && <button className={`status-dot ${dotClass(m)}`} aria-label={`Sources for ${c.name}`}
                            title={`${m.model}${m.confidence != null ? ` · confidence ${Math.round(m.confidence * 100)}%` : ""}`}
                            onClick={() => setSources(sources?.row === r._id && sources.col === c.name ? null : { row: r._id, col: c.name })} />}
                        </div>
                      )}
                    </td>
                  );
                })}
                <td className="p-0 text-center">
                  <button className="icon-btn" aria-label={`Deep dive into row ${r._id}`} title="Deep dive: research this row" onClick={() => dive(r)}><Binoculars size={15} /></button>
                </td>
              </tr>
            ))}
            {page && !rows.length && <tr><td colSpan={cols.length + 2} className="py-8 text-center text-text-muted">{q ? "No matching rows" : "No rows yet"}</td></tr>}
          </tbody>
        </table>
      </div>
      {meta && sources && (
        <div className="rounded-md border border-line bg-surface-primary p-3 text-sm" role="dialog" aria-label="Cell sources">
          <p className="mb-1 font-medium">{sources.col} · {meta.model}{meta.confidence != null ? ` · ${Math.round(meta.confidence * 100)}% sure` : ""}</p>
          {meta.sources.length ? meta.sources.map((u) => <a key={u} className="block truncate text-accent-600 hover:underline" href={safeUrl(u)} target="_blank" rel="noreferrer">{u}</a>)
            : <p className="text-text-muted">Filled from the row itself.</p>}
          <button className="btn-ghost mt-1 px-0" onClick={() => setSources(null)}>Close</button>
        </div>
      )}
    </div>
  );
}

function AddColumn({ sheetId, onDone }: { sheetId: string; onDone: () => void }) {
  const [name, setName] = useState("");
  const [type, setType] = useState("text");
  const [kind, setKind] = useState("plain");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const add = async () => {
    setError("");
    try { await api(`/sheets/${sheetId}/columns`, { method: "POST", json: { name, type, kind, prompt } }); onDone(); }
    catch (e) { setError(errText(e)); }
  };
  return (
    <div className="rounded-md border border-line bg-surface-primary p-3">
      <div className="grid gap-2 md:grid-cols-[10rem_7rem_11rem_1fr_auto]">
        <input className="input-field" placeholder="Column name" aria-label="Column name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        <select className="input-field" aria-label="Type" value={type} onChange={(e) => setType(e.target.value)}>
          {["text", "number", "date", "boolean", "url"].map((t) => <option key={t}>{t}</option>)}
        </select>
        <select className="input-field" aria-label="How it is filled" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="plain">By hand</option><option value="ai">AI, from the row</option><option value="web">Web, via KeiroLabs</option>
        </select>
        <input className="input-field" placeholder={kind === "plain" ? "" : "Instruction, e.g. the company's founding year"} aria-label="Instruction"
          disabled={kind === "plain"} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <div className="flex gap-2">
          <button className="btn-primary" disabled={!name.trim() || (kind !== "plain" && !prompt.trim())} onClick={add}>Add</button>
          <button className="btn-ghost" onClick={onDone}>Cancel</button>
        </div>
      </div>
      <ErrorNote error={error} />
    </div>
  );
}
