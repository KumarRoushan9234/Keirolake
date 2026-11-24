import {
  ArrowsOutSimple, Books, Brain, Cloud, Cube, Database, Eye, EyeSlash, FileText, Globe, Key, Lightning, LinkSimple, MagnifyingGlassMinus, MagnifyingGlassPlus,
  Note, Plug, Plugs, Plus, Rss, Table, Trash, Warning, WebhooksLogo, X, AppWindow, ArrowsClockwise, FolderOpen, Code,
} from "@phosphor-icons/react";
import { PointerEvent as RPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errText, isDesktop, shell } from "../api";
import { Card, ErrorNote, PageHeader, useKept, useNav, usePoll } from "../ui";

type Action = { label: string; method: string; path: string; confirm: string | null; danger: boolean; body: unknown };
type MapNode = {
  id: string; layer: string; kind: string; label: string; detail: string; status: "ok" | "warn" | "error" | "off" | "busy"; custom?: boolean;
  metrics: Record<string, string | number>; issues: string[]; actions: Action[]; go: { page: string; params?: Record<string, string> } | null; pos: { x: number; y: number } | null;
};
type Edge = { from: string; to: string; label: string; custom?: boolean };
type LakeMap = { layers: string[]; nodes: MapNode[]; edges: Edge[]; hidden: { id: string; label: string }[]; problems: number; totals: Record<string, number> };
type Pos = Record<string, { x: number; y: number }>;
type Panel = "details" | "connector" | "box";

const ICON: Record<string, typeof Table> = {
  web: Globe, files: FileText, open: Books, sheet: Table, build: Lightning, collection: Database, library: Books, embedder: Brain, vectors: Cube,
  runtime: Cube, model: Brain, cloud: Cloud, api: Key, webhooks: WebhooksLogo, connector: Plug, app: AppWindow, note: Note,
};
const DOT: Record<string, string> = { ok: "bg-success", warn: "bg-warning", error: "bg-danger", off: "bg-text-muted", busy: "bg-accent-400" };
const NODE_W = 200, NODE_H = 60, COL_GAP = 70, ROW_GAP = 14, PAD = 20, HEAD = 30;

