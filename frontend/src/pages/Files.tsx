import { ArrowUp, DownloadSimple, File, FileArrowUp, Folder, FolderOpen } from "@phosphor-icons/react";
import { useRef, useState } from "react";
import { api, base64Download, errText, isDesktop, shell } from "../api";
import { Empty, ErrorNote, PageHeader, useKept, usePoll } from "../ui";

type Entry = { name: string; path: string; dir: boolean; bytes: number | null; modified: number };
type Listing = { path: string; zones: string[]; entries: Entry[]; folder: string };

const size = (b: number | null) => (b == null ? "" : b > 2 ** 20 ? `${(b / 2 ** 20).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
const ZONE_NOTE: Record<string, string> = { raw: "Exactly as it arrived", clean: "Parsed and tidied", curated: "Exports, topic CSVs and table history" };

/** The lake's folders on disk: what came in (raw), what was tidied (clean), and what was made from it (curated). */
export default function Files() {
  const [path, setPath] = useKept("files:path", "");
  const [list, reload] = usePoll<Listing>(`/lake/files?path=${encodeURIComponent(path)}`, 15000, [path]);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const parts = path ? path.split("/") : [];
  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    setError(""); setNote("");
    try {
      for (const f of Array.from(files)) {
        const form = new FormData();
        form.append("file", f);
        await api("/sheets/import", { method: "POST", body: form });
      }
      setNote(`${files.length} file${files.length === 1 ? "" : "s"} added as tables. The originals are kept in raw.`);
    } catch (e) { setError(errText(e)); }
    reload();
  };
  return (
    <div className="space-y-3">
      <PageHeader title="Files">
        {isDesktop && list && <button className="btn-ghost" onClick={() => shell("open_data_folder", { sub: "lake" }).catch(() => {})}><FolderOpen size={14} /> Show in folder</button>}
        <button className="btn-primary" onClick={() => input.current?.click()}><FileArrowUp size={14} /> Add a table file</button>
        <input ref={input} type="file" hidden aria-label="Table files" multiple accept=".csv,.tsv,.xlsx,.xls,.json,.jsonl" onChange={(e) => upload(e.target.files)} />
      </PageHeader>
      <ErrorNote error={error} onClose={() => setError("")} />
      {note && <p className="field-ok" role="status">{note}</p>}
      <nav aria-label="Folder" className="flex flex-wrap items-center gap-1 text-sm">
        <button className="text-accent-600 hover:underline" onClick={() => setPath("")}>Lake</button>
        {parts.map((p, i) => <span key={i} className="flex items-center gap-1"><span className="text-text-muted">/</span>
          <button className="text-accent-600 hover:underline" onClick={() => setPath(parts.slice(0, i + 1).join("/"))}>{p}</button></span>)}
      </nav>
      {!list ? <div className="skeleton h-40" /> : !list.entries.length ? <Empty title="This folder is empty" /> : (
        <div className="card overflow-x-auto p-0">
          <table className="table">
            <thead><tr><th>Name</th><th className="w-28">Size</th><th className="w-44">Changed</th><th className="w-12"><span className="sr-only">Download</span></th></tr></thead>
            <tbody>
              {path && <tr><td colSpan={4}><button className="flex items-center gap-2 text-text-secondary hover:text-text-primary" onClick={() => setPath(parts.slice(0, -1).join("/"))}><ArrowUp size={14} />Up</button></td></tr>}
              {list.entries.map((e) => (
                <tr key={e.path}>
                  <td>{e.dir ? <button className="flex items-center gap-2 font-medium hover:text-accent-600" onClick={() => setPath(e.path)}><Folder size={16} className="text-accent-600" />{e.name}
                    {!path && ZONE_NOTE[e.name] && <span className="font-normal text-text-muted">· {ZONE_NOTE[e.name]}</span>}</button>
                    : <span className="flex items-center gap-2"><File size={16} className="text-text-muted" /><span className="mono truncate">{e.name}</span></span>}</td>
                  <td className="tabular-nums text-text-muted">{size(e.bytes)}</td>
                  <td className="text-text-muted">{new Date(e.modified * 1000).toLocaleString()}</td>
                  <td>{!e.dir && <button className="icon-btn" aria-label={`Download ${e.name}`} onClick={() => base64Download(`/lake/file?path=${encodeURIComponent(e.path)}`).catch((x) => setError(errText(x)))}><DownloadSimple size={15} /></button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
