import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import {
  BookOpenText, Books, BookOpen, Broadcast, CaretRight, ChatCircleText, Cpu, Database, FolderOpen, GearSix, HardDrives, House, Key, Lightbulb, List,
  LockKey, MagnifyingGlass, PlusCircle, Sparkle, Toolbox, Plug, Queue, ShareNetwork, SidebarSimple, SquaresFour, Table, TreeStructure, WebhooksLogo, Wrench, Robot,
} from "@phosphor-icons/react";
import { ComponentType, lazy, StrictMode, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, authState, connect, errText, isDesktop, signIn } from "./api";
import { DisplayMenu, useDisplay } from "./display";
import Palette, { Command } from "./Palette";
import Ask from "./pages/Ask";
import JobsBell from "./shell/Jobs";
import Notices from "./shell/Notices";
import BottomNav from "./shell/BottomNav";
import { useQueueFlusher } from "./capture";
import { UpdateWatcher } from "./shell/Updates";
import { setActivePage } from "./background";
import Sidebar, { NavSection } from "./shell/Sidebar";
import StatusBar from "./shell/StatusBar";
import Welcome from "./shell/Welcome";
// Pages other than Home and Ask load on first visit, so the window opens faster.
import Home from "./pages/Home";
const MapPage = lazy(() => import("./pages/Map"));
const Jobs = lazy(() => import("./pages/Runs"));
const Models = lazy(() => import("./pages/Models"));
const Settings = lazy(() => import("./pages/Settings"));
const Troubleshoot = lazy(() => import("./pages/Help"));
const Reader = lazy(() => import("./pages/Reader"));
const Files = lazy(() => import("./pages/Files"));
const DataTools = lazy(() => import("./pages/DataTools"));
const Builder = lazy(() => import("./pages/Builder"));
const Capture = lazy(() => import("./pages/Capture"));
const AiApps = lazy(() => import("./pages/AiApps"));
const Docs = lazy(() => import("./pages/Docs"));
const data = () => import("./pages/DataPages");
const serve = () => import("./pages/ServePages");
const Topics = lazy(() => data().then((m) => ({ default: m.TopicsPage })));
const Tables = lazy(() => data().then((m) => ({ default: m.TablesPage })));
const Collections = lazy(() => data().then((m) => ({ default: m.CollectionsPage })));
const Libraries = lazy(() => data().then((m) => ({ default: m.LibrariesPage })));
const Vault = lazy(() => data().then((m) => ({ default: m.VaultPage })));
const Keys = lazy(() => serve().then((m) => ({ default: m.KeysPage })));
const Gateway = lazy(() => serve().then((m) => ({ default: m.GatewayPage })));
const Sharing = lazy(() => serve().then((m) => ({ default: m.SharingPage })));
const Webhooks = lazy(() => serve().then((m) => ({ default: m.WebhooksPage })));
import "./styles.css";
import { NavContext, NavTarget, Page } from "./ui";

type Entry = { id: Page; label: string; icon: typeof Books; el: ComponentType };
const SECTIONS: (NavSection & { pages: Entry[] })[] = [
  { id: "workspace", label: "Workspace", icon: SquaresFour, pages: [
    { id: "Home", label: "Home", icon: House, el: Home },
    { id: "Ask", label: "Ask", icon: ChatCircleText, el: Ask },
    { id: "Capture", label: "Capture", icon: PlusCircle, el: Capture },
    { id: "Map", label: "Map", icon: TreeStructure, el: MapPage },
  ] },
  { id: "data", label: "Data", icon: Database, pages: [
    { id: "Topics", label: "Topics", icon: Lightbulb, el: Topics },
    { id: "Builder", label: "Build with AI", icon: Sparkle, el: Builder },
    { id: "Tables", label: "Tables", icon: Table, el: Tables },
    { id: "Collections", label: "Collections", icon: Books, el: Collections },
    { id: "Files", label: "Files", icon: FolderOpen, el: Files },
    { id: "DataTools", label: "Data tools", icon: Toolbox, el: DataTools },
    { id: "Libraries", label: "Libraries", icon: BookOpen, el: Libraries },
    { id: "Vault", label: "Vault", icon: LockKey, el: Vault },
  ] },
  { id: "serve", label: "Serve", icon: Broadcast, pages: [
    { id: "AiApps", label: "AI apps", icon: Robot, el: AiApps },
    { id: "Keys", label: "API keys", icon: Key, el: Keys },
    { id: "Gateway", label: "LLM gateway", icon: Plug, el: Gateway },
    { id: "Sharing", label: "Sharing", icon: ShareNetwork, el: Sharing },
    { id: "Webhooks", label: "Webhooks", icon: WebhooksLogo, el: Webhooks },
  ] },
  { id: "system", label: "System", icon: HardDrives, pages: [
    { id: "Models", label: "Models", icon: Cpu, el: Models },
    { id: "Jobs", label: "Jobs", icon: Queue, el: Jobs },
    { id: "Settings", label: "Settings", icon: GearSix, el: Settings },
    { id: "Docs", label: "Docs", icon: BookOpenText, el: Docs },
    { id: "Troubleshoot", label: "Troubleshoot", icon: Wrench, el: Troubleshoot },
  ] },
];
const NAV: Entry[] = SECTIONS.flatMap((s) => s.pages);
const OTHER: Entry[] = [{ id: "Reader", label: "Reader", icon: Books, el: Reader }];
const ALL = [...NAV, ...OTHER];
const LEGACY: Record<string, Page> = { Runs: "Jobs", Connect: "Keys", Library: "Libraries", Pipelines: "Tables", Warehouse: "Tables", Help: "Troubleshoot",
  Status: "Troubleshoot", Learn: "Topics", Emergency: "Home" };
