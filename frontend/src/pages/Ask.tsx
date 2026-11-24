import { ArrowUp, BookOpen, BookmarkSimple, ClockCounterClockwise, Copy, FileArrowUp, Globe, Lightbulb, LockKey, MagnifyingGlass, NotePencil, PencilSimple, Stop, Table as TableIcon, Trash, TreeStructure, Warning } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, follow, KB, safeUrl } from "../api";
import MicButton from "../shell/MicButton";
import { NavTarget, useKept, useNav } from "../ui";

type Msg = {
  id: string; role: "user" | "assistant"; content: string; status?: string;
  local?: any[]; web?: any[]; library?: any[]; private?: any[]; safety?: string; model?: string;
  route?: { route: string; reason: string }; progress?: string; error?: string;
  research?: { id: number; url: string; title: string; origin: string }[]; verify?: { checked: number; supported: number; unsupported: number };
};
type Conv = { id: string; title: string; kb_id: string | null; updated_at: string };

const store = {
  get: (k: string) => { try { return localStorage.getItem(k) ?? ""; } catch { return ""; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

const fromServer = (m: any): Msg => ({
  id: m.id, role: m.role, content: m.content, status: m.status, ...m.meta,
  error: m.meta?.error ?? (m.status === "interrupted" ? "Interrupted by a restart." : undefined),
});

export default function Ask() {
  const { go, online } = useNav();
  const [kbs, setKbs] = useState<KB[]>([]);
  const [convs, setConvs] = useState<Conv[]>([]);
  const [convId, setConvId] = useState(store.get("conv"));
  const [kb, setKb] = useState(store.get("kb"));
  const [web, setWeb] = useState(store.get("web") || "auto");
  const [depth, setDepth] = useState(store.get("depth") || "answer");
  const [useLibrary, setUseLibrary] = useState(store.get("lib") !== "0");
  const [libCount, setLibCount] = useState(0);
  const [vaultOpen, setVaultOpen] = useState(false);
  const [usePrivate, setUsePrivate] = useState(false);
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useKept("ask:draft", "");
  const [error, setError] = useState("");
  const watching = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const [q, setQ] = useState("");
  const [renaming, setRenaming] = useState("");
  const [copied, setCopied] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const busy = msgs.some((m) => m.status === "running");

  const loadList = useCallback(() => api<Conv[]>("/conversations").then(setConvs).catch(() => {}), []);
  const patch = (id: string, fn: (m: Msg) => Msg) => setMsgs((all) => all.map((m) => (m.id === id ? fn(m) : m)));

  /** Show an answer that is being written in the background, from its first word. */
  const watch = useCallback(async (id: string) => {
    watching.current?.abort();
    const ctl = new AbortController();
    watching.current = ctl;
    patch(id, (m) => ({ ...m, content: "", status: "running" }));
    try {
      for await (const e of follow(id, 0, ctl.signal)) {
        if (e.type === "token") patch(id, (m) => ({ ...m, content: m.content + e.text, progress: undefined }));
        else if (e.type === "status") patch(id, (m) => ({ ...m, progress: e.message }));
        else if (e.type === "route") patch(id, (m) => ({ ...m, route: { route: e.route, reason: e.reason } }));
        else if (e.type === "meta") patch(id, (m) => ({ ...m, model: e.model }));
        else if (e.type === "sources") patch(id, (m) => ({ ...m, local: e.local, web: e.web, library: e.library, private: e.private, research: e.research }));
        else if (e.type === "verify") patch(id, (m) => ({ ...m, verify: { checked: e.checked, supported: e.supported, unsupported: e.unsupported } }));
        else if (e.type === "safety") patch(id, (m) => ({ ...m, safety: e.message }));
        else if (e.type === "error") patch(id, (m) => ({ ...m, error: e.message }));
        else if (e.type === "final") patch(id, () => fromServer(e.message));
        else if (e.type === "end") patch(id, (m) => ({ ...m, status: e.status, progress: undefined }));
      }
    } catch (e) {
      if (!ctl.signal.aborted) patch(id, (m) => ({ ...m, error: String(e instanceof Error ? e.message : e), status: "failed" }));
    }
    if (!ctl.signal.aborted) loadList();
  }, [loadList]);

  const open = useCallback(async (id: string) => {
    watching.current?.abort();
    store.set("conv", id);
    setConvId(id);
    setError("");
    if (!id) { setMsgs([]); return; }
    try {
      const conv = await api<{ kb_id: string | null; messages: any[] }>(`/conversations/${id}`);
      setKb(conv.kb_id ?? "");
      const list = conv.messages.map(fromServer);
      setMsgs(list);
      const live = list.find((m) => m.status === "running");
      if (live) watch(live.id);
    } catch {
      store.set("conv", "");
      setConvId("");
      setMsgs([]);
    }
  }, [watch]);

  useEffect(() => {
    api<KB[]>("/kbs").then((list) => { setKbs(list); if (kb && !list.some((k) => k.id === kb)) setKb(""); });
    api<{ unlocked: boolean }>("/vault").then((v) => setVaultOpen(v.unlocked)).catch(() => {});
    api<any[]>("/libraries").then((l) => setLibCount(l.filter((x) => x.status === "ready" && x.enabled).length)).catch(() => {});
    loadList();
    if (convId) open(convId);
    return () => watching.current?.abort();  // the answer keeps going on the server
  }, []);
  // Follow the answer as it is written, by scrolling the chat itself (never the window).
  useEffect(() => {
    const el = scroller.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 240) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [msgs.length, msgs[msgs.length - 1]?.content.length]);
  useEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight; }, [convId]);
  useEffect(() => {  // the composer grows with the text, up to its maximum height
    const t = box.current;
    if (t) { t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, 192)}px`; }
  }, [input]);
  useEffect(() => {  // starter questions from what is in the lake
    Promise.all([api<{ name: string }[]>("/sheets").catch(() => []), api<{ name: string; status: string }[]>("/topics").catch(() => [])]).then(([sheets, topics]) => {
      const out = [...topics.filter((t) => t.status === "ready").slice(0, 2).map((t) => `Summarise what I know about ${t.name}`),
        ...sheets.slice(0, 2).map((s) => `What stands out in the ${s.name} table?`)];
      setSuggestions(out.length ? out : ["What can KeiroLake do with my data?", "How do I add a table?"]);
    });
  }, []);

  async function send(text = input) {
    const question = text.trim();
    if (!question || busy) return;
    setError("");
    try {
      let id = convId;
      if (!id) {
        const conv = await api<Conv>("/conversations", { method: "POST", json: { kb_id: kb || null } });
        id = conv.id;
        store.set("conv", id);
        setConvId(id);
      }
      const ids = await api<{ user_id: string; message_id: string }>(`/conversations/${id}/ask`, {
        method: "POST", json: { question, web, library: useLibrary, private: usePrivate && vaultOpen, depth },
      });
      setInput("");
      setMsgs((all) => [...all, { id: ids.user_id, role: "user", content: question },
        { id: ids.message_id, role: "assistant", content: "", status: "running" }]);
      loadList();
      watch(ids.message_id);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }

  /** Put the sources behind an answer into a sheet and open it in the Lake. */
  async function toSheet(m: Msg) {
    setSaved((s) => ({ ...s, [m.id + "s"]: "Reading sources…" }));
    try {
      const sheet = await api<{ id: string; rows: number }>(`/conversations/messages/${m.id}/to-sheet`, { method: "POST" });
      try { sessionStorage.setItem("kept:lake:sheet", JSON.stringify(sheet.id)); sessionStorage.setItem("kept:lake:tab", JSON.stringify("sheets")); } catch { /* ignore */ }
      setSaved((s) => ({ ...s, [m.id + "s"]: `Sheet with ${sheet.rows} rows` }));
      go({ page: "Lake" });
    } catch (e) { setSaved((s) => ({ ...s, [m.id + "s"]: String(e instanceof Error ? e.message : e) })); }
  }

  async function changeKb(value: string) {
    store.set("kb", value);
    setKb(value);
    if (convId) await api(`/conversations/${convId}`, { method: "PATCH", json: { kb_id: value || null, set_kb: true } }).catch(() => {});
  }

  async function remove(id: string) {
    await api(`/conversations/${id}`, { method: "DELETE" }).catch(() => {});
    if (id === convId) open("");
    loadList();
  }

  async function saveAnswer(i: number) {
    const m = msgs[i];
    const question = msgs[i - 1]?.content ?? "Answer";
    const target = kb || kbs[0]?.id;
    if (!target) { setSaved((s) => ({ ...s, [m.id]: "Create a collection first" })); return; }
    const cited = [...(m.library ?? []).map((z, n) => `[Z${n + 1}] ${z.title}`), ...(m.web ?? []).map((w, n) => `[W${n + 1}] ${w.title || w.url} ${w.url}`)];
    const text = `# ${question}\n\n${m.content}\n\n${cited.length ? "Sources:\n" + cited.join("\n") : ""}`;
    try {
      await api(`/kbs/${target}/ingest/text`, { method: "POST", json: { title: question.slice(0, 120), text } });
      setSaved((s) => ({ ...s, [m.id]: `Saved to ${kbs.find((k) => k.id === target)?.name}` }));
    } catch (e) { setSaved((s) => ({ ...s, [m.id]: String(e instanceof Error ? e.message : e) })); }
  }

  const pick = (k: string, v: string, set: (v: string) => void) => { store.set(k, v); set(v); };
  const running = msgs.find((m) => m.status === "running");
  const current = convs.find((c) => c.id === convId);
  const shown = convs.filter((c) => !q || c.title.toLowerCase().includes(q.toLowerCase()));
  const groups = groupByDay(shown);
  const rename = async (id: string, title: string) => {
    setRenaming("");
    if (title.trim()) await api(`/conversations/${id}`, { method: "PATCH", json: { title: title.trim() } }).catch(() => {});
    loadList();
  };
  const copy = (id: string, text: string) => { navigator.clipboard?.writeText(text).then(() => { setCopied(id); setTimeout(() => setCopied(""), 1500); }).catch(() => {}); };
  const sourceCount = (m: Msg) => (m.local?.length ?? 0) + (m.library?.length ?? 0) + (m.web?.length ?? 0) + (m.private?.length ?? 0) + (m.research?.length ?? 0);

  const history = (
    <div className="flex h-full flex-col">
      <button className="btn-primary w-full" onClick={() => { open(""); setHistoryOpen(false); }}><NotePencil size={16} /> New chat</button>
      <div className="relative mt-3">
        <MagnifyingGlass size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-text-muted" />
        <input className="input-field h-8 pl-8 text-sm" placeholder="Search chats" aria-label="Search chats" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <nav aria-label="Chats" className="mt-3 min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain pr-1">
        {groups.map(([label, items]) => (
          <div key={label}>
            <p className="px-2 pb-1 text-xs font-medium text-text-muted">{label}</p>
            <ul className="space-y-0.5">
              {items.map((c) => (
                <li key={c.id} className="group flex items-center gap-0.5">
                  {renaming === c.id ? (
                    <input autoFocus aria-label="Chat name" className="input-field h-8 flex-1 py-0 text-sm" defaultValue={c.title}
                      onBlur={(e) => rename(c.id, e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") rename(c.id, (e.target as HTMLInputElement).value); if (e.key === "Escape") setRenaming(""); }} />
                  ) : (
                    <button className={`min-w-0 flex-1 truncate rounded-md px-2 py-1.5 text-left text-sm ${c.id === convId ? "bg-accent-50 font-medium text-accent-600" : "text-text-secondary hover:bg-surface-tertiary hover:text-text-primary"}`}
                      onClick={() => { open(c.id); setHistoryOpen(false); }} onDoubleClick={() => setRenaming(c.id)} title={c.title}>{c.title}</button>
                  )}
                  <button className="icon-btn h-7 w-7 opacity-0 group-hover:opacity-100 focus:opacity-100" aria-label={`Rename ${c.title}`} onClick={() => setRenaming(c.id)}><PencilSimple size={13} /></button>
                  <button className="icon-btn h-7 w-7 opacity-0 group-hover:opacity-100 focus:opacity-100" aria-label={`Delete ${c.title}`}
                    onClick={() => confirm(`Delete the chat "${c.title}"?`) && remove(c.id)}><Trash size={13} /></button>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {!convs.length && <p className="px-2 text-sm text-text-muted">Your chats appear here.</p>}
        {!!convs.length && !shown.length && <p className="px-2 text-sm text-text-muted">No chats match.</p>}
      </nav>
    </div>
  );

  return (
    <div className="-mx-4 -my-5 flex h-[calc(100dvh-5rem)] min-h-0 sm:-mx-6 lg:-mx-8">
      <aside className="hidden w-64 shrink-0 border-r border-line bg-surface-primary p-3 lg:block" aria-label="Chat history">{history}</aside>
      {historyOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-label="Chat history">
          <button className="absolute inset-0 bg-black/40" aria-label="Close chat history" onClick={() => setHistoryOpen(false)} />
          <div className="absolute inset-y-0 left-0 w-72 border-r border-line bg-surface-primary p-3">{history}</div>
        </div>
      )}

      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line bg-surface-primary px-4">
          <button className="icon-btn lg:hidden" aria-label="Chat history" onClick={() => setHistoryOpen(true)}><ClockCounterClockwise size={18} /></button>
          <h1 className="sr-only">Ask</h1><p className="min-w-0 flex-1 truncate text-sm font-semibold" aria-label="Chat">{current?.title ?? "New chat"}</p>
          {!online && <span className="badge-warn">Offline · web off</span>}
          {msgs.length > 0 && <button className="btn-ghost py-1" onClick={() => open("")}><NotePencil size={15} /> New</button>}
        </header>

        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          <div className="mx-auto w-full max-w-3xl space-y-6 px-4 py-6">
            {!msgs.length && (
              <div className="pt-[8vh] text-center">
                <img src="/keirolabs.png" alt="" className="mx-auto mb-4 h-12 w-12 rounded-xl shadow-sm" />
                <h2 className="text-2xl font-semibold tracking-tight">What do you want to know?</h2>
                <p className="mt-1 text-sm text-text-secondary">Answers come from your tables, collections and libraries first{web !== "never" && online ? ", then the web" : ""}.</p>
                {!!suggestions.length && (
                  <div className="mx-auto mt-6 flex max-w-2xl flex-wrap justify-center gap-2">
                    {suggestions.map((s) => <button key={s} className="chip" onClick={() => send(s)}>{s}</button>)}
                  </div>
                )}
                <div className="mx-auto mt-8 grid max-w-2xl gap-3 text-left sm:grid-cols-3">
                  {([[Lightbulb, "Build a topic", "A clean table and searchable pages from one question.", { page: "Topics" }],
                    [FileArrowUp, "Add a file", "CSV, Excel, JSON or PDF.", { page: "Tables", params: { new: "import" } }],
                    [TreeStructure, "See the map", "How your data, models and apps connect.", { page: "Map" }]] as const).map(([I, t, d, to]) => (
                    <button key={t} className="rounded-md border border-line bg-surface-primary p-3 transition hover:border-accent-400" onClick={() => go(to as NavTarget)}>
                      <I size={18} className="text-accent-600" /><span className="mt-1.5 block text-sm font-medium">{t}</span><span className="block text-xs text-text-muted">{d}</span>
                    </button>))}
                </div>
              </div>
            )}
            {msgs.map((m, i) => m.role === "user" ? (
              <div key={m.id} className="ml-auto w-fit max-w-[85%] animate-fade-in whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent-50 px-4 py-2.5 text-sm text-text-primary">{m.content}</div>
            ) : (
              <article key={m.id} className="flex animate-fade-in gap-3">
                <img src="/keirolabs.png" alt="" className="mt-0.5 h-7 w-7 shrink-0 rounded-md" />
                <div className="min-w-0 flex-1">
                {m.safety && <p className="mb-3 flex items-start gap-2 rounded-md bg-cream-100 p-3 text-xs text-text-secondary"><Warning size={14} className="mt-0.5 shrink-0 text-cream-500" />{m.safety}</p>}
                {m.progress && <p className="mb-2 flex items-center gap-2 text-xs text-text-muted"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent-600" />{m.progress}</p>}
                {!m.content && m.status === "running" && !m.progress && <div className="space-y-2"><div className="skeleton h-4 w-2/3" /><div className="skeleton h-4 w-1/2" /></div>}
                <div className="md text-[15px] leading-relaxed"><Markdown remarkPlugins={[remarkGfm]} urlTransform={(u) => safeUrl(u) ?? ""}>{m.content}</Markdown></div>
                {m.status === "stopped" && <p className="mt-2 text-xs text-text-muted">Stopped</p>}
                {m.error && <ErrorText text={m.error} />}
                {sourceCount(m) > 0 && (
                  <details className="mt-3 rounded-md border border-line bg-surface-primary">
                    <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-text-secondary">
                      {sourceCount(m)} source{sourceCount(m) === 1 ? "" : "s"}
                      {m.verify && m.verify.checked > 0 && <span className={`ml-2 ${m.verify.unsupported ? "badge-warn" : "badge-ok"}`}>{m.verify.supported}/{m.verify.checked} claims confirmed</span>}
                    </summary>
                    <div className="flex flex-wrap gap-1.5 border-t border-line p-3">
                      {m.local?.map((h, n) => <span key={h.chunk_id} className="chip" title={h.text?.slice(0, 200)}>[{n + 1}] {h.title}</span>)}
                      {m.library?.map((z, n) => (
                        <button key={z.library_id + z.path + n} className="chip" title={z.text?.slice(0, 200)} onClick={() => go({ page: "Reader", params: { lib: z.library_id, path: z.path } })}>
                          <BookOpen size={12} /> [Z{n + 1}] {z.title}</button>))}
                      {m.private?.map((p, n) => <span key={p.id} className="chip" title="Private note"><LockKey size={12} /> [P{n + 1}] {p.title}</span>)}
                      {m.web?.map((w, n) => <a key={w.url + n} className="chip" href={safeUrl(w.url)} target="_blank" rel="noreferrer"><Globe size={12} /> [W{n + 1}] {w.title || w.url}</a>)}
                      {m.research?.map((r) => (r.url && safeUrl(r.url)
                        ? <a key={r.id} className="chip" href={safeUrl(r.url)} target="_blank" rel="noreferrer" title={r.url}><Globe size={12} /> [S{r.id}] {r.title || r.url}</a>
                        : <span key={r.id} className="chip" title={r.origin}><BookOpen size={12} /> [S{r.id}] {r.title}</span>))}
                    </div>
                  </details>
                )}
                {m.status !== "running" && m.content && (
                  <div className="mt-2 flex flex-wrap items-center gap-1 text-xs text-text-muted">
                    <button className="btn-ghost px-2 py-1 text-xs" onClick={() => copy(m.id, m.content)}><Copy size={13} />{copied === m.id ? "Copied" : "Copy"}</button>
                    {!m.private?.length && m.route?.route !== "chat" && (
                      <button className="btn-ghost px-2 py-1 text-xs" onClick={() => saveAnswer(i)}><BookmarkSimple size={13} />{saved[m.id] ?? "Save to lake"}</button>)}
                    {m.status === "done" && !m.private?.length && (m.research?.length || m.web?.length || m.local?.length) ? (
                      <button className="btn-ghost px-2 py-1 text-xs" onClick={() => toSheet(m)}><TableIcon size={13} />{saved[m.id + "s"] ?? "Make a table"}</button>) : null}
                    <span className="flex-1" />
                    {m.route && <span className="hidden sm:inline">{m.route.reason}</span>}
                    {m.model && <span className="badge-muted">{m.model}</span>}
                  </div>
                )}
                </div>
              </article>
            ))}
            {error && <p className="field-error">{error}</p>}
          </div>
        </div>

        <div className="shrink-0 border-t border-line bg-surface-secondary px-4 py-3">
          <div className="mx-auto max-w-3xl rounded-lg border border-line bg-surface-primary shadow-sm focus-within:border-accent-400">
            <textarea ref={box} rows={1} className="block max-h-48 min-h-[48px] w-full resize-none bg-transparent px-4 pt-3 text-[15px] outline-none placeholder:text-text-muted"
              placeholder={current ? "Ask a follow-up" : "Ask about your data"} value={input} autoFocus aria-label="Message"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
            <div className="flex flex-wrap items-center gap-1.5 px-2 pb-2">
              <select className="h-8 rounded-md bg-transparent px-2 text-xs text-text-secondary hover:bg-surface-tertiary" value={kb} aria-label="Collection" onChange={(e) => changeKb(e.target.value)}>
                <option value="">All my data</option>
                {kbs.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
              </select>
              <select className="h-8 rounded-md bg-transparent px-2 text-xs text-text-secondary hover:bg-surface-tertiary disabled:opacity-50" value={web} aria-label="Web search" disabled={!online}
                onChange={(e) => pick("web", e.target.value, setWeb)}>
                <option value="auto">Web when needed</option>
                <option value="always">Always search the web</option>
                <option value="never">Never use the web</option>
              </select>
              <select className="h-8 rounded-md bg-transparent px-2 text-xs text-text-secondary hover:bg-surface-tertiary" value={depth} aria-label="Answer or research" onChange={(e) => pick("depth", e.target.value, setDepth)}>
                <option value="answer">Quick answer</option>
                <option value="standard">Research</option>
                <option value="deep">Deep research</option>
              </select>
              <button className={`h-8 rounded-md px-2 text-xs ${useLibrary ? "text-accent-600" : "text-text-muted"} hover:bg-surface-tertiary`} aria-pressed={useLibrary}
                onClick={() => { store.set("lib", useLibrary ? "0" : "1"); setUseLibrary(!useLibrary); }} title={libCount ? `${libCount} offline libraries` : "No offline libraries yet"}>
                <BookOpen size={13} className="mr-1 inline" />Libraries{libCount ? ` · ${libCount}` : ""}</button>
              {vaultOpen && (
                <button className={`h-8 rounded-md px-2 text-xs ${usePrivate ? "text-accent-600" : "text-text-muted"} hover:bg-surface-tertiary`} aria-pressed={usePrivate} onClick={() => setUsePrivate(!usePrivate)} title="Local models only">
                  <LockKey size={13} className="mr-1 inline" />Private notes</button>
              )}
              <span className="flex-1" />
              <MicButton onText={(t) => setInput((v) => (v.trim() ? `${v.trim()} ${t}` : t))} />
              {running
                ? <button className="btn-secondary h-9 w-9 p-0" aria-label="Stop" onClick={() => api(`/conversations/messages/${running.id}/stop`, { method: "POST" })}><Stop size={16} weight="fill" /></button>
                : <button className="btn-primary h-9 w-9 p-0" aria-label="Send" disabled={!input.trim()} onClick={() => send()}><ArrowUp size={16} weight="bold" /></button>}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

/** Chats grouped the way people remember them: today, yesterday, this week, earlier. */
function groupByDay(list: Conv[]): [string, Conv[]][] {
  const start = new Date(); start.setHours(0, 0, 0, 0);
  const day = 86400000;
  const label = (c: Conv) => {
    const t = new Date(c.updated_at).getTime();
    if (t >= start.getTime()) return "Today";
    if (t >= start.getTime() - day) return "Yesterday";
    if (t >= start.getTime() - 6 * day) return "Previous 7 days";
    if (t >= start.getTime() - 29 * day) return "Previous 30 days";
    return "Older";
  };
  const out = new Map<string, Conv[]>();
  for (const c of list) out.set(label(c), [...(out.get(label(c)) ?? []), c]);
  return [...out.entries()];
}

/** Model failures read as one plain sentence; the provider-by-provider detail stays one click away. */
function ErrorText({ text }: { text: string }) {
  if (!text.startsWith("All LLM providers failed")) return <p className="field-error mt-2">{text}</p>;
  const keys = /401|expired|invalid api key/i.test(text);
  return (
    <div className="field-error mt-2">
      <p>No model could answer. {keys ? "The cloud keys were refused (expired or wrong), and no local model took over." : "None of the models answered."} Choose a local model in Models, or update the cloud key in Settings.</p>
      <details className="mt-1 text-xs opacity-80"><summary className="cursor-pointer">Details</summary><p className="mt-1 break-words">{text}</p></details>
    </div>
  );
}