/** The lake as a graph you can arrange: drag boxes, pan and zoom, add connectors, apps and links, hide what you don't need. */
export default function MapPage() {
  const [map, reload] = usePoll<LakeMap>("/map", 6000);
  const { go } = useNav();
  const [picked, setPicked] = useKept<string>("map:picked", "");
  const [problemsOnly, setProblemsOnly] = useKept("map:problems", false);
  const [off, setOff] = useKept<string[]>("map:layersOff", []);
  const [q, setQ] = useState("");
  const [panel, setPanel] = useState<Panel>("details");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [linking, setLinking] = useState(false);
  const [local, setLocal] = useState<Pos>({});
  const [view, setView] = useState({ x: 0, y: 0, k: 1, fitted: false });
  const box = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(max-width: 639px)").matches);
  useEffect(() => {
    const mq = matchMedia("(max-width: 639px)");
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const drag = useRef<{ id: string; sx: number; sy: number; ox: number; oy: number; moved: boolean } | null>(null);
  const pan = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null);

  const auto = useMemo(() => {
    const pos: Pos = {};
    if (!map) return { pos, width: 600, height: 300 };
    const layers = map.layers.filter((l) => map.nodes.some((n) => n.layer === l));
    let height = 0;
    layers.forEach((layer, ci) => map.nodes.filter((n) => n.layer === layer).forEach((n, ri) => {
      pos[n.id] = { x: PAD + ci * (NODE_W + COL_GAP), y: PAD + HEAD + ri * (NODE_H + ROW_GAP) };
      height = Math.max(height, PAD + HEAD + (ri + 1) * (NODE_H + ROW_GAP));
    }));
    return { pos, layers, width: PAD * 2 + Math.max(1, layers.length) * NODE_W + Math.max(0, layers.length - 1) * COL_GAP, height: height + PAD };
  }, [map]);

  const posOf = useCallback((n: MapNode) => local[n.id] ?? n.pos ?? auto.pos[n.id] ?? { x: PAD, y: PAD }, [local, auto]);
  const shown = useMemo(() => {
    if (!map) return [];
    const bad = new Set(map.nodes.filter((n) => n.issues.length).map((n) => n.id));
    const near = new Set<string>();
    if (problemsOnly) map.edges.forEach((e) => { if (bad.has(e.from)) near.add(e.to); if (bad.has(e.to)) near.add(e.from); });
    return map.nodes.filter((n) => !off.includes(n.layer) && (!problemsOnly || bad.has(n.id) || near.has(n.id)) &&
      (!q || `${n.label} ${n.detail}`.toLowerCase().includes(q.toLowerCase())));
  }, [map, off, problemsOnly, q]);
  const shownIds = useMemo(() => new Set(shown.map((n) => n.id)), [shown]);

  const fit = useCallback(() => {
    const el = box.current;
    if (!el || !shown.length) return;
    const ps = shown.map(posOf);
    const minX = Math.min(...ps.map((p) => p.x)) - PAD, maxX = Math.max(...ps.map((p) => p.x)) + NODE_W + PAD;
    const minY = Math.min(...ps.map((p) => p.y)) - PAD - HEAD, maxY = Math.max(...ps.map((p) => p.y)) + NODE_H + PAD;
    const k = Math.max(0.35, Math.min(1.2, el.clientWidth / (maxX - minX), el.clientHeight / (maxY - minY)));
    setView({ k, x: (el.clientWidth - (maxX - minX) * k) / 2 - minX * k, y: 8 - minY * k - HEAD * k + HEAD * k, fitted: true });
  }, [shown, posOf]);
  useEffect(() => { if (map && !view.fitted) fit(); }, [map, view.fitted, fit]);

  const node = map?.nodes.find((n) => n.id === picked) ?? null;
  const linked = useMemo(() => {
    const set = new Set<string>();
    if (map && picked) map.edges.forEach((e) => { if (e.from === picked) set.add(e.to); if (e.to === picked) set.add(e.from); });
    return set;
  }, [map, picked]);

  const save = async (patch: Partial<{ positions: Pos; hidden: string[]; links: Edge[]; custom: unknown[] }>) => {
    if (!map) return;
    const positions: Pos = { ...Object.fromEntries(map.nodes.filter((n) => n.pos).map((n) => [n.id, n.pos!])), ...local };
    const body = {
      positions, hidden: map.hidden.map((h) => h.id), links: map.edges.filter((e) => e.custom).map(({ from, to, label }) => ({ from, to, label })),
      custom: map.nodes.filter((n) => n.custom).map((n) => ({ id: n.id, kind: n.kind, layer: n.layer, label: n.label, detail: n.detail })), ...patch,
    };
    try { await api("/map/state", { method: "PUT", json: body }); setLocal({}); } catch (e) { setError(errText(e)); }
    reload();
  };

  const run = async (a: Action, n: MapNode) => {
    setError("");
    if (a.method === "GO") { if (n.go) go(n.go); return; }
    if (a.method === "OPEN") { window.open(`/api${a.path}`, "_blank", "noopener"); return; }
    if (a.confirm && !confirm(a.confirm)) return;
    setBusy(a.label);
    try { await api(a.path, { method: a.method, json: a.body ?? undefined }); } catch (e) { setError(errText(e)); }
    setBusy(""); reload();
  };

  // ----- pointer handling: drag a box, pan the canvas -----
  const down = (e: RPointerEvent, n: MapNode) => {
    e.stopPropagation();
    const p = posOf(n);
    drag.current = { id: n.id, sx: e.clientX, sy: e.clientY, ox: p.x, oy: p.y, moved: false };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const moveNode = (e: RPointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = (e.clientX - d.sx) / view.k, dy = (e.clientY - d.sy) / view.k;
    if (!d.moved && Math.hypot(dx, dy) * view.k < 4) return;
    d.moved = true;
    setLocal((l) => ({ ...l, [d.id]: { x: Math.round(d.ox + dx), y: Math.round(d.oy + dy) } }));
  };
  const wasDrag = useRef(false);
  const upNode = () => {
    const d = drag.current;
    drag.current = null;
    wasDrag.current = !!d?.moved;
    if (d?.moved) save({});
  };
  const choose = (n: MapNode) => {
    if (wasDrag.current) { wasDrag.current = false; return; }
    if (linking && picked && picked !== n.id) { addLink(picked, n.id); return; }
    setPicked(picked === n.id ? "" : n.id); setPanel("details");
  };
  const addLink = async (from: string, to: string) => {
    setLinking(false);
    if (map?.edges.some((e) => e.from === from && e.to === to)) return;
    await save({ links: [...(map?.edges.filter((e) => e.custom) ?? []).map(({ from, to, label }) => ({ from, to, label })), { from, to, label: "" }] as Edge[] });
  };
  const bgDown = (e: RPointerEvent) => { pan.current = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y }; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); };
  const bgMove = (e: RPointerEvent) => { const p = pan.current; if (p) setView((v) => ({ ...v, x: p.ox + e.clientX - p.sx, y: p.oy + e.clientY - p.sy })); };
  const zoom = (f: number, cx?: number, cy?: number) => setView((v) => {
    const el = box.current, k = Math.max(0.3, Math.min(2, v.k * f));
    const px = cx ?? (el?.clientWidth ?? 0) / 2, py = cy ?? (el?.clientHeight ?? 0) / 2;
    return { ...v, k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k };
  });
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoom(e.deltaY < 0 ? 1.1 : 0.9, e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });
  const nudge = (e: React.KeyboardEvent, n: MapNode) => {
    const step = e.shiftKey ? 40 : 10, d: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (!d[e.key] || !e.altKey) return;
    e.preventDefault();
    const p = posOf(n);
    setLocal((l) => ({ ...l, [n.id]: { x: p.x + d[e.key][0], y: p.y + d[e.key][1] } }));
  };
  const commitNudge = () => { if (Object.keys(local).length) save({}); };

  const center = (n: MapNode) => { const p = posOf(n); return { x: p.x, y: p.y }; };
  const unlink = (a: string, b: string) => save({ links: (map?.edges.filter((e) => e.custom && !((e.from === a && e.to === b) || (e.from === b && e.to === a))) ?? []).map(({ from, to, label }) => ({ from, to, label })) as Edge[] });
  const removeCustom = (id: string) => confirm("Remove this box and its links?") && save({
    custom: map!.nodes.filter((n) => n.custom && n.id !== id).map((n) => ({ id: n.id, kind: n.kind, layer: n.layer, label: n.label, detail: n.detail })),
    links: map!.edges.filter((e) => e.custom && e.from !== id && e.to !== id).map(({ from, to, label }) => ({ from, to, label })) as Edge[],
  }).then(() => setPicked(""));
  const hide = (id: string) => save({ hidden: [...(map?.hidden.map((h) => h.id) ?? []), id] }).then(() => setPicked(""));
  const show = (id: string) => save({ hidden: (map?.hidden.map((h) => h.id) ?? []).filter((h) => h !== id) });
  const reset = async () => { if (confirm("Put every box back in its default place?")) { await save({ positions: {} }); setView((v) => ({ ...v, fitted: false })); } };

  const layersAll = map?.layers ?? [];
  const worldH = Math.max(auto.height, 300);
  return (
    <div className="space-y-3">
      <PageHeader title="Map">
        {map && <span className="hidden text-xs text-text-muted md:inline">{map.totals.sheets} sheets · {map.totals.collections} collections · {map.totals.libraries} libraries · {map.totals.rows.toLocaleString()} rows</span>}
        <button className={problemsOnly ? "btn-primary" : "btn-secondary"} aria-pressed={problemsOnly} onClick={() => setProblemsOnly(!problemsOnly)}>
          <Warning size={14} /> {map?.problems ?? 0} to fix
        </button>
      </PageHeader>
      <ErrorNote error={error} onClose={() => setError("")} />

      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Map tools">
        <input className="input-field h-8 w-44 py-0" aria-label="Find a box" placeholder="Find…" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn-secondary" onClick={() => { setPanel("connector"); setPicked(""); }}><Plug size={14} /> Add connector</button>
        <button className="btn-secondary" onClick={() => { setPanel("box"); setPicked(""); }}><Plus size={14} /> Add box</button>
        <span className="mx-1 hidden h-5 w-px bg-line sm:block" />
        {layersAll.map((l) => (
          <button key={l} aria-pressed={!off.includes(l)} className={`rounded-full border px-2.5 py-0.5 text-xs ${off.includes(l) ? "border-line text-text-muted line-through" : "border-accent-400/60 text-text-primary"}`}
            onClick={() => setOff(off.includes(l) ? off.filter((x) => x !== l) : [...off, l])}>{l}</button>
        ))}
        <span className="flex-1" />
        {!!map?.hidden.length && (
          <details className="relative">
            <summary className="btn-ghost cursor-pointer list-none"><EyeSlash size={14} /> {map.hidden.length} hidden</summary>
            <div className="absolute right-0 z-20 mt-1 w-56 rounded-md border border-line bg-surface-primary p-1 shadow">
              {map.hidden.map((h) => <button key={h.id} className="flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-sm hover:bg-surface-secondary" onClick={() => show(h.id)}><span className="truncate">{h.label}</span><Eye size={14} /></button>)}
            </div>
          </details>
        )}
        <button className="icon-btn max-sm:hidden" aria-label="Zoom out" onClick={() => zoom(0.85)}><MagnifyingGlassMinus size={16} /></button>
        <span className="mono w-10 text-center text-xs text-text-muted max-sm:hidden">{Math.round(view.k * 100)}%</span>
        <button className="icon-btn max-sm:hidden" aria-label="Zoom in" onClick={() => zoom(1.18)}><MagnifyingGlassPlus size={16} /></button>
        <button className="btn-ghost max-sm:hidden" onClick={fit}><ArrowsOutSimple size={14} /> Fit</button>
        <button className="btn-ghost max-sm:hidden" onClick={reset}><ArrowsClockwise size={14} /> Reset layout</button>
      </div>
      {linking && <p className="field-ok" role="status">Pick the box to connect {node ? `"${node.label}"` : "it"} to. <button className="font-semibold underline" onClick={() => setLinking(false)}>Cancel</button></p>}

      <div className="grid gap-3 xl:grid-cols-[1fr_21rem]">
        {narrow && map && (  // phones: the same boxes as a grouped list, tap one for its details below
          <div className="space-y-3">
            {(auto.layers ?? []).filter((l) => !off.includes(l)).map((l) => {
              const inLayer = shown.filter((n) => n.layer === l);
              return !!inLayer.length && (
                <div key={l} className="card p-2">
                  <p className="px-2 pb-1 text-xs font-medium text-text-muted">{l}</p>
                  {inLayer.map((n) => {
                    const Icon = ICON[n.kind] ?? Plugs;
                    return (
                      <button key={n.id} aria-pressed={picked === n.id} onClick={() => { setPicked(picked === n.id ? "" : n.id); setPanel("details"); }}
                        className={`flex w-full items-center gap-2 rounded-md p-2 text-left ${picked === n.id ? "bg-accent-50" : "hover:bg-surface-secondary"}`}>
                        <Icon size={18} className="shrink-0 text-accent-600" />
                        <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{n.label}</span><span className="block truncate text-xs text-text-muted">{n.detail}</span></span>
                        {n.issues.length > 0 && <Warning size={14} className="shrink-0 text-warning" aria-hidden="true" />}
                        <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[n.status]}`} aria-hidden="true" />
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
        )}
        <div ref={box} className={`card relative overflow-hidden p-0 touch-none ${narrow ? "hidden" : ""}`} style={{ height: "calc(100vh - 17rem)", minHeight: 360, cursor: pan.current ? "grabbing" : "grab" }}
          onPointerDown={bgDown} onPointerMove={bgMove} onPointerUp={() => { pan.current = null; }} onPointerCancel={() => { pan.current = null; }}>
          {!map ? <div className="skeleton m-4 h-64" /> : (
            <div className="absolute left-0 top-0 origin-top-left" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`, width: 1, height: 1 }}>
              {auto.layers?.filter((l) => !off.includes(l)).map((l) => {
                const x = auto.pos[map.nodes.find((n) => n.layer === l)!.id]?.x ?? PAD;
                return <p key={l} className="pointer-events-none absolute whitespace-nowrap text-xs font-semibold uppercase tracking-wide text-text-muted" style={{ left: x, top: PAD }}>{l}</p>;
              })}
              <svg className="pointer-events-none absolute overflow-visible" width={1} height={1} aria-hidden="true">
                {map.edges.map((e, i) => {
                  const A = map.nodes.find((n) => n.id === e.from), B = map.nodes.find((n) => n.id === e.to);
                  if (!A || !B || !shownIds.has(A.id) || !shownIds.has(B.id)) return null;
                  const a = center(A), b = center(B);
                  const right = b.x >= a.x;
                  const x1 = right ? a.x + NODE_W : a.x, y1 = a.y + NODE_H / 2, x2 = right ? b.x : b.x + NODE_W, y2 = b.y + NODE_H / 2;
                  const dx = Math.max(40, Math.abs(x2 - x1) / 2) * (right ? 1 : -1);
                  const on = picked && (e.from === picked || e.to === picked);
                  return (
                    <g key={i}>
                      <path d={`M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`} fill="none" stroke={on || e.custom ? "rgb(var(--accent-600))" : "rgb(var(--line))"}
                        strokeWidth={on ? 2.25 : 1.5} strokeDasharray={e.custom ? "6 4" : undefined} opacity={!picked || on ? 1 : 0.3} />
                      {e.label && on && <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 6} textAnchor="middle" fontSize={11} fill="rgb(var(--text-secondary))">{e.label}</text>}
                    </g>
                  );
                })}
              </svg>
              {shown.map((n) => {
                const p = posOf(n), Icon = ICON[n.kind] ?? Plugs, active = picked === n.id;
                return (
                  <button type="button" key={n.id} aria-pressed={active} aria-label={`${n.label}, ${n.detail}${n.issues.length ? `, ${n.issues.length} to fix` : ""}. Alt and arrow keys move it.`}
                    onPointerDown={(e) => down(e, n)} onPointerMove={moveNode} onPointerUp={upNode} onClick={() => choose(n)} onKeyDown={(e) => nudge(e, n)} onKeyUp={commitNudge}
                    className={`absolute flex select-none items-center gap-2 rounded-md border bg-surface-primary p-2 text-left shadow-sm hover:border-accent-400 focus-visible:outline focus-visible:outline-2 ${active ? "border-accent-600 ring-2 ring-accent-400/40" : n.custom ? "border-dashed border-line" : "border-line"} ${linking && !active ? "cursor-crosshair" : "cursor-grab"} ${picked && !active && !linked.has(n.id) ? "opacity-55" : ""}`}
                    style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H, touchAction: "none" }}>
                    <span className="grid h-8 w-8 shrink-0 place-items-center rounded bg-surface-secondary text-accent-600"><Icon size={18} /></span>
                    <span className="min-w-0 flex-1"><span className="block truncate text-sm font-semibold">{n.label}</span><span className="block truncate text-xs text-text-muted">{n.detail}</span></span>
                    {n.issues.length > 0 && <Warning size={14} className="shrink-0 text-warning" aria-hidden="true" />}
                    <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[n.status]}`} aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          )}
          {map && !shown.length && <p className="absolute left-4 top-4 text-sm text-text-secondary">{problemsOnly ? "Nothing needs fixing." : "No boxes match."}</p>}
          <p className="pointer-events-none absolute bottom-2 left-3 text-[11px] text-text-muted">Drag to pan · drag a box to move it · Ctrl + scroll to zoom</p>
        </div>

        {panel === "connector" ? <ConnectorForm onClose={() => setPanel("details")} onDone={() => { setPanel("details"); reload(); }} /> :
          panel === "box" ? <BoxForm layers={layersAll} onClose={() => setPanel("details")} onAdd={async (b) => {
            await save({ custom: [...(map?.nodes.filter((n) => n.custom) ?? []).map((n) => ({ id: n.id, kind: n.kind, layer: n.layer, label: n.label, detail: n.detail })), b] });
            setPanel("details"); setPicked(b.id);
          }} /> : (
            <Card title={node ? <span className="flex items-center justify-between gap-2">{node.label}<button className="icon-btn" aria-label="Close details" onClick={() => setPicked("")}><X size={14} /></button></span> : "Details"}>
              {!node ? <p className="text-sm text-text-secondary">Pick a box to see what it holds, what it connects to and what you can do with it. Add a connector to pull in a JSON API, a feed or a folder.</p> : (
                <div className="space-y-3 text-sm">
                  <p className="text-text-secondary">{node.detail}</p>
                  <div className="flex flex-wrap gap-1.5">
                    <span className="badge-muted">{node.layer}</span>
                    <span className={node.status === "ok" ? "badge-ok" : "badge-muted"}>{{ ok: "Working", warn: "Needs attention", error: "Not working", off: "Off", busy: "Busy" }[node.status]}</span>
                    {Object.entries(node.metrics).map(([k, v]) => <span key={k} className="badge-muted">{k}: {typeof v === "number" ? v.toLocaleString() : v}</span>)}
                  </div>
                  {node.issues.length > 0 && <ul className="field-error space-y-1">{node.issues.map((t) => <li key={t}>{t}</li>)}</ul>}
                  {map && (
                    <div><p className="mb-1 text-xs font-semibold text-text-muted">Connected to</p>
                      <ul className="space-y-1">{map.edges.filter((e) => e.from === node.id || e.to === node.id).map((e) => {
                        const other = e.from === node.id ? e.to : e.from, m = map.nodes.find((x) => x.id === other);
                        return m && <li key={`${e.from}>${e.to}`} className="flex items-center gap-2"><button className="text-accent-600 hover:underline" onClick={() => setPicked(other)}>{m.label}</button>
                          {e.label && <span className="text-xs text-text-muted">{e.label}</span>}
                          {e.custom && <button className="icon-btn ml-auto" aria-label={`Remove link to ${m.label}`} onClick={() => unlink(e.from, e.to)}><X size={12} /></button>}</li>;
                      })}{!linked.size && <li className="text-text-muted">Nothing yet</li>}</ul></div>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {node.go && !node.actions.some((a) => a.method === "GO") && node.go.page !== "Map" && <button className="btn-secondary" onClick={() => go(node.go!)}><ArrowsOutSimple size={14} /> Open</button>}
                    {node.actions.map((a) => <button key={a.label} disabled={!!busy} className={a.danger ? "btn-secondary text-danger" : "btn-secondary"} onClick={() => run(a, node)}>{busy === a.label ? "Working…" : a.label}</button>)}
                    <button className="btn-secondary" onClick={() => setLinking(true)}><LinkSimple size={14} /> Connect to…</button>
                    {node.custom ? <button className="btn-secondary text-danger" onClick={() => removeCustom(node.id)}><Trash size={14} /> Remove</button>
                      : <button className="btn-ghost" onClick={() => hide(node.id)}><EyeSlash size={14} /> Hide</button>}
                  </div>
                </div>
              )}
            </Card>
          )}
      </div>
    </div>
  );
}

const KIND_INFO = {
  rest: { label: "JSON API", icon: Code, hint: "A public address that returns JSON" },
  rss: { label: "RSS or Atom feed", icon: Rss, hint: "News, blogs, releases" },
  folder: { label: "Folder on this computer", icon: FolderOpen, hint: "CSV, Excel and JSON files; new files are added" },
} as const;

function ConnectorForm({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [kind, setKind] = useKept<"rest" | "rss" | "folder">("connector:kind", "rest");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [path, setPath] = useState("");
  const [jsonPath, setJsonPath] = useState("");
  const [key, setKey] = useState("");
  const [every, setEvery] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const desktop = isDesktop;
  const pick = async () => { const f = await shell<string | null>("pick_folder"); if (f) setPath(f); };
  const ok = name.trim() && (kind === "folder" ? path.trim() : /^https?:\/\//i.test(url.trim()));
  const create = async () => {
    setBusy(true); setError("");
    try { await api("/connectors", { method: "POST", json: { name, kind, url, path, json_path: jsonPath, key_column: key, interval_minutes: every, run_now: true } }); onDone(); }
    catch (e) { setError(errText(e)); }
    setBusy(false);
  };
  return (
    <Card title={<span className="flex items-center justify-between">Add connector<button className="icon-btn" aria-label="Close" onClick={onClose}><X size={14} /></button></span>}>
      <div className="space-y-3 text-sm">
        <div role="radiogroup" aria-label="Kind" className="space-y-1">
          {(Object.keys(KIND_INFO) as (keyof typeof KIND_INFO)[]).filter((k) => k !== "folder" || desktop).map((k) => {
            const I = KIND_INFO[k].icon;
            return <button key={k} role="radio" aria-checked={kind === k} onClick={() => setKind(k)} className={`flex w-full items-center gap-2 rounded-md border p-2 text-left ${kind === k ? "border-accent-600" : "border-line"}`}>
              <I size={18} className="text-accent-600" /><span><span className="block font-medium">{KIND_INFO[k].label}</span><span className="block text-xs text-text-muted">{KIND_INFO[k].hint}</span></span></button>;
          })}
        </div>
        <div><label htmlFor="cn" className="label">Name (also the sheet's name)</label><input id="cn" className="input-field" value={name} onChange={(e) => setName(e.target.value)} /></div>
        {kind === "folder" ? (
          <div><label htmlFor="cp" className="label">Folder</label>
            <div className="flex gap-2"><input id="cp" className="input-field mono min-w-0 flex-1" value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} />
              <button className="btn-secondary" onClick={pick}>Choose</button></div></div>
        ) : (
          <div><label htmlFor="cu" className="label">Address</label><input id="cu" className="input-field mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" spellCheck={false} /></div>
        )}
        {kind === "rest" && <div><label htmlFor="cj" className="label">Path to the list (optional)</label><input id="cj" className="input-field mono" value={jsonPath} onChange={(e) => setJsonPath(e.target.value)} placeholder="data.items" spellCheck={false} /></div>}
        <div><label htmlFor="ck" className="label">Key column (optional)</label><input id="ck" className="input-field mono" value={key} onChange={(e) => setKey(e.target.value)} placeholder={kind === "rss" ? "link" : "id"} spellCheck={false} />
          <p className="mt-1 text-xs text-text-muted">With a key, rows with the same key are updated and new ones added. Without one, each run replaces the sheet, keeping a copy in its history.</p></div>
        <div><label htmlFor="ce" className="label">Refresh</label>
          <select id="ce" className="input-field" value={every} onChange={(e) => setEvery(+e.target.value)}>
            {[[0, "By hand"], [15, "Every 15 minutes"], [60, "Hourly"], [360, "Every 6 hours"], [1440, "Daily"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
        <ErrorNote error={error} onClose={() => setError("")} />
        <button className="btn-primary w-full" disabled={busy || !ok} onClick={create}>{busy ? "Connecting…" : "Connect and read now"}</button>
      </div>
    </Card>
  );
}

function BoxForm({ layers, onClose, onAdd }: { layers: string[]; onClose: () => void; onAdd: (b: { id: string; kind: string; layer: string; label: string; detail: string }) => void }) {
  const [kind, setKind] = useState<"app" | "note">("app");
  const [label, setLabel] = useState("");
  const [detail, setDetail] = useState("");
  const [layer, setLayer] = useState("Access");
  return (
    <Card title={<span className="flex items-center justify-between">Add box<button className="icon-btn" aria-label="Close" onClick={onClose}><X size={14} /></button></span>}>
      <div className="space-y-3 text-sm">
        <p className="text-text-secondary">Add your own app or a note to the map, then connect it to what it uses.</p>
        <div><label htmlFor="bk" className="label">Type</label>
          <select id="bk" className="input-field" value={kind} onChange={(e) => setKind(e.target.value as "app" | "note")}><option value="app">An app or service that uses the lake</option><option value="note">A note</option></select></div>
        <div><label htmlFor="bl" className="label">Name</label><input id="bl" className="input-field" value={label} onChange={(e) => setLabel(e.target.value)} /></div>
        <div><label htmlFor="bd" className="label">Detail (optional)</label><input id="bd" className="input-field" value={detail} onChange={(e) => setDetail(e.target.value)} /></div>
        <div><label htmlFor="bc" className="label">Column</label>
          <select id="bc" className="input-field" value={layer} onChange={(e) => setLayer(e.target.value)}>{layers.map((l) => <option key={l}>{l}</option>)}</select></div>
        <button className="btn-primary w-full" disabled={!label.trim()} onClick={() => onAdd({ id: `custom:${Date.now().toString(36)}`, kind, layer, label: label.trim(), detail })}>Add to the map</button>
      </div>
    </Card>
  );
}