const LAKE_TABS: Record<string, Page> = { topics: "Topics", sheets: "Tables", collections: "Collections", libraries: "Libraries", vault: "Vault" };

/** Old links (Lake with a tab, Runs, Connect...) land on the page that holds that thing now. */
function resolve(t: NavTarget): NavTarget {
  if (t.page === "Lake") {
    const p = t.params ?? {};
    const page = p.sheet || p.new ? "Tables" : LAKE_TABS[p.tab ?? ""] ?? "Tables";
    return { page, params: p };
  }
  if (t.page === "Settings" && t.params?.section === "network") return { page: "Sharing" };
  return { ...t, page: (LEGACY[t.page] ?? t.page) as Page };
}

const store = {
  get: (k: string) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

/** A link like /?page=Capture&shared=1 (the share sheet lands here) opens that page once. */
function linked(): NavTarget | null {
  const q = new URLSearchParams(location.search);
  const page = q.get("page");
  if (!page) return null;
  history.replaceState(null, "", location.pathname);
  return { page, params: q.get("shared") ? { shared: "1" } : undefined };
}
const LINKED = typeof window === "undefined" ? null : linked();

function initialPage(): Page {
  if (LINKED && ALL.some((p) => p.id === LINKED.page)) return LINKED.page as Page;
  const saved = resolve({ page: store.get("page") ?? "Home" }).page;
  return (ALL.some((p) => p.id === saved) ? saved : "Home") as Page;
}

function App() {
  const [target, setTarget] = useState<NavTarget>({ page: initialPage(), params: LINKED?.params });
  useQueueFlusher();  // captures saved on this device while the lake was unreachable go out when it is back
  const [online, setOnline] = useState(true);
  const [palette, setPalette] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [crumb, setCrumb] = useState<string[]>([]);
  const [welcome, setWelcome] = useState(false);
  const display = useDisplay();
  const main = useRef<HTMLElement>(null);
  const go = useCallback((t: NavTarget) => {
    const next = resolve(t);
    store.set("page", next.page); setTarget(next); setCrumb([]); setDrawer(false);
  }, []);

  useEffect(() => {
    const load = () => api("/net").then((n) => setOnline(n.online)).catch(() => {});
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {  // the guide opens once on a fresh install (desktop window only), and on request
    if (isDesktop) api("/onboarding").then((o) => setWelcome(!o.done)).catch(() => {});
    const again = () => setWelcome(true);
    window.addEventListener("keirolake:welcome", again);
    return () => window.removeEventListener("keirolake:welcome", again);
  }, []);
  const closeWelcome = (finished: boolean) => { setWelcome(false); api("/onboarding/done", { method: "POST" }).catch(() => {}); if (finished) go({ page: "Lake" }); };

  const entry = ALL.find((p) => p.id === target.page) ?? NAV[0];
  setActivePage(String(target.page));
  const shown = useRef(target);  // the page focus was last moved for; the first one keeps the skip link as the first Tab stop
  useEffect(() => {
    document.title = `${[entry.label, ...crumb].join(" › ")} · KeiroLake`;
    if (shown.current === target) return;
    shown.current = target;
    main.current?.focus({ preventScroll: true });
  }, [target, crumb]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return;
      if (e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => !p); }
      else if (e.key.toLowerCase() === "b") { e.preventDefault(); display.set({ collapsed: !display.collapsed }); }
      else if (e.key === "=" || e.key === "+") { e.preventDefault(); display.step(1); }
      else if (e.key === "-") { e.preventDefault(); display.step(-1); }
      else if (e.key === "0") { e.preventDefault(); display.step(0); }
      else if (e.key === ",") { e.preventDefault(); go({ page: "Settings" }); }
      else if (/^[1-9]$/.test(e.key) && NAV[+e.key - 1]) { e.preventDefault(); go({ page: NAV[+e.key - 1].id }); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [display, go]);

  const commands = useMemo<Command[]>(() => [
    ...NAV.map((p, i) => ({ id: `go-${p.id}`, label: `Go to ${p.label}`, hint: `Ctrl ${i + 1}`, run: () => go({ page: p.id }) })),
    { id: "go-settings", label: "Settings", hint: "Ctrl ,", run: () => go({ page: "Settings" }) },
    { id: "welcome", label: "Take the welcome tour", run: () => window.dispatchEvent(new Event("keirolake:welcome")) },
    { id: "go-troubleshoot", label: "Troubleshoot and health check", run: () => go({ page: "Troubleshoot" }) },
    { id: "sheet-new", label: "New sheet", run: () => go({ page: "Lake", params: { new: "sheet" } }) },
    { id: "sheet-import", label: "Import a file into a sheet", run: () => go({ page: "Lake", params: { new: "import" } }) },
    { id: "sheet-describe", label: "Build a sheet from a description", hint: "KeiroLabs", run: () => go({ page: "Lake", params: { new: "describe" } }) },
    { id: "api-key", label: "Make an API key for an app", run: () => go({ page: "Connect" }) },
    { id: "ai-apps", label: "Connect Claude, ChatGPT, Cursor or VS Code", run: () => go({ page: "AiApps" }) },
    { id: "logs", label: "Logs", run: () => go({ page: "Docs" }) },
    { id: "backup", label: "Backups and copies", run: () => go({ page: "Settings", params: { section: "data" } }) },
    { id: "network", label: "Open the lake to other devices", run: () => go({ page: "Settings", params: { section: "network" } }) },
    { id: "text-up", label: "Larger text", hint: "Ctrl +", run: () => display.step(1) },
    { id: "text-down", label: "Smaller text", hint: "Ctrl −", run: () => display.step(-1) },
    { id: "text-reset", label: "Reset text size", hint: "Ctrl 0", run: () => display.step(0) },
    { id: "theme-light", label: "Theme: Light", run: () => display.set({ theme: "light" }) },
    { id: "theme-dark", label: "Theme: Dark", run: () => display.set({ theme: "dark" }) },
    { id: "theme-system", label: "Theme: Follow system", run: () => display.set({ theme: "system" }) },
    { id: "contrast", label: display.contrast ? "High contrast: off" : "High contrast: on", run: () => display.set({ contrast: !display.contrast }) },
    { id: "sidebar", label: "Toggle sidebar", hint: "Ctrl B", run: () => display.set({ collapsed: !display.collapsed }) },
    { id: "vault-lock", label: "Lock private vault", run: () => { api("/vault/lock", { method: "POST" }).catch(() => {}); } },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [display.contrast, display.collapsed, go]);

  const Current = entry.el;
  const slim = display.collapsed;
  const nav = (onPick?: () => void, compact = false) => (
    <Sidebar sections={SECTIONS} current={target.page} slim={compact} onGo={(id) => { go({ page: id }); onPick?.(); }} />
  );

  return (
    <NavContext.Provider value={{ go, params: target.params ?? {}, online, setCrumb }}>
      <a href="#content" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-md focus:bg-surface-primary focus:px-4 focus:py-2 focus:shadow-lg">Skip to content</a>
      <div className="flex h-full flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line bg-surface-primary px-3">
          <button className="icon-btn lg:hidden" aria-label="Open navigation" aria-expanded={drawer} onClick={() => setDrawer(true)}><List size={18} /></button>
          <span className="flex shrink-0 items-center gap-2 pr-2">
            <img src="/keirolabs.png" alt="" className="h-6 w-6 shrink-0" />
            <span className="hidden text-sm font-semibold sm:inline">KeiroLake</span>
          </span>
          <button className="mx-auto flex h-8 min-w-0 flex-1 max-w-md items-center gap-2 rounded-md border border-line bg-surface-secondary px-3 text-sm text-text-muted hover:border-accent-200"
            onClick={() => setPalette(true)} aria-label="Search and commands">
            <MagnifyingGlass size={15} /><span className="hidden flex-1 text-left sm:inline">Search pages, tables and commands</span>
            <kbd className="hidden rounded border border-line px-1.5 font-mono text-[11px] sm:inline">Ctrl K</kbd>
          </button>
          <span className={`hidden sm:inline ${online ? "badge-ok" : "badge-warn"}`}>{online ? "Online" : "Offline"}</span>
          <JobsBell />
          <button className="icon-btn" aria-label="Settings" title="Settings (Ctrl ,)" onClick={() => go({ page: "Settings" })}><GearSix size={18} /></button>
          <DisplayMenu api={display} />
        </header>

        <div className="flex min-h-0 flex-1">
          <aside aria-label="Primary" className={`hidden shrink-0 flex-col border-r border-line bg-surface-primary p-2 transition-[width] lg:flex ${slim ? "w-14" : "w-52"}`}>
            <div className="flex-1 overflow-y-auto">{nav(undefined, slim)}</div>
            <button className={`icon-btn ${slim ? "mx-auto" : "self-end"}`} aria-label={slim ? "Expand sidebar" : "Collapse sidebar"} title="Ctrl B"
              onClick={() => display.set({ collapsed: !slim })}><SidebarSimple size={18} /></button>
          </aside>

          {drawer && (
            <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-label="Navigation">
              <button className="absolute inset-0 bg-black/40" aria-label="Close navigation" onClick={() => setDrawer(false)} />
              <div className="absolute inset-y-0 left-0 w-60 border-r border-line bg-surface-primary p-3">{nav(() => setDrawer(false))}</div>
            </div>
          )}

          <main id="content" ref={main} tabIndex={-1} className="min-w-0 flex-1 overflow-y-auto overscroll-contain bg-surface-secondary outline-none">
            <div className="mx-auto w-full max-w-[96rem] px-4 py-5 sm:px-6 lg:px-8">
              <Suspense fallback={<div className="space-y-3" role="status" aria-label="Loading"><div className="skeleton h-8 w-48" /><div className="skeleton h-40" /></div>}>
                <Current key={target.page + JSON.stringify(target.params ?? {})} />
              </Suspense>
            </div>
          </main>
        </div>
        <div className="max-md:hidden"><StatusBar /></div>
        <BottomNav current={String(target.page)} onGo={(id) => go({ page: id })} onMore={() => setDrawer(true)} />
      </div>
      <Notices />
      <UpdateWatcher />
      {palette && <Palette commands={commands} onClose={() => setPalette(false)} />}
      {welcome && <Welcome onClose={closeWelcome} />}
    </NavContext.Provider>
  );
}

function Login({ passwordSet, onDone }: { passwordSet: boolean; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try { await signIn(password); onDone(); } catch (err) { setError(errText(err)); }
    setBusy(false);
  };
  return (
    <main className="flex h-full items-center justify-center overflow-y-auto bg-surface-secondary p-6">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4">
        <div className="flex items-center gap-2"><img src="/keirolabs.png" alt="" className="h-7 w-7" /><h1 className="text-lg font-semibold">KeiroLake</h1></div>
        {passwordSet ? (
          <>
            <div>
              <label htmlFor="pw" className="label">Password</label>
              <input id="pw" type="password" className="input-field" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            {error && <p className="field-error" role="alert">{error}</p>}
            <button className="btn-primary w-full" disabled={busy || !password}>{busy ? "Signing in…" : "Sign in"}</button>
          </>
        ) : (
          <p className="text-sm text-text-secondary">Remote sign-in isn't set up. On the computer running KeiroLake, open Settings → Network and set a password.</p>
        )}
      </form>
    </main>
  );
}

function Boot() {
  const [state, setState] = useState<"starting" | "ready" | "login" | string>("starting");
  const [passwordSet, setPasswordSet] = useState(false);
  const check = useCallback(async () => {
    try {
      await connect();
      if (isDesktop) { setState("ready"); return; }
      const a = await authState();
      setPasswordSet(a.password_set);
      setState(a.signed_in ? "ready" : "login");
    } catch (e) { setState(errText(e)); }
  }, []);
  useEffect(() => {
    check();
    const again = () => setState("login");
    window.addEventListener("keirolake:signin", again);
    return () => window.removeEventListener("keirolake:signin", again);
  }, [check]);
  if (state === "ready") return <App />;
  if (state === "login") return <Login passwordSet={passwordSet} onDone={() => setState("ready")} />;
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center" role="status">
      <img src="/keirolabs.png" alt="" className="h-12 w-12" />
      {state === "starting"
        ? <p className="animate-skeleton text-sm text-text-muted">Starting KeiroLake</p>
        : <p className="field-error max-w-md">{state}</p>}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><Boot /></StrictMode>);

// In a browser (not the desktop window), KeiroLake installs as an app: "Add to home screen" on a phone, "Install" on a
// computer. Only the app's own files are kept offline; data always comes live from the lake.
if (!isDesktop && import.meta.env.PROD && "serviceWorker" in navigator
  && (location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname))) {
  window.addEventListener("load", () => { navigator.serviceWorker.register("/sw.js").catch(() => {}); });
}
