import { ArrowClockwise, CaretDown, DownloadSimple, FileArrowUp, Globe, MagnifyingGlass, Plus, Sparkle, Table as TableIcon, Trash } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, base64Download, errText } from "../../api";
import { useFinishedJobs } from "../../background";
import { Card, Empty, ErrorNote, useKept, useNav, usePoll } from "../../ui";
import Describe from "./Describe";
import FromPages from "./FromPages";
import OpenData from "./OpenData";
import SheetView from "./SheetView";
import { intervalLabel, Sheet, SOURCE_LABEL } from "./types";

/** The sheets in the lake, as a resource table like Docker Desktop's Containers. */
export default function Sheets() {
  const { params } = useNav();
  const [sheets, reload] = usePoll<Sheet[]>("/sheets", 10000);
  const landed = useFinishedJobs();
  useEffect(() => { if (landed) reload(); }, [landed]);  // a fill or import just finished somewhere: show it now
  const [open, setOpen] = useKept<string | null>("lake:sheet", null);
  const [mode, setMode] = useState<"" | "blank" | "describe" | "opendata" | "pages">(params.new === "describe" ? "describe" : params.new === "sheet" ? "blank" : "");
  const [q, setQ] = useKept("lake:q", "");
  const [picked, setPicked] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [menu, setMenu] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => { if (params.sheet) setOpen(params.sheet); }, [params.sheet]);
  useEffect(() => { if (params.new === "import") file.current?.click(); }, [params.new]);

  const shown = useMemo(() => (sheets ?? []).filter((s) => !q || `${s.name} ${s.table} ${s.description}`.toLowerCase().includes(q.toLowerCase())), [sheets, q]);
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); };

  async function upload(f?: File) {
    if (!f) return;
    await act(async () => {
      const body = new FormData();
      body.append("file", f);
      const s = await api<Sheet>("/sheets/import", { method: "POST", body });
      setOpen(s.id);
    });
  }

  if (open && sheets?.some((s) => s.id === open)) return <SheetView id={open} onClose={() => { setOpen(null); reload(); }} />;
  if (mode === "pages") return <FromPages onDone={(id) => { setMode(""); reload(); if (id) setOpen(id); }} onCancel={() => setMode("")} />;
  if (mode === "opendata") return <OpenData onDone={(id) => { setMode(""); reload(); if (id) setOpen(id); }} onCancel={() => setMode("")} />;
  if (mode === "describe") return <Describe onDone={(id) => { setMode(""); reload(); if (id) setOpen(id); }} onCancel={() => setMode("")} />;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative min-w-0 flex-1 sm:max-w-sm">
          <MagnifyingGlass size={15} className="pointer-events-none absolute left-2.5 top-2.5 text-text-muted" />
          <input className="input-field pl-8" placeholder="Search sheets" aria-label="Search sheets" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        {picked.length > 0 && (
          <button className="btn-danger" onClick={() => confirm(`Delete ${picked.length} sheet(s) and their rows?`) &&
            act(async () => { for (const id of picked) await api(`/sheets/${id}`, { method: "DELETE" }); setPicked([]); })}>
            <Trash size={15} /> Delete {picked.length}
          </button>
        )}
        <div className="relative ml-auto">
          <button className="btn-primary" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu(!menu)}><Plus size={15} /> Create <CaretDown size={12} /></button>
          {menu && (
            <div role="menu" className="absolute right-0 top-10 z-30 w-56 rounded-md border border-line bg-surface-primary p-1 shadow-lg" onMouseLeave={() => setMenu(false)}>
              <button role="menuitem" className="nav-item w-full" onClick={() => { setMenu(false); setMode("blank"); }}><TableIcon size={16} /> Blank sheet</button>
              <button role="menuitem" className="nav-item w-full" onClick={() => { setMenu(false); file.current?.click(); }}><FileArrowUp size={16} /> Import a file</button>
              <button role="menuitem" className="nav-item w-full" onClick={() => { setMenu(false); setMode("describe"); }}><Sparkle size={16} /> Describe it</button>
              <button role="menuitem" className="nav-item w-full" onClick={() => { setMenu(false); setMode("pages"); }}><Globe size={16} /> From web pages</button>
              <button role="menuitem" className="nav-item w-full" onClick={() => { setMenu(false); setMode("opendata"); }}><Globe size={16} /> From open data</button>
            </div>
          )}
          <input ref={file} type="file" hidden accept=".csv,.tsv,.xlsx,.xls,.json,.jsonl" onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ""; }} />
        </div>
      </div>
      <ErrorNote error={error} onClose={() => setError("")} />
      {mode === "blank" && <BlankSheet onDone={(id) => { setMode(""); reload(); if (id) setOpen(id); }} />}

      <Card className="overflow-hidden p-0">
        {!sheets ? <div className="skeleton m-4 h-32" /> : !shown.length ? (
          <Empty title={q ? "No matching sheets" : "No sheets yet"}>
            {!q && <button className="btn-primary mt-2" onClick={() => setMode("blank")}><Plus size={15} /> Create a sheet</button>}
          </Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th className="w-8"><input type="checkbox" aria-label="Select all" checked={picked.length === shown.length}
                    onChange={(e) => setPicked(e.target.checked ? shown.map((s) => s.id) : [])} /></th>
                  <th>Name</th><th>Rows</th><th>Columns</th><th>Source</th><th>Refresh</th><th>Updated</th><th className="text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((s) => (
                  <tr key={s.id}>
                    <td><input type="checkbox" aria-label={`Select ${s.name}`} checked={picked.includes(s.id)}
                      onChange={(e) => setPicked(e.target.checked ? [...picked, s.id] : picked.filter((x) => x !== s.id))} /></td>
                    <td className="max-w-xs">
                      <button className="block max-w-full truncate text-left font-medium text-accent-600 hover:underline" onClick={() => setOpen(s.id)}>{s.name}</button>
                      <span className="mono block truncate text-text-muted">{s.table}</span>
                    </td>
                    <td className="mono">{(s.rows ?? 0).toLocaleString()}</td>
                    <td className="mono">{s.columns.length}</td>
                    <td><span className={s.source === "build" ? "badge-ok" : "badge-muted"}>{SOURCE_LABEL[s.source] ?? s.source}</span></td>
                    <td className="whitespace-nowrap text-text-muted">{s.build ? intervalLabel(s.build.interval_minutes) : "—"}</td>
                    <td className="whitespace-nowrap text-text-muted">{new Date(s.updated_at).toLocaleDateString()}</td>
                    <td className="whitespace-nowrap text-right">
                      {s.build && <button className="icon-btn" aria-label={`Refresh ${s.name}`} title="Refresh now"
                        onClick={() => act(() => api(`/sheets/${s.id}/refresh`, { method: "POST" }))}><ArrowClockwise size={16} /></button>}
                      <button className="icon-btn" aria-label={`Export ${s.name} as CSV`} title="Export CSV"
                        onClick={() => act(() => base64Download(`/sheets/${s.id}/export?format=csv`))}><DownloadSimple size={16} /></button>
                      <button className="icon-btn" aria-label={`Delete ${s.name}`} title="Delete"
                        onClick={() => confirm(`Delete ${s.name} and its rows?`) && act(() => api(`/sheets/${s.id}`, { method: "DELETE" }))}><Trash size={16} /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function BlankSheet({ onDone }: { onDone: (id?: string) => void }) {
  const [name, setName] = useState("");
  const [cols, setCols] = useState("name, notes");
  const [error, setError] = useState("");
  const create = async () => {
    setError("");
    try {
      const columns = cols.split(",").map((c) => c.trim()).filter(Boolean).map((c) => ({ name: c, type: "text" }));
      const s = await api<Sheet>("/sheets", { method: "POST", json: { name, columns } });
      onDone(s.id);
    } catch (e) { setError(errText(e)); }
  };
  return (
    <Card title="New sheet">
      <div className="grid gap-2 sm:grid-cols-[1fr_2fr_auto]">
        <input className="input-field" placeholder="Name" aria-label="Sheet name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        <input className="input-field" placeholder="Columns, separated by commas" aria-label="Columns" value={cols} onChange={(e) => setCols(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && name.trim() && create()} />
        <div className="flex gap-2">
          <button className="btn-primary" disabled={!name.trim()} onClick={create}>Create</button>
          <button className="btn-ghost" onClick={() => onDone()}>Cancel</button>
        </div>
      </div>
      <ErrorNote error={error} />
    </Card>
  );
}
