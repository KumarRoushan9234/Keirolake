import { ArrowClockwise, BookOpen, DownloadSimple, FolderOpen, MagnifyingGlass, Pause, ShieldCheck, Trash } from "@phosphor-icons/react";
import Markdown from "react-markdown";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errText, gb, Job, Library as Lib } from "../api";
import { Card, Empty, ErrorNote, PageHeader, Progress, useKept, useNav, usePoll } from "../ui";

type Entry = {
  name: string; title: string; summary: string; category: string; articles: number; size_bytes: number;
  issued: string; url: string; installed?: string | null; essential: boolean; fulltext: boolean; pictures: boolean;
};

export function Offline() {
  const { go, online } = useNav();
  const [libs, reload] = usePoll<Lib[]>("/libraries", 3000);
  const [jobs] = usePoll<Job[]>("/jobs?limit=40", 2000);
  const [hw] = usePoll<any>("/local/hardware", 60000);
  const [updates] = usePoll<{ id: string; current: string; available: string; size_bytes: number }[]>("/libraries-updates", 600000);
  const [error, setError] = useState("");
  const [browse, setBrowse] = useState(false);
  const [path, setPath] = useState("");

  const running = useMemo(() => (jobs ?? []).filter((j) => j.status === "running" || j.status === "queued"), [jobs]);
  const updateFor = (lib: Lib) => updates?.find((u) => u.id === lib.id);
  const jobFor = (lib: Lib) => running.find((j) => j.title?.includes(lib.title || lib.name));
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); } catch (e) { setError(errText(e)); } reload(); };
  const free = hw?.hardware?.disk_free_gb;
  const rec = hw?.recommendation;

  return (
    <div className="space-y-4">
      <ErrorNote error={error} onClose={() => setError("")} />
      {libs?.some((l) => l.status === "ready" && l.enabled) && <Find />}
      {libs && !libs.length && !browse && (
        <Card>
          <Empty title="No offline libraries yet">
            Download Wikipedia and practical guides so your lake answers without internet.
          </Empty>
          <div className="flex flex-wrap justify-center gap-2 pb-4">
            {rec?.wikipedia && (
              <button className="btn-primary" disabled={!online}
                onClick={() => act(() => api("/libraries/download", { method: "POST", json: { name: rec.wikipedia.name } }))}>
                <DownloadSimple size={16} /> {rec.wikipedia.label} · {rec.wikipedia.size_gb} GB
              </button>
            )}
            <button className="btn-secondary" disabled={!online} onClick={() => act(() => api("/libraries/essentials", { method: "POST" }))}>
              Practical essentials · ~{rec?.essentials_gb ?? 25} GB
            </button>
            <button className="btn-ghost" onClick={() => setBrowse(true)}>Browse all</button>
          </div>
          {free != null && <p className="text-center text-xs text-text-muted">{free} GB free on the library drive</p>}
        </Card>
      )}

      {libs?.map((lib) => {
        const job = jobFor(lib);
        const fullyIndexed = lib.message === "Index complete";
        return (
          <Card key={lib.id}
            title={<span className="flex items-center gap-2"><BookOpen size={16} className="text-accent-400" />{lib.title || lib.name}</span>}
            actions={<>
              {lib.status === "ready" && <label className="flex items-center gap-2 text-xs text-text-secondary">
                <input type="checkbox" className="accent-accent-400" checked={lib.enabled}
                  onChange={(e) => act(() => api(`/libraries/${lib.id}`, { method: "PATCH", json: { enabled: e.target.checked } }))} />
                Use in Ask</label>}
              {updateFor(lib) && <span className="badge-warn" title={`Newer snapshot ${updateFor(lib)!.available}`}>Update available</span>}
              <span className={lib.status === "ready" ? "badge-ok" : lib.status === "failed" ? "badge-bad" : "badge-warn"}>{lib.status}</span>
            </>}>
            <p className="text-sm text-text-secondary">{lib.summary}</p>
            <p className="mt-1 text-xs text-text-muted">
              {gb(lib.size_bytes)} · {lib.articles.toLocaleString()} articles{lib.issued ? ` · ${lib.issued}` : ""}
              {lib.index_tier > 0 && ` · ${lib.indexed.toLocaleString()} ${lib.index_tier === 1 ? "intros" : "passages"} indexed`}
              {lib.message && ` · ${lib.message}`}
            </p>
            {job && <div className="mt-3"><Progress value={job.progress} label={job.message || "Queued"} /></div>}
            {lib.status === "ready" && (
              <div className="mt-3 flex flex-wrap gap-2">
                <button className="btn-secondary" onClick={() => go({ page: "Reader", params: { lib: lib.id } })}>Open</button>
                {updateFor(lib) && (
                  <button className="btn-ghost" disabled={!online || !!job} title={`${gb(updateFor(lib)!.size_bytes)}; the current version stays usable meanwhile`}
                    onClick={() => act(() => api(`/libraries/${lib.id}/update`, { method: "POST" }))}>
                    <ArrowClockwise size={14} /> Update to {updateFor(lib)!.available}
                  </button>
                )}
                {job?.title?.startsWith("Index")
                  ? <button className="btn-ghost" onClick={() => act(() => api(`/libraries/${lib.id}/index/pause`, { method: "POST" }))}><Pause size={14} /> Pause</button>
                  : <>
                    <button className="btn-ghost" disabled={!!job || (lib.index_tier === 1 && fullyIndexed)} title="Semantic search over every article intro"
                      onClick={() => act(() => api(`/libraries/${lib.id}/index`, { method: "POST", json: { tier: 1 } }))}>
                      {lib.index_tier === 1 && !fullyIndexed ? "Resume intro index" : "Index intros"}
                    </button>
                    <button className="btn-ghost" disabled={!!job || (lib.index_tier === 2 && fullyIndexed)} title="Index every passage (slow, large)"
                      onClick={() => act(() => api(`/libraries/${lib.id}/index`, { method: "POST", json: { tier: 2 } }))}>
                      {lib.index_tier === 2 && !fullyIndexed ? "Resume passage index" : "Index passages"}
                    </button>
                  </>}
                {lib.index_tier > 0 && !job && <button className="btn-ghost" onClick={() => confirm("Delete this library's index?") && act(() => api(`/libraries/${lib.id}/index`, { method: "DELETE" }))}>Reset index</button>}
                <button className="icon-btn" aria-label="Check file" title="Verify checksum" disabled={!!job}
                  onClick={() => act(() => api(`/libraries/${lib.id}/check`, { method: "POST" }))}><ShieldCheck size={16} /></button>
                <button className="icon-btn" aria-label="Remove" title="Remove"
                  onClick={() => confirm(`Remove ${lib.title || lib.name} and delete its file?`) && act(() => api(`/libraries/${lib.id}`, { method: "DELETE" }))}><Trash size={16} /></button>
              </div>
            )}
            {lib.status === "failed" && (
              <div className="mt-3 flex gap-2">
                <button className="btn-secondary" disabled={!online} onClick={() => act(() => api("/libraries/download", { method: "POST", json: { name: lib.name } }))}><ArrowClockwise size={14} /> Retry</button>
                <button className="btn-ghost" onClick={() => act(() => api(`/libraries/${lib.id}`, { method: "DELETE" }))}>Remove</button>
              </div>
            )}
          </Card>
        );
      })}

      {libs && (libs.length > 0 || browse) && (
        <div className="flex flex-wrap gap-2">
          <button className={browse ? "btn-secondary" : "btn-ghost"} onClick={() => setBrowse(!browse)}>{browse ? "Hide catalog" : "Get more libraries"}</button>
        </div>
      )}
      {browse && <Catalog onQueued={reload} free={free} />}

      <Card title="Add a file you already have">
        <div className="flex flex-wrap gap-2">
          <input className="input-field flex-1" placeholder="Path to a .zim file" value={path} onChange={(e) => setPath(e.target.value)} />
          <button className="btn-secondary" disabled={!path.trim()} onClick={() => act(async () => { await api("/libraries/register", { method: "POST", json: { path: path.trim() } }); setPath(""); })}>
            <FolderOpen size={16} /> Add
          </button>
        </div>
      </Card>
    </div>
  );
}

