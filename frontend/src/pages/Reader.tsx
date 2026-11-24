import { AppWindow, ArrowLeft, BookmarkSimple, House, MagnifyingGlass, Plus, X } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, errText, isDesktop, KB, Library, onShellEvent, safeUrl, shell, waitJob, zimUrl } from "../api";
import { Card, Empty, ErrorNote, useNav } from "../ui";

type Tab =
  | { id: number; kind: "zim"; lib: string; path: string; title: string }
  | { id: number; kind: "search"; lib: string; q: string; title: string; results?: { path: string; title: string; library_id?: string; library?: string }[] }
  | { id: number; kind: "web"; url: string; title: string; markdown?: string; via?: string; error?: string }
  | { id: number; kind: "new"; title: string };

type TabInput = Tab extends infer T ? (T extends Tab ? Omit<T, "id"> : never) : never;

let nextId = 1;
const isUrl = (s: string) => /^https?:\/\//i.test(s) || /^[\w-]+(\.[\w-]+)+(\/\S*)?$/.test(s);

export default function Reader() {
  const { params, online } = useNav();
  const [libs, setLibs] = useState<Library[]>([]);
  const [kbs, setKbs] = useState<KB[]>([]);
  const [lib, setLib] = useState(params.lib ?? "");
  const [scope, setScope] = useState("all");
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState(0);
  const [address, setAddress] = useState("");
  const [kb, setKb] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const ready = useMemo(() => libs.filter((l) => l.status === "ready"), [libs]);
  const tab = tabs.find((t) => t.id === active);

  const started = useRef(false);
  useEffect(() => {
    if (started.current) return; // StrictMode runs effects twice in development
    started.current = true;
    api<Library[]>("/libraries").then((list) => {
      setLibs(list);
      const first = params.lib ?? list.find((l) => l.status === "ready")?.id ?? "";
      setLib(first);
      if (params.lib) openTab({ kind: "zim", lib: params.lib, path: params.path ?? "", title: params.path ?? "Home" });
      else if (first) openTab({ kind: "zim", lib: first, path: "", title: list.find((l) => l.id === first)?.title ?? "Home" });
      else openTab({ kind: "new", title: "New tab" });
    });
    api<KB[]>("/kbs").then((list) => { setKbs(list); setKb(list[0]?.id ?? ""); });
  }, []);

  useEffect(() => {
    let off = () => {};
    onShellEvent<{ url: string; title: string; html: string; kb: string }>("page-captured", async (page) => {
      try {
        const { job_id } = await api(`/kbs/${page.kb}/capture`, { method: "POST", json: { url: page.url, title: page.title, html: page.html } });
        await waitJob(job_id);
        setNote(`Saved "${page.title || page.url}"`);
      } catch (e) { setError(errText(e)); }
    }).then((f) => { off = f; });
    return () => off();
  }, []);

  function openTab(t: TabInput, replace = false) {
    const id = replace && tab ? tab.id : nextId++;
    const full = { ...t, id } as Tab;
    setTabs((all) => replace && tab ? all.map((x) => (x.id === id ? full : x)) : [...all, full]);
    setActive(id);
    setAddress(t.kind === "web" ? t.url : t.kind === "search" ? t.q : "");
    if (full.kind === "search") runSearch(full);
    if (full.kind === "web") loadWeb(full);
  }
  const update = (id: number, patch: Partial<Tab>) => setTabs((all) => all.map((x) => (x.id === id ? ({ ...x, ...patch } as Tab) : x)));

  async function runSearch(t: Extract<Tab, { kind: "search" }>) {
    try {
      const url = t.lib === "all" ? `/search/libraries?q=${encodeURIComponent(t.q)}` : `/libraries/${t.lib}/search?q=${encodeURIComponent(t.q)}`;
      update(t.id, { results: (await api(url)).results });
    }
    catch (e) { setError(errText(e)); }
  }
  async function loadWeb(t: Extract<Tab, { kind: "web" }>) {
    try {
      const page = await api(`/reader?url=${encodeURIComponent(t.url)}`);
      update(t.id, { title: page.title, markdown: page.markdown, via: page.via });
    } catch (e) { update(t.id, { error: errText(e) }); }
  }

  function go(text = address) {
    const value = text.trim();
    if (!value) return;
    if (isUrl(value)) {
      const url = /^https?:\/\//i.test(value) ? value : `https://${value}`;
      openTab({ kind: "web", url, title: url }, tab?.kind === "new");
    } else if (lib) {
      openTab({ kind: "search", lib: ready.length > 1 && scope === "all" ? "all" : (scope === "all" ? lib : scope), q: value, title: value }, tab?.kind === "new");
    } else setError("Download an offline library first, or type a web address");
  }

  function close(id: number) {
    const rest = tabs.filter((t) => t.id !== id);
    setTabs(rest.length ? rest : [{ id: nextId, kind: "new", title: "New tab" }]);
    if (!rest.length) setActive(nextId++);
    else if (id === active) setActive(rest[rest.length - 1].id);
  }

  async function save() {
    if (!tab || !kb) return;
    setError(""); setNote("");
    try {
      let body: any;
      if (tab.kind === "web" && tab.markdown) body = { url: tab.url, title: tab.title, markdown: tab.markdown };
      else if (tab.kind === "zim") {
        const html = await (await fetch(zimUrl(tab.lib, tab.path))).text();
        const libTitle = libs.find((l) => l.id === tab.lib)?.title ?? "library";
        body = { url: `zim://${libTitle}/${tab.path}`, title: tab.title, html };
      } else return;
      const { job_id } = await api(`/kbs/${kb}/capture`, { method: "POST", json: body });
      await waitJob(job_id);
      setNote(`Saved "${tab.title}" to ${kbs.find((k) => k.id === kb)?.name}`);
    } catch (e) { setError(errText(e)); }
  }

  const canSave = !!kb && (tab?.kind === "zim" || (tab?.kind === "web" && !!tab.markdown));

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col gap-2 md:h-[calc(100vh-4rem)]">
      <h1 className="sr-only">Reader</h1>
      <div className="flex items-center gap-1 overflow-x-auto">
        {tabs.map((t) => (
          <div key={t.id} className={`group flex max-w-[12rem] shrink-0 items-center gap-1 rounded-xl px-3 py-1.5 text-xs ${t.id === active ? "bg-surface-primary font-semibold shadow-sm" : "text-text-muted hover:bg-surface-tertiary"}`}>
            <button className="truncate" onClick={() => { setActive(t.id); setAddress(t.kind === "web" ? t.url : t.kind === "search" ? t.q : ""); }}>{t.title || "Untitled"}</button>
            <button aria-label="Close tab" className="opacity-60 hover:opacity-100" onClick={() => close(t.id)}><X size={12} /></button>
          </div>
        ))}
        <button className="icon-btn" aria-label="New tab" onClick={() => openTab({ kind: "new", title: "New tab" })}><Plus size={14} /></button>
      </div>

      <div className="card flex flex-wrap items-center gap-2 p-2">
        {tab?.kind === "zim" && <button className="icon-btn" aria-label="Library home" onClick={() => openTab({ kind: "zim", lib: tab.lib, path: "", title: "Home" }, true)}><House size={16} /></button>}
        {ready.length > 1 && (
          <select className="input-field py-1.5 text-xs" value={scope} aria-label="Search in"
            onChange={(e) => { setScope(e.target.value); if (e.target.value !== "all") setLib(e.target.value); }}>
            <option value="all">All libraries</option>
            {ready.map((l) => <option key={l.id} value={l.id}>{l.title || l.name}</option>)}
          </select>
        )}
        <div className="flex min-w-[12rem] flex-1 items-center gap-2 rounded-xl border border-line bg-surface-secondary px-3">
          <MagnifyingGlass size={14} className="text-text-muted" />
          <input className="w-full bg-transparent py-1.5 text-sm outline-none placeholder:text-text-muted" value={address}
            placeholder={ready.length ? "Search the library, or type a web address" : "Type a web address"}
            onChange={(e) => setAddress(e.target.value)} onKeyDown={(e) => e.key === "Enter" && go()} />
        </div>
        {tab?.kind === "web" && isDesktop && online && (
          <button className="btn-ghost" title="Open the live page in a window (runs its scripts)" onClick={() => shell("open_web", { url: tab.url })}>
            <AppWindow size={16} /> Live
          </button>
        )}
        {kbs.length > 0 && (
          <>
            <select className="input-field py-1.5 text-xs" value={kb} onChange={(e) => setKb(e.target.value)} aria-label="Save to">
              {kbs.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
            </select>
            <button className="btn-secondary" disabled={!canSave} onClick={save}><BookmarkSimple size={16} /> Save</button>
            {isDesktop && <button className="btn-ghost" title="Save the page open in the live window" onClick={() => shell("capture_web", { kb })}>Save live</button>}
          </>
        )}
      </div>
      <ErrorNote error={error} onClose={() => setError("")} />
      {note && <p className="field-ok">{note}</p>}

      <div className="min-h-0 flex-1 overflow-hidden rounded-2xl border border-line bg-surface-primary">
        {!tab || tab.kind === "new" ? (
          <div className="p-6">
            <Empty title="Read offline">Search a library or open a web page. Saved pages become answerable offline.</Empty>
            {ready.length > 0 && (
              <div className="mx-auto grid max-w-2xl gap-2 sm:grid-cols-2">
                {ready.map((l) => (
                  <button key={l.id} className="card text-left transition hover:-translate-y-0.5 hover:border-accent-200"
                    onClick={() => openTab({ kind: "zim", lib: l.id, path: "", title: l.title || l.name }, true)}>
                    <p className="text-sm font-semibold">{l.title || l.name}</p>
                    <p className="line-clamp-2 text-xs text-text-muted">{l.summary}</p>
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : tab.kind === "zim" ? (
          <iframe key={tab.id + tab.path} title={tab.title} src={zimUrl(tab.lib, tab.path)} className="h-full w-full bg-white"
            sandbox="allow-same-origin allow-popups" referrerPolicy="no-referrer" />
        ) : tab.kind === "search" ? (
          <div className="h-full overflow-y-auto p-4">
            {!tab.results ? <div className="skeleton h-20" /> : tab.results.length ? (
              <div className="divide-y divide-line">
                {tab.results.map((r) => (
                  <button key={r.path} className="block w-full py-2 text-left text-sm hover:text-accent-600"
                    onClick={() => openTab({ kind: "zim", lib: r.library_id ?? tab.lib, path: r.path, title: r.title })}>
                    {r.title}{r.library && <span className="ml-2 text-xs text-text-muted">{r.library}</span>}</button>
                ))}
              </div>
            ) : <Empty title="No matching articles" />}
          </div>
        ) : (
          <div className="h-full overflow-y-auto p-5 md:p-8">
            {tab.error ? <ErrorNote error={tab.error} /> : !tab.markdown ? <div className="skeleton h-40" /> : (
              <article className="md mx-auto max-w-3xl">
                <p className="text-xs text-text-muted">
                  <a href={safeUrl(tab.url)} target="_blank" rel="noreferrer"><ArrowLeft size={10} className="inline" /> {tab.url}</a>
                  {tab.via === "keirolabs" && " · read via KeiroLabs"}
                </p>
                <h1 className="!mt-2 !text-2xl">{tab.title}</h1>
                <Markdown remarkPlugins={[remarkGfm]} urlTransform={(u) => safeUrl(u) ?? ""}>{tab.markdown}</Markdown>
              </article>
            )}
          </div>
        )}
      </div>
      {!ready.length && libs.length === 0 && <Card><p className="text-sm text-text-muted">No offline libraries yet. Get one in Library.</p></Card>}
    </div>
  );
}
