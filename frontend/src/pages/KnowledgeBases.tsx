import { ArrowsClockwise, FolderOpen, Plus, Trash, UploadSimple } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, errText, isDesktop, KB, safeUrl, shell } from "../api";
import { Card, Empty, ErrorNote, Progress, useJobRunner, useNav } from "../ui";

type Doc = { id: string; title: string; url: string | null; chunks: number; created_at: string };
type Watch = { id: string; path: string; interval_minutes: number; last_scan_at: string | null; last_result: Record<string, any>; files: number };
type Feed = { id: string; query: string; interval_minutes: number; last_run_at: string | null };

const PLACEHOLDER: Record<string, string> = {
  search: "Topic to search the web for",
  research: "A question to research in depth",
  url: "https://…",
  site: "example.com",
};

export default function KnowledgeBases() {
  const { online } = useNav();
  const [kbs, setKbs] = useState<KB[]>([]);
  const [sel, setSel] = useState("");
  const [docs, setDocs] = useState<Doc[]>([]);
  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [watches, setWatches] = useState<Watch[]>([]);
  const [folder, setFolder] = useState("");
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("search");
  const files = useRef<HTMLInputElement>(null);

  const loadKbs = useCallback(async () => {
    const list = await api<KB[]>("/kbs");
    setKbs(list);
    setSel((s) => (list.some((k) => k.id === s) ? s : list[0]?.id ?? ""));
  }, []);
  const loadDocs = useCallback(async () => {
    setDocs(sel ? await api<Doc[]>(`/kbs/${sel}/documents`) : []);
    setFeeds(sel ? await api<Feed[]>(`/kbs/${sel}/feeds`) : []);
    setWatches(sel ? await api<Watch[]>(`/kbs/${sel}/watches`) : []);
  }, [sel]);
  const refresh = useCallback(() => { loadKbs(); loadDocs(); }, [loadKbs, loadDocs]);
  const { job, error, setError, run } = useJobRunner(refresh, "kbs");

  useEffect(() => { loadKbs(); }, [loadKbs]);
  useEffect(() => { loadDocs(); }, [loadDocs]);

  const post = (path: string, json: unknown) => () => api<{ job_id: string }>(`/kbs/${sel}${path}`, { method: "POST", json });

  async function create() {
    if (!name.trim()) return;
    try {
      const kb = await api<KB>("/kbs", { method: "POST", json: { name: name.trim() } });
      setName("");
      await loadKbs();
      setSel(kb.id);
    } catch (e) { setError(errText(e)); }
  }

  function add() {
    const q = query.trim();
    if (!q) return;
    setQuery("");
    if (mode === "search") run(post("/ingest/web-search", { query: q, mode: "content", max_results: 5 }));
    else if (mode === "research") run(post("/ingest/research", { question: q }));
    else if (mode === "site") run(post("/ingest/site", { site: q, limit: 20 }));
    else run(post("/ingest/urls", { urls: q.split(/\s+/) }));
  }

  async function follow() {
    const q = query.trim();
    if (!q) return;
    setQuery("");
    await api(`/kbs/${sel}/feeds`, { method: "POST", json: { query: q, mode: "fast", interval_minutes: 1440 } }).catch((e) => setError(errText(e)));
    loadDocs();
  }

  function upload(list: FileList | null) {
    if (!list?.length) return;
    const body = new FormData();
    [...list].forEach((f) => body.append("files", f));
    run(() => api(`/kbs/${sel}/ingest/files`, { method: "POST", body }));
    if (files.current) files.current.value = "";
  }

  const kb = kbs.find((k) => k.id === sel);
  return (
    <div className="grid gap-4 md:grid-cols-[14rem_1fr]">
      <div className="space-y-2">
        <div className="flex flex-col gap-1">
          {kbs.map((k) => (
            <button key={k.id} className={k.id === sel ? "nav-item-active" : "nav-item"} onClick={() => setSel(k.id)}>
              <span className="truncate">{k.name}</span><span className="ml-auto text-xs text-text-muted">{k.documents}</span>
            </button>
          ))}
        </div>
        <div className="flex gap-1">
          <input className="input-field" placeholder="New collection" value={name}
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
          <button className="icon-btn" aria-label="Create" disabled={!name.trim()} onClick={create}><Plus size={16} /></button>
        </div>
      </div>

      <div className="min-w-0 space-y-4">
        <ErrorNote error={error} onClose={() => setError("")} />
        {!kb ? <Card><Empty title="Create a collection">Documents, notes, saved pages and web research.</Empty></Card> : (
          <>
            <Card>
              <div className="flex flex-wrap gap-2">
                <select className="input-field" value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Source">
                  <option value="search">Web search</option>
                  <option value="research">Research</option>
                  <option value="url">URLs</option>
                  <option value="site">Website</option>
                </select>
                <input className="input-field flex-1" placeholder={PLACEHOLDER[mode]} value={query}
                  onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
                <button className="btn-primary" disabled={!!job || !online || !query.trim()} onClick={add}>Add</button>
                {mode === "search" && <button className="btn-ghost" disabled={!query.trim()} onClick={follow} title="Re-run daily">Follow</button>}
                <button className="btn-secondary" disabled={!!job} onClick={() => files.current?.click()}><UploadSimple size={16} /> Upload</button>
                <input ref={files} type="file" multiple hidden onChange={(e) => upload(e.target.files)} />
              </div>
              {!online && <p className="mt-2 text-xs text-text-muted">Offline: uploads work, web sources wait for the internet.</p>}
              {job && <div className="mt-3"><Progress value={job.progress} label={job.message || "Queued"} /></div>}
            </Card>

            <Card title={`${kb.documents} documents · ${kb.chunks} chunks`}
              actions={<span className="text-xs text-text-muted">{kb.models.map((m) => `${m.model} (${m.points})`).join(" · ")}</span>}>
              {docs.length ? (
                <div className="divide-y divide-line">
                  {docs.map((d) => (
                    <div key={d.id} className="flex items-center gap-3 py-2 text-sm">
                      <span className="min-w-0 flex-1 truncate">
                        {safeUrl(d.url) ? <a className="hover:text-accent-600" href={safeUrl(d.url)} target="_blank" rel="noreferrer">{d.title}</a> : d.title}
                      </span>
                      <span className="text-xs text-text-muted">{d.chunks} chunks</span>
                      <button className="icon-btn" aria-label={`Delete ${d.title}`}
                        onClick={async () => { await api(`/documents/${d.id}`, { method: "DELETE" }); refresh(); }}><Trash size={14} /></button>
                    </div>
                  ))}
                </div>
              ) : <p className="text-sm text-text-muted">No documents yet.</p>}
            </Card>

            {feeds.length > 0 && (
              <Card title="Followed searches">
                <div className="divide-y divide-line">
                  {feeds.map((f) => (
                    <div key={f.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                      <span className="min-w-0 flex-1 truncate">{f.query}</span>
                      <span className="text-xs text-text-muted">{f.last_run_at ? `last run ${new Date(f.last_run_at).toLocaleDateString()}` : "not run yet"}</span>
                      <button className="btn-ghost" disabled={!!job || !online} onClick={() => run(() => api(`/feeds/${f.id}/run`, { method: "POST" }))}>Run now</button>
                      <button className="btn-ghost" onClick={async () => { await api(`/feeds/${f.id}`, { method: "DELETE" }); loadDocs(); }}>Unfollow</button>
                    </div>
                  ))}
                </div>
              </Card>
            )}

            <Card title="Watched folders" actions={<span className="text-xs text-text-muted">Keeps this collection in sync with a folder or network share</span>}>
              <div className="flex flex-wrap gap-2">
                <input className="input-field flex-1" placeholder="Full folder path" value={folder} onChange={(e) => setFolder(e.target.value)} />
                {isDesktop && <button className="btn-ghost" onClick={async () => { const dir = await shell<string | null>("pick_folder"); if (dir) setFolder(dir); }}>Choose…</button>}
                <button className="btn-secondary" disabled={!folder.trim() || !!job}
                  onClick={() => run(async () => { const r = await api(`/kbs/${sel}/watches`, { method: "POST", json: { path: folder.trim() } }); setFolder(""); return r; })}>
                  <FolderOpen size={16} /> Watch
                </button>
              </div>
              {watches.length > 0 && (
                <div className="mt-3 divide-y divide-line">
                  {watches.map((w) => (
                    <div key={w.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                      <span className="min-w-0 flex-1 truncate" title={w.path}>{w.path}</span>
                      <span className="text-xs text-text-muted">
                        {w.files} files{w.last_scan_at ? ` · scanned ${new Date(w.last_scan_at).toLocaleTimeString()}` : " · not scanned yet"}
                        {w.last_result?.added != null && ` · +${w.last_result.added} ~${w.last_result.changed} -${w.last_result.removed}`}
                      </span>
                      <button className="icon-btn" aria-label="Scan now" disabled={!!job} onClick={() => run(() => api(`/watches/${w.id}/scan`, { method: "POST" }))}><ArrowsClockwise size={14} /></button>
                      <button className="icon-btn" aria-label="Stop watching" onClick={async () => { await api(`/watches/${w.id}`, { method: "DELETE" }); loadDocs(); }}><Trash size={14} /></button>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <div className="flex flex-wrap gap-2">
              <button className="btn-ghost" disabled={!!job} onClick={() => run(() => api(`/kbs/${sel}/reembed`, { method: "POST" }))}>Re-embed</button>
              <button className="btn-danger" onClick={async () => {
                if (confirm(`Delete "${kb.name}" and all its data?`)) { await api(`/kbs/${sel}`, { method: "DELETE" }); loadKbs(); }
              }}>Delete collection</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