function Catalog({ onQueued, free }: { onQueued: () => void; free?: number }) {
  const { online } = useNav();
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [data, setData] = useState<{ entries: Entry[]; categories: string[]; error?: string } | null>(null);
  const [error, setError] = useState("");
  const [onlyEssentials, setOnlyEssentials] = useState(false);

  const load = useCallback(async (refresh = false) => {
    try { setData(await api(`/library/catalog?q=${encodeURIComponent(q)}&category=${encodeURIComponent(category)}&refresh=${refresh}`)); }
    catch (e) { setError(errText(e)); }
  }, [q, category]);
  useEffect(() => { const t = setTimeout(() => load(), 250); return () => clearTimeout(t); }, [load]);

  const get = async (name: string) => {
    setError("");
    try { await api("/libraries/download", { method: "POST", json: { name } }); onQueued(); load(); }
    catch (e) { setError(errText(e)); }
  };
  const entries = (data?.entries ?? []).filter((e) => !onlyEssentials || e.essential);

  return (
    <Card title="Kiwix catalog" actions={<>
      {free != null && <span className="text-xs text-text-muted">{free} GB free</span>}
      <button className="btn-ghost" disabled={!online} onClick={() => load(true)}><ArrowClockwise size={14} /> Refresh</button>
    </>}>
      <div className="mb-3 flex flex-wrap gap-2">
        <input className="input-field flex-1" placeholder="Search: wikipedia, medicine, repair…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="input-field" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
          <option value="">All categories</option>
          {data?.categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <button className={onlyEssentials ? "chip-active" : "chip"} onClick={() => setOnlyEssentials(!onlyEssentials)}>Essentials</button>
      </div>
      <ErrorNote error={error || data?.error || ""} />
      <div className="max-h-[32rem] divide-y divide-line overflow-y-auto">
        {entries.map((e) => (
          <div key={e.name} className="flex items-start gap-3 py-3">
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold">{e.title} {e.essential && <span className="badge-ok ml-1">Essential</span>}</p>
              <p className="line-clamp-2 text-xs text-text-secondary">{e.summary}</p>
              <p className="mt-0.5 text-xs text-text-muted">
                {gb(e.size_bytes)} · {e.articles.toLocaleString()} articles · {e.issued}{e.fulltext ? "" : " · no search index"}
              </p>
            </div>
            {e.installed
              ? <span className="badge-muted shrink-0">{e.installed}</span>
              : <button className="btn-secondary shrink-0" disabled={!online || (free != null && e.size_bytes / 2 ** 30 > free)}
                  title={free != null && e.size_bytes / 2 ** 30 > free ? "Not enough free space" : undefined}
                  onClick={() => get(e.name)}><DownloadSimple size={14} /> Get</button>}
          </div>
        ))}
        {data && !entries.length && <Empty title="Nothing matches" />}
        {!data && <div className="skeleton h-24" />}
      </div>
    </Card>
  );
}

type Found = { answer: string; model?: string; message?: string; articles: { library_id: string; library: string; path: string; title: string; snippet: string }[] };

/** Ask the offline libraries in plain words: a short answer and the articles to open. */
function Find() {
  const { go } = useNav();
  const [q, setQ] = useKept("lib:q", "");
  const [found, setFound] = useKept<Found | null>("lib:found", null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const find = async () => {
    setBusy(true); setError("");
    try { setFound(await api<Found>("/library/find", { method: "POST", json: { q } })); } catch (e) { setError(errText(e)); }
    setBusy(false);
  };
  return (
    <Card>
      <div className="flex gap-2">
        <input className="input-field flex-1" placeholder="How do I purify water without a filter?" aria-label="Search the libraries" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q.trim().length > 1 && find()} />
        <button className="btn-primary" disabled={busy || q.trim().length < 2} onClick={find}><MagnifyingGlass size={16} /> {busy ? "Searching…" : "Find"}</button>
      </div>
      <ErrorNote error={error} onClose={() => setError("")} />
      {found && (
        <div className="mt-4 space-y-3">
          {found.message && <p className="text-sm text-text-muted">{found.message}</p>}
          {found.answer && <div className="md text-sm"><Markdown>{found.answer}</Markdown></div>}
          {found.articles.length > 0 && (
            <ul className="divide-y divide-line rounded-xl border border-line">
              {found.articles.map((a) => (
                <li key={a.library_id + a.path}>
                  <button className="w-full px-3 py-2 text-left hover:bg-surface-secondary" onClick={() => go({ page: "Reader", params: { lib: a.library_id, path: a.path } })}>
                    <span className="block text-sm font-semibold">{a.title}</span>
                    <span className="block truncate text-xs text-text-muted">{a.library} · {a.snippet}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {!found.answer && !found.articles.length && !found.message && <p className="text-sm text-text-muted">Nothing found.</p>}
        </div>
      )}
    </Card>
  );
}
